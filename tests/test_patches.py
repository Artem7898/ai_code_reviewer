from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.patches import (
    PatchValidationError,
    read_artifacts,
    remove_artifact,
    save_artifacts,
    save_patch_proposal,
)
from app.utils import ProjectPathError, read_source

PATCH = "--- a/main.py\n+++ b/main.py\n@@ -1 +1 @@\n-x=1\n+x: int = 2\n"


def test_valid_patch_is_artifact_only(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("x=1\n")
    artifact_root = tmp_path / "artifacts"
    proposal = save_patch_proposal(project, PATCH, artifact_root)
    assert proposal.verified_syntax
    assert (project / "main.py").read_text() == "x=1\n"
    files = read_artifacts(artifact_root, proposal.artifact_id)
    assert files["main.py"] == "x: int = 2\n"
    assert files["proposal.patch"] == PATCH
    assert proposal.artifact_dir.stat().st_mode & 0o777 == 0o700
    remove_artifact(artifact_root, proposal.artifact_id)
    assert not proposal.artifact_dir.exists()


@pytest.mark.parametrize(
    "patch",
    [
        PATCH.replace("main.py", "../../outside.py"),
        PATCH.replace("main.py", "/tmp/escape.py"),
        PATCH.replace("main.py", "C:\\escape.py"),
        PATCH.replace("+x: int = 2", "+if invalid"),
        PATCH.replace("-x=1", "-x=999"),
        PATCH.replace("@@ -1 +1 @@", "@@ -1,2 +1 @@"),
        PATCH.replace("+++ b/main.py", "+++ b/renamed.py"),
        "GIT binary patch\n",
    ],
)
def test_rejects_unsafe_invalid_or_stale_patches(tmp_path: Path, patch: str) -> None:
    (tmp_path / "main.py").write_text("x=1\n")
    with pytest.raises((PatchValidationError, ProjectPathError)):
        save_patch_proposal(tmp_path, patch, tmp_path / "artifacts")
    assert (tmp_path / "main.py").read_text() == "x=1\n"


def test_patch_cannot_follow_symlink(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("x=1\n")
    (project / "main.py").symlink_to(outside)
    with pytest.raises(ProjectPathError):
        save_patch_proposal(project, PATCH, tmp_path / "artifacts")
    (project / "linked").symlink_to(tmp_path, target_is_directory=True)
    addition = "--- /dev/null\n+++ b/linked/new.py\n@@ -0,0 +1 @@\n+x=1\n"
    with pytest.raises(ProjectPathError):
        save_patch_proposal(project, addition, tmp_path / "artifacts")


def test_parallel_proposals_have_private_unique_artifacts(tmp_path: Path) -> None:
    with ThreadPoolExecutor(max_workers=8) as pool:
        proposals = list(
            pool.map(
                lambda index: save_artifacts({"tests/test_new.py": f"x={index}\n"}, tmp_path),
                range(20),
            )
        )
    assert len({proposal.artifact_id for proposal in proposals}) == 20
    for index, proposal in enumerate(proposals):
        assert read_artifacts(tmp_path, proposal.artifact_id)["tests/test_new.py"] == f"x={index}\n"


def test_patch_no_newline_and_addition(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("x=1")
    patch = "--- a/main.py\n+++ b/main.py\n@@ -1 +1 @@\n-x=1\n\\ No newline at end of file\n+x=2\n\\ No newline at end of file\n"
    proposal = save_patch_proposal(tmp_path, patch, tmp_path / "artifacts")
    assert read_artifacts(tmp_path / "artifacts", proposal.artifact_id)["main.py"] == "x=2"
    added = save_patch_proposal(
        tmp_path, "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+x=3\n", tmp_path / "artifacts"
    )
    assert read_artifacts(tmp_path / "artifacts", added.artifact_id)["new.py"] == "x=3\n"


def test_artifact_symlink_root_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "link"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        save_artifacts({"x.py": "x=1\n"}, link)


@pytest.mark.parametrize(
    "content", ["return 1\n", "await something()\n", "break\n", "nonlocal x\n"]
)
def test_ast_only_modules_are_not_marked_valid(tmp_path: Path, content: str) -> None:
    with pytest.raises(PatchValidationError):
        save_artifacts({"x.py": content}, tmp_path)


def test_prefix_conflict_is_rejected_without_partial_files(tmp_path: Path) -> None:
    with pytest.raises(PatchValidationError):
        save_artifacts({"file.py": "x=1\n", "file.py/nested.py": "x=2\n"}, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_failed_write_removes_partial_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import patches

    original = patches._write_private

    def fail_manifest(root: Path, path: str, content: str) -> None:
        if path == "manifest.json":
            raise OSError("Simulated full disk")
        original(root, path, content)

    monkeypatch.setattr(patches, "_write_private", fail_manifest)
    with pytest.raises(OSError):
        save_artifacts({"main.py": "x=1\n"}, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_strict_patch_rejects_changes_outside_hunk(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("x=1\nunchanged = True\n")
    expected = {"main.py": read_source(project, "main.py").sha256}
    (project / "main.py").write_text("x=1\nunchanged = False\n")
    with pytest.raises(PatchValidationError, match="Reviewed source changed"):
        save_patch_proposal(project, PATCH, tmp_path / "artifacts", expected_hashes=expected)
    assert not (tmp_path / "artifacts").exists()


def test_new_test_patch_retains_all_reviewed_hashes_and_rejects_staleness(tmp_path: Path) -> None:
    from app.sandbox import _snapshot

    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("x=1\n")
    (project / "helper.py").write_text("y=2\n")
    expected = {path: read_source(project, path).sha256 for path in ["main.py", "helper.py"]}
    patch = "--- /dev/null\n+++ b/test_main.py\n@@ -0,0 +1 @@\n+assert True\n"
    proposal = save_patch_proposal(project, patch, tmp_path / "artifacts", expected_hashes=expected)
    assert proposal.source_hashes == {**expected, "test_main.py": ""}
    (project / "main.py").write_text("x=3\n")
    with pytest.raises(PatchValidationError, match="Sources changed"):
        _snapshot(project, proposal, tmp_path / "snapshot")
    with pytest.raises(PatchValidationError, match="Reviewed source changed"):
        save_patch_proposal(project, patch, tmp_path / "other_artifacts", expected_hashes=expected)
    assert not (tmp_path / "other_artifacts").exists()


def test_strict_patch_rejects_unreviewed_existing_file(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("x=1\n")
    (tmp_path / "unreviewed.py").write_text("x=1\n")
    expected = {"main.py": read_source(tmp_path, "main.py").sha256}
    patch = PATCH.replace("main.py", "unreviewed.py")
    with pytest.raises(PatchValidationError, match="outside the reviewed snapshot"):
        save_patch_proposal(tmp_path, patch, tmp_path / "artifacts", expected_hashes=expected)


def test_strict_artifacts_validate_sources_and_new_target_absence(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("x=1\n")
    expected = {"main.py": read_source(project, "main.py").sha256}
    proposal = save_artifacts(
        {"test_main.py": "assert True\n"},
        tmp_path / "artifacts",
        project=project,
        expected_hashes=expected,
    )
    assert proposal.source_hashes == {**expected, "test_main.py": ""}
    (project / "unknown.py").write_text("y=2\n")
    with pytest.raises(PatchValidationError, match="already exists"):
        save_artifacts(
            {"unknown.py": "y=3\n"},
            tmp_path / "other_artifacts",
            project=project,
            expected_hashes=expected,
        )
    (project / "main.py").write_text("x=3\n")
    with pytest.raises(PatchValidationError, match="Reviewed source changed"):
        save_artifacts(
            {"test_other.py": "assert True\n"},
            tmp_path / "other_artifacts",
            project=project,
            expected_hashes=expected,
        )


def test_strict_patch_rechecks_source_when_parsing_old_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import patches

    (tmp_path / "main.py").write_text("x=1\nextra = True\n")
    expected = {"main.py": read_source(tmp_path, "main.py").sha256}
    original = patches.read_source
    reads = 0

    def mutate_before_second_read(project: Path, path: str, max_file_bytes: int = 262144):
        nonlocal reads
        reads += 1
        if reads == 2:
            (tmp_path / "main.py").write_text("x=1\nextra = False\n")
        return original(project, path, max_file_bytes)

    monkeypatch.setattr(patches, "read_source", mutate_before_second_read)
    with pytest.raises(PatchValidationError, match="changed while parsing"):
        save_patch_proposal(tmp_path, PATCH, tmp_path / "artifacts", expected_hashes=expected)
    assert not (tmp_path / "artifacts").exists()


def test_strict_patch_rechecks_all_sources_immediately_before_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import patches

    (tmp_path / "main.py").write_text("x=1\n")
    (tmp_path / "helper.py").write_text("y=2\n")
    expected = {path: read_source(tmp_path, path).sha256 for path in ["main.py", "helper.py"]}
    original = patches._check_artifacts

    def mutate_other_source(artifacts) -> None:
        original(artifacts)
        (tmp_path / "helper.py").write_text("y=3\n")

    monkeypatch.setattr(patches, "_check_artifacts", mutate_other_source)
    with pytest.raises(PatchValidationError, match="Reviewed source changed"):
        save_patch_proposal(tmp_path, PATCH, tmp_path / "artifacts", expected_hashes=expected)
    assert not (tmp_path / "artifacts").exists()
