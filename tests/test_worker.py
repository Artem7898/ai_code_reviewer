"""Worker failure, cancellation and immutable-input integration checks."""

import asyncio
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import pytest
from sqlmodel import select

from app.analysis import ReviewAnalyzer
from app.db import Database
from app.jobs import JobStore
from app.models import ReviewReport, User
from app.schemas import GeneratedProposal, ProposedArtifact, ReviewResult, SourceFile
from app.settings import Settings
from app.worker import Worker


async def make_database(tmp_path: Path, **overrides: object) -> Database:
    settings = Settings(
        _env_file=None,
        environment="test",
        data_dir=tmp_path / "data",
        database_path=tmp_path / "database.db",
        projects_root=tmp_path / "projects",
        ai_provider="static",
        ruff_enabled=False,
        **overrides,
    )
    database = Database(settings)
    await database.initialize()
    async with database.session() as session:
        session.add(User(id=1, username="one", hashed_password="unused"))
        await session.commit()
    return database


def snapshot(content: str = "x = 1\n") -> dict[str, object]:
    return {
        "project_name": "sample",
        "snapshot": [{"path": "a.py", "content": content, "changed_lines": [1]}],
    }


@pytest.mark.asyncio
async def test_static_review_persists_only_snapshot_and_success(tmp_path: Path) -> None:
    database = await make_database(tmp_path)
    try:
        store = JobStore(database)
        job = await store.enqueue(
            1, "review", snapshot("import subprocess\nsubprocess.run('x', shell=True)\n")
        )
        async with Worker(database, database.settings) as worker:
            assert await worker.run_once()
            assert not await worker.run_once()
        finished = await store.get(1, job.id)
        assert finished and finished.status == "completed" and finished.progress == 1
        assert finished.result and isinstance(json.loads(finished.result)["sarif"], dict)
        async with database.session() as session:
            reports = (await session.exec(select(ReviewReport))).all()
            assert len(reports) == 1 and reports[0].owner_id == 1
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_provider_error_never_becomes_successful_report(tmp_path: Path) -> None:
    class BrokenAnalyzer:
        async def review(self, files: Sequence[SourceFile], context: str = "") -> ReviewResult:
            raise RuntimeError("provider failed with api-key private-secret")

    database = await make_database(tmp_path)
    try:
        store = JobStore(database)
        job = await store.enqueue(1, "review", snapshot())
        async with Worker(
            database, database.settings, cast(ReviewAnalyzer, BrokenAnalyzer())
        ) as worker:
            assert await worker.run_once()
        finished = await store.get(1, job.id)
        assert finished and finished.status == "failed" and finished.result is None
        assert "private-secret" not in str(finished.error)
        async with database.session() as session:
            assert not (await session.exec(select(ReviewReport))).all()
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_cancellation_stops_inflight_provider_and_rejects_stale_success(
    tmp_path: Path,
) -> None:
    started, cancelled = asyncio.Event(), asyncio.Event()

    class BlockingAnalyzer:
        async def review(self, files: Sequence[SourceFile], context: str = "") -> ReviewResult:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            raise AssertionError("unreachable")

    database = await make_database(tmp_path)
    try:
        store = JobStore(database)
        job = await store.enqueue(1, "review", snapshot())
        async with Worker(
            database, database.settings, cast(ReviewAnalyzer, BlockingAnalyzer())
        ) as worker:
            running = asyncio.create_task(worker.run_once())
            await asyncio.wait_for(started.wait(), 3)
            await store.cancel(1, job.id)
            assert await asyncio.wait_for(running, 4)
            assert cancelled.is_set()
        finished = await store.get(1, job.id)
        assert finished and finished.status == "cancelled"
        async with database.session() as session:
            assert not (await session.exec(select(ReviewReport))).all()
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_execution_timeout_cancels_provider_and_releases_tasks(tmp_path: Path) -> None:
    cancelled = asyncio.Event()

    class BlockingAnalyzer:
        async def review(self, files: Sequence[SourceFile], context: str = "") -> ReviewResult:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
            raise AssertionError("unreachable")

    database = await make_database(tmp_path, job_timeout_seconds=0.03)
    try:
        store = JobStore(database)
        job = await store.enqueue(1, "review", snapshot())
        async with Worker(
            database, database.settings, cast(ReviewAnalyzer, BlockingAnalyzer())
        ) as worker:
            assert await worker.run_once()
            assert cancelled.is_set()
        finished = await store.get(1, job.id)
        assert finished and finished.status == "failed" and "time limit" in str(finished.error)
        assert not any(
            task.get_name() in {f"job-{job.id}", f"lease-{job.id}"} for task in asyncio.all_tasks()
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_generated_artifacts_are_private_proposals_and_never_source_writes(
    tmp_path: Path,
) -> None:
    class Generator:
        async def generate_proposal(
            self, files: Sequence[SourceFile], kind: str, instructions: str = ""
        ) -> GeneratedProposal:
            return GeneratedProposal(
                kind="scaffold",
                summary="Suggested scaffold",
                artifacts=[ProposedArtifact(path="main.py", content="print('review first')\n")],
            )

    database = await make_database(tmp_path)
    try:
        store = JobStore(database)
        job = await store.enqueue(1, "scaffold", {"instructions": "Create an example"})
        async with Worker(database, database.settings, cast(ReviewAnalyzer, Generator())) as worker:
            assert await worker.run_once()
        finished = await store.get(1, job.id)
        assert finished and finished.status == "completed" and finished.result
        result = json.loads(finished.result)
        assert result["verified_syntax"] is True
        assert result["validation"]["status"] == "unavailable"
        saved = (
            database.settings.data_dir / "artifacts" / result["artifact_id"] / "files" / "main.py"
        )
        assert saved.read_text() == "print('review first')\n"
        assert not database.settings.projects_root.exists()
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_stop_joins_running_loops_and_preserves_recoverable_lease(tmp_path: Path) -> None:
    started, cancelled = asyncio.Event(), asyncio.Event()

    class BlockingAnalyzer:
        async def review(self, files: Sequence[SourceFile], context: str = "") -> ReviewResult:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
            raise AssertionError("unreachable")

    database = await make_database(tmp_path)
    try:
        store = JobStore(database)
        job = await store.enqueue(1, "review", snapshot())
        async with Worker(
            database, database.settings, cast(ReviewAnalyzer, BlockingAnalyzer())
        ) as worker:
            running = asyncio.create_task(worker.run_forever())
            await asyncio.wait_for(started.wait(), 3)
            worker.stop()
            await asyncio.wait_for(running, 3)
            assert cancelled.is_set()
        interrupted = await store.get(1, job.id)
        assert interrupted and interrupted.status == "running" and interrupted.lease_until
        assert not any(task.get_name().startswith("review-worker-") for task in asyncio.all_tasks())
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_cancelled_patch_validation_removes_unpublished_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.sandbox import SandboxResult

    started = asyncio.Event()

    class Generator:
        async def generate_proposal(
            self, files: Sequence[SourceFile], kind: str, instructions: str = ""
        ) -> GeneratedProposal:
            return GeneratedProposal(
                kind="migration",
                summary="Suggested change",
                diff="--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n",
            )

    async def blocking_validation(*args: object, **kwargs: object) -> SandboxResult:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr("app.sandbox.validate_proposal", blocking_validation)
    database = await make_database(tmp_path)
    try:
        project = database.settings.projects_root / "sample"
        project.mkdir(parents=True)
        (project / "a.py").write_text("x = 1\n")
        store = JobStore(database)
        job = await store.enqueue(1, "migrate", snapshot())
        async with Worker(database, database.settings, cast(ReviewAnalyzer, Generator())) as worker:
            running = asyncio.create_task(worker.run_once())
            await asyncio.wait_for(started.wait(), 3)
            await store.cancel(1, job.id)
            assert await asyncio.wait_for(running, 4)
        assert (project / "a.py").read_text() == "x = 1\n"
        assert not list((database.settings.data_dir / "artifacts").iterdir())
        finished = await store.get(1, job.id)
        assert finished and finished.status == "cancelled" and finished.result is None
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_patch_rejects_source_changes_outside_matching_hunk(tmp_path: Path) -> None:
    class Generator:
        async def generate_proposal(
            self, files: Sequence[SourceFile], kind: str, instructions: str = ""
        ) -> GeneratedProposal:
            return GeneratedProposal(
                kind="migration",
                summary="Suggested change",
                diff="--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n",
            )

    database = await make_database(tmp_path)
    try:
        project = database.settings.projects_root / "sample"
        project.mkdir(parents=True)
        # The patch hunk still matches. A context-only patch check would miss
        # this edit on a separate line after the immutable input was collected.
        (project / "a.py").write_text("x = 1\nuntouched = 2\n")
        store = JobStore(database)
        job = await store.enqueue(1, "migrate", snapshot("x = 1\nuntouched = 1\n"))
        async with Worker(database, database.settings, cast(ReviewAnalyzer, Generator())) as worker:
            assert await worker.run_once()
        finished = await store.get(1, job.id)
        assert finished and finished.status == "failed" and finished.result is None
        assert (project / "a.py").read_text() == "x = 1\nuntouched = 2\n"
        artifact_root = database.settings.data_dir / "artifacts"
        assert not artifact_root.exists() or not list(artifact_root.iterdir())
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_artifact_only_tests_preserve_source_provenance(tmp_path: Path) -> None:
    class Generator:
        async def generate_proposal(
            self, files: Sequence[SourceFile], kind: str, instructions: str = ""
        ) -> GeneratedProposal:
            return GeneratedProposal(
                kind="tests",
                summary="Suggested tests",
                artifacts=[
                    ProposedArtifact(path="test_a.py", content="def test_a():\n    assert True\n")
                ],
            )

    database = await make_database(tmp_path)
    try:
        project = database.settings.projects_root / "sample"
        project.mkdir(parents=True)
        (project / "a.py").write_text("x = 1\n")
        store = JobStore(database)
        job = await store.enqueue(1, "tests", snapshot())
        async with Worker(database, database.settings, cast(ReviewAnalyzer, Generator())) as worker:
            assert await worker.run_once()
        finished = await store.get(1, job.id)
        assert finished and finished.status == "completed" and finished.result
        result = json.loads(finished.result)
        manifest = json.loads(
            (
                database.settings.data_dir / "artifacts" / result["artifact_id"] / "manifest.json"
            ).read_text()
        )
        expected = hashlib.sha256(b"x = 1\n").hexdigest()
        assert manifest["source_hashes"]["a.py"] == expected
        assert result["source_hashes"]["a.py"] == expected
        assert not (project / "test_a.py").exists()
    finally:
        await database.close()
