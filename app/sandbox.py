"""Optional generated-code validation, exclusively in a constrained Docker container.

Docker is a prerequisite, never a reason to execute generated code on the host.
Only bounded Python sources are copied; credentials/configuration are not mounted.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4

from .patches import PatchProposal, PatchValidationError, _validate_new_path
from .utils import collect_project, read_source, relative_parts


@dataclass(frozen=True, slots=True)
class SandboxResult:
    status: Literal["passed", "failed", "unavailable"]
    output: str


def _snapshot(project: Path, proposal: PatchProposal, target: Path) -> bool:
    sources = collect_project(project, mode="full", max_files=200)
    contents = {source.path: source.content for source in sources}
    hashes = {source.path: source.sha256 for source in sources}
    for path, expected in proposal.source_hashes.items():
        if not expected:
            _validate_new_path(project, path)
            continue
        actual = read_source(project, path)
        if actual.sha256 != expected or (path in hashes and hashes[path] != expected):
            raise PatchValidationError("Sources changed after the proposal; generate it again")
        if path.endswith(".py") and path not in contents:
            contents[path] = actual.content
    for path in proposal.deleted_files:
        contents.pop(path, None)
    for path in proposal.files:
        if path.endswith(".py"):
            contents[path] = read_source(proposal.artifact_dir, f"files/{path}").content
    total = sum(len(content.encode("utf-8")) for content in contents.values())
    if len(contents) > 200 or total > 2 * 1024 * 1024:
        raise PatchValidationError("Sandbox snapshot exceeds its resource limits")
    has_tests = False
    for path, content in contents.items():
        relative_parts(path)
        name = Path(path).name
        has_tests |= name.startswith("test") or name.endswith("_test.py")
        destination = target / path
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        destination.write_text(content, encoding="utf-8", newline="")
        destination.chmod(0o444)
    # Container's unprivileged UID must be able to traverse the read-only bind.
    for folder, dirs, _ in os.walk(target):
        Path(folder).chmod(0o755)
        for name in dirs:
            (Path(folder) / name).chmod(0o755)
    return has_tests


async def _drain(stream: asyncio.StreamReader, limit: int = 65536) -> bytes:
    output = bytearray()
    while chunk := await stream.read(8192):
        if len(output) < limit:
            output.extend(chunk[: limit - len(output)])
    return bytes(output)


async def _remove_container(docker: str, name: str) -> None:
    try:
        process = await asyncio.create_subprocess_exec(
            docker,
            "rm",
            "--force",
            name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.wait()
    except OSError:
        return


async def validate_proposal(
    project: Path,
    proposal: PatchProposal,
    timeout_seconds: float = 60,
    *,
    image: str = "python:3.14.8-slim",
    runner: Literal["unittest", "pytest"] = "unittest",
) -> SandboxResult:
    docker = shutil.which("docker")
    if docker is None:
        return SandboxResult("unavailable", "Docker is unavailable; no generated code was executed")
    if not 1 <= timeout_seconds <= 300 or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,199}", image
    ):
        raise ValueError("Invalid sandbox timeout or image")
    if runner not in {"unittest", "pytest"}:
        raise ValueError("Unsupported sandbox test runner")
    with tempfile.TemporaryDirectory(prefix="ai-review-sandbox-") as temporary:
        snapshot = Path(temporary)
        try:
            snapshot_task = asyncio.create_task(
                asyncio.to_thread(_snapshot, project, proposal, snapshot)
            )
            try:
                has_tests = await asyncio.shield(snapshot_task)
            except asyncio.CancelledError:
                # Finish the bounded writer before TemporaryDirectory removes it.
                await snapshot_task
                raise
        except (ValueError, OSError) as exc:
            return SandboxResult("failed", str(exc))
        if not has_tests:
            return SandboxResult(
                "unavailable", "No Python test files are available for isolated validation"
            )
        name = f"ai-review-{uuid4().hex}"
        command = ["python", "-I", "-m", runner]
        command += (
            ["discover", "-s", "/code", "-p", "test*.py"]
            if runner == "unittest"
            else ["-p", "no:cacheprovider", "/code"]
        )
        args = [
            docker,
            "run",
            "--rm",
            "--pull=never",
            "--name",
            name,
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=64",
            "--memory=256m",
            "--memory-swap=256m",
            "--cpus=1",
            "--user=65534:65534",
            "--workdir=/code",
            "--env=PYTHONDONTWRITEBYTECODE=1",
            "--env=PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
            "--tmpfs=/tmp:rw,noexec,nosuid,size=16m",
            "--mount",
            f"type=bind,src={snapshot},dst=/code,readonly",
            image,
            *command,
        ]
        process: asyncio.subprocess.Process | None = None
        drain: asyncio.Task[bytes] | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            assert process.stdout is not None
            drain = asyncio.create_task(_drain(process.stdout))
            try:
                await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
            except TimeoutError:
                return SandboxResult("failed", "Sandbox exceeded its wall-clock time limit")
            output = (await drain).decode("utf-8", errors="replace")
            if process.returncode == 125 or "No module named pytest" in output:
                return SandboxResult("unavailable", output)
            if process.returncode == 0:
                count = (
                    re.search(r"Ran ([1-9][0-9]*) tests?", output)
                    if runner == "unittest"
                    else re.search(r"([1-9][0-9]*) passed", output)
                )
                if count is None:
                    return SandboxResult(
                        "unavailable",
                        "No completed tests were reported by the selected runner\n" + output,
                    )
            return SandboxResult("passed" if process.returncode == 0 else "failed", output)
        except OSError:
            return SandboxResult(
                "unavailable", "Docker could not start; no generated code was executed"
            )
        finally:
            # Killing the CLI alone does not stop a container; always remove it.
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            await asyncio.shield(_remove_container(docker, name))
            if drain is not None and not drain.done():
                drain.cancel()
                await asyncio.gather(drain, return_exceptions=True)
