"""Bounded asynchronous workers. Only explicit queued work is ever processed."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import signal
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Literal, cast

from .ai_client import ProviderBudgetError, ProviderPrivacyError
from .db import Database
from .jobs import BudgetLimitError, JobStore, JSONValue
from .models import ReviewJob, ReviewReport
from .settings import Settings

if TYPE_CHECKING:
    from .ai_client import AIProvider
    from .analysis import ReviewAnalyzer
    from .schemas import SourceFile

logger = logging.getLogger(__name__)


class _LeaseLost(Exception):
    """Cancellation or another worker invalidated this execution's lease."""


class Worker:
    """One reusable provider pool, fixed worker count and bounded input snapshots.

    Callers injecting an analyzer retain ownership of its lifecycle. Otherwise
    use this worker as an asynchronous context manager, or call ``aclose``.
    """

    def __init__(
        self, database: Database, settings: Settings, analyzer: ReviewAnalyzer | None = None
    ) -> None:
        self.database = database
        self.settings = settings
        self.store = JobStore(database)
        self._stop = asyncio.Event()
        self._provider: AIProvider | None = None
        self._closed = False
        if analyzer is None:
            from .ai_client import AIProvider
            from .analysis import ReviewAnalyzer

            self._provider = AIProvider(settings, budget_reserver=self.store.reserve_budget)
            self.analyzer = ReviewAnalyzer(settings, provider=self._provider)
        else:
            self.analyzer = analyzer

    async def __aenter__(self) -> Worker:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    def stop(self) -> None:
        self._stop.set()

    async def aclose(self) -> None:
        self.stop()
        if not self._closed:
            self._closed = True
            if self._provider is not None:
                await self._provider.aclose()

    async def _heartbeat(self, job: ReviewJob) -> None:
        if job.lease_token is None:
            raise _LeaseLost
        while True:
            await asyncio.sleep(min(self.settings.job_lease_seconds / 3, 2.0))
            if not await self.store.heartbeat(job.id, job.lease_token):
                raise _LeaseLost

    async def run_once(self) -> bool:
        if self._closed:
            raise RuntimeError("Worker is closed")
        job = await self.store.claim()
        if job is None:
            return False
        assert job.lease_token is not None
        execution = asyncio.create_task(self._execute(job), name=f"job-{job.id}")
        heartbeat = asyncio.create_task(self._heartbeat(job), name=f"lease-{job.id}")
        result: dict[str, JSONValue] | None = None
        persisted = False
        try:
            async with asyncio.timeout(self.settings.job_timeout_seconds):
                done, _ = await asyncio.wait(
                    (execution, heartbeat), return_when=asyncio.FIRST_COMPLETED
                )
                if heartbeat in done:
                    await heartbeat
                    raise _LeaseLost
                result, reports = await execution
                persisted = await self.store.complete(job.id, job.lease_token, result, reports)
        except _LeaseLost:
            # A stale worker must never update or persist results after losing
            # its lease, even when its provider eventually returns successfully.
            pass
        except TimeoutError:
            await self.store.fail(job.id, job.lease_token, "timeout")
        except BudgetLimitError, ProviderBudgetError:
            await self.store.fail(job.id, job.lease_token, "budget")
        except ProviderPrivacyError:
            await self.store.fail(job.id, job.lease_token, "privacy")
        except ValueError, OSError, UnicodeError:
            await self.store.fail(job.id, job.lease_token, "invalid_input")
            logger.warning("Job %s rejected invalid input", job.id)
        except asyncio.CancelledError:
            # Leave a recoverable lease after shutdown; another process can
            # reclaim it. No successful report is recorded for interrupted work.
            raise
        except Exception:
            await self.store.fail(job.id, job.lease_token)
            # Keep provider exception text out of logs: URLs, model output and
            # source snippets may contain confidential information.
            logger.error("Job %s failed during analysis", job.id)
        finally:
            execution.cancel()
            heartbeat.cancel()
            await asyncio.gather(execution, heartbeat, return_exceptions=True)
            if result is None and not execution.cancelled() and execution.exception() is None:
                result = execution.result()[0]
            if not persisted and result is not None:
                artifact_id = result.get("artifact_id")
                if isinstance(artifact_id, str):
                    # A cancellation can arrive immediately after SQLite's
                    # COMMIT. Check durable state before removing its artifact.
                    current = await self.store.get(job.owner_id, job.id)
                    committed = (
                        current is not None
                        and current.status == "completed"
                        and current.result is not None
                        and json.loads(current.result).get("artifact_id") == artifact_id
                    )
                    if not committed:
                        await self._remove_artifact(artifact_id)
        return True

    async def _loop(self) -> None:
        while not self._stop.is_set():
            if not await self.run_once():
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self.settings.worker_poll_seconds
                    )
                except TimeoutError:
                    pass

    async def run_forever(self) -> None:
        # TaskGroup cancels and joins sibling loops on failure. There is no
        # unbounded create_task-per-job scanner and no shared mutable session.
        async with asyncio.TaskGroup() as group:
            loops: list[asyncio.Task[None]] = []
            for index in range(self.settings.worker_concurrency):
                loops.append(group.create_task(self._loop(), name=f"review-worker-{index}"))
            await self._stop.wait()
            for task in loops:
                task.cancel()

    def _snapshot(self, payload: Mapping[str, JSONValue]) -> list[SourceFile] | None:
        from .schemas import SourceFile

        value = payload.get("snapshot")
        if value is None:
            return None
        if not isinstance(value, list) or len(value) > self.settings.max_review_files:
            raise ValueError("Invalid source snapshot")
        files = [SourceFile.model_validate_json(json.dumps(item)) for item in value]
        if len({source.path for source in files}) != len(files):
            raise ValueError("Duplicate source paths")
        if sum(len(source.content) for source in files) > self.settings.max_review_chars:
            raise ValueError("Source snapshot exceeds context limit")
        if any(len(source.content.encode()) > self.settings.max_file_bytes for source in files):
            raise ValueError("Source snapshot exceeds file limit")
        if any(
            source.sha256 and hashlib.sha256(source.content.encode()).hexdigest() != source.sha256
            for source in files
        ):
            raise ValueError("Source snapshot digest mismatch")
        return files

    @staticmethod
    def _text(payload: Mapping[str, JSONValue], key: str, default: str = "") -> str:
        value = payload.get(key, default)
        if not isinstance(value, str):
            raise ValueError("Invalid job parameter")
        return value

    async def _files(self, job: ReviewJob, payload: Mapping[str, JSONValue]) -> list[SourceFile]:
        snapshot = self._snapshot(payload)
        if snapshot is not None:
            return snapshot
        from .utils import collect_project, get_project, read_source, read_upload

        if job.kind == "upload":
            source = await asyncio.to_thread(
                read_upload,
                self.settings.data_dir,
                self._text(payload, "upload_id"),
                self._text(payload, "filename"),
                max_file_bytes=self.settings.max_upload_bytes,
                max_pages=self.settings.max_pdf_pages,
            )
            if len(source.content) > self.settings.max_review_chars:
                raise ValueError("Upload exceeds context limit")
            return [source]
        project = await asyncio.to_thread(
            get_project, self.settings.projects_root, self._text(payload, "project_name")
        )
        if job.kind in {"migrate", "tests"}:
            return [
                await asyncio.to_thread(
                    read_source,
                    project,
                    self._text(payload, "file_path"),
                    max_file_bytes=self.settings.max_file_bytes,
                )
            ]
        return await asyncio.to_thread(
            collect_project,
            project,
            mode=self._text(payload, "mode", "diff"),
            base_ref=self._text(payload, "base_ref", "HEAD"),
            max_files=self.settings.max_review_files,
            max_file_bytes=self.settings.max_file_bytes,
            max_total_bytes=self.settings.max_review_chars,
        )

    async def _execute(self, job: ReviewJob) -> tuple[dict[str, JSONValue], Sequence[ReviewReport]]:
        payload = self.store.payload(job)
        if job.kind in {"review", "upload"}:
            from .sarif import review_to_sarif

            files = await self._files(job, payload)
            context = self._text(payload, "context")
            review = await self.analyzer.review(files, context=context)
            encoded = review.model_dump_json()
            report = ReviewReport(
                project_name=self._text(payload, "project_name", "Uploaded file"),
                file_path=files[0].path if len(files) == 1 else ".",
                content_type="pdf"
                if job.kind == "upload" and files and files[0].path.endswith(".pdf")
                else "code",
                summary=review.summary,
                review_result=encoded,
                status="completed",
                owner_id=job.owner_id,
                job_id=job.id,
            )
            result = cast("dict[str, JSONValue]", review.model_dump(mode="json"))
            result["sarif"] = cast("dict[str, JSONValue]", review_to_sarif(review))
            return result, [report]
        if job.kind in {"migrate", "tests", "scaffold"}:
            return await self._proposal(job, payload), []
        raise ValueError("Unsupported job kind")

    async def _proposal(
        self, job: ReviewJob, payload: Mapping[str, JSONValue]
    ) -> dict[str, JSONValue]:
        from .patches import save_artifacts, save_patch_proposal
        from .sandbox import validate_proposal
        from .utils import get_project

        kind = cast(
            "Literal['migration', 'tests', 'scaffold']",
            "migration" if job.kind == "migrate" else job.kind,
        )
        files = [] if job.kind == "scaffold" else await self._files(job, payload)
        expected_hashes = {
            source.path: hashlib.sha256(source.content.encode("utf-8")).hexdigest()
            for source in files
        }
        instructions = self._text(payload, "instructions")
        proposal = await self.analyzer.generate_proposal(
            files, kind=kind, instructions=instructions
        )
        artifacts = {artifact.path: artifact.content for artifact in proposal.artifacts}
        if len(artifacts) != len(proposal.artifacts):
            raise ValueError("Duplicate generated artifacts")
        # The safe artifact writer creates a unique directory for each attempt,
        # preventing an expired worker from overwriting a winning proposal.
        artifact_dir = self.settings.data_dir / "artifacts"
        project = (
            await asyncio.to_thread(
                get_project, self.settings.projects_root, self._text(payload, "project_name")
            )
            if job.kind != "scaffold"
            else None
        )
        if proposal.diff and project is not None:
            writing = asyncio.create_task(
                asyncio.to_thread(
                    save_patch_proposal,
                    project,
                    proposal.diff,
                    artifact_dir,
                    expected_hashes=expected_hashes,
                )
            )
        else:
            writing = asyncio.create_task(
                asyncio.to_thread(
                    save_artifacts,
                    artifacts,
                    artifact_dir,
                    project=project,
                    expected_hashes=expected_hashes if project is not None else None,
                )
            )
        try:
            # Cancellation cannot stop a thread halfway through writing. Join
            # this bounded write before removing its otherwise orphaned output.
            saved = await asyncio.shield(writing)
        except BaseException:
            outcome = await asyncio.gather(writing, return_exceptions=True)
            if not isinstance(outcome[0], BaseException):
                await self._remove_artifact(outcome[0].artifact_id)
            raise
        try:
            if project is not None:
                validation = await validate_proposal(
                    project,
                    saved,
                    timeout_seconds=min(max(self.settings.job_timeout_seconds, 1), 60),
                )
                validation_data = {"status": validation.status, "output": validation.output}
            else:
                validation_data = {"status": "unavailable", "output": "No project patch to execute"}
        except BaseException:
            await self._remove_artifact(saved.artifact_id)
            raise
        return cast(
            "dict[str, JSONValue]",
            {
                "kind": proposal.kind,
                "summary": proposal.summary,
                "artifact_id": saved.artifact_id,
                "files": list(saved.files),
                "verified_syntax": saved.verified_syntax,
                "validation": validation_data,
                "model": proposal.model,
                "tokens_used": proposal.tokens_used,
                "estimated_cost_usd": proposal.estimated_cost_usd,
                "source_hashes": dict(saved.source_hashes),
            },
        )

    async def _remove_artifact(self, artifact_id: str) -> None:
        from .patches import remove_artifact

        try:
            await asyncio.to_thread(
                remove_artifact, self.settings.data_dir / "artifacts", artifact_id
            )
        except OSError:
            logger.warning("Could not remove an abandoned proposal artifact")


async def run_worker(settings: Settings) -> None:
    """CLI entry point: signals cancel work and release clients and sessions."""
    database = Database(settings)
    try:
        await database.initialize()
        async with Worker(database, settings) as worker:
            loop = asyncio.get_running_loop()
            signals = (signal.SIGINT, signal.SIGTERM)
            for signum in signals:
                loop.add_signal_handler(signum, worker.stop)
            try:
                await worker.run_forever()
            finally:
                for signum in signals:
                    loop.remove_signal_handler(signum)
    finally:
        await database.close()
