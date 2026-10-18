#!/usr/bin/env python3
"""Inspect and, only with --apply, protect this repository's default branch.

Uses the authenticated GitHub CLI; never reads or stores authentication secrets.
Existing supported restrictions are preserved. Unknown policy fields stop changes.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any, Protocol

REPOSITORY = "Artem7898/ai_code_reviewer"
BRANCH = "master"
POLICY_PATH = Path(__file__).resolve().parents[1] / ".github/branch-protection.json"


class ProtectionError(RuntimeError):
    """The operation cannot safely continue."""


class Client(Protocol):
    def request(
        self,
        method: str,
        endpoint: str,
        payload: dict[str, Any] | None = None,
        *,
        allow_unprotected: bool = False,
    ) -> dict[str, Any] | None: ...


class GitHubClient:
    def request(
        self,
        method: str,
        endpoint: str,
        payload: dict[str, Any] | None = None,
        *,
        allow_unprotected: bool = False,
    ) -> dict[str, Any] | None:
        command = [
            "gh",
            "api",
            "--hostname",
            "github.com",
            "--method",
            method,
            endpoint,
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            "X-GitHub-Api-Version: 2022-11-28",
        ]
        if payload is not None:
            command.extend(["--input", "-"])
        try:
            result = subprocess.run(
                command,
                input=None if payload is None else json.dumps(payload),
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ProtectionError("GitHub CLI unavailable or API request timed out.") from error
        if result.returncode:
            if allow_unprotected and "(HTTP 404)" in result.stderr:
                return None
            # Do not print CLI stderr or authentication configuration.
            raise ProtectionError(
                f"GitHub API {method} {endpoint} failed (exit {result.returncode}). "
                "Check network access to api.github.com and authenticated repository admin access."
            )
        try:
            response = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise ProtectionError("GitHub API returned invalid JSON.") from error
        if not isinstance(response, dict):
            raise ProtectionError("Expected a GitHub API object.")
        return response


def _actors(value: dict[str, Any]) -> dict[str, list[str]]:
    result = {}
    for kind, key in (("users", "login"), ("teams", "slug"), ("apps", "slug")):
        result[kind] = [actor[key] for actor in value.get(kind, [])]
    unknown = set(value) - {"users", "teams", "apps", "url", "users_url", "teams_url", "apps_url"}
    if unknown:
        raise ProtectionError(f"Unsupported restriction fields: {sorted(unknown)}")
    return result


def _enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, dict) and isinstance(value.get("enabled"), bool):
        return bool(value["enabled"])
    raise ProtectionError("Unexpected protection flag format; no changes made.")


def existing_policy(response: dict[str, Any] | None) -> dict[str, Any]:
    """Convert readable API metadata into the supported update payload."""
    if response is None:
        return {}
    policy: dict[str, Any] = {}
    boolean_fields = {
        "enforce_admins",
        "required_linear_history",
        "allow_force_pushes",
        "allow_deletions",
        "required_conversation_resolution",
        "block_creations",
        "lock_branch",
        "allow_fork_syncing",
    }
    allowed = boolean_fields | {
        "url",
        "required_status_checks",
        "required_pull_request_reviews",
        "restrictions",
        # Signatures have a separate API and are intentionally left untouched.
        "required_signatures",
    }
    unknown = set(response) - allowed
    if unknown:
        raise ProtectionError(f"Unsupported existing protection fields: {sorted(unknown)}")
    for key in boolean_fields & response.keys():
        policy[key] = _enabled(response[key])
    if response.get("required_status_checks") is not None:
        status = response["required_status_checks"]
        unknown = set(status) - {"url", "contexts_url", "strict", "contexts", "checks"}
        if unknown:
            raise ProtectionError(f"Unsupported status-check fields: {sorted(unknown)}")
        checks = []
        for check in status.get("checks", []):
            if set(check) - {"context", "app_id"}:
                raise ProtectionError("Unsupported existing status-check requirement.")
            checks.append(dict(check))
        for context in status.get("contexts", []):
            if not any(check["context"] == context for check in checks):
                checks.append({"context": context})
        policy["required_status_checks"] = {"strict": bool(status["strict"]), "checks": checks}
    if response.get("required_pull_request_reviews") is not None:
        reviews = response["required_pull_request_reviews"]
        simple = {
            "dismiss_stale_reviews",
            "require_code_owner_reviews",
            "required_approving_review_count",
            "require_last_push_approval",
        }
        unknown = (
            set(reviews)
            - simple
            - {
                "url",
                "dismissal_restrictions",
                "bypass_pull_request_allowances",
            }
        )
        if unknown:
            raise ProtectionError(f"Unsupported pull-request requirements: {sorted(unknown)}")
        copied = {key: reviews[key] for key in simple & reviews.keys()}
        for key in ("dismissal_restrictions", "bypass_pull_request_allowances"):
            if reviews.get(key) is not None:
                copied[key] = _actors(reviews[key])
        policy["required_pull_request_reviews"] = copied
    policy["restrictions"] = (
        _actors(response["restrictions"]) if response.get("restrictions") is not None else None
    )
    return policy


def merge_policy(existing: dict[str, Any], minimum: dict[str, Any]) -> dict[str, Any]:
    """Tighten the minimum requirements without removing existing restrictions."""
    result: dict[str, Any] = json.loads(json.dumps(existing))
    checks = result.setdefault("required_status_checks", {"checks": []})
    checks["strict"] = True
    for requirement in minimum["required_status_checks"]["checks"]:
        if not any(check["context"] == requirement["context"] for check in checks["checks"]):
            checks["checks"].append(requirement)
    reviews = result.setdefault("required_pull_request_reviews", {})
    reviews["dismiss_stale_reviews"] = True
    reviews["required_approving_review_count"] = max(
        minimum["required_pull_request_reviews"]["required_approving_review_count"],
        reviews.get("required_approving_review_count", 0),
    )
    result.setdefault("restrictions", None)
    for key in ("enforce_admins", "required_conversation_resolution"):
        result[key] = True
    for key in ("allow_force_pushes", "allow_deletions"):
        result[key] = False
    return result


def _verify(actual: Any, expected: Any, path: str = "protection") -> None:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            raise ProtectionError(f"Read-back verification failed at {path}.")
        for key, value in expected.items():
            _verify(actual.get(key), value, f"{path}.{key}")
    elif isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            raise ProtectionError(f"Read-back verification failed at {path}.")
        for value in expected:
            if not any(_matches(candidate, value) for candidate in actual):
                raise ProtectionError(f"Read-back verification failed at {path}.")
    elif actual != expected:
        raise ProtectionError(f"Read-back verification failed at {path}.")


def _matches(actual: Any, expected: Any) -> bool:
    try:
        _verify(actual, expected)
    except ProtectionError:
        return False
    return True


def protect(client: Client, *, apply: bool = False) -> dict[str, Any]:
    repository_path = f"repos/{REPOSITORY}"
    repository = client.request("GET", repository_path)
    if (
        repository is None
        or repository.get("full_name", "").lower() != REPOSITORY.lower()
        or repository.get("default_branch") != BRANCH
        or repository.get("permissions", {}).get("admin") is not True
    ):
        raise ProtectionError(
            "Expected repository, default branch master and admin access required."
        )
    branch = client.request("GET", f"{repository_path}/branches/{BRANCH}")
    if branch is None or branch.get("name") != BRANCH:
        raise ProtectionError("The default branch could not be verified.")
    endpoint = f"{repository_path}/branches/{BRANCH}/protection"
    response = client.request("GET", endpoint, allow_unprotected=True)
    if response is None and branch.get("protected") is not False:
        raise ProtectionError("Branch reports protection but its policy cannot be read; stopped.")
    minimum = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    desired = merge_policy(existing_policy(response), minimum)
    if apply:
        client.request("PUT", endpoint, desired)
        read_back = client.request("GET", endpoint)
        if read_back is None:
            raise ProtectionError("Protection was submitted but read-back verification failed.")
        _verify(existing_policy(read_back), desired)
        # The update endpoint cannot modify signatures; still verify existing enforcement survived.
        if response and response.get("required_signatures") is not None:
            _verify(read_back.get("required_signatures"), response["required_signatures"])
    return desired


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write and verify branch protection")
    arguments = parser.parse_args()
    try:
        desired = protect(GitHubClient(), apply=arguments.apply)
    except (ProtectionError, KeyError, TypeError, ValueError) as error:
        print(f"Stopped: {error}")
        return 1
    print(json.dumps(desired, indent=2))
    print("Protection applied and verified." if arguments.apply else "Dry run: no changes made.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
