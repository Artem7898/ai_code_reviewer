"""Small deterministic regression benchmark; this does not measure LLM quality."""

import json
import time
import tracemalloc
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from .analysis import analyze_static
from .schemas import SourceFile


class BenchmarkCase(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str
    code: str
    expected_rules: list[str]


def evaluate(path: Path | None = None) -> dict[str, object]:
    fixture_path = path or Path(__file__).resolve().parents[1] / "evaluation" / "fixtures.json"
    from pydantic import TypeAdapter

    cases = TypeAdapter(list[BenchmarkCase]).validate_json(fixture_path.read_bytes())
    true_positive = false_positive = false_negative = true_negative = 0
    started = time.perf_counter()
    tracemalloc.start()
    try:
        for case in cases:
            expected = set(case.expected_rules)
            actual = {
                finding.rule_id
                for finding in analyze_static(
                    [SourceFile(path=case.name + ".py", content=case.code)]
                )
            }
            true_positive += len(expected & actual)
            false_positive += len(actual - expected)
            false_negative += len(expected - actual)
            true_negative += int(not actual and not expected)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return {
        "scope": "Deterministic AST rules on eight seeded regression fixtures; no LLM quality claim or external calls.",
        "cases": len(cases),
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "true_negative_cases": true_negative,
        "precision": true_positive / (true_positive + false_positive)
        if true_positive + false_positive
        else 0,
        "recall": true_positive / (true_positive + false_negative)
        if true_positive + false_negative
        else 0,
        "elapsed_seconds": round(time.perf_counter() - started, 6),
        "peak_traced_bytes": peak,
    }


def main() -> int:
    result = evaluate()
    print(json.dumps(result, indent=2))
    return int(result["false_positive"] != 0 or result["false_negative"] != 0)


if __name__ == "__main__":
    raise SystemExit(main())
