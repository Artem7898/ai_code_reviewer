"""Validated, shared configuration for the API, workers and Django UI."""

from __future__ import annotations

import os
import secrets
import stat
import time
from functools import cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _local_signing_key(directory: Path) -> str:
    """Create a private persistent key, safely sharing it between processes."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = directory / "signing.key"
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        try:
            with os.fdopen(descriptor, "w", encoding="ascii") as stream:
                stream.write(secrets.token_urlsafe(64))
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            target.unlink(missing_ok=True)
            raise

    # A second process may observe the file between exclusive creation and write.
    for _ in range(50):
        descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "r", encoding="ascii") as stream:
            attributes = os.fstat(stream.fileno())
            if not stat.S_ISREG(attributes.st_mode) or attributes.st_mode & 0o077:
                raise ValueError("Signing key must be a regular file with permissions 0600")
            value = stream.read(256).strip()
        if len(value) >= 32:
            return value
        time.sleep(0.02)
    raise ValueError("Persistent signing key is empty or invalid")


class Settings(BaseSettings):
    """Environment variables are case-insensitive; secret values never enter repr."""

    model_config = SettingsConfigDict(
        env_file=REPOSITORY_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        validate_default=True,
        hide_input_in_errors=True,
        allow_inf_nan=False,
    )

    environment: Literal["development", "test", "production"] = "development"
    data_dir: Path = REPOSITORY_ROOT / ".data"
    database_path: Path = REPOSITORY_ROOT / "database.db"
    projects_root: Path = REPOSITORY_ROOT / "projects"
    secret_key: SecretStr | None = Field(default=None, repr=False)
    access_token_expire_minutes: int = Field(default=30, ge=1, le=1440)
    jwt_issuer: str = "ai-code-reviewer"
    jwt_audience: str = "ai-code-reviewer"
    django_debug: bool = False
    allowed_hosts: list[str] = ["localhost", "127.0.0.1", "testserver"]
    cors_origins: list[str] = []

    ai_provider: Literal["ollama", "mistral", "static"] = "ollama"
    ollama_base_url: str = "http://127.0.0.1:11434/v1"
    ollama_model: str = "qwen2.5-coder:7b"
    mistral_api_key: SecretStr | None = Field(default=None, repr=False)
    mistral_model: str = "mistral-small-latest"
    allow_cloud_ai: bool = False
    ai_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    ai_max_retries: int = Field(default=2, ge=0, le=5)
    ai_max_response_bytes: int = Field(default=1_048_576, ge=1024, le=10_485_760)
    ai_max_output_tokens: int = Field(default=4096, ge=64, le=32768)
    ai_max_input_tokens: int = Field(default=24000, ge=128, le=1_000_000)
    ai_max_cost_usd: float = Field(default=0.0, ge=0)
    ai_input_cost_per_million: float = Field(default=0.0, ge=0)
    ai_output_cost_per_million: float = Field(default=0.0, ge=0)
    max_review_files: int = Field(default=100, ge=1, le=10000)
    max_review_chars: int = Field(default=200000, ge=1024, le=10_000_000)
    max_file_bytes: int = Field(default=1_048_576, ge=1024, le=10_485_760)
    max_upload_bytes: int = Field(default=10_485_760, ge=1024, le=104_857_600)
    max_pdf_pages: int = Field(default=50, ge=1, le=1000)
    ruff_enabled: bool = True
    read_only: bool = True

    worker_concurrency: int = Field(default=2, ge=1, le=16)
    job_lease_seconds: int = Field(default=180, ge=10, le=3600)
    job_timeout_seconds: float = Field(default=600, gt=0, le=3600)
    worker_poll_seconds: float = Field(default=1, gt=0, le=60)
    job_max_attempts: int = Field(default=3, ge=1, le=10)
    max_jobs_per_user: int = Field(default=10, ge=1, le=1000)
    cache_ttl_seconds: int = Field(default=3600, ge=0, le=604800)
    http_concurrency: int = Field(default=4, ge=1, le=16)
    login_rate_limit: int = Field(default=10, ge=1, le=100)

    @field_validator("data_dir", "database_path", "projects_root")
    @classmethod
    def absolute_paths(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    @model_validator(mode="after")
    def secure_signing_key(self) -> Settings:
        secret = self.secret_key.get_secret_value() if self.secret_key else ""
        if secret and (
            len(secret) < 32 or secret in {"dev-secret-key", "django-insecure-fallback-key"}
        ):
            raise ValueError("SECRET_KEY must contain at least 32 unpredictable characters")
        if not secret:
            if self.environment == "production":
                raise ValueError("Production requires an explicit SECRET_KEY")
            self.secret_key = SecretStr(_local_signing_key(self.data_dir))
        return self


@cache
def get_settings() -> Settings:
    return Settings()
