import asyncio
import os
from pathlib import Path

import pytest

from app import sandbox
from app.patches import save_artifacts


async def test_no_docker_never_executes_generated_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposal = save_artifacts(
        {"danger.py": "raise RuntimeError('must never execute')\n"}, tmp_path / "artifacts"
    )
    monkeypatch.setattr(sandbox.shutil, "which", lambda _: None)
    result = await sandbox.validate_proposal(tmp_path, proposal)
    assert result.status == "unavailable"
    assert "no generated code was executed" in result.output


class FakeProcess:
    def __init__(self, output: bytes, returncode: int = 0) -> None:
        self.returncode: int | None = returncode
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(output)
        self.stdout.feed_eof()

    async def wait(self) -> int:
        return self.returncode or 0

    def kill(self) -> None:
        self.returncode = -9


async def test_docker_enforces_isolation_and_does_not_mount_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("x=1\n")
    (project / ".env").write_text("SECRET=do-not-copy\n")
    proposal = save_artifacts(
        {
            "test_main.py": "import unittest\nclass T(unittest.TestCase):\n def test_value(self): self.assertEqual(1, 1)\n"
        },
        tmp_path / "artifacts",
    )
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(sandbox.shutil, "which", lambda _: "/usr/bin/docker")

    async def fake_exec(*args: str, **kwargs: object) -> FakeProcess:
        del kwargs
        calls.append(args)
        if args[1] == "run":
            mount = args[args.index("--mount") + 1]
            snapshot = Path(mount.split("src=", 1)[1].split(",", 1)[0])
            assert (snapshot / "main.py").is_file()
            assert (snapshot / "test_main.py").is_file()
            assert not (snapshot / ".env").exists()
            return FakeProcess(b"Ran 1 test in 0.01s\nOK\n")
        return FakeProcess(b"")

    monkeypatch.setattr(sandbox.asyncio, "create_subprocess_exec", fake_exec)
    result = await sandbox.validate_proposal(project, proposal)
    assert result.status == "passed"
    run = calls[0]
    for required in [
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit=64",
        "--memory=256m",
        "--cpus=1",
        "--user=65534:65534",
        "--pull=never",
    ]:
        assert required in run
    assert any(call[1:3] == ("rm", "--force") for call in calls)


@pytest.mark.parametrize(
    "output", [b"Ran 0 tests\nOK\n", b"no tests ran\n", b"generated code exited without testing\n"]
)
async def test_zero_tests_are_never_reported_as_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: bytes
) -> None:
    (tmp_path / "test_x.py").write_text("# no tests\n")
    proposal = save_artifacts({"test_x.py": "# still no tests\n"}, tmp_path / "artifacts")
    monkeypatch.setattr(sandbox.shutil, "which", lambda _: "/usr/bin/docker")

    async def fake_exec(*args: str, **kwargs: object) -> FakeProcess:
        del kwargs
        return FakeProcess(output if args[1] == "run" else b"")

    monkeypatch.setattr(sandbox.asyncio, "create_subprocess_exec", fake_exec)
    result = await sandbox.validate_proposal(tmp_path, proposal)
    assert result.status == "unavailable"


async def test_stdout_is_bounded() -> None:
    stream = asyncio.StreamReader()
    stream.feed_data(b"x" * 200000)
    stream.feed_eof()
    assert len(await sandbox._drain(stream)) == 65536


@pytest.mark.skipif(
    os.environ.get("RUN_DOCKER_TESTS") != "1", reason="Explicit local Docker integration check"
)
async def test_real_docker_readonly_networkless_unprivileged_and_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("VALUE = 1\n")
    (project / ".env").write_text("EXAMPLE_SECRET=must-not-be-mounted\n")
    monkeypatch.setenv("AICR_TEST_SENTINEL", "must-not-be-inherited")
    tests = """import os
import socket
import unittest
from main import VALUE
class IsolationTest(unittest.TestCase):
    def test_source_overlay(self):
        self.assertEqual(VALUE, 2)
    def test_unprivileged_and_no_host_secrets(self):
        self.assertEqual(os.getuid(), 65534)
        self.assertNotIn('AICR_TEST_SENTINEL', os.environ)
        self.assertFalse(os.path.exists('/code/.env'))
    def test_readonly_source(self):
        with self.assertRaises(OSError):
            open('/code/changed.py', 'w')
    def test_network_disabled(self):
        with socket.socket() as connection:
            connection.settimeout(0.2)
            with self.assertRaises(OSError):
                connection.connect(('192.0.2.1', 443))
"""
    proposal = save_artifacts(
        {"main.py": "VALUE = 2\n", "test_isolation.py": tests}, tmp_path / "artifacts"
    )
    result = await sandbox.validate_proposal(project, proposal)
    assert result.status == "passed", result.output
    assert "Ran 4 tests" in result.output
    assert (project / "main.py").read_text() == "VALUE = 1\n"
    infinite = save_artifacts(
        {"test_infinite.py": "while True:\n    pass\n"}, tmp_path / "artifacts"
    )
    timed_out = await sandbox.validate_proposal(project, infinite, timeout_seconds=1)
    assert timed_out.status == "failed"
    assert "wall-clock" in timed_out.output
