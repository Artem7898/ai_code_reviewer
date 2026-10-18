"""Bound ingress memory and time even when clients omit Content-Length."""

import asyncio
from collections import deque

import pytest
from starlette.types import Message, Receive, Scope, Send

from app.http_security import LoginLimiter, RequestBoundary


def http_scope() -> Scope:
    return {"type": "http", "method": "POST", "path": "/token", "headers": []}


async def status_for(boundary: RequestBoundary, receive: Receive) -> int:
    messages: list[Message] = []

    async def send(message: Message) -> None:
        messages.append(message)

    await boundary(http_scope(), receive, send)
    starts = [message for message in messages if message["type"] == "http.response.start"]
    assert len(starts) == 1
    return int(starts[0]["status"])


@pytest.mark.asyncio
async def test_chunked_body_limit_without_content_length() -> None:
    reached_application = False

    async def application(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal reached_application
        reached_application = True

    boundary = RequestBoundary(application, max_bytes=10)
    chunks: deque[Message] = deque(
        [
            {"type": "http.request", "body": b"12345", "more_body": True},
            {"type": "http.request", "body": b"67890", "more_body": True},
            {"type": "http.request", "body": b"1", "more_body": False},
        ]
    )

    async def receive() -> Message:
        return chunks.popleft()

    assert await status_for(boundary, receive) == 413
    assert not reached_application


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", [1, 4])
async def test_total_body_deadline_releases_slots_for_waiting_requests(concurrency: int) -> None:
    async def application(scope: Scope, receive: Receive, send: Send) -> None:
        assert (await receive())["body"] == b"ok"
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    boundary = RequestBoundary(
        application, max_bytes=10, timeout_seconds=0.05, concurrency=concurrency
    )
    all_receiving = asyncio.Event()
    never_complete = asyncio.Event()
    entered = 0

    async def stalled_receive() -> Message:
        nonlocal entered
        entered += 1
        if entered == concurrency:
            all_receiving.set()
        await never_complete.wait()
        return {"type": "http.request", "body": b"", "more_body": False}

    stalled = [
        asyncio.create_task(status_for(boundary, stalled_receive)) for _ in range(concurrency)
    ]
    await asyncio.wait_for(all_receiving.wait(), timeout=1)

    async def healthy_receive() -> Message:
        return {"type": "http.request", "body": b"ok", "more_body": False}

    healthy = asyncio.create_task(status_for(boundary, healthy_receive))
    assert await asyncio.wait_for(healthy, timeout=1) == 200
    assert await asyncio.gather(*stalled) == [408] * concurrency


def test_login_limiter_caps_addresses_and_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1000.0
    monkeypatch.setattr("app.http_security.time.monotonic", lambda: now)
    limiter = LoginLimiter(attempts=2, capacity=3)
    for index in range(100):
        assert limiter.consume(str(index))
        assert len(limiter.entries) <= 3
    assert limiter.consume("99")
    assert not limiter.consume("99")
    assert all(len(timestamps) <= 2 for timestamps in limiter.entries.values())
    now += 61
    assert limiter.consume("99")


@pytest.mark.asyncio
async def test_slow_drip_cannot_extend_total_body_deadline() -> None:
    reached_application = False

    async def application(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal reached_application
        reached_application = True

    boundary = RequestBoundary(application, max_bytes=100, timeout_seconds=0.05)
    chunks = 0

    async def receive() -> Message:
        nonlocal chunks
        await asyncio.sleep(0.015)
        chunks += 1
        return {"type": "http.request", "body": b"x", "more_body": chunks < 10}

    assert await status_for(boundary, receive) == 408
    assert chunks < 10 and not reached_application
