from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import bcrypt
import jwt
import pytest
from pydantic import SecretStr, ValidationError

from app.auth import (
    create_access_token,
    decode_access_token,
    decode_identity,
    hash_password,
    verify_and_update_password,
    verify_password,
)
from app.models import User
from app.settings import Settings


def configuration(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        database_path=tmp_path / "database.db",
        projects_root=tmp_path / "projects",
        secret_key=SecretStr("test-signing-key-which-is-at-least-32-characters"),
        **overrides,
    )


@pytest.mark.asyncio
async def test_argon2_password_hash_and_legacy_bcrypt_verification() -> None:
    password = "a-long-test-passphrase"
    hashed = await hash_password(password)
    assert hashed.startswith("$argon2id$")
    assert await verify_password(password, hashed)
    assert not await verify_password("incorrect-password", hashed)
    assert not await verify_password(password, "invalid-hash")
    legacy = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=4)).decode()
    assert await verify_password(password, legacy)
    verified, upgraded = await verify_and_update_password(password, legacy)
    assert verified and upgraded is not None and upgraded.startswith("$argon2id$")
    with pytest.raises(ValueError, match="12"):
        await hash_password("short")


def test_tokens_require_claims_signature_and_active_user(tmp_path: Path) -> None:
    settings = configuration(tmp_path)
    user = User(id=7, username="reviewer", hashed_password="not-used")
    token = create_access_token(user, settings)
    assert decode_access_token(token, settings) == 7
    invalid = jwt.encode(
        {"sub": "7"}, "another-key-long-enough-for-hmac-signature", algorithm="HS256"
    )
    with pytest.raises(jwt.InvalidTokenError):
        decode_access_token(invalid, settings)
    expired = jwt.decode(token, options={"verify_signature": False})
    expired["exp"] = datetime.now(UTC) - timedelta(minutes=1)
    assert settings.secret_key is not None
    with pytest.raises(jwt.ExpiredSignatureError):
        decode_access_token(
            jwt.encode(expired, settings.secret_key.get_secret_value(), algorithm="HS256"), settings
        )
    user.is_active = False
    with pytest.raises(ValueError, match="active"):
        create_access_token(user, settings)


def test_production_requires_strong_explicit_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SECRET_KEY", raising=False)
    with pytest.raises(ValidationError, match="explicit SECRET_KEY"):
        Settings(_env_file=None, environment="production", data_dir=tmp_path)
    with pytest.raises(ValidationError, match="32"):
        Settings(_env_file=None, secret_key="dev-secret-key", data_dir=tmp_path)


@pytest.mark.asyncio
async def test_local_signing_key_is_persistent_private_and_race_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SECRET_KEY", raising=False)

    def load() -> Settings:
        return Settings(_env_file=None, data_dir=tmp_path, environment="test")

    settings = await asyncio.gather(*(asyncio.to_thread(load) for _ in range(8)))
    assert len({item.secret_key.get_secret_value() for item in settings if item.secret_key}) == 1
    assert os.stat(tmp_path / "signing.key").st_mode & 0o777 == 0o600
    assert "signing-key" not in repr(settings[0])


@pytest.mark.parametrize("value", [float("inf"), float("nan"), float("-inf")])
def test_budgets_reject_non_finite_settings(tmp_path: Path, value: float) -> None:
    with pytest.raises(ValidationError, match="finite"):
        configuration(tmp_path, ai_max_cost_usd=value)


def test_identity_requires_authorization_revision(tmp_path: Path) -> None:
    settings = configuration(tmp_path)
    user = User(id=3, username="owner", hashed_password="not-used")
    token = create_access_token(user, settings)
    identity = decode_identity(token, settings)
    assert identity.user_id == user.id and identity.auth_revision == user.auth_revision
    user.auth_revision += 1
    assert identity.auth_revision != user.auth_revision
    claims = jwt.decode(token, options={"verify_signature": False})
    del claims["ver"]
    assert settings.secret_key is not None
    legacy = jwt.encode(claims, settings.secret_key.get_secret_value(), algorithm="HS256")
    with pytest.raises(jwt.MissingRequiredClaimError):
        decode_identity(legacy, settings)
    claims["ver"] = True
    malformed = jwt.encode(claims, settings.secret_key.get_secret_value(), algorithm="HS256")
    with pytest.raises(jwt.InvalidTokenError, match="revision"):
        decode_identity(malformed, settings)
