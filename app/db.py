"""Async SQLite sessions and serialized, additive legacy-schema migrations."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from sqlalchemy import event, inspect
from sqlalchemy.engine import URL, Connection
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from . import models as _models  # noqa: F401 - register tables before migration
from .settings import Settings

SCHEMA_VERSION = 2


def _migrate(connection: Connection) -> None:
    """Preserve historical rows and serialize startup across API/worker processes."""
    connection.exec_driver_sql("BEGIN IMMEDIATE")
    try:
        connection.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        version = connection.exec_driver_sql(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).scalar_one()
        if version > SCHEMA_VERSION:
            raise RuntimeError("Database schema is newer than this application")
        if version < 1:
            inspector = inspect(connection)
            tables = set(inspector.get_table_names())
            if "user" in tables:
                columns = {column["name"] for column in inspector.get_columns("user")}
                if "is_active" not in columns:
                    connection.exec_driver_sql(
                        'ALTER TABLE "user" ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT 1'
                    )
                if "is_admin" not in columns:
                    connection.exec_driver_sql(
                        'ALTER TABLE "user" ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT 0'
                    )
                # The old startup seeded a publicly known administrator password.
                # Preserve its row/references, but require an explicit CLI reset.
                connection.exec_driver_sql(
                    'UPDATE "user" SET is_active = 0 WHERE username = ?', ("admin",)
                )
            if "reviewreport" in tables:
                columns = {column["name"] for column in inspector.get_columns("reviewreport")}
                if "owner_id" not in columns:
                    connection.exec_driver_sql(
                        'ALTER TABLE reviewreport ADD COLUMN owner_id INTEGER REFERENCES "user"(id)'
                    )
                if "job_id" not in columns:
                    connection.exec_driver_sql(
                        "ALTER TABLE reviewreport ADD COLUMN job_id VARCHAR REFERENCES reviewjob(id)"
                    )
            SQLModel.metadata.create_all(connection)
            # create_all intentionally does not add indices to existing legacy tables.
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_reviewreport_owner_id ON reviewreport(owner_id)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_reviewreport_job_id ON reviewreport(job_id)"
            )
            connection.exec_driver_sql(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (1, datetime.now(UTC).isoformat()),
            )
        if version < 2:
            columns = {column["name"] for column in inspect(connection).get_columns("user")}
            if "auth_revision" not in columns:
                connection.exec_driver_sql(
                    'ALTER TABLE "user" ADD COLUMN auth_revision INTEGER NOT NULL DEFAULT 1'
                )
            # Add new tables (currently durable cloud budget reservations) without
            # dropping or rewriting historical application or Django tables.
            SQLModel.metadata.create_all(connection)
            connection.exec_driver_sql(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (2, datetime.now(UTC).isoformat()),
            )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


class Database:
    """One engine per application; each task owns an independent session."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        settings.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            descriptor = os.open(
                settings.database_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        self.engine: AsyncEngine = create_async_engine(
            URL.create("sqlite+aiosqlite", database=str(settings.database_path)),
            connect_args={"timeout": 30},
            pool_size=5,
            max_overflow=0,
            pool_pre_ping=True,
            hide_parameters=True,
        )
        self._sessions = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )

        @event.listens_for(self.engine.sync_engine, "connect")
        def configure_sqlite(connection: sqlite3.Connection, record: object) -> None:
            cursor = connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=30000")
                cursor.execute("PRAGMA journal_mode=WAL")
            finally:
                cursor.close()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self._sessions() as session:
            try:
                yield session
            except BaseException:
                await session.rollback()
                raise

    async def initialize(self) -> None:
        async with self.engine.connect() as connection:
            await connection.run_sync(_migrate)

    async def close(self) -> None:
        await self.engine.dispose()
