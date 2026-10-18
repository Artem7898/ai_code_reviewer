import json
import os
import subprocess
import sys
from pathlib import Path


def test_cli_exports_actual_findings_without_executing_source(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    marker = tmp_path / "executed"
    (project / "danger.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\nvalue = eval('1')\n"
    )
    output, sarif = tmp_path / "review.json", tmp_path / "review.sarif"
    environment = {"PATH": os.defpath, "RUFF_ENABLED": "false", "DATA_DIR": str(tmp_path / "data")}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.review_cli",
            str(project),
            "--full",
            "--provider",
            "static",
            "--output",
            str(output),
            "--sarif",
            str(sarif),
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    review = json.loads(output.read_text())
    assert any(item["rule_id"] == "PY-UNSAFE-EVAL" for item in review["findings"])
    assert review["metadata"]["snapshot"]["danger.py"]
    assert json.loads(sarif.read_text())["version"] == "2.1.0"
