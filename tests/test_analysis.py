import json

import pytest
from pydantic import SecretStr, ValidationError

from app.ai_client import Completion, ProviderResponseError
from app.analysis import ReviewAnalyzer, ReviewValidationError, analyze_static
from app.evaluate import evaluate
from app.schemas import SourceFile
from app.settings import Settings


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        data_dir=tmp_path,
        secret_key=SecretStr("r" * 64),
        ai_provider="static",
        ruff_enabled=False,
    )


class FakeProvider:
    def __init__(self, value):
        self.value = value

    async def complete(self, messages):
        return Completion(content=json.dumps(self.value), tokens_used=20, model="fake")


def finding(**overrides):
    data = {
        "rule_id": "AI-BUG",
        "severity": "warning",
        "path": "code.py",
        "line_start": 1,
        "line_end": 1,
        "evidence": "value = 1",
        "explanation": "A demonstrated issue",
        "suggestion": "Check inputs",
        "source": "ai",
    }
    return data | overrides


async def test_static_review_catches_syntax_and_never_uses_ai(settings):
    result = await ReviewAnalyzer(settings).review(
        [SourceFile(path="bad.py", content="def broken(:\n    pass\n")]
    )
    assert {item.rule_id for item in result.findings} == {"PY-SYNTAX"}
    assert result.tokens_used == 0
    assert result.model == "static"


@pytest.mark.parametrize(
    "path",
    ["../secret", "/etc/passwd", "x/../secret", "x\\secret", "a//b", "./a", "C:/key", "a\x00.py"],
)
def test_source_paths_are_relative_unambiguous(path):
    with pytest.raises(ValidationError):
        SourceFile(path=path, content="")


async def test_grounded_ai_finding_is_preserved(settings):
    settings.ai_provider = "ollama"
    analyzer = ReviewAnalyzer(settings, FakeProvider({"findings": [finding()], "summary": "Check"}))
    result = await analyzer.review([SourceFile(path="code.py", content="value = 1\n")])
    assert result.findings[0].line_start == 1
    assert result.tokens_used == 20


@pytest.mark.parametrize(
    "overrides",
    [
        {"path": "other.py"},
        {"line_start": 2, "line_end": 2},
        {"evidence": "invented"},
        {"source": "ruff"},
    ],
)
async def test_hallucinated_findings_fail(settings, overrides):
    settings.ai_provider = "ollama"
    analyzer = ReviewAnalyzer(
        settings, FakeProvider({"findings": [finding(**overrides)], "summary": "Check"})
    )
    with pytest.raises(ReviewValidationError):
        await analyzer.review([SourceFile(path="code.py", content="value = 1\n")])


async def test_malformed_json_schema_never_becomes_success(settings):
    settings.ai_provider = "ollama"
    analyzer = ReviewAnalyzer(
        settings, FakeProvider({"findings": "API Error 401", "summary": "failed"})
    )
    with pytest.raises(ProviderResponseError):
        await analyzer.review([SourceFile(path="code.py", content="value = 1\n")])


async def test_budget_and_duplicate_files_are_rejected(settings):
    settings.max_review_files = 1
    analyzer = ReviewAnalyzer(settings)
    with pytest.raises(ReviewValidationError, match="file limit"):
        await analyzer.review(
            [SourceFile(path="a.py", content=""), SourceFile(path="b.py", content="")]
        )
    settings.max_review_files = 2
    source = SourceFile(path="a.py", content="")
    with pytest.raises(ReviewValidationError, match="duplicate"):
        await analyzer.review([source, source])


def test_diff_only_reports_findings_overlapping_changed_lines():
    source = SourceFile(path="code.py", content="eval('1')\nexec('2')\n", changed_lines=(2,))
    assert [item.rule_id for item in analyze_static([source])] == ["PY-UNSAFE-EXEC"]


async def test_ruff_is_bounded_and_reports_undefined_names(settings):
    settings.ruff_enabled = True
    result = await ReviewAnalyzer(settings).review(
        [SourceFile(path="code.py", content="def answer():\n    return missing_name\n")]
    )
    assert "RUFF-F821" in {item.rule_id for item in result.findings}


async def test_proposal_generates_patch_without_touching_files(settings, tmp_path):
    settings.ai_provider = "ollama"
    proposal = {
        "kind": "migration",
        "summary": "Add annotation",
        "artifacts": [{"path": "code.py", "content": "value: int = 1\n"}],
        "diff": "untrusted diff",
    }
    result = await ReviewAnalyzer(settings, FakeProvider(proposal)).generate_proposal(
        [SourceFile(path="code.py", content="value = 1\n")], "migration"
    )
    assert "--- a/code.py" in result.diff
    assert "+value: int = 1" in result.diff
    assert "untrusted" not in result.diff
    assert not (tmp_path / "code.py").exists()


async def test_generated_python_must_parse(settings):
    settings.ai_provider = "ollama"
    proposal = {
        "kind": "tests",
        "summary": "Tests",
        "artifacts": [{"path": "test_code.py", "content": "def broken(:"}],
    }
    with pytest.raises(ReviewValidationError, match="invalid syntax"):
        await ReviewAnalyzer(settings, FakeProvider(proposal)).generate_proposal([], "tests")


def test_seeded_benchmark_is_explicit_and_reproducible():
    metrics = evaluate()
    assert metrics["cases"] == 8
    assert metrics["false_positive"] == metrics["false_negative"] == 0
    assert metrics["precision"] == metrics["recall"] == 1
    assert "no LLM quality claim" in metrics["scope"]


async def test_patch_roundtrip_preserves_missing_final_newline(settings, tmp_path):
    from app.patches import save_patch_proposal

    settings.ai_provider = "ollama"
    project = tmp_path / "project"
    project.mkdir()
    (project / "code.py").write_text("value = 1", encoding="utf-8")
    proposal = {
        "kind": "migration",
        "summary": "Annotate",
        "artifacts": [{"path": "code.py", "content": "value: int = 1"}],
    }
    result = await ReviewAnalyzer(settings, FakeProvider(proposal)).generate_proposal(
        [SourceFile(path="code.py", content="value = 1")], "migration"
    )
    saved = save_patch_proposal(project, result.diff, tmp_path / "artifacts")
    assert (saved.artifact_dir / "files" / "code.py").read_text() == "value: int = 1"
    assert (project / "code.py").read_text() == "value = 1"


async def test_top_level_return_is_not_a_valid_generated_module(settings):
    settings.ai_provider = "ollama"
    proposal = {
        "kind": "tests",
        "summary": "Tests",
        "artifacts": [{"path": "test_code.py", "content": "return 1\n"}],
    }
    with pytest.raises(ReviewValidationError, match="invalid syntax"):
        await ReviewAnalyzer(settings, FakeProvider(proposal)).generate_proposal([], "tests")


def test_security_rules_do_not_flag_shadowed_builtins_or_unrelated_shell_keyword():
    source = SourceFile(
        path="code.py",
        content="def eval(value):\n    return value\nanswer = eval('data')\nservice.run(shell=True)\n",
    )
    assert analyze_static([source]) == []
