# Modernization validation

**English** · [Русский](ru/VALIDATION.md) · [Setup](../README.md) · [Architecture and migration](MODERNIZATION.md)

Local validation was performed on **18 October 2026**, using CPython **3.14.8** on Linux. This report records the modernization checks in that environment. See [GitHub Actions](https://github.com/Artem7898/ai_code_reviewer/actions) for subsequent remote runs.

| Check | Result |
| --- | --- |
| `RUN_DOCKER_TESTS=1 python -m pytest -q` | **175 passed**, 23.78 s, including real Docker isolation, timeout cleanup, and 12 branch-protection helper tests |
| `python admin_panel/manage.py test core --noinput` | **8 tests, OK**, using a separate test database |
| `ruff check .` | Passed |
| `ruff format --check .` | 59 Python files formatted |
| `mypy` | Strict checking of 21 application files passed |
| `PYTHONPATH=admin_panel:. mypy --config-file admin_panel/mypy.ini` | Strict checking of 13 Django files passed |
| `mypy scripts/protect_branch.py --strict` | Strict checking of the administration helper passed |
| `manage.py makemigrations --check --dry-run` | No Django migration changes required |
| `manage.py check --deploy --fail-level WARNING` with production settings | No warnings or errors; the key was not printed |
| `uv lock --check`, frozen sync, repeated init/migrate/collectstatic | Passed; repeated setup preserves data and creates no users |
| `pip-audit` against hashed runtime requirements | 45 dependencies, no known advisories found |
| Docker build and Compose | **19 functional checks passed** |
| Docker image contents and configuration | 39 source files matched the checkout; no `.env`, stale host bytecode, temporary CA, or persisted secret/proxy values |
| Chromium UI smoke test | Login, literal rendering of hostile HTML, progress, cancellation, logout, cleanup, and no persisted token; API responses were mocked |
| `git diff --check` | Passed |

Run Django tests from the repository root with the explicit `core` label, as CI does. Running `python admin_panel/manage.py test` without a label from the repository root can discover zero tests because of the nested project layout; that is not a validation result. Default discovery from the `admin_panel` directory also finds eight tests.

## Coverage

- Argon2id, legacy bcrypt verification, no seeded account, strict JWT claims, and token revocation after password changes.
- Concurrent password resets and compare-and-swap bcrypt upgrades: an old password cannot overwrite a newer reset.
- Anonymous and cross-user access checks, Django staff/view permissions, and safe report rendering.
- Path traversal, symlinks, hard links, special files, symlink swaps during reads, and upload-name overwrite attempts.
- Concurrent uploads with identical filenames, without collisions or retained temporary files.
- Immutable queued snapshots. Patch hashes are checked before saving artifacts and before isolated execution; changes outside patch hunks cannot be silently incorporated.
- Atomic quotas, deduplication and claims, fenced leases, restart recovery, cancellation, timeouts, and worker task cleanup.
- Daily UTC budgets across concurrent processes, restarts, midnight rollover, and conservative monetary rounding.
- Bounded HTTP bodies, chunked requests, total deadlines, slow-drip uploads, released ingress slots, and bounded login-limiter state.
- Job lists exclude large payload/result documents. Full results are fetched separately and are not downloaded again on every UI poll.
- Provider failures, malformed JSON, schemas, paths, line ranges, evidence, retries, and HTTP-client cleanup, without real external model calls.
- Isolated Ruff uses the explicit Python 3.14 target and accepts deferred self annotations. Provider tests select their backend independently of ambient `AI_PROVIDER`.
- Git clean filters do not execute: review uses raw Git objects, bounded reads, and content hashes.
- PDF parsing runs in a resource-limited subprocess. Generated Python executes only in a constrained container.

The real Docker test verified UID 65534, no original `.env` or inherited host environment, read-only `/code`, no outbound network, overlay-only patch application, and container removal after an infinite test. It does not establish the behavioral correctness of arbitrary generated tests.

The Compose smoke test exercised **HTTP → SQLite job → separate worker → findings → SARIF → shared Django history**, using static analysis. It also verified authentication, local CSS/JS, database readiness, a read-only project mount, and an unprivileged runtime user. Its temporary containers, network, and volume were removed.

Validated local image: `ai-code-reviewer:modernization-check`.

```text
sha256:254f3269cc6e0eace4713a56beca436b660eaf88f47f636b6b216abef1a17b6e
```

The image was rebuilt and its 39 runtime source files rechecked after the final Ruff correction. The 19 Compose checks were run on the preceding build with the same container and dependency configuration; they were not repeated after this analyzer change.

## Performance and evidence limits

`python -m app.evaluate` evaluated eight small labeled AST fixtures: TP 4, FP 0, FN 0, TN 4. One run took approximately 3.6 ms with a peak of 32,629 bytes measured by `tracemalloc`. These figures describe a small static-rule regression suite, not LLM quality, whole-server RSS, or a measured speedup over the previous version.

Memory growth is bounded by source/response sizes, fixed worker concurrency, HTTP connection pools, four ingress slots by default, and lightweight list queries. No long-running production soak test was performed, and universal absence of memory leaks is not claimed. Disk history grows until explicit retention runs; reports are preserved by default.

**Real Mistral/Ollama requests and model quality have not been validated:** no cloud key was supplied and no local model weights were downloaded. Static checks and mocked clients do not replace that verification. Configure a local endpoint/model, or securely provide a Mistral key, enable cloud egress, and set prices/budgets before testing real AI behavior. Credential detection is heuristic and does not replace a data-handling policy.

GitHub Actions commands were exercised locally. Check the current remote runs separately in [GitHub Actions](https://github.com/Artem7898/ai_code_reviewer/actions). Cloud-environment publication and restoration in a new task were not validated by these checks.

For setup and safe upgrades, see [README](../README.md) and [Modernization](MODERNIZATION.md).
