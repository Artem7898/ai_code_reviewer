"""SARIF 2.1.0 export of already grounded review findings."""

import hashlib
from urllib.parse import quote

from .schemas import ReviewResult


def review_to_sarif(result: ReviewResult) -> dict[str, object]:
    rule_ids = sorted({finding.rule_id for finding in result.findings})
    rule_indexes = {rule: index for index, rule in enumerate(rule_ids)}
    results: list[dict[str, object]] = []
    for finding in result.findings:
        # Exclude absolute positions so fingerprints survive unrelated inserted lines.
        fingerprint = hashlib.sha256(
            "\0".join((finding.rule_id, finding.path, finding.evidence.strip())).encode()
        ).hexdigest()
        results.append(
            {
                "ruleId": finding.rule_id,
                "ruleIndex": rule_indexes[finding.rule_id],
                "level": finding.severity,
                "message": {
                    "text": finding.explanation
                    + ("\nSuggestion: " + finding.suggestion if finding.suggestion else "")
                },
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {
                                "uri": quote(finding.path, safe="/"),
                            },
                            "region": {
                                "startLine": finding.line_start,
                                "endLine": finding.line_end,
                                "snippet": {"text": finding.evidence},
                            },
                        }
                    }
                ],
                "partialFingerprints": {"reviewer/v1": fingerprint},
                "properties": {"source": finding.source, "confidence": finding.confidence},
            }
        )
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "ai_code_reviewer",
                        "rules": [{"id": rule} for rule in rule_ids],
                    }
                },
                "results": results,
            }
        ],
    }
