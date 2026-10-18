"""Bounded, descriptor-relative project access. Call via asyncio.to_thread.

Project contents are never modified. Symlinks and hard links are unsupported.
"""

from __future__ import annotations

import hashlib
import os
import re
import selectors
import stat
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from .schemas import SourceFile

DEFAULT_MAX_FILE_BYTES = 256 * 1024
DEFAULT_MAX_TOTAL_BYTES = 2 * 1024 * 1024
EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        "ENV",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        "build",
        "dist",
        "site-packages",
        "generated",
        ".idea",
        ".vscode",
        ".aws",
        ".ssh",
        ".kube",
        ".azure",
    }
)
_OPEN_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_OPEN_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


class ProjectPathError(ValueError):
    """A path is outside its allowed root or contains a symlink."""


class ProjectReadError(ValueError):
    """A selected file cannot be safely decoded or read."""


class ProjectLimitError(ValueError):
    """A selection exceeds a resource limit; code is never truncated."""


def relative_parts(value: str) -> tuple[str, ...]:
    """Validate platform-independent relative paths before filesystem use."""
    if not value or "\\" in value or ":" in value or any(ord(c) < 32 for c in value):
        raise ProjectPathError("A nonempty relative POSIX path is required")
    path = PurePosixPath(value)
    parts = tuple(value.split("/"))
    if len(value.encode("utf-8")) > 1024 or any(len(part.encode("utf-8")) > 255 for part in parts):
        raise ProjectPathError("Path exceeds filesystem name limits")
    if path.is_absolute() or any(part in {"", ".", ".."} for part in parts):
        raise ProjectPathError("Absolute paths and traversal are forbidden")
    if any(part in EXCLUDED_DIRS for part in parts):
        raise ProjectPathError("Generated, environment and metadata paths are excluded")
    if any(part == ".env" or part.startswith(".env.") for part in parts):
        raise ProjectPathError("Environment secrets are excluded")
    return parts


@contextmanager
def directory_fd(path: Path) -> Iterator[int]:
    """Open every absolute component with NOFOLLOW, retaining the final anchor."""
    absolute = path.absolute()
    fd = os.open("/", _OPEN_DIRECTORY)
    try:
        for part in absolute.parts[1:]:
            if part in {".", ".."}:
                raise ProjectPathError("Directory traversal is forbidden")
            try:
                next_fd = os.open(part, _OPEN_DIRECTORY, dir_fd=fd)
            except OSError as exc:
                raise ProjectPathError("Directory is unavailable or contains a symlink") from exc
            os.close(fd)
            fd = next_fd
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _parent_fd(project: Path, parts: tuple[str, ...]) -> Iterator[tuple[int, str]]:
    with directory_fd(project) as project_fd:
        fd = os.dup(project_fd)
        try:
            for part in parts[:-1]:
                try:
                    next_fd = os.open(part, _OPEN_DIRECTORY, dir_fd=fd)
                except OSError as exc:
                    raise ProjectPathError("A parent is unavailable or a symlink") from exc
                os.close(fd)
                fd = next_fd
            yield fd, parts[-1]
        finally:
            os.close(fd)


def get_project(root: Path, project_name: str) -> Path:
    parts = relative_parts(project_name)
    if len(parts) != 1:
        raise ProjectPathError("Select one project immediately inside PROJECTS_ROOT")
    project = root.absolute() / parts[0]
    with directory_fd(project):
        return project


def _read_bytes(project: Path, path: str, limit: int) -> bytes:
    if limit < 1:
        raise ProjectLimitError("The file size limit must be positive")
    parts = relative_parts(path)
    with _parent_fd(project, parts) as (parent, name):
        try:
            fd = os.open(name, _OPEN_FILE, dir_fd=parent)
        except OSError as exc:
            raise ProjectPathError("File is unavailable or is a symlink") from exc
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise ProjectPathError("Only regular files can be read")
            if metadata.st_nlink != 1:
                raise ProjectPathError("Hard-linked files are excluded")
            if metadata.st_size > limit:
                raise ProjectLimitError(f"File exceeds {limit} bytes: {path}")
            chunks: list[bytes] = []
            remaining = limit + 1
            while remaining:
                chunk = os.read(fd, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            if len(data) > limit:
                raise ProjectLimitError(f"File exceeds {limit} bytes: {path}")
            final = os.fstat(fd)
            if (metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns) != (
                final.st_size,
                final.st_mtime_ns,
                final.st_ctime_ns,
            ):
                raise ProjectReadError("File changed during reading; retry the review")
            return data
        finally:
            os.close(fd)


def read_source(
    project: Path, path: str, max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
) -> SourceFile:
    data = _read_bytes(project, path, max_file_bytes)
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProjectReadError(f"File is not valid UTF-8: {path}") from exc
    if "\x00" in content:
        raise ProjectReadError("Binary files are unsupported")
    return SourceFile(path=path, content=content, sha256=hashlib.sha256(data).hexdigest())


def _git(
    project: Path,
    args: list[str],
    *,
    output_limit: int = 4 * 1024 * 1024,
    timeout_seconds: float = 15,
) -> bytes:
    """Run built-in Git operations with bounded output, duration and no hooks."""
    env = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_LAZY_FETCH": "1",
        }
    )
    with directory_fd(project) as fd:
        try:
            process = subprocess.Popen(
                [
                    "git",
                    "--no-optional-locks",
                    "-c",
                    "core.fsmonitor=false",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    f"safe.directory={project.absolute()}",
                    "-c",
                    f"core.worktree={project.absolute()}",
                    "-c",
                    "core.bare=false",
                    "-c",
                    "protocol.allow=never",
                    *args,
                ],
                cwd=f"/proc/self/fd/{fd}",
                pass_fds=(fd,),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
        except OSError as exc:
            raise ProjectReadError("Git is unavailable") from exc
        assert process.stdout is not None
        output = bytearray()
        deadline = time.monotonic() + timeout_seconds
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    if time.monotonic() >= deadline:
                        raise ProjectLimitError("Git operation exceeded its time limit")
                    for key, _ in selector.select(timeout=0.1):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        output.extend(chunk)
                        if len(output) > output_limit:
                            raise ProjectLimitError("Git selection exceeds the output limit")
            code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
            if code:
                raise ProjectReadError("Git command failed; check repository and base_ref")
            return bytes(output)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()


def _walk_python(project: Path, max_entries: int = 20000) -> list[str]:
    paths: list[str] = []
    visited = 0
    with directory_fd(project) as initial_fd:

        def walk(fd: int, prefix: str, depth: int) -> None:
            nonlocal visited
            if depth > 40:
                raise ProjectLimitError("Project nesting exceeds 40 directories")
            with os.scandir(fd) as entries:
                for entry in entries:
                    visited += 1
                    if visited > max_entries:
                        raise ProjectLimitError("Project traversal exceeds the entry limit")
                    name = entry.name
                    if name in EXCLUDED_DIRS or name == ".env" or name.startswith(".env."):
                        continue
                    relative = f"{prefix}/{name}" if prefix else name
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        try:
                            child_fd = os.open(name, _OPEN_DIRECTORY, dir_fd=fd)
                        except OSError as exc:
                            raise ProjectPathError("Project changed during traversal") from exc
                        try:
                            walk(child_fd, relative, depth + 1)
                        finally:
                            os.close(child_fd)
                    elif name.endswith(".py") and entry.is_file(follow_symlinks=False):
                        relative_parts(relative)
                        paths.append(relative)

        walk(initial_fd, "", 0)
    return sorted(paths)


def _git_paths(raw: bytes) -> list[str]:
    try:
        paths = [item.decode("utf-8") for item in raw.split(b"\0") if item]
    except UnicodeDecodeError as exc:
        raise ProjectReadError("Git filenames must be valid UTF-8") from exc
    valid: set[str] = set()
    for path in paths:
        try:
            relative_parts(path)
        except ProjectPathError:
            continue
        if path.endswith(".py"):
            valid.add(path)
    return sorted(valid)


def collect_project(
    project: Path,
    mode: str = "diff",
    base_ref: str = "HEAD",
    max_files: int = 100,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
) -> list[SourceFile]:
    """Collect complete sources respecting ignores and explicit resource bounds.

    Diff compares tracked worktree files with a resolved commit and includes
    untracked, non-ignored Python files. Deleted files have no current source.
    Full supports non-Git directories with a bounded conservative traversal.
    """
    if mode not in {"diff", "full"}:
        raise ProjectPathError("Review mode must be diff or full")
    if not 1 <= max_files <= 1000 or max_total_bytes < 1:
        raise ProjectLimitError("Invalid file count or total byte limit")
    deadline = time.monotonic() + 30

    def git(args: list[str], *, output_limit: int = 4 * 1024 * 1024) -> bytes:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProjectLimitError("Project collection exceeded 30 seconds")
        return _git(project, args, output_limit=output_limit, timeout_seconds=min(15, remaining))

    base_blobs: dict[str, str] = {}
    with directory_fd(project) as fd:
        try:
            git_metadata = os.stat(".git", dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            git_metadata = None
        if git_metadata is not None and not stat.S_ISDIR(git_metadata.st_mode):
            raise ProjectPathError("Git metadata must be an ordinary directory")
    if git_metadata is None:
        if mode == "diff":
            raise ProjectReadError("Diff review requires Git; use full mode")
        paths = _walk_python(project)
    elif mode == "full":
        selected = _git_paths(
            git(["ls-files", "--cached", "--others", "--exclude-standard", "-z", "--"])
        )
        removed = set(_git_paths(git(["ls-files", "--deleted", "-z", "--"])))
        paths = [path for path in selected if path not in removed]
    else:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/@{}~^+-]{0,199}", base_ref):
            raise ProjectPathError("Invalid Git base reference")
        revision = (
            git(
                ["rev-parse", "--verify", "--end-of-options", f"{base_ref}^{{commit}}"],
                output_limit=4096,
            )
            .decode("ascii")
            .strip()
        )
        if not re.fullmatch(r"[a-f0-9]{40,64}", revision):
            raise ProjectReadError("Git did not resolve a commit")
        # Never run `git diff/status` against an untrusted worktree: even
        # --no-ext-diff/--no-textconv can execute .gitattributes clean filters.
        tree = git(["ls-tree", "-r", "-z", revision, "--"])
        for entry in tree.split(b"\0"):
            if not entry:
                continue
            metadata, separator, raw_path = entry.partition(b"\t")
            fields = metadata.split(b" ")
            if not separator or len(fields) != 3:
                raise ProjectReadError("Malformed Git tree metadata")
            mode_value, object_type, object_id = fields
            if object_type != b"blob" or mode_value not in {b"100644", b"100755"}:
                continue
            candidate = _git_paths(raw_path + b"\0")
            if candidate:
                base_blobs[candidate[0]] = object_id.decode("ascii")
        selected = _git_paths(
            git(["ls-files", "--cached", "--others", "--exclude-standard", "-z", "--"])
        )
        removed = set(_git_paths(git(["ls-files", "--deleted", "-z", "--"])))
        paths = [path for path in selected if path not in removed]
        if len(paths) > 20000:
            raise ProjectLimitError("Git project exceeds 20000 source candidates")
    if mode == "full" and len(paths) > max_files:
        raise ProjectLimitError(f"Selection exceeds {max_files} files; narrow the diff")
    sources: list[SourceFile] = []
    total = 0
    changed_line_count = 0
    for path in paths:
        if time.monotonic() >= deadline:
            raise ProjectLimitError("Project collection exceeded 30 seconds")
        source = read_source(project, path, max_file_bytes)
        if mode == "diff":
            expected = base_blobs.get(path)
            raw = source.content.encode("utf-8")
            object_digest = (
                hashlib.sha256()
                if expected is not None and len(expected) == 64
                else hashlib.sha1(usedforsecurity=False)
            )
            object_digest.update(f"blob {len(raw)}\0".encode("ascii"))
            object_digest.update(raw)
            if expected == object_digest.hexdigest():
                continue
            if len(sources) >= max_files:
                raise ProjectLimitError(f"Selection exceeds {max_files} changed files")
        total += len(source.content.encode("utf-8"))
        if total > max_total_bytes:
            raise ProjectLimitError(f"Selection exceeds {max_total_bytes} bytes")
        if mode == "diff":
            old_lines: list[str] = []
            if expected is not None:
                if not re.fullmatch(r"[a-f0-9]{40,64}", expected):
                    raise ProjectReadError("Malformed Git blob identifier")
                old_bytes = git(["cat-file", "blob", expected], output_limit=max_file_bytes)
                try:
                    old_lines = old_bytes.decode("utf-8").splitlines()
                except UnicodeDecodeError as exc:
                    raise ProjectReadError("Git base source is not UTF-8") from exc
            new_lines = source.content.splitlines()
            # A linear changed span avoids SequenceMatcher's adversarial O(n²)
            # behavior. It conservatively includes intervening unchanged lines.
            prefix = 0
            while (
                prefix < min(len(old_lines), len(new_lines))
                and old_lines[prefix] == new_lines[prefix]
            ):
                prefix += 1
            suffix = 0
            while (
                suffix < min(len(old_lines), len(new_lines)) - prefix
                and old_lines[-1 - suffix] == new_lines[-1 - suffix]
            ):
                suffix += 1
            start, end = prefix + 1, len(new_lines) - suffix
            if end < start and new_lines:
                start = end = min(max(1, prefix), len(new_lines))
            count = max(0, end - start + 1)
            changed_line_count += count
            if changed_line_count > 20000:
                raise ProjectLimitError("Diff exceeds 20000 affected lines; narrow the selection")
            source = source.model_copy(update={"changed_lines": tuple(range(start, end + 1))})
        sources.append(source)
    return sources


def read_upload(
    data_dir: Path,
    upload_id: str,
    filename: str,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_pages: int = 40,
) -> SourceFile:
    """Read an opaque upload; the supplied filename is metadata only."""
    if not re.fullmatch(r"[a-f0-9]{32}", upload_id):
        raise ProjectPathError("Invalid upload identifier")
    raw = _read_bytes(data_dir, f"uploads/{upload_id}", max_file_bytes)
    if filename.lower().endswith(".pdf"):
        from .pdf_extract import extract_pdf

        content = extract_pdf(raw, max_pages=max_pages, max_text_bytes=max_file_bytes)
    else:
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProjectReadError("Upload must contain valid UTF-8 source text") from exc
    if "\x00" in content:
        raise ProjectReadError("Binary uploads are unsupported")
    display = filename.replace("\\", "/").rsplit("/", 1)[-1]
    try:
        relative_parts(display)
    except ProjectPathError:
        display = "uploaded_source.py"
    return SourceFile(
        path=display, content=content, sha256=hashlib.sha256(content.encode()).hexdigest()
    )
