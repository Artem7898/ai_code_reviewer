"""Exercise independent SQLite connections, lease fencing and tenant isolation."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import event, inspect, update
from sqlmodel import col, select

from app.db import Database
from app.jobs import BudgetLimitError, JobLimitError, JobStore
from app.models import CloudBudget, ReviewJob, ReviewReport, User
from app.settings import Settings


async def make_database(tmp_path: Path, **overrides: object) -> Database:
    settings = Settings(
        _env_file=None,
        environment="test",
        data_dir=tmp_path / "data",
        database_path=tmp_path / "database.db",
        projects_root=tmp_path / "projects",
        ai_provider="static",
        **overrides,
    )
    database = Database(settings)
    await database.initialize()
    async with database.session() as session:
        session.add(User(id=1, username="one", hashed_password="not-used"))
        session.add(User(id=2, username="two", hashed_password="not-used"))
        await session.commit()
    return database


@pytest.mark.asyncio
async def test_concurrent_dedup_and_claim_across_connections(tmp_path: Path) -> None:
    database = await make_database(tmp_path)
    second = Database(database.settings)
    try:
        stores = [JobStore(database), JobStore(second)]
        jobs = await asyncio.gather(
            *(
                stores[index % 2].enqueue(1, "review", {"snapshot": []}, "digest")
                for index in range(12)
            )
        )
        assert len({job.id for job in jobs}) == 1
        claimed = await asyncio.gather(*(store.claim() for store in stores))
        assert sum(job is not None for job in claimed) == 1
        job = next(job for job in claimed if job is not None)
        assert job.attempts == 1
        assert job.lease_token
    finally:
        await second.close()
        await database.close()


@pytest.mark.asyncio
async def test_quota_is_atomic_and_tenant_scoped(tmp_path: Path) -> None:
    database = await make_database(tmp_path, max_jobs_per_user=2)
    try:
        store = JobStore(database)
        outcomes = await asyncio.gather(
            *(store.enqueue(1, "review", {"request": index}) for index in range(8)),
            return_exceptions=True,
        )
        assert sum(isinstance(item, ReviewJob) for item in outcomes) == 2
        assert sum(isinstance(item, JobLimitError) for item in outcomes) == 6
        foreign = await store.enqueue(2, "review", {})
        assert await store.get(1, foreign.id) is None
        assert await store.cancel(1, foreign.id) is None
        assert len(await store.list(1)) == 2
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_restart_reclaim_fences_stale_result_and_sanitizes_errors(tmp_path: Path) -> None:
    database = await make_database(tmp_path)
    second = Database(database.settings)
    try:
        store, recovered = JobStore(database), JobStore(second)
        queued = await store.enqueue(1, "review", {}, "stable-snapshot")
        first = await store.claim()
        assert first and first.lease_token
        async with database.session() as session:
            await session.exec(
                update(ReviewJob)
                .where(col(ReviewJob.id) == queued.id)
                .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
            )
            await session.commit()
        next_worker = await recovered.claim()
        assert next_worker and next_worker.lease_token
        assert next_worker.attempts == 2
        assert next_worker.lease_token != first.lease_token
        assert not await store.complete(first.id, first.lease_token, {"stale": True})
        assert not await store.fail(first.id, first.lease_token)
        assert await recovered.fail(next_worker.id, next_worker.lease_token, "private-token-secret")
        failed = await store.get(1, queued.id)
        assert failed and failed.status == "failed" and failed.result is None
        assert "private-token-secret" not in str(failed.error)
        retried = await store.enqueue(1, "review", {}, "stable-snapshot")
        assert retried.id != queued.id
    finally:
        await second.close()
        await database.close()


@pytest.mark.asyncio
async def test_cancel_and_success_are_atomic_with_owner_bound_reports(tmp_path: Path) -> None:
    database = await make_database(tmp_path)
    try:
        store = JobStore(database)
        queued = await store.enqueue(1, "review", {})
        claim = await store.claim()
        assert claim and claim.lease_token
        await store.cancel(1, queued.id)
        assert not await store.heartbeat(claim.id, claim.lease_token)
        assert not await store.complete(claim.id, claim.lease_token, {"unexpected": True})
        async with database.session() as session:
            assert not (await session.exec(select(ReviewReport))).all()
        successful = await store.enqueue(1, "review", {}, "repeatable")
        claim = await store.claim()
        assert claim and claim.id == successful.id and claim.lease_token
        report = ReviewReport(
            project_name="sample",
            file_path="a.py",
            content_type="code",
            summary="fine",
            review_result="{}",
            owner_id=2,
        )
        assert await store.complete(claim.id, claim.lease_token, {"summary": "fine"}, [report])
        assert not await store.complete(claim.id, claim.lease_token, {}, [report])
        async with database.session() as session:
            reports = (await session.exec(select(ReviewReport))).all()
            assert len(reports) == 1
            assert reports[0].owner_id == 1 and reports[0].job_id == successful.id
        assert (await store.enqueue(1, "review", {}, "repeatable")).id == successful.id
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_exhausted_crashed_job_is_terminal(tmp_path: Path) -> None:
    database = await make_database(tmp_path, job_max_attempts=1)
    try:
        store = JobStore(database)
        queued = await store.enqueue(1, "review", {})
        assert await store.claim()
        async with database.session() as session:
            await session.exec(
                update(ReviewJob)
                .where(col(ReviewJob.id) == queued.id)
                .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
            )
            await session.commit()
        assert await store.claim() is None
        failed = await store.get(1, queued.id)
        assert failed and failed.status == "failed"
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("include_reports", [False, True])
async def test_retention_preserves_reports_unless_explicitly_requested(
    tmp_path: Path, include_reports: bool
) -> None:
    database = await make_database(tmp_path)
    try:
        store = JobStore(database)
        old = await store.enqueue(1, "review", {})
        claim = await store.claim()
        assert claim and claim.lease_token
        report = ReviewReport(
            project_name="sample",
            file_path="a.py",
            content_type="code",
            summary="fine",
            review_result="{}",
        )
        assert await store.complete(
            claim.id, claim.lease_token, {"artifact_id": "a" * 32}, [report]
        )
        active = await store.enqueue(1, "review", {})
        async with database.session() as session:
            await session.exec(
                update(ReviewJob).values(updated_at=datetime.now(UTC) - timedelta(days=60))
            )
            await session.commit()
        pruned = await store.prune(include_reports=include_reports)
        assert pruned.removed_jobs == 1 and pruned.artifact_ids == ("a" * 32,)
        assert await store.get(1, old.id) is None
        assert await store.get(1, active.id) is not None
        async with database.session() as session:
            reports = (await session.exec(select(ReviewReport))).all()
            assert len(reports) == (0 if include_reports else 1)
            if reports:
                assert reports[0].job_id is None and reports[0].owner_id == 1
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_cloud_budget_atomic_across_process_connections_and_restarts(tmp_path: Path) -> None:
    database = await make_database(tmp_path, ai_max_cost_usd=0.2)
    second = Database(database.settings)
    try:
        stores = [JobStore(database), JobStore(second)]
        outcomes = await asyncio.gather(
            *(stores[index % 2].reserve_budget(0.05) for index in range(12)),
            return_exceptions=True,
        )
        assert sum(item is None for item in outcomes) == 4
        assert sum(isinstance(item, BudgetLimitError) for item in outcomes) == 8
        async with database.session() as session:
            entries = (await session.exec(select(CloudBudget))).all()
            assert len(entries) == 1 and entries[0].reserved_micro_usd == 200000
            assert entries[0].day == datetime.now(UTC).date().isoformat()
    finally:
        await second.close()
        await database.close()
    restarted = Database(database.settings)
    try:
        await restarted.initialize()
        with pytest.raises(BudgetLimitError):
            await JobStore(restarted).reserve_budget(0.000001)
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_cloud_budget_rounds_conservatively_and_rejects_invalid_reservations(
    tmp_path: Path,
) -> None:
    database = await make_database(tmp_path, ai_max_cost_usd=0.000001)
    try:
        store = JobStore(database)
        await store.reserve_budget(0.0000001)
        with pytest.raises(BudgetLimitError):
            await store.reserve_budget(0.0000001)
        for value in (float("nan"), float("inf"), -0.1, 0):
            with pytest.raises(BudgetLimitError):
                await store.reserve_budget(value)
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_daily_budget_rolls_over_at_utc_midnight_without_worker_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = await make_database(tmp_path, ai_max_cost_usd=0.1)
    try:
        store = JobStore(database)
        today = datetime.now(UTC)
        monkeypatch.setattr("app.jobs._utcnow", lambda: today)
        await store.reserve_budget(0.1)
        with pytest.raises(BudgetLimitError):
            await store.reserve_budget(0.001)
        monkeypatch.setattr("app.jobs._utcnow", lambda: today + timedelta(days=1))
        await store.reserve_budget(0.1)
        async with database.session() as session:
            entries = (await session.exec(select(CloudBudget))).all()
            assert len(entries) == 2 and all(row.reserved_micro_usd == 100000 for row in entries)
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_list_loads_only_metadata_and_detail_retains_large_documents(tmp_path: Path) -> None:
    database = await make_database(tmp_path)
    try:
        store = JobStore(database)
        original = await store.enqueue(1, "review", {"snapshot": "x" * 150000})
        claim = await store.claim()
        assert claim and claim.lease_token
        assert await store.complete(claim.id, claim.lease_token, {"sarif": "y" * 150000})
        statements: list[str] = []

        @event.listens_for(database.engine.sync_engine, "before_cursor_execute")
        def capture(connection: object, cursor: object, statement: str, *args: object) -> None:
            statements.append(statement)

        summaries = await store.list(1)
        assert len(summaries) == 1 and summaries[0].status == "completed"
        assert {"payload", "result"} <= inspect(summaries[0]).unloaded
        assert not any(
            "reviewjob.payload" in statement or "reviewjob.result" in statement
            for statement in statements
        )
        detail = await store.get(1, original.id)
        assert (
            detail
            and len(detail.payload) > 150000
            and detail.result
            and len(detail.result) > 150000
        )
    finally:
        await database.close()
