import hashlib
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.utils import (
    ProjectLimitError,
    ProjectPathError,
    ProjectReadError,
    collect_project,
    get_project,
    read_source,
    read_upload,
)


@pytest.mark.parametrize(
    "name",
    [
        "../outside",
        "/tmp",
        "C:\\Users",
        "C:/Users",
        "foo/../bar",
        "foo/bar",
        "foo\\bar",
        ".",
        "",
        "foo\x00",
    ],
)
def test_project_selection_rejects_untrusted_paths(tmp_path: Path, name: str) -> None:
    with pytest.raises(ProjectPathError):
        get_project(tmp_path, name)


def test_nofollow_and_hardlinks(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    project = root / "demo"
    project.mkdir(parents=True)
    outside = tmp_path / "secret.py"
    outside.write_text("SECRET = 'private'\n")
    (project / "link.py").symlink_to(outside)
    (project / "linked").symlink_to(tmp_path, target_is_directory=True)
    (root / "escape").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ProjectPathError):
        get_project(root, "escape")
    for path in ["link.py", "linked/secret.py", "../secret.py"]:
        with pytest.raises(ProjectPathError):
            read_source(project, path)
    os.link(outside, project / "hard.py")
    with pytest.raises(ProjectPathError):
        read_source(project, "hard.py")


def test_sources_are_complete_and_bounded(tmp_path: Path) -> None:
    content = "answer = '✓'\n"
    (tmp_path / "a.py").write_text(content)
    source = read_source(tmp_path, "a.py")
    assert source.content == content
    assert source.sha256 == hashlib.sha256(content.encode()).hexdigest()
    with pytest.raises(ProjectLimitError):
        read_source(tmp_path, "a.py", 3)
    with pytest.raises(ProjectLimitError):
        collect_project(tmp_path, mode="full", max_total_bytes=3)
    (tmp_path / "binary.py").write_bytes(b"\xff\x00")
    with pytest.raises(ProjectReadError):
        read_source(tmp_path, "binary.py")


def test_non_git_walk_excludes_generated_and_symlinks(tmp_path: Path) -> None:
    (tmp_path / "source.py").write_text("x=1\n")
    for directory in [".git", ".venv", "node_modules", "generated", ".aws"]:
        # No .git here: its existence intentionally selects the Git backend.
        if directory == ".git":
            continue
        (tmp_path / directory).mkdir()
        (tmp_path / directory / "secret.py").write_text("x=2\n")
    (tmp_path / "link.py").symlink_to(tmp_path / "source.py")
    assert [source.path for source in collect_project(tmp_path, mode="full")] == ["source.py"]
    with pytest.raises(ProjectReadError):
        collect_project(tmp_path, mode="diff")


def _git(project: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
        cwd=project,
        check=True,
        capture_output=True,
    )


def test_diff_respects_gitignore_includes_untracked_and_hashes(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    (tmp_path / "changed.py").write_text("x=1\n")
    (tmp_path / "unchanged.py").write_text("y=2\n")
    (tmp_path / ".gitignore").write_text("ignored.py\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "Initial")
    assert collect_project(tmp_path) == []
    (tmp_path / "changed.py").write_text("x=3\n")
    (tmp_path / "untracked.py").write_text("z=4\n")
    (tmp_path / "ignored.py").write_text("secret=5\n")
    assert [source.path for source in collect_project(tmp_path)] == ["changed.py", "untracked.py"]
    assert all(source.changed_lines == (1,) for source in collect_project(tmp_path))
    assert [source.path for source in collect_project(tmp_path, mode="full")] == [
        "changed.py",
        "unchanged.py",
        "untracked.py",
    ]
    with pytest.raises(ProjectPathError):
        collect_project(tmp_path, base_ref="--output=/tmp/leak")
    (tmp_path / "unchanged.py").unlink()
    assert [source.path for source in collect_project(tmp_path, mode="full")] == [
        "changed.py",
        "untracked.py",
    ]


def test_parallel_upload_reads_never_share_names(tmp_path: Path) -> None:
    (tmp_path / "uploads").mkdir()
    ids = [f"{index:032x}" for index in range(20)]
    for index, identifier in enumerate(ids):
        (tmp_path / "uploads" / identifier).write_text(f"x={index}\n")
    with ThreadPoolExecutor(max_workers=8) as pool:
        sources = list(
            pool.map(lambda identifier: read_upload(tmp_path, identifier, "../../same.py"), ids)
        )
    assert [source.content for source in sources] == [f"x={index}\n" for index in range(20)]
    assert all(source.path == "same.py" for source in sources)
    with pytest.raises(ProjectPathError):
        read_upload(tmp_path, "../escape", "same.py")


def test_symlink_swap_cannot_disclose_outside_content(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "secret.py"
    outside.write_text("DO_NOT_READ_OUTSIDE\n")
    (project / "safe.py").write_text("SAFE\n")
    (project / "target.py").write_text("SAFE\n")

    def swap() -> None:
        for _ in range(100):
            (project / "newlink.py").symlink_to(outside)
            os.replace(project / "newlink.py", project / "target.py")
            (project / "replacement.py").write_text("SAFE\n")
            os.replace(project / "replacement.py", project / "target.py")

    def read() -> None:
        for _ in range(100):
            try:
                source = read_source(project, "target.py")
            except ProjectPathError, ProjectReadError:
                continue
            assert "DO_NOT_READ_OUTSIDE" not in source.content

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(swap), pool.submit(read), pool.submit(read)]
        for future in futures:
            future.result()


def test_pdf_uses_bounded_trusted_subprocess(tmp_path: Path) -> None:
    import io

    from pypdf import PdfWriter

    from app.pdf_extract import extract_pdf

    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    buffer = io.BytesIO()
    writer.write(buffer)
    assert extract_pdf(buffer.getvalue()).strip() == ""
    writer.encrypt("secret")
    encrypted = io.BytesIO()
    writer.write(encrypted)
    with pytest.raises(ProjectReadError):
        extract_pdf(encrypted.getvalue())
    with pytest.raises(ProjectReadError):
        extract_pdf(b"not a valid PDF")
    with pytest.raises(ProjectReadError):
        extract_pdf(buffer.getvalue(), max_pages=0)
    (tmp_path / "uploads").mkdir()
    identifier = "a" * 32
    (tmp_path / "uploads" / identifier).write_bytes(buffer.getvalue())
    assert read_upload(tmp_path, identifier, "document.pdf").content.strip() == ""


def test_untrusted_git_clean_filter_never_executes(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    (tmp_path / "main.py").write_text("x=1\n")
    (tmp_path / ".gitattributes").write_text("*.py filter=untrusted\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "Initial")
    marker = tmp_path / "FILTER_EXECUTED"
    _git(tmp_path, "config", "filter.untrusted.clean", f"touch {marker}; cat")
    _git(tmp_path, "config", "diff.evil.command", f"touch {marker}")
    (tmp_path / "main.py").write_text("x=2\n")
    sources = collect_project(tmp_path)
    assert sources[0].content == "x=2\n"
    assert sources[0].changed_lines == (1,)
    assert not marker.exists()


def test_diff_changed_span_excludes_unchanged_prefix_suffix(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    (tmp_path / "main.py").write_text("a=1\nb=2\nc=3\nd=4\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "Initial")
    (tmp_path / "main.py").write_text("a=1\nb=20\nc=3\nd=4\n")
    assert collect_project(tmp_path)[0].changed_lines == (2,)
