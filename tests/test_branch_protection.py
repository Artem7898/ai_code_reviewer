"""Branch protection must never weaken existing policy or mutate during inspection."""

from copy import deepcopy
from typing import Any

import pytest

from scripts.protect_branch import ProtectionError, existing_policy, merge_policy, protect

MINIMUM = {
    "required_status_checks": {"strict": True, "checks": [{"context": "validate"}]},
    "required_pull_request_reviews": {"required_approving_review_count": 1},
}


def policy_response(policy: dict[str, Any]) -> dict[str, Any]:
    response = deepcopy(policy)
    for field in (
        "enforce_admins",
        "required_conversation_resolution",
        "allow_force_pushes",
        "allow_deletions",
        "required_linear_history",
        "lock_branch",
    ):
        if field in response:
            response[field] = {"enabled": response[field]}
    for check in response.get("required_status_checks", {}).get("checks", []):
        check.setdefault("app_id", 15368)
    restrictions = [response.get("restrictions")]
    reviews = response.get("required_pull_request_reviews", {})
    restrictions.extend(
        reviews.get(key)
        for key in (
            "dismissal_restrictions",
            "bypass_pull_request_allowances",
        )
    )
    for actors in restrictions:
        if actors is not None:
            for kind, key in (("users", "login"), ("teams", "slug"), ("apps", "slug")):
                actors[kind] = [{key: actor} for actor in actors.get(kind, [])]
    return response


class FakeClient:
    def __init__(self, protection: dict[str, Any] | None = None):
        self.protection = protection
        self.calls: list[tuple[str, str]] = []
        self.repository = {
            "full_name": "Artem7898/ai_code_reviewer",
            "default_branch": "master",
            "permissions": {"admin": True},
        }

    def request(self, method, endpoint, payload=None, *, allow_unprotected=False):
        self.calls.append((method, endpoint))
        if endpoint == "repos/Artem7898/ai_code_reviewer":
            return self.repository
        if endpoint.endswith("/branches/master"):
            return {"name": "master", "protected": self.protection is not None}
        if method == "PUT":
            signatures = (self.protection or {}).get("required_signatures")
            self.protection = policy_response(payload)
            if signatures is not None:
                self.protection["required_signatures"] = signatures
        return self.protection


def test_dry_run_does_not_mutate_unprotected_branch():
    client = FakeClient()
    desired = protect(client)
    assert desired["required_status_checks"]["strict"] is True
    assert desired["required_pull_request_reviews"]["required_approving_review_count"] == 1
    assert all(method == "GET" for method, _ in client.calls)


def test_blocked_read_never_attempts_write():
    class BlockedClient(FakeClient):
        def request(self, method, endpoint, payload=None, *, allow_unprotected=False):
            self.calls.append((method, endpoint))
            raise ProtectionError("API blocked")

    client = BlockedClient()
    with pytest.raises(ProtectionError, match="API blocked"):
        protect(client, apply=True)
    assert len(client.calls) == 1
    assert client.calls[0][0] == "GET"


@pytest.mark.parametrize(
    "field,value",
    [
        ("full_name", "attacker/other"),
        ("default_branch", "main"),
        ("permissions", {"admin": False}),
    ],
)
def test_repository_identity_branch_and_admin_required(field, value):
    client = FakeClient()
    client.repository[field] = value
    with pytest.raises(ProtectionError, match="admin access required"):
        protect(client, apply=True)
    assert all(method == "GET" for method, _ in client.calls)


def test_existing_stronger_requirements_and_restrictions_survive():
    response = {
        "required_status_checks": {
            "strict": False,
            "contexts": ["security"],
            "checks": [
                {"context": "validate", "app_id": 15368},
            ],
        },
        "required_pull_request_reviews": {
            "required_approving_review_count": 3,
            "require_code_owner_reviews": True,
            "require_last_push_approval": True,
            "dismissal_restrictions": {"users": [{"login": "owner"}], "teams": [], "apps": []},
            "bypass_pull_request_allowances": {"users": [], "teams": [], "apps": []},
        },
        "restrictions": {"users": [], "teams": [{"slug": "maintainers"}], "apps": []},
        "required_linear_history": {"enabled": True},
        "lock_branch": {"enabled": True},
        "required_signatures": {"enabled": True, "url": "https://api.github.com/signatures"},
    }
    client = FakeClient(response)
    result = protect(client, apply=True)
    assert result["required_pull_request_reviews"]["required_approving_review_count"] == 3
    assert result["required_pull_request_reviews"]["require_last_push_approval"] is True
    assert result["required_linear_history"] is True
    assert result["lock_branch"] is True
    assert result["restrictions"]["teams"] == ["maintainers"]
    assert {"context": "validate", "app_id": 15368} in result["required_status_checks"]["checks"]
    assert {"context": "security"} in result["required_status_checks"]["checks"]
    assert result["required_pull_request_reviews"]["dismissal_restrictions"]["users"] == ["owner"]


def test_apply_writes_once_and_reads_back():
    client = FakeClient()
    protect(client, apply=True)
    assert [method for method, _ in client.calls] == ["GET", "GET", "GET", "PUT", "GET"]


def test_read_back_mismatch_reports_failure():
    class BrokenWrite(FakeClient):
        def request(self, method, endpoint, payload=None, *, allow_unprotected=False):
            result = super().request(method, endpoint, payload, allow_unprotected=allow_unprotected)
            if method == "PUT":
                self.protection["enforce_admins"] = {"enabled": False}
            return result

    with pytest.raises(ProtectionError, match="verification failed"):
        protect(BrokenWrite(), apply=True)


def test_unknown_existing_policy_is_not_silently_discarded():
    client = FakeClient({"future_stronger_restriction": {"enabled": True}})
    with pytest.raises(ProtectionError, match="Unsupported existing protection"):
        protect(client, apply=True)
    assert all(method == "GET" for method, _ in client.calls)


def test_unknown_nested_policy_is_not_silently_discarded():
    with pytest.raises(ProtectionError, match="Unsupported pull-request"):
        existing_policy({"required_pull_request_reviews": {"future_restriction": True}})


def test_check_app_binding_is_kept_when_minimum_is_added():
    existing = {
        "required_status_checks": {
            "strict": False,
            "checks": [
                {"context": "security", "app_id": 123},
            ],
        }
    }
    merged = merge_policy(existing, MINIMUM)
    assert merged["required_status_checks"]["checks"] == [
        {"context": "security", "app_id": 123},
        {"context": "validate"},
    ]
    assert existing["required_status_checks"]["strict"] is False


def test_configured_higher_review_count_is_respected():
    minimum = deepcopy(MINIMUM)
    minimum["required_pull_request_reviews"]["required_approving_review_count"] = 2
    assert (
        merge_policy({}, minimum)["required_pull_request_reviews"][
            "required_approving_review_count"
        ]
        == 2
    )
