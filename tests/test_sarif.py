from app.sarif import review_to_sarif
from app.schemas import Finding, ReviewResult


def result(line=1):
    return ReviewResult(
        summary="Review",
        findings=[
            Finding(
                rule_id="PY-UNSAFE-EVAL",
                severity="warning",
                path="folder/my file.py",
                line_start=line,
                line_end=line,
                evidence="eval(value)",
                explanation="Dynamic execution",
                source="ast",
            )
        ],
    )


def test_sarif_has_uri_encoded_relative_path_and_coordinates():
    report = review_to_sarif(result())
    assert report["version"] == "2.1.0"
    run = report["runs"][0]
    finding = run["results"][0]
    physical = finding["locations"][0]["physicalLocation"]
    assert physical["artifactLocation"]["uri"] == "folder/my%20file.py"
    assert physical["region"]["startLine"] == 1
    assert run["tool"]["driver"]["rules"][0]["id"] == finding["ruleId"]


def test_fingerprints_are_stable_when_unrelated_lines_are_inserted():
    first = review_to_sarif(result(1))["runs"][0]["results"][0]
    second = review_to_sarif(result(10))["runs"][0]["results"][0]
    assert first["partialFingerprints"] == second["partialFingerprints"]
