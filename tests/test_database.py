from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlmodel import select

from app.auth import create_access_token, decode_identity
from app.cli import create_user
from app.db import Database
from app.models import CloudBudget, ReviewJob, ReviewReport, User
from app.settings import Settings


def configuration(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        database_path=tmp_path / "legacy.db",
        data_dir=tmp_path / "data",
        projects_root=tmp_path / "projects",
        secret_key=SecretStr("database-tests-signing-key-with-32-characters"),
    )


@pytest.mark.asyncio
async def test_fresh_schema_has_no_seeded_users_and_separate_sessions(tmp_path: Path) -> None:
    database = Database(configuration(tmp_path))
    try:
        await database.initialize()
        async with database.session() as first, database.session() as second:
            assert first is not second
            assert (await first.exec(select(User))).all() == []
            user = User(username="owner", hashed_password="hash")
            first.add(user)
            await first.commit()
            assert user.id is not None
            job = ReviewJob(owner_id=user.id, kind="review", payload="{}")
            first.add(job)
            await first.commit()
            await first.refresh(job)
            assert job.created_at.tzinfo is not None
        async with database.session() as session:
            assert (await session.exec(select(ReviewJob))).one().id == job.id
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_legacy_migration_preserves_reports_and_disables_seeded_admin(tmp_path: Path) -> None:
    settings = configuration(tmp_path)
    with sqlite3.connect(settings.database_path) as connection:
        connection.executescript("""
            CREATE TABLE user (id INTEGER PRIMARY KEY, username VARCHAR UNIQUE, hashed_password VARCHAR NOT NULL);
            INSERT INTO user VALUES (1, 'admin', 'legacy-hash');
            INSERT INTO user VALUES (2, 'other', 'other-hash');
            CREATE TABLE reviewreport (
                id INTEGER PRIMARY KEY, project_name VARCHAR, file_path VARCHAR,
                content_type VARCHAR, summary VARCHAR, review_result VARCHAR,
                status VARCHAR, created_at DATETIME
            );
            INSERT INTO reviewreport VALUES (
                4, 'legacy-project', 'legacy.py', 'code', 'summary', 'result',
                'completed', '2025-01-02 03:04:05.000000'
            );
        """)
    database = Database(settings)
    other_process = Database(settings)
    try:
        await asyncio.gather(database.initialize(), other_process.initialize())
        await database.initialize()
        async with database.session() as session:
            admin = await session.get(User, 1)
            other = await session.get(User, 2)
            report = await session.get(ReviewReport, 4)
            assert admin is not None and not admin.is_active
            assert admin.hashed_password == "legacy-hash"
            assert other is not None and other.is_active
            assert report is not None and report.review_result == "result"
            assert report.owner_id is None and report.created_at.tzinfo is not None
            connection = await session.connection()
            assert (
                await connection.exec_driver_sql("SELECT COUNT(*) FROM schema_migrations")
            ).scalar_one() == 2
    finally:
        await database.close()
        await other_process.close()
    await create_user(
        settings, "admin", "an-explicit-new-password", admin=True, reset_password=True
    )
    database = Database(settings)
    try:
        await database.initialize()
        async with database.session() as session:
            admin = await session.get(User, 1)
            assert admin is not None and admin.is_active and admin.is_admin
            assert admin.auth_revision == 2
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_failed_session_rolls_back_and_releases_connection(tmp_path: Path) -> None:
    database = Database(configuration(tmp_path))
    try:
        await database.initialize()
        with pytest.raises(RuntimeError, match="abort"):
            async with database.session() as session:
                session.add(User(username="rolled-back", hashed_password="hash"))
                await session.flush()
                raise RuntimeError("abort")
        async with database.session() as session:
            assert (await session.exec(select(User))).all() == []
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_version_one_upgrade_preserves_reset_admin_and_adds_budget_table(
    tmp_path: Path,
) -> None:
    settings = configuration(tmp_path)
    with sqlite3.connect(settings.database_path) as connection:
        connection.executescript("""
            CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            INSERT INTO schema_migrations VALUES (1, '2026-10-18T00:00:00+00:00');
            CREATE TABLE user (
                id INTEGER PRIMARY KEY, username VARCHAR UNIQUE, hashed_password VARCHAR NOT NULL,
                is_active BOOLEAN NOT NULL, is_admin BOOLEAN NOT NULL
            );
            INSERT INTO user VALUES (1, 'admin', 'explicitly-reset-hash', 1, 1);
        """)
    database = Database(settings)
    try:
        await database.initialize()
        await database.initialize()
        async with database.session() as session:
            user = await session.get(User, 1)
            assert user is not None and user.is_active and user.is_admin
            assert user.auth_revision == 1
            assert user.hashed_password == "explicitly-reset-hash"
            session.add(
                CloudBudget(account_hash="a" * 64, day="2026-10-18", reserved_micro_usd=100)
            )
            await session.commit()
            budget = await session.get(CloudBudget, ("a" * 64, "2026-10-18"))
            assert budget is not None and budget.reserved_micro_usd == 100
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_concurrent_password_resets_advance_authorization_revision(tmp_path: Path) -> None:
    settings = configuration(tmp_path)
    await create_user(settings, "owner", "initial-long-password")
    database = Database(settings)
    try:
        async with database.session() as session:
            user = (await session.exec(select(User))).one()
            token = create_access_token(user, settings)
        await asyncio.gather(
            create_user(settings, "owner", "first-new-password", reset_password=True),
            create_user(settings, "owner", "second-new-password", reset_password=True),
        )
        async with database.session() as session:
            updated = (await session.exec(select(User))).one()
            assert updated.auth_revision == 3
            assert decode_identity(token, settings).auth_revision != updated.auth_revision
    finally:
        await database.close()
