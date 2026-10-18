"""Validate model proposals and save review artifacts without changing sources."""

from __future__ import annotations

import ast
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .utils import (
    DEFAULT_MAX_FILE_BYTES,
    DEFAULT_MAX_TOTAL_BYTES,
    ProjectLimitError,
    ProjectPathError,
    ProjectReadError,
    directory_fd,
    read_source,
    relative_parts,
)


class PatchValidationError(ValueError):
    """The proposal is malformed, stale, unsafe or syntactically invalid."""


@dataclass(frozen=True, slots=True)
class PatchProposal:
    artifact_id: str
    patch_path: Path | None
    files: tuple[str, ...]
    verified_syntax: bool
    artifact_dir: Path
    deleted_files: tuple[str, ...]
    source_hashes: dict[str, str]


def _directory(path: Path) -> None:
    """Create private directories without following preexisting symlinks."""
    absolute = path.absolute()
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in absolute.parts[1:]:
            if part in {".", ".."}:
                raise ProjectPathError("Artifact directory traversal is forbidden")
            try:
                os.mkdir(part, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
    finally:
        os.close(fd)


def _write_private(root: Path, path: str, content: str) -> None:
    parts = relative_parts(path)
    with directory_fd(root) as root_fd:
        fd = os.dup(root_fd)
        try:
            for part in parts[:-1]:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            target = os.open(
                parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd
            )
            with os.fdopen(target, "w", encoding="utf-8", newline="") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(fd)


def _check_artifacts(artifacts: Mapping[str, str]) -> None:
    if not artifacts or len(artifacts) > 100:
        raise PatchValidationError("A proposal must contain between 1 and 100 files")
    total = 0
    paths = set(artifacts)
    for path, content in artifacts.items():
        parts = relative_parts(path)
        if any("/".join(parts[:index]) in paths for index in range(1, len(parts))):
            raise PatchValidationError("A proposed file is also used as a parent directory")
        size = len(content.encode("utf-8"))
        total += size
        if size > DEFAULT_MAX_FILE_BYTES or total > DEFAULT_MAX_TOTAL_BYTES:
            raise ProjectLimitError("Proposal exceeds its file or total byte limit")
        if path.endswith(".py"):
            try:
                ast.parse(content, filename=path)
                compile(content, path, "exec", dont_inherit=True)
            except (SyntaxError, ValueError, RecursionError) as exc:
                raise PatchValidationError(f"Invalid Python syntax in {path}") from exc


def _save(
    artifacts: Mapping[str, str],
    artifact_root: Path,
    *,
    patch: str | None = None,
    deleted_files: tuple[str, ...] = (),
    source_hashes: dict[str, str] | None = None,
    project: Path | None = None,
    expected_hashes: Mapping[str, str] | None = None,
) -> PatchProposal:
    _check_artifacts(artifacts)
    if expected_hashes is not None:
        if project is None:
            raise PatchValidationError("Reviewed source hashes require a project")
        verify_source_hashes(project, expected_hashes)
    if project is not None:
        for path, expected in (source_hashes or {}).items():
            if not expected:
                _validate_new_path(project, path)
    _directory(artifact_root)
    identifier = uuid4().hex
    with directory_fd(artifact_root) as fd:
        os.mkdir(identifier, 0o700, dir_fd=fd)
    target = artifact_root.absolute() / identifier
    try:
        for path, content in artifacts.items():
            _write_private(target, f"files/{path}", content)
        if patch is not None:
            _write_private(target, "proposal.patch", patch)
        python_files = [path for path in artifacts if path.endswith(".py")]
        manifest = {
            "artifact_id": identifier,
            "files": sorted(artifacts),
            "deleted_files": list(deleted_files),
            "verified_syntax": bool(python_files),
            "source_hashes": source_hashes or {},
        }
        # Manifest is written last. Incomplete proposals are never advertised.
        _write_private(target, "manifest.json", json.dumps(manifest, ensure_ascii=False))
    except BaseException:
        try:
            remove_artifact(artifact_root, identifier)
        except OSError, ValueError:
            pass
        raise
    return PatchProposal(
        identifier,
        target / "proposal.patch" if patch is not None else None,
        tuple(sorted(artifacts)),
        bool(python_files),
        target,
        deleted_files,
        source_hashes or {},
    )


def verify_source_hashes(project: Path, expected_hashes: Mapping[str, str]) -> dict[str, str]:
    """Verify the entire reviewed input, including files not modified by a patch."""
    snapshot = dict(expected_hashes)
    if len(snapshot) > 1000:
        raise PatchValidationError("Reviewed source snapshot exceeds its file limit")
    total = 0
    for path, expected in snapshot.items():
        relative_parts(path)
        if not re.fullmatch(r"[a-f0-9]{64}", expected):
            raise PatchValidationError("Reviewed sources require SHA-256 content hashes")
        try:
            source = read_source(project, path)
        except ProjectPathError, ProjectReadError:
            raise PatchValidationError(
                "Reviewed source is unavailable; generate the proposal again"
            ) from None
        if source.sha256 != expected:
            raise PatchValidationError("Reviewed source changed; generate the proposal again")
        total += len(source.content.encode("utf-8"))
        if total > DEFAULT_MAX_TOTAL_BYTES:
            raise ProjectLimitError("Reviewed source snapshot exceeds its total byte limit")
    return snapshot


def save_artifacts(
    artifacts: Mapping[str, str],
    artifact_dir: Path,
    *,
    project: Path | None = None,
    expected_hashes: Mapping[str, str] | None = None,
) -> PatchProposal:
    hashes: dict[str, str] = {}
    reviewed: dict[str, str] | None = None
    if expected_hashes is not None:
        if project is None:
            raise PatchValidationError("Reviewed source hashes require a project")
        reviewed = verify_source_hashes(project, expected_hashes)
        hashes = dict(reviewed)
        for path in artifacts:
            if path not in hashes:
                _validate_new_path(project, path)
                hashes[path] = ""
    return _save(
        artifacts,
        artifact_dir,
        source_hashes=hashes,
        project=project,
        expected_hashes=reviewed,
    )


def _header(line: str, marker: str) -> str | None:
    value = line[len(marker) :].rstrip("\r\n").split("\t", 1)[0]
    if value == "/dev/null":
        return None
    if value.startswith(("a/", "b/")):
        value = value[2:]
    relative_parts(value)
    return value


def save_patch_proposal(
    project: Path,
    patch: str,
    artifact_dir: Path,
    *,
    expected_hashes: Mapping[str, str] | None = None,
) -> PatchProposal:
    """Apply a restricted unified diff in memory; reject stale context and renames."""
    if not patch or len(patch.encode("utf-8")) > 1024 * 1024:
        raise PatchValidationError("A nonempty patch of at most 1 MiB is required")
    if "\x00" in patch:
        raise PatchValidationError("Binary patches are unsupported")
    reviewed = (
        verify_source_hashes(project, expected_hashes) if expected_hashes is not None else None
    )
    normalized: list[str] = []
    for line in patch.splitlines(keepends=True):
        if line.rstrip("\r\n") == "\\ No newline at end of file":
            if not normalized or normalized[-1][:1] not in {" ", "+", "-"}:
                raise PatchValidationError("Misplaced no-newline marker")
            normalized[-1] = normalized[-1].removesuffix("\n").removesuffix("\r")
        else:
            normalized.append(line)
    index = 0
    artifacts: dict[str, str] = {}
    hashes: dict[str, str] = dict(reviewed or {})
    deleted: list[str] = []
    seen: set[str] = set()
    while index < len(normalized):
        line = normalized[index]
        if not line.startswith("--- "):
            if (
                line.startswith(
                    ("diff --git ", "index ", "new file mode 100644", "deleted file mode 100644")
                )
                or not line.strip()
            ):
                index += 1
                continue
            raise PatchValidationError("Unsupported patch metadata or missing file header")
        old_path = _header(line, "--- ")
        index += 1
        if index >= len(normalized) or not normalized[index].startswith("+++ "):
            raise PatchValidationError("Missing new-file header")
        new_path = _header(normalized[index], "+++ ")
        index += 1
        path = new_path or old_path
        if path is None or (old_path and new_path and old_path != new_path):
            raise PatchValidationError("Renames and empty file paths are unsupported")
        if path in seen or len(seen) >= 100:
            raise PatchValidationError("Duplicate file or too many patched files")
        seen.add(path)
        if old_path is None:
            # Refuse an addition over any existing object, including symlinks.
            try:
                read_source(project, path)
            except ProjectPathError:
                # Parent symlinks must still be rejected even for new files.
                _validate_new_path(project, path)
            else:
                raise PatchValidationError("A new file already exists")
            original: list[str] = []
            hashes[path] = ""
        else:
            if reviewed is not None and path not in reviewed:
                raise PatchValidationError("Patch modifies a file outside the reviewed snapshot")
            source = read_source(project, path)
            if reviewed is not None and source.sha256 != reviewed[path]:
                raise PatchValidationError("Reviewed source changed while parsing the patch")
            original = source.content.splitlines(keepends=True)
            hashes[path] = source.sha256
        result: list[str] = []
        cursor = 0
        hunk_count = 0
        while index < len(normalized) and normalized[index].startswith("@@ "):
            match = re.fullmatch(
                r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[^\r\n]*\r?\n?", normalized[index]
            )
            if match is None:
                raise PatchValidationError("Invalid unified-diff hunk")
            old_start, old_count, new_start, new_count = (
                int(match[1]),
                int(match[2] or "1"),
                int(match[3]),
                int(match[4] or "1"),
            )
            target = old_start if old_count == 0 else old_start - 1
            if target < cursor or target > len(original):
                raise PatchValidationError("Overlapping or out-of-range hunk")
            result.extend(original[cursor:target])
            cursor = target
            expected_new_start = len(result) if new_count == 0 else len(result) + 1
            if new_start != expected_new_start:
                raise PatchValidationError("Incorrect new-file hunk position")
            index += 1
            consumed = produced = 0
            while index < len(normalized) and (consumed < old_count or produced < new_count):
                item = normalized[index]
                marker = item[:1]
                if marker not in {" ", "-", "+"}:
                    raise PatchValidationError("Hunk ended before its declared line count")
                text = item[1:]
                if marker in {" ", "-"}:
                    if cursor >= len(original) or original[cursor] != text:
                        raise PatchValidationError("Patch context does not match current source")
                    cursor += 1
                    consumed += 1
                if marker in {" ", "+"}:
                    result.append(text)
                    produced += 1
                if consumed > old_count or produced > new_count:
                    raise PatchValidationError("Hunk exceeds its declared line count")
                index += 1
            if consumed != old_count or produced != new_count:
                raise PatchValidationError("Truncated patch hunk")
            hunk_count += 1
        if not hunk_count:
            raise PatchValidationError("Each patched file needs at least one hunk")
        result.extend(original[cursor:])
        content = "".join(result)
        if new_path is None:
            if content:
                raise PatchValidationError("A file deletion must remove every line")
            deleted.append(path)
        else:
            artifacts[path] = content
    if not artifacts:
        raise PatchValidationError("Deletion-only proposals are unsupported")
    return _save(
        artifacts,
        artifact_dir,
        patch=patch,
        deleted_files=tuple(deleted),
        source_hashes=hashes,
        project=project,
        expected_hashes=reviewed,
    )


def _validate_new_path(project: Path, path: str) -> None:
    """Check existing parents and final object without following links."""
    parts = relative_parts(path)
    with directory_fd(project) as root_fd:
        fd = os.dup(root_fd)
        try:
            for part in parts[:-1]:
                try:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                except FileNotFoundError:
                    return
                except OSError as exc:
                    raise ProjectPathError("New-file parent contains a symlink") from exc
                os.close(fd)
                fd = child
            try:
                os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                return
            raise PatchValidationError("New-file target already exists")
        finally:
            os.close(fd)


def read_artifacts(artifact_root: Path, artifact_id: str) -> dict[str, str]:
    if not re.fullmatch(r"[a-f0-9]{32}", artifact_id):
        raise ProjectPathError("Invalid artifact identifier")
    root = artifact_root / artifact_id
    try:
        manifest: object = json.loads(read_source(root, "manifest.json").content)
    except (json.JSONDecodeError, ProjectReadError) as exc:
        raise PatchValidationError("Artifact manifest is invalid") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
        raise PatchValidationError("Artifact manifest is invalid")
    paths = manifest["files"]
    if len(paths) > 100 or any(not isinstance(path, str) for path in paths):
        raise PatchValidationError("Artifact manifest exceeds its limits")
    files: dict[str, str] = {}
    total = 0
    for path in paths:
        relative_parts(path)
        content = read_source(root, f"files/{path}").content
        total += len(content.encode("utf-8"))
        if total > DEFAULT_MAX_TOTAL_BYTES:
            raise ProjectLimitError("Artifacts exceed the total byte limit")
        files[path] = content
    # The patch is optional; files are the authoritative syntax-checked result.
    try:
        patch = read_source(root, "proposal.patch", 1024 * 1024).content
    except ProjectPathError:
        patch = None
    if patch is not None:
        files["proposal.patch"] = patch
    return files


def remove_artifact(artifact_root: Path, artifact_id: str) -> None:
    """Remove one private artifact tree without following any symlinks."""
    if not re.fullmatch(r"[a-f0-9]{32}", artifact_id):
        raise ProjectPathError("Invalid artifact identifier")

    def remove_children(fd: int) -> None:
        with os.scandir(fd) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    child = os.open(
                        entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                    )
                    try:
                        remove_children(child)
                    finally:
                        os.close(child)
                    os.rmdir(entry.name, dir_fd=fd)
                else:
                    os.unlink(entry.name, dir_fd=fd)

    with directory_fd(artifact_root) as root_fd:
        try:
            fd = os.open(artifact_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        except FileNotFoundError:
            return
        try:
            remove_children(fd)
        finally:
            os.close(fd)
        os.rmdir(artifact_id, dir_fd=root_fd)
