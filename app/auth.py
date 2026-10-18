"""Argon2 password hashing and narrowly scoped, expiring JWT credentials."""

from __future__ import annotations

import asyncio
import secrets
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from jwt import InvalidTokenError
from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError
from pwdlib.hashers.argon2 import Argon2Hasher
from pwdlib.hashers.bcrypt import BcryptHasher

from .models import User
from .settings import Settings

# Bcrypt remains verification-only for preserved legacy credentials. New hashes use
# Argon2id with OWASP's memory setting; bound concurrent hashes to cap memory usage.
_password_hash = PasswordHash(
    (Argon2Hasher(memory_cost=19456, time_cost=2, parallelism=1), BcryptHasher())
)
_hash_slots = threading.BoundedSemaphore(2)


def _hash_password(password: str) -> str:
    with _hash_slots:
        return _password_hash.hash(password)


async def hash_password(password: str) -> str:
    if not 12 <= len(password) <= 1024:
        raise ValueError("Password must contain between 12 and 1024 characters")
    return await asyncio.to_thread(_hash_password, password)


def _verify_password(password: str, hashed_password: str) -> bool:
    if not password or len(password) > 1024:
        return False
    try:
        with _hash_slots:
            return _password_hash.verify(password, hashed_password)
    except UnknownHashError, ValueError, TypeError:
        return False


async def verify_password(password: str, hashed_password: str) -> bool:
    return await asyncio.to_thread(_verify_password, password, hashed_password)


def _verify_and_update_password(password: str, hashed_password: str) -> tuple[bool, str | None]:
    if not password or len(password) > 1024:
        return False, None
    try:
        with _hash_slots:
            return _password_hash.verify_and_update(password, hashed_password)
    except UnknownHashError, ValueError, TypeError:
        return False, None


async def verify_and_update_password(
    password: str, hashed_password: str
) -> tuple[bool, str | None]:
    """Upgrade successfully verified legacy bcrypt hashes to Argon2id."""
    return await asyncio.to_thread(_verify_and_update_password, password, hashed_password)


async def dummy_password_hash() -> str:
    """Prepare once during lifespan and verify it for unknown usernames."""
    return await hash_password(secrets.token_urlsafe(32))


def _secret(settings: Settings) -> str:
    if settings.secret_key is None:
        raise ValueError("Signing key has not been configured")
    return settings.secret_key.get_secret_value()


def create_access_token(user: User, settings: Settings) -> str:
    if user.id is None or not user.is_active or user.auth_revision < 1:
        raise ValueError("Tokens require an active, persisted user")
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": str(user.id),
            "ver": user.auth_revision,
            "iat": now,
            "nbf": now,
            "exp": now + timedelta(minutes=settings.access_token_expire_minutes),
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "jti": secrets.token_urlsafe(16),
        },
        _secret(settings),
        algorithm="HS256",
    )


@dataclass(frozen=True, slots=True)
class TokenIdentity:
    user_id: int
    auth_revision: int


def decode_identity(token: str, settings: Settings) -> TokenIdentity:
    payload = jwt.decode(
        token,
        _secret(settings),
        algorithms=["HS256"],
        audience=settings.jwt_audience,
        issuer=settings.jwt_issuer,
        options={"require": ["sub", "ver", "exp", "iat", "nbf", "iss", "aud", "jti"]},
    )
    subject = payload.get("sub")
    if not isinstance(subject, str) or not subject.isdecimal() or int(subject) < 1:
        raise InvalidTokenError("Invalid token subject")
    revision = payload.get("ver")
    if type(revision) is not int or revision < 1:
        raise InvalidTokenError("Invalid token authorization revision")
    return TokenIdentity(int(subject), revision)


def decode_access_token(token: str, settings: Settings) -> int:
    """Decode the subject; request authentication must also compare auth_revision."""
    return decode_identity(token, settings).user_id
