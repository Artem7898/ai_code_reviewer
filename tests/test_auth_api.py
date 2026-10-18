"""Credential reset and legacy rehash races exercise the real HTTP boundary."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import bcrypt
import httpx
import pytest
from sqlmodel import select

from app import main as api_module
from app.auth import hash_password, verify_password
from app.cli import create_user
from app.db import Database
from app.models import User
from app.settings import Settings


@pytest.fixture
async def authenticated_api(
    tmp_path: Path,
) -> AsyncIterator[tuple[httpx.AsyncClient, Database, Settings]]:
    settings = Settings(
        _env_file=None,
        environment="test",
        data_dir=tmp_path / "data",
        database_path=tmp_path / "private.db",
        projects_root=tmp_path / "projects",
        ai_provider="static",
        ruff_enabled=False,
    )
    database = Database(settings)
    application = api_module.create_app(settings, database)
    async with application.router.lifespan_context(application):
        async with database.session() as session:
            session.add(
                User(
                    username="alice", hashed_password=await hash_password("original-password-alice")
                )
            )
            await session.commit()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://testserver"
        ) as client:
            yield client, database, settings


async def token_response(client: httpx.AsyncClient, password: str) -> httpx.Response:
    return await client.post("/token", data={"username": "alice", "password": password})


async def test_cli_password_reset_revokes_old_http_token(
    authenticated_api: tuple[httpx.AsyncClient, Database, Settings],
) -> None:
    client, _, settings = authenticated_api
    response = await token_response(client, "original-password-alice")
    assert response.status_code == 200
    old = {"Authorization": "Bearer " + response.json()["access_token"]}
    assert (await client.get("/api/reports", headers=old)).status_code == 200
    await create_user(settings, "alice", "replacement-password-alice", reset_password=True)
    assert (await client.get("/api/reports", headers=old)).status_code == 401
    assert (await token_response(client, "original-password-alice")).status_code == 401
    fresh = await token_response(client, "replacement-password-alice")
    assert fresh.status_code == 200
    assert (
        await client.get(
            "/api/reports", headers={"Authorization": "Bearer " + fresh.json()["access_token"]}
        )
    ).status_code == 200


async def test_legacy_bcrypt_login_upgrades_hash_without_revoking_identity(
    authenticated_api: tuple[httpx.AsyncClient, Database, Settings],
) -> None:
    client, database, _ = authenticated_api
    async with database.session() as session:
        user = (await session.exec(select(User))).one()
        user.hashed_password = bcrypt.hashpw(
            b"original-password-alice", bcrypt.gensalt(rounds=4)
        ).decode()
        session.add(user)
        await session.commit()
    response = await token_response(client, "original-password-alice")
    assert response.status_code == 200
    async with database.session() as session:
        updated = (await session.exec(select(User))).one()
        assert updated.hashed_password.startswith("$argon2id$")
        assert updated.auth_revision == 1
    headers = {"Authorization": "Bearer " + response.json()["access_token"]}
    assert (await client.get("/api/reports", headers=headers)).status_code == 200


async def test_legacy_rehash_cannot_restore_password_after_concurrent_cli_reset(
    authenticated_api: tuple[httpx.AsyncClient, Database, Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, database, settings = authenticated_api
    async with database.session() as session:
        user = (await session.exec(select(User))).one()
        user.hashed_password = bcrypt.hashpw(
            b"original-password-alice", bcrypt.gensalt(rounds=4)
        ).decode()
        session.add(user)
        await session.commit()
    verified = asyncio.Event()
    continue_login = asyncio.Event()
    original_verify = api_module.verify_and_update_password

    async def paused_verify(password: str, encoded: str) -> tuple[bool, str | None]:
        outcome = await original_verify(password, encoded)
        assert outcome[0] and outcome[1] is not None
        verified.set()
        await continue_login.wait()
        return outcome

    monkeypatch.setattr(api_module, "verify_and_update_password", paused_verify)
    attempt = asyncio.create_task(token_response(client, "original-password-alice"))
    try:
        await asyncio.wait_for(verified.wait(), timeout=2)
        await create_user(settings, "alice", "replacement-password-alice", reset_password=True)
        continue_login.set()
        assert (await asyncio.wait_for(attempt, timeout=2)).status_code == 401
    finally:
        continue_login.set()
        if not attempt.done():
            attempt.cancel()
            await asyncio.gather(attempt, return_exceptions=True)
    monkeypatch.setattr(api_module, "verify_and_update_password", original_verify)
    async with database.session() as session:
        current = (await session.exec(select(User))).one()
        assert current.auth_revision == 2
        assert await verify_password("replacement-password-alice", current.hashed_password)
        assert not await verify_password("original-password-alice", current.hashed_password)
    assert (await token_response(client, "replacement-password-alice")).status_code == 200
