"""Durable, owner-scoped jobs with SQLite transactions and fenced worker leases."""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

from sqlalchemy import ColumnElement, and_, delete, func, or_, update
from sqlalchemy.orm import InstrumentedAttribute, load_only
from sqlmodel import col, select

from .db import Database
from .models import CloudBudget, ReviewJob, ReviewReport

type JobKind = Literal["review", "upload", "migrate", "tests", "scaffold"]
type JSONValue = None | bool | int | float | str | list[JSONValue] | dict[str, JSONValue]

JOB_KINDS: frozenset[str] = frozenset({"review", "upload", "migrate", "tests", "scaffold"})
TERMINAL_STATUSES: frozenset[str] = frozenset({"completed", "failed", "cancelled"})


class JobLimitError(ValueError):
    """The owner's bounded queue has reached its configured capacity."""


class BudgetLimitError(RuntimeError):
    """The durable daily cloud reservation would exceed the configured cap."""


@dataclass(frozen=True, slots=True)
class PruneResult:
    removed_jobs: int
    artifact_ids: tuple[str, ...]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


class JobStore:
    """All mutations use short transactions; no session spans provider requests.

    ``dedup_key`` must represent immutable input (e.g. a SHA256 source snapshot).
    Mutable project names alone are deliberately never treated as cache keys.
    Model and analysis policy are included in the key automatically.
    """

    def __init__(self, database: Database) -> None:
        self.database = database
        self.settings = database.settings

    def _policy_key(self, dedup_key: str | None) -> str | None:
        if dedup_key is None:
            return None
        if not dedup_key or len(dedup_key) > 1024:
            raise ValueError("Invalid input digest")
        policy = {
            "schema": 1,
            "digest": dedup_key,
            "provider": self.settings.ai_provider,
            "local_model": self.settings.ollama_model,
            "cloud_model": self.settings.mistral_model,
            "local_endpoint": self.settings.ollama_base_url,
            "cloud_enabled": self.settings.allow_cloud_ai,
            "ruff": self.settings.ruff_enabled,
            "max_output_tokens": self.settings.ai_max_output_tokens,
        }
        return hashlib.sha256(_json(policy).encode()).hexdigest()

    async def reserve_budget(self, amount_usd: float) -> None:
        """Reserve each cloud attempt before I/O, atomically across processes.

        Reservations are conservative and intentionally never refunded: a
        timeout or dropped connection can still be billed by the provider.
        Integer microdollars avoid floating-point races/rounding overspend.
        The configured account scope survives worker restart and key rotation.
        """
        if not math.isfinite(amount_usd) or amount_usd <= 0:
            raise BudgetLimitError("Invalid cloud budget reservation")
        cap = self.settings.ai_max_cost_usd
        if not math.isfinite(cap) or not 0 < cap <= 9_000_000_000_000:
            raise BudgetLimitError(
                "A finite positive cloud budget within ledger limits is required"
            )
        if amount_usd > cap:
            raise BudgetLimitError("Daily cloud budget would be exceeded")
        amount = math.ceil(amount_usd * 1_000_000)
        limit = math.floor(cap * 1_000_000)
        if amount > limit:
            raise BudgetLimitError("Daily cloud budget would be exceeded")
        now = _utcnow()
        day = now.date().isoformat()
        account = hashlib.sha256(b"mistral:environment").hexdigest()
        async with self.database.session() as session:
            connection = await session.connection()
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
            budget = await session.get(CloudBudget, (account, day))
            used = budget.reserved_micro_usd if budget is not None else 0
            if used + amount > limit:
                raise BudgetLimitError("Daily cloud budget would be exceeded")
            if budget is None:
                budget = CloudBudget(account_hash=account, day=day, reserved_micro_usd=amount)
            else:
                budget.reserved_micro_usd = used + amount
                budget.updated_at = now
            session.add(budget)
            await session.commit()

    async def enqueue(
        self,
        owner_id: int,
        kind: JobKind,
        payload: Mapping[str, JSONValue],
        dedup_key: str | None = None,
    ) -> ReviewJob:
        if kind not in JOB_KINDS or owner_id <= 0:
            raise ValueError("Invalid job request")
        encoded = _json(dict(payload))
        if len(encoded.encode()) > 6 * self.settings.max_review_chars + 65536:
            raise ValueError("Job payload exceeds the configured limit")
        key = self._policy_key(dedup_key)
        now = _utcnow()
        async with self.database.session() as session:
            # A write lock makes quota checking and dedup insertion atomic across
            # independent processes, not just tasks sharing an asyncio lock.
            connection = await session.connection()
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
            if key is not None:
                existing = (
                    await session.exec(
                        select(ReviewJob).where(
                            col(ReviewJob.owner_id) == owner_id,
                            col(ReviewJob.kind) == kind,
                            col(ReviewJob.dedup_key) == key,
                        )
                    )
                ).first()
                if existing is not None:
                    cutoff = now - timedelta(seconds=self.settings.cache_ttl_seconds)
                    if existing.status not in TERMINAL_STATUSES or (
                        existing.status == "completed"
                        and self.settings.cache_ttl_seconds > 0
                        and existing.updated_at >= cutoff
                    ):
                        await session.commit()
                        return existing
                    existing.dedup_key = None
                    session.add(existing)
                    await session.flush()
            count = (
                await session.exec(
                    select(func.count())
                    .select_from(ReviewJob)
                    .where(
                        col(ReviewJob.owner_id) == owner_id,
                        col(ReviewJob.status).in_(("queued", "running")),
                    )
                )
            ).one()
            if count >= self.settings.max_jobs_per_user:
                raise JobLimitError("Too many pending jobs; wait or cancel an existing job")
            project_name = payload.get("project_name")
            job = ReviewJob(
                id=str(uuid.uuid4()),
                owner_id=owner_id,
                kind=kind,
                project_name=project_name if isinstance(project_name, str) else None,
                payload=encoded,
                dedup_key=key,
                status="queued",
                created_at=now,
                updated_at=now,
            )
            session.add(job)
            await session.commit()
            return job

    async def get(self, owner_id: int, job_id: str) -> ReviewJob | None:
        async with self.database.session() as session:
            return (
                await session.exec(
                    select(ReviewJob).where(
                        col(ReviewJob.id) == job_id, col(ReviewJob.owner_id) == owner_id
                    )
                )
            ).first()

    async def list(self, owner_id: int, limit: int = 20, offset: int = 0) -> list[ReviewJob]:
        """Return detached metadata only; use ``get`` for snapshots/results.

        Polling a page of jobs must not reload every source snapshot and SARIF
        document. Deferred attributes raise instead of causing implicit I/O.
        """
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("Invalid pagination")
        metadata = (
            "id",
            "owner_id",
            "kind",
            "status",
            "progress",
            "error",
            "created_at",
            "updated_at",
        )
        columns = tuple(
            cast("InstrumentedAttribute[object]", getattr(ReviewJob, name)) for name in metadata
        )
        async with self.database.session() as session:
            return list(
                (
                    await session.exec(
                        select(ReviewJob)
                        .options(load_only(*columns, raiseload=True))
                        .where(col(ReviewJob.owner_id) == owner_id)
                        .order_by(col(ReviewJob.created_at).desc(), col(ReviewJob.id).desc())
                        .offset(offset)
                        .limit(limit)
                    )
                ).all()
            )

    async def cancel(self, owner_id: int, job_id: str) -> ReviewJob | None:
        async with self.database.session() as session:
            statement = (
                update(ReviewJob)
                .where(
                    col(ReviewJob.id) == job_id,
                    col(ReviewJob.owner_id) == owner_id,
                    col(ReviewJob.status).in_(("queued", "running")),
                )
                .values(
                    status="cancelled",
                    updated_at=_utcnow(),
                    lease_token=None,
                    lease_until=None,
                    dedup_key=None,
                    error=None,
                )
                .returning(ReviewJob)
            )
            result = (await session.exec(statement)).scalar_one_or_none()
            await session.commit()
        return result if result is not None else await self.get(owner_id, job_id)

    async def claim(self) -> ReviewJob | None:
        now = _utcnow()
        eligible = or_(
            col(ReviewJob.status) == "queued",
            and_(col(ReviewJob.status) == "running", col(ReviewJob.lease_until) <= now),
        )
        async with self.database.session() as session:
            # Exhausted leases must not remain eternally running after crashes.
            await session.exec(
                update(ReviewJob)
                .where(eligible, col(ReviewJob.attempts) >= self.settings.job_max_attempts)
                .values(
                    status="failed",
                    error="Worker retry limit reached",
                    lease_token=None,
                    lease_until=None,
                    dedup_key=None,
                    updated_at=now,
                )
            )
            candidate = (
                select(col(ReviewJob.id))
                .where(eligible, col(ReviewJob.attempts) < self.settings.job_max_attempts)
                .order_by(col(ReviewJob.created_at), col(ReviewJob.id))
                .limit(1)
                .scalar_subquery()
            )
            claimed = (
                await session.exec(
                    update(ReviewJob)
                    .where(col(ReviewJob.id) == candidate, eligible)
                    .values(
                        status="running",
                        lease_token=uuid.uuid4().hex,
                        lease_until=now + timedelta(seconds=self.settings.job_lease_seconds),
                        attempts=col(ReviewJob.attempts) + 1,
                        updated_at=now,
                        error=None,
                    )
                    .returning(ReviewJob)
                )
            ).scalar_one_or_none()
            await session.commit()
            return claimed

    async def heartbeat(self, job_id: str, lease_token: str, progress: float | None = None) -> bool:
        if progress is not None and (not math.isfinite(progress) or not 0 <= progress < 1):
            raise ValueError("Progress must be a finite fraction below 1")
        now = _utcnow()
        values: dict[str, object] = {
            "lease_until": now + timedelta(seconds=self.settings.job_lease_seconds),
            "updated_at": now,
        }
        if progress is not None:
            values["progress"] = progress
        async with self.database.session() as session:
            result = await session.exec(
                update(ReviewJob)
                .where(*self._lease_conditions(job_id, lease_token, now))
                .values(**values)
                .returning(col(ReviewJob.id))
            )
            renewed = result.scalar_one_or_none() is not None
            await session.commit()
            return renewed

    @staticmethod
    def _lease_conditions(
        job_id: str, lease_token: str, now: datetime
    ) -> tuple[ColumnElement[bool], ...]:
        return (
            col(ReviewJob.id) == job_id,
            col(ReviewJob.status) == "running",
            col(ReviewJob.lease_token) == lease_token,
            col(ReviewJob.lease_until) > now,
        )

    async def complete(
        self,
        job_id: str,
        lease_token: str,
        result: Mapping[str, JSONValue],
        reports: Sequence[ReviewReport] = (),
    ) -> bool:
        now = _utcnow()
        async with self.database.session() as session:
            job = (
                await session.exec(
                    update(ReviewJob)
                    .where(*self._lease_conditions(job_id, lease_token, now))
                    .values(
                        status="completed",
                        result=_json(dict(result)),
                        error=None,
                        progress=1.0,
                        updated_at=now,
                        lease_token=None,
                        lease_until=None,
                    )
                    .returning(ReviewJob)
                )
            ).scalar_one_or_none()
            if job is None:
                return False
            for report in reports:
                # Never trust caller-provided owner/job associations.
                report.owner_id = job.owner_id
                report.job_id = job.id
                report.status = "completed"
                session.add(report)
            await session.commit()
            return True

    async def fail(
        self, job_id: str, lease_token: str, error_code: str = "analysis_failed"
    ) -> bool:
        # Exception strings can include source fragments, credentials and paths.
        messages = {
            "analysis_failed": "Analysis failed; check provider configuration and worker logs",
            "invalid_input": "Input is unavailable or exceeds the configured limits",
            "timeout": "Job exceeded the configured execution time limit",
            "shutdown": "Worker interrupted; job can be retried",
            "budget": "Configured AI token or daily cloud budget limit reached",
            "privacy": "Cloud analysis blocked because source may contain credentials; use local analysis or remove secrets",
        }
        message = messages.get(error_code, messages["analysis_failed"])
        now = _utcnow()
        async with self.database.session() as session:
            result = await session.exec(
                update(ReviewJob)
                .where(*self._lease_conditions(job_id, lease_token, now))
                .values(
                    status="failed",
                    error=message,
                    result=None,
                    updated_at=now,
                    lease_token=None,
                    lease_until=None,
                    dedup_key=None,
                )
                .returning(col(ReviewJob.id))
            )
            changed = result.scalar_one_or_none() is not None
            await session.commit()
            return changed

    async def prune(
        self,
        older_than_seconds: float = 2_592_000,
        limit: int = 100,
        *,
        include_reports: bool = False,
    ) -> PruneResult:
        """Explicit retention maintenance; active jobs and their leases survive.

        Returns artifact identifiers for the maintenance caller to remove using
        the safe artifact API. Reports retain their owner and successful review
        content by default. Deleting report history requires explicit opt-in.
        """
        if (
            not math.isfinite(older_than_seconds)
            or older_than_seconds < 0
            or not 1 <= limit <= 1000
        ):
            raise ValueError("Invalid retention policy")
        cutoff = _utcnow() - timedelta(seconds=older_than_seconds)
        artifact_ids: list[str] = []
        async with self.database.session() as session:
            connection = await session.connection()
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
            identifiers = list(
                (
                    await session.exec(
                        select(col(ReviewJob.id))
                        .where(
                            col(ReviewJob.status).in_(TERMINAL_STATUSES),
                            col(ReviewJob.updated_at) < cutoff,
                        )
                        .order_by(col(ReviewJob.updated_at), col(ReviewJob.id))
                        .limit(limit)
                    )
                ).all()
            )
            # Load one result at a time; a batch of large source snapshots never
            # needs to be materialized in the maintenance process's memory.
            for identifier in identifiers:
                encoded = (
                    await session.exec(
                        select(col(ReviewJob.result)).where(col(ReviewJob.id) == identifier)
                    )
                ).one()
                if encoded:
                    value: object = json.loads(encoded)
                    if isinstance(value, dict) and isinstance(value.get("artifact_id"), str):
                        artifact_ids.append(value["artifact_id"])
            if identifiers:
                if include_reports:
                    await session.exec(
                        delete(ReviewReport).where(col(ReviewReport.job_id).in_(identifiers))
                    )
                else:
                    await session.exec(
                        update(ReviewReport)
                        .where(col(ReviewReport.job_id).in_(identifiers))
                        .values(job_id=None)
                    )
                await session.exec(delete(ReviewJob).where(col(ReviewJob.id).in_(identifiers)))
            await session.commit()
        return PruneResult(len(identifiers), tuple(artifact_ids))

    @staticmethod
    def payload(job: ReviewJob) -> dict[str, JSONValue]:
        value: object = json.loads(job.payload)
        if not isinstance(value, dict):
            raise ValueError("Invalid stored job payload")
        return cast("dict[str, JSONValue]", value)
