"""Bounded HTTP ingress and login throttling, independent of application state."""

import asyncio
import time
from collections import OrderedDict, deque

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class LoginLimiter:
    def __init__(self, attempts: int, capacity: int = 1024) -> None:
        self.attempts = attempts
        self.capacity = capacity
        self.entries: OrderedDict[str, deque[float]] = OrderedDict()

    def consume(self, address: str) -> bool:
        now = time.monotonic()
        entries = self.entries.setdefault(address, deque(maxlen=self.attempts))
        while entries and entries[0] <= now - 60:
            entries.popleft()
        self.entries.move_to_end(address)
        if len(self.entries) > self.capacity:
            self.entries.popitem(last=False)
        if len(entries) >= self.attempts:
            return False
        entries.append(now)
        return True


class RequestBoundary:
    """Count streamed bodies before parsing forms and bound ingress memory."""

    def __init__(
        self, app: ASGIApp, max_bytes: int, timeout_seconds: float = 30, concurrency: int = 4
    ) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.timeout_seconds = timeout_seconds
        self.slots = asyncio.Semaphore(concurrency)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        length = dict(scope["headers"]).get(b"content-length", b"0")
        try:
            oversized = int(length) > self.max_bytes or int(length) < 0
        except ValueError:
            oversized = True
        if oversized:
            await JSONResponse({"detail": "Request body exceeds limit"}, 413)(scope, receive, send)
            return
        async with self.slots:
            chunks = bytearray()
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            return
                        chunks.extend(message.get("body", b""))
                        if len(chunks) > self.max_bytes:
                            await JSONResponse({"detail": "Request body exceeds limit"}, 413)(
                                scope, receive, send
                            )
                            return
                        if not message.get("more_body", False):
                            break
            except TimeoutError:
                await JSONResponse({"detail": "Request body deadline exceeded"}, 408)(
                    scope, receive, send
                )
                return
            body = bytes(chunks)
            del chunks
            delivered = False

            async def replay() -> Message:
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            async def secure_send(message: Message) -> None:
                if message["type"] == "http.response.start":
                    headers = [
                        (b"x-content-type-options", b"nosniff"),
                        (b"referrer-policy", b"no-referrer"),
                        (b"cache-control", b"no-store"),
                        (b"x-frame-options", b"DENY"),
                        (
                            b"content-security-policy",
                            b"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
                        ),
                    ]
                    message = {**message, "headers": [*message.get("headers", []), *headers]}
                await send(message)

            await self.app(scope, replay, secure_send)
