"""HTTP regressions for authentication, isolation and immutable queued inputs."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlmodel import select

from app.auth import hash_password
from app.db import Database
from app.main import create_app
from app.models import User
from app.settings import Settings
from app.worker import Worker


@pytest.fixture
async def api(tmp_path: Path) -> AsyncIterator[tuple[httpx.AsyncClient, Database, Settings]]:
    settings = Settings(
        environment="test",
        data_dir=tmp_path / "data",
        database_path=tmp_path / "db.sqlite3",
        projects_root=tmp_path / "projects",
        ai_provider="static",
        ruff_enabled=False,
    )
    (settings.projects_root / "demo").mkdir(parents=True)
    (settings.projects_root / "demo" / "example.py").write_text("value = 1\n")
    database = Database(settings)
    application = create_app(settings, database)
    async with application.router.lifespan_context(application):
        async with database.session() as session:
            session.add(
                User(username="alice", hashed_password=await hash_password("test-password-alice"))
            )
            session.add(
                User(username="bob", hashed_password=await hash_password("test-password-bob"))
            )
            await session.commit()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://testserver"
        ) as client:
            yield client, database, settings


async def login(client: httpx.AsyncClient, username: str = "alice") -> dict[str, str]:
    response = await client.post(
        "/token", data={"username": username, "password": f"test-password-{username}"}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["access_token"]}


async def test_root_health_and_authentication(api: Any) -> None:
    client, database, _ = api
    root = await client.get("/")
    assert root.status_code == 200
    assert "script-src 'self'" in root.headers["content-security-policy"]
    assert "cdn." not in root.text
    assert (await client.get("/healthz")).json() == {"status": "ok"}
    assert (await client.get("/readyz")).status_code == 200
    assert (await client.get("/api/reports")).status_code == 401
    assert (await client.get("/api/jobs")).status_code == 401
    assert (
        await client.post("/token", data={"username": "admin", "password": "not-a-real-password"})
    ).status_code == 401
    assert (await client.get("/api/reports", headers=await login(client))).json() == []
    async with database.session() as session:
        assert (await session.exec(select(User).where(User.username == "admin"))).first() is None


@pytest.mark.parametrize(
    "project", ["../demo", "/tmp", "demo/../demo", "C:\\projects", "demo\\file"]
)
async def test_project_traversal_rejected(api: Any, project: str) -> None:
    client, _, _ = api
    headers = await login(client)
    assert (
        await client.post(
            "/api/scan-local-project",
            data={"project_name": project, "mode": "full"},
            headers=headers,
        )
    ).status_code == 400
    assert (
        await client.post(
            "/api/scaffold-app",
            data={"project_name": project, "description": "demo"},
            headers=headers,
        )
    ).status_code == 400


async def test_source_paths_and_symlinks_rejected(api: Any, tmp_path: Path) -> None:
    client, _, settings = api
    headers = await login(client)
    outside = tmp_path / "outside.py"
    outside.write_text("secret_value = 42\n")
    (settings.projects_root / "demo" / "link.py").symlink_to(outside)
    for path in (str(outside), "../outside.py", "link.py"):
        response = await client.post(
            "/api/generate-tests", data={"project_name": "demo", "file_path": path}, headers=headers
        )
        assert response.status_code == 400
    assert outside.read_text() == "secret_value = 42\n"
    assert not await asyncio.to_thread(lambda: list(tmp_path.glob("test_*.py")))


async def test_snapshot_dedup_owner_and_worker_roundtrip(api: Any) -> None:
    client, database, settings = api
    alice, bob = await login(client), await login(client, "bob")
    data = {"project_name": "demo", "mode": "full"}
    response = await client.post("/api/scan-local-project", data=data, headers=alice)
    assert response.status_code == 202, response.text
    job_id = response.json()["id"]
    assert "payload" not in response.json()
    assert (await client.post("/api/scan-local-project", data=data, headers=alice)).json()[
        "id"
    ] == job_id
    assert (await client.get(f"/api/jobs/{job_id}", headers=bob)).status_code == 404
    assert (await client.post(f"/api/jobs/{job_id}/cancel", headers=bob)).status_code == 404
    # Queued snapshots must not race subsequent source changes.
    (settings.projects_root / "demo" / "example.py").write_text("invalid syntax =\n")
    async with Worker(database, settings) as worker:
        assert await worker.run_once()
    result = (await client.get(f"/api/jobs/{job_id}", headers=alice)).json()
    assert result["status"] == "completed", result
    assert result["result"]["findings"] == []
    summaries = (await client.get("/api/jobs", headers=alice)).json()
    assert summaries[0]["id"] == job_id
    assert summaries[0]["result"] is None
    assert (await client.get(f"/api/jobs/{job_id}/sarif", headers=alice)).json()[
        "version"
    ] == "2.1.0"
    assert (await client.get(f"/api/jobs/{job_id}/sarif", headers=bob)).status_code == 404
    assert len((await client.get("/api/reports", headers=alice)).json()) == 1
    assert (await client.get("/api/reports", headers=bob)).json() == []


async def test_parallel_uploads_do_not_collide_or_leave_files(api: Any) -> None:
    client, database, settings = api
    headers = await login(client)
    results = await asyncio.gather(
        *[
            client.post(
                "/api/upload-and-review",
                files={"file": ("same.py", f"value = {index}\n")},
                headers=headers,
            )
            for index in range(5)
        ]
    )
    assert all(response.status_code == 202 for response in results), [
        response.text for response in results
    ]
    assert len({response.json()["id"] for response in results}) == 5
    assert not list((settings.data_dir / "uploads").iterdir())
    async with Worker(database, settings) as worker:
        for _ in results:
            assert await worker.run_once()
    assert len((await client.get("/api/reports", headers=headers)).json()) == 5


async def test_upload_traversal_never_overwrites(api: Any, tmp_path: Path) -> None:
    client, _, _ = api
    victim = tmp_path / "victim.py"
    victim.write_text("original\n")
    response = await client.post(
        "/api/upload-and-review",
        files={"file": ("../../victim.py", "changed")},
        headers=await login(client),
    )
    assert response.status_code == 400
    assert victim.read_text() == "original\n"


async def test_cancelled_job_not_executed_and_limits_validated(api: Any) -> None:
    client, database, settings = api
    headers = await login(client)
    response = await client.post(
        "/api/scan-local-project", data={"project_name": "demo", "mode": "full"}, headers=headers
    )
    job_id = response.json()["id"]
    assert (await client.post(f"/api/jobs/{job_id}/cancel", headers=headers)).json()[
        "status"
    ] == "cancelled"
    async with Worker(database, settings) as worker:
        assert not await worker.run_once()
    assert (await client.get("/api/reports?limit=10000", headers=headers)).status_code == 422
    assert (await client.get("/api/jobs?skip=-1", headers=headers)).status_code == 422


async def test_login_rate_and_body_limits(api: Any) -> None:
    client, _, settings = api
    for _ in range(settings.login_rate_limit):
        assert (
            await client.post("/token", data={"username": "missing", "password": "bad"})
        ).status_code == 401
    response = await client.post("/token", data={"username": "missing", "password": "bad"})
    assert response.status_code == 429
    assert response.headers["retry-after"] == "60"
    response = await client.post(
        "/token", content=b"x", headers={"Content-Length": str(settings.max_upload_bytes + 65537)}
    )
    assert response.status_code == 413


async def test_inaccessible_project_configuration_is_sanitized(
    api: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _, settings = api
    headers = await login(client)
    import os

    original = os.scandir

    def denied(path: Any) -> Any:
        if path == settings.projects_root:
            raise PermissionError("private server directory must not appear in HTTP errors")
        return original(path)

    monkeypatch.setattr(os, "scandir", denied)
    response = await client.get("/api/config", headers=headers)
    assert response.status_code == 503
    assert "private server" not in response.text
