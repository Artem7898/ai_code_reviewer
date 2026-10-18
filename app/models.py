"""Persistent domain models shared by asynchronous API and workers."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import Column, DateTime, Text, UniqueConstraint
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator
from sqlmodel import Field, SQLModel


class UTCDateTime(TypeDecorator[datetime]):
    """SQLite stores naive timestamps; always expose timezone-aware UTC values."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("New timestamps must include a timezone")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def utc_now() -> datetime:
    return datetime.now(UTC)


class User(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True, max_length=150)
    hashed_password: str
    is_active: bool = True
    is_admin: bool = False
    auth_revision: int = Field(default=1, ge=1)


class ReviewJob(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("owner_id", "kind", "dedup_key", name="uq_job_dedup"),)

    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True, max_length=36)
    owner_id: int = Field(foreign_key="user.id", index=True)
    kind: str = Field(max_length=40)
    project_name: str | None = Field(default=None, max_length=255)
    payload: str = Field(sa_column=Column(Text, nullable=False))
    dedup_key: str | None = Field(default=None, max_length=128)
    status: str = Field(default="queued", index=True, max_length=20)
    progress: float = 0.0
    result: str | None = Field(default=None, sa_column=Column(Text))
    error: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=utc_now, sa_type=UTCDateTime, nullable=False)
    updated_at: datetime = Field(default_factory=utc_now, sa_type=UTCDateTime, nullable=False)
    lease_token: str | None = Field(default=None, max_length=64)
    lease_until: datetime | None = Field(default=None, sa_type=UTCDateTime)
    attempts: int = 0


class ReviewReport(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    project_name: str
    file_path: str
    content_type: str
    summary: str = Field(sa_column=Column(Text, nullable=False))
    review_result: str = Field(sa_column=Column(Text, nullable=False))
    status: str = "pending"
    created_at: datetime = Field(default_factory=utc_now, sa_type=UTCDateTime, nullable=False)
    # NULL identifies historical records; callers must restrict them to administrators.
    owner_id: int | None = Field(default=None, foreign_key="user.id", index=True)
    job_id: str | None = Field(default=None, foreign_key="reviewjob.id", index=True)


class CloudBudget(SQLModel, table=True):
    """Durable daily reservations shared by all workers for one provider account."""

    account_hash: str = Field(primary_key=True, max_length=64)
    day: str = Field(primary_key=True, max_length=10)
    reserved_micro_usd: int = Field(default=0, ge=0)
    updated_at: datetime = Field(default_factory=utc_now, sa_type=UTCDateTime, nullable=False)
