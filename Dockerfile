FROM ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336 AS uv

FROM python:3.14.8-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS builder
COPY --from=uv /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
# Optional public CA bundle for corporate/cloud HTTPS proxies; never copy secrets.
RUN --mount=type=secret,id=build_ca \
    if [ -s /run/secrets/build_ca ]; then export SSL_CERT_FILE=/run/secrets/build_ca; fi; \
    uv lock --check --system-certs && uv sync --frozen --no-dev --no-editable --system-certs

FROM python:3.14.8-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    ENVIRONMENT=production \
    DATA_DIR=/data \
    DATABASE_PATH=/data/reviewer.sqlite3 \
    PROJECTS_ROOT=/projects
WORKDIR /app
RUN apt-get update \
    && apt-get install --no-install-recommends -y git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 reviewer \
    && useradd --uid 10001 --gid reviewer --no-create-home reviewer \
    && mkdir -p /data /projects \
    && chown reviewer:reviewer /data /projects
COPY --from=builder /app/.venv /app/.venv
COPY --chown=reviewer:reviewer app ./app
COPY --chown=reviewer:reviewer static ./static
COPY --chown=reviewer:reviewer admin_panel ./admin_panel
COPY --chown=reviewer:reviewer templates ./templates
USER reviewer
EXPOSE 8000 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).read()"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--limit-concurrency", "32"]
