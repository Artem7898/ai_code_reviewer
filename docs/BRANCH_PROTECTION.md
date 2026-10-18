# Default branch protection

[Русский](ru/BRANCH_PROTECTION.md) · [Main documentation](../README.md)

The target is `Artem7898/ai_code_reviewer`, default branch `master`.
The policy in [`.github/branch-protection.json`](../.github/branch-protection.json)
is a proposed GitHub API configuration. **Committing this file does not enable
branch protection.** Protection is active only after a successful API write and
read-back verification, or after an administrator saves the equivalent settings
in GitHub.

The minimum policy requires:

- A pull request with one approval; new commits dismiss stale approvals.
- The `validate` check from `.github/workflows/ci.yml`, with the branch up to date.
- Resolution of review conversations and enforcement for administrators.
- No force pushes and no branch deletion.

One required approval means another eligible collaborator must review a pull
request. A pull-request author cannot approve their own changes. Add a reviewer
before relying on this workflow for a repository maintained by one person.

## Inspect and apply

Use Python 3.14 and an installed, authenticated GitHub CLI (`gh`) whose account
has repository administrator permissions. The GitHub App installation or
fine-grained access token used by the CLI also needs repository **Administration:
read** to inspect protection and **Administration: write** to change it. Account
administrator status alone does not grant these permissions to an integration.
Do not put a token into the command, repository, or a chat message. HTTPS access
to `api.github.com` is required.

```bash
# Read repository metadata and existing policy, then print the merged proposal.
# This default mode sends GET requests only.
python scripts/protect_branch.py

# Explicitly write the merged policy, then read it back and verify it.
python scripts/protect_branch.py --apply
```

The helper checks the exact repository, default branch and admin permission before
any write. It retains existing required checks and their GitHub App bindings,
larger approval counts, code-owner requirements, last-push approval, allowed
review-dismissal actors, bypass lists, push restrictions and other recognized
protection flags. It rejects unknown existing policy fields rather than silently
discarding them. Existing signed-commit requirements use a separate GitHub API;
the helper leaves them unchanged and verifies they survived. Repository rulesets
and merge settings are not changed.

During publication, the API was reachable and repository metadata reported
administrator access. Branch metadata confirmed `protected: false`, but both
reading `branches/master/protection` and attempting to apply the committed policy
returned HTTP 403, `Resource not accessible by integration`. For the read request,
GitHub identified the required permission as `administration=read` in
`X-Accepted-GitHub-Permissions`. The current integration cannot inspect or apply
protection, and the attempt did not enable it. Securely reauthorize the GitHub
integration with Administration read/write access to this repository, or use the
manual Settings route below. Supply credentials only through the platform's secure
authorization or environment settings, then rerun inspection before `--apply`.

If API access is blocked, enable `api.github.com` in the cloud environment network
settings, save/apply those settings, and retry. A draft allowlist change does not
prove that the running machine can reach the API. A failed read sends no policy
update, and a JSON file or dry-run output is not evidence that protection is active.

As an alternative, an administrator can open GitHub **Settings → Branches → Add
classic branch protection rule**, select `master`, and configure the requirements
above. Check the result through GitHub and rerun the inspection command when API
access and integration permissions are available. Repository visibility, account
plan and organization policy can affect which protection features GitHub permits.

Run the `validate` workflow at least once so GitHub can identify its check name.
After protection is enabled, submit future changes through pull requests. Review
the Actions result and protection settings after the initial publication.
