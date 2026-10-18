"""Retention command exercises a private database and never the working database."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlmodel import select

from app import cli
from app.db import Database
from app.models import ReviewJob, ReviewReport, User
from app.settings import Settings


@pytest.mark.parametrize("include_reports", [False, True])
def test_prune_command_is_explicit_bounded_and_preserves_active_jobs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    include_reports: bool,
) -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        database_path=tmp_path / "private.db",
        data_dir=tmp_path / "data",
        projects_root=tmp_path / "projects",
        ai_provider="static",
    )
    old = datetime.now(UTC) - timedelta(days=60)
    artifact_id = "a" * 32
    artifact = settings.data_dir / "artifacts" / artifact_id
    artifact.mkdir(parents=True)
    (artifact / "proposal.patch").write_text("private generated patch", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    (artifact / "external-link").symlink_to(outside)

    async def seed() -> None:
        database = Database(settings)
        try:
            await database.initialize()
            async with database.session() as session:
                session.add(User(id=1, username="owner", hashed_password="not-used"))
                await session.commit()
                session.add(
                    ReviewJob(
                        id="old",
                        owner_id=1,
                        kind="review",
                        payload="{}",
                        status="completed",
                        updated_at=old,
                        result=json.dumps({"artifact_id": artifact_id}),
                    )
                )
                session.add(
                    ReviewJob(
                        id="active",
                        owner_id=1,
                        kind="review",
                        payload="{}",
                        status="queued",
                        updated_at=old,
                    )
                )
                session.add(
                    ReviewJob(
                        id="recent", owner_id=1, kind="review", payload="{}", status="completed"
                    )
                )
                await session.commit()
                session.add(
                    ReviewReport(
                        owner_id=1,
                        job_id="old",
                        project_name="sample",
                        file_path="main.py",
                        content_type="code",
                        summary="historical",
                        review_result="keep-history",
                        status="completed",
                    )
                )
                await session.commit()
        finally:
            await database.close()

    asyncio.run(seed())
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    arguments = ["app.cli", "prune", "--days", "30", "--limit", "1"]
    if include_reports:
        arguments.append("--include-reports")
    monkeypatch.setattr("sys.argv", arguments)
    cli.main()
    assert "Deleted 1 old terminal jobs" in capsys.readouterr().out
    assert not artifact.exists() and outside.read_text(encoding="utf-8") == "keep"

    async def verify() -> None:
        database = Database(settings)
        try:
            async with database.session() as session:
                assert {job.id for job in (await session.exec(select(ReviewJob))).all()} == {
                    "active",
                    "recent",
                }
                reports = (await session.exec(select(ReviewReport))).all()
                assert len(reports) == (0 if include_reports else 1)
                if reports:
                    assert reports[0].review_result == "keep-history" and reports[0].job_id is None
        finally:
            await database.close()

    asyncio.run(verify())


@pytest.mark.asyncio
async def test_prune_rejects_non_finite_retention_before_opening_database(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None, data_dir=tmp_path / "data", database_path=tmp_path / "private.db"
    )
    with pytest.raises(ValueError, match="finite"):
        await cli.prune_history(settings, days=float("inf"))
    assert not settings.database_path.exists()
