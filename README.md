# AI Code Reviewer — Python 3.14

**English** · [Русский](README.ru.md)

A local-first assistant for reviewing Python code: Git diff → AST/Ruff → optional language model → structured findings and SARIF. FastAPI provides the API and interface, a separate worker runs long-lived jobs, and Django displays review history.

Local Ollama is the default provider. Set `AI_PROVIDER=static` for deterministic checks without a model. Cloud Mistral processing requires explicit configuration. Projects are read-only: migration and test proposals are saved separately, and source files are never replaced automatically.

[Architecture and migration](docs/MODERNIZATION.md) · [Validation evidence and limitations](docs/VALIDATION.md) · [Branch protection](docs/BRANCH_PROTECTION.md) · [Документация на русском](README.ru.md)

## Installation

Requires Linux, Python **3.14.8**, Git, and [uv](https://docs.astral.sh/uv/). Dependency versions and hashes are locked in `uv.lock`. Run the following commands from the repository root.

```bash
uv sync --frozen
cp .env.example .env
mkdir -p projects
uv run --frozen python -m app.cli init-db
uv run --frozen python -m app.cli create-user reviewer --admin
uv run --frozen python admin_panel/manage.py migrate --noinput
uv run --frozen python admin_panel/manage.py createsuperuser
uv run --frozen python admin_panel/manage.py collectstatic --noinput
```

Account creation prompts for a password; no accounts are seeded. The API and Django use separate accounts. For API account automation, `--password-env VARIABLE` reads an existing environment variable instead of putting the password in command-line arguments.

Before upgrading an existing installation, back up its database and follow the [migration guide](docs/MODERNIZATION.md#upgrading-an-existing-installation). Migration disables the legacy automatically created API `admin` account. To reactivate it explicitly with a new password:

```bash
uv run --frozen python -m app.cli create-user admin --admin --reset-password
```

In development, the application creates a random signing key at `.data/signing.key` with permissions `0600`. Keep this file between runs. Production requires a separate random `SECRET_KEY` of at least 32 characters; `.env.example` includes a local generation command. Store the value in secure environment settings or a local `.env` file, and keep it out of Git.

## Running the services

Start these commands in three terminals:

```bash
uv run --frozen uvicorn app.main:app --host 127.0.0.1 --port 8000 --limit-concurrency 32
uv run --frozen python -m app.cli worker
uv run --frozen uvicorn admin_panel.asgi:application --app-dir admin_panel --host 127.0.0.1 --port 8080
```

The API interface is available on port 8000 and Django on port 8080. `/healthz` checks the API process; `/readyz` checks database access. Neither endpoint verifies model availability. `/stats/` requires a Django login. Public deployment requires HTTPS at a reverse proxy and an appropriate allowed-host configuration.

Place each repository in its own directory under `PROJECTS_ROOT`, for example `projects/my_project`. Symbolic links, hard links, absolute paths, `..` traversal, `.env`, and internal directories are excluded. Safe file access uses Linux file-descriptor operations, `openat`/`O_NOFOLLOW`, and `/proc`.

### Local model

Install [Ollama](https://ollama.com/), start it, and pull a model:

```bash
ollama pull qwen2.5-coder:7b
```

The application uses the OpenAI-compatible endpoint `http://127.0.0.1:11434/v1` and the model selected by `OLLAMA_MODEL`. Choose a model that fits available memory. The `static` provider runs AST/Ruff without network requests and does not generate code.

### Mistral and cloud budgets

In secure environment settings, configure `AI_PROVIDER=mistral`, `ALLOW_CLOUD_AI=true`, `MISTRAL_API_KEY`, a positive `AI_MAX_COST_USD`, and current `AI_INPUT_COST_PER_MILLION`/`AI_OUTPUT_COST_PER_MILLION` prices. Cloud processing is rejected without these settings. The selected source files are sent to the provider. HTTPX honors standard HTTPS proxy environment variables for cloud requests and verifies TLS.

The daily UTC job budget limits conservatively reserved request costs, including retries. Reservations are atomic across workers sharing the database and survive restarts. This is an estimate based on operator-supplied prices and token upper bounds, rather than provider billing. Also configure a financial limit with the provider. Installations using separate databases have separate budgets.

## Docker Compose

Create `.env` from the example, supply a random `SECRET_KEY`, set `PROJECTS_DIRECTORY`, and change `OLLAMA_BASE_URL` to `http://ollama:11434/v1` when using the bundled Ollama service.

```bash
docker compose --profile local-ai up --build -d
docker compose exec ollama ollama pull qwen2.5-coder:7b
docker compose exec app python -m app.cli create-user reviewer --admin
docker compose exec admin python admin_panel/manage.py createsuperuser
```

For an existing Ollama server, configure an endpoint reachable from the containers and run `docker compose up --build -d` without the profile. Inside a container, `localhost` refers to that container.

The init service prepares both database schemas and static assets before starting the API, admin, and worker. They share `/data/reviewer.sqlite3` in the `reviewer-data` volume. Application containers run as an unprivileged user with a read-only root filesystem; projects are mounted read-only. Published ports bind to `127.0.0.1`. `.env` is excluded from the image.

Project directory permissions must allow UID/GID `10001:10001` to read selected files and traverse directories, including `.git`. For restricted repositories, use an appropriate shared group or targeted ACLs. The application does not change repository permissions automatically.

Compose defaults to `development` for local HTTP access, with `DJANGO_DEBUG=false` and a required separate signing key. The image itself defaults to `production`. For public deployment, set `ENVIRONMENT=production` and configure HTTPS: Django enables HTTPS redirects and secure cookies. When a proxy terminates TLS, configure scheme-header trust only for a controlled proxy that strips spoofed incoming headers. The application does not automatically trust arbitrary `X-Forwarded-Proto` values.

`OLLAMA_IMAGE` allows a verified image version or digest to be pinned. The example's `latest` tag is intended for local use; external model images and weights are outside the Python lockfile.

Docker builds support standard proxy build arguments (`HTTP_PROXY`, `HTTPS_PROXY`, and lowercase equivalents). For an enterprise certificate authority, pass a public CA bundle through the BuildKit secret `build_ca`:

```bash
docker build -t ai-code-reviewer:local --secret id=build_ca,src=/path/to/ca-bundle.pem .
docker compose up --no-build -d
```

The bundle is used only while installing Python packages. TLS and hash verification remain enabled, and the bundle is not included in the image.

## Reviews and change proposals

The default review mode analyzes changes relative to a Git ref; a full scan requires explicit selection. File-count and byte limits bound the context. Exceeding them returns an error rather than silently truncating code. Each finding contains a rule, severity, relative path, line range, and an exact quote from the reviewed snapshot. Invalid locations and unsupported quotes are rejected.

Long-running operations return a job. The separate worker updates its status and progress, and the user can cancel it. Provider errors, invalid JSON, timeouts, and budget exhaustion fail the job without creating a successful report. Results and cache entries are scoped to their owner.

After signing in through `POST /token`, send `Authorization: Bearer …`. `POST /api/scan-local-project` accepts the form fields `project_name`, `mode=diff`, and `base_ref`, and returns HTTP 202 with the job `id`. Use `mode=full` for a full scan.

| Request | Result |
| --- | --- |
| `GET /api/jobs` | Current user's jobs |
| `GET /api/jobs/{id}` | Status, progress, result, or error |
| `POST /api/jobs/{id}/cancel` | Cancel a job |
| `GET /api/jobs/{id}/sarif` | SARIF for a completed review |
| `GET /api/jobs/{id}/artifacts` | Files from a completed proposal |

Migration, test generation, and scaffolding save proposals under `DATA_DIR/artifacts`. The server checks Python syntax and produces a unified diff. Proposals are not applied to source files automatically. Generated tests run only when supported isolation is available; `unavailable` means the tests **did not run**. Syntax validation does not establish behavioral correctness.

### CLI and pull-request integration

```bash
uv run --frozen python -m app.review_cli ./projects/my_project \
  --base-ref HEAD~1 --provider static --output review.json --sarif review.sarif
```

The CLI supports `static` and `ollama` providers. Cloud reviews use the authenticated worker API and its durable budget.

The `.github/workflows/review.yml` workflow reviews pull-request diffs without a model and uploads JSON/SARIF as workflow artifacts. It does not send source code to an external LLM or execute the reviewed code. GitHub Code Scanning upload is a separate integration that requires the appropriate `security-events: write` permission.

## Configuration

Environment variable names are case-insensitive; lists use JSON array syntax. All processes must share `DATABASE_PATH`, `DATA_DIR`, and the signing key.

| Variable | Default | Purpose |
| --- | --- | --- |
| `ENVIRONMENT` | `development` | `development`, `test`, or `production` |
| `DATABASE_PATH` | Repository `database.db` | Shared SQLite database; an absolute path is preferable |
| `DATA_DIR` | Repository `.data` | Development key, uploads, and artifacts |
| `PROJECTS_ROOT` | Repository `projects` | Allowed project directory |
| `AI_PROVIDER` | `ollama` | `ollama`, `mistral`, or `static` |
| `ALLOWED_HOSTS` | localhost, 127.0.0.1, testserver | Allowed API and Django hosts |
| `CORS_ORIGINS` | `[]` | Explicitly trusted origins |
| `MAX_REVIEW_FILES` | `100` | Maximum files in a snapshot |
| `MAX_REVIEW_CHARS` | `200000` | Review context limit |
| `MAX_FILE_BYTES` | `1048576` | Maximum individual file size |
| `MAX_UPLOAD_BYTES` | `10485760` | Maximum upload size |
| `MAX_PDF_PAGES` | `50` | Maximum PDF pages |
| `WORKER_CONCURRENCY` | `2` | Concurrent jobs per worker process |
| `HTTP_CONCURRENCY` | `4` | Concurrent HTTP handlers, including password hashing |
| `MAX_JOBS_PER_USER` | `10` | Maximum unfinished jobs per user |
| `JOB_TIMEOUT_SECONDS` | `600` | Job processing timeout |
| `CACHE_TTL_SECONDS` | `3600` | Result reuse lifetime |

See `app/settings.py` for the complete list and accepted ranges. Keep tokens, passwords, and keys out of logs and reports. Uploaded code and history are stored locally; configure backups, disk access, and retention to match your requirements.

To remove one bounded batch of old completed, failed, or cancelled jobs:

```bash
uv run --frozen python -m app.cli prune --days 30 --limit 100
```

This removes their snapshots, results, and artifacts while preserving report history. Add `--include-reports` explicitly to delete associated reports. Active jobs are preserved. Cleanup is not scheduled automatically.

## Validation and dependency updates

```bash
uv run --frozen ruff check .
uv run --frozen mypy
PYTHONPATH=admin_panel:. uv run --frozen mypy --config-file admin_panel/mypy.ini
uv run --frozen pytest -q
uv run --frozen python admin_panel/manage.py check
uv run --frozen python admin_panel/manage.py test core --noinput
uv run --frozen python -m app.evaluate
uv run --frozen pip-audit --require-hashes --no-deps -r requirements.txt
```

Mypy runs in strict mode for `app` and separately for Django with django-stubs. Tests cover authentication, file access boundaries, provider failures, owner isolation, and competing workers. The benchmark uses a small labeled AST regression dataset; its precision/recall apply only to that dataset and do not measure LLM quality. See [validation evidence](docs/VALIDATION.md) ([русский](docs/ru/VALIDATION.md)) for recorded results and remaining limitations.

To run the real Docker sandbox integration tests, preload `python:3.14.8-slim`, then run:

```bash
RUN_DOCKER_TESTS=1 uv run --frozen pytest -q tests/test_sandbox.py
```

These checks cover disabled networking, an unprivileged user, no inherited secrets, read-only source access, and timeout cleanup. The main CI suite does not require a Docker daemon.

`requirements.txt` exports hashed runtime dependencies for tools that require pip. Update dependencies deliberately:

```bash
uv lock --upgrade
uv sync --frozen
uv export --frozen --no-dev --no-emit-project --format requirements-txt --output-file requirements.txt
```

Run the validation commands after updating. Architecture, fixes, migration, customization profiles, and operating limits are documented in [docs/MODERNIZATION.md](docs/MODERNIZATION.md) ([русский](docs/ru/MODERNIZATION.md)).
