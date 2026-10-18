"""Bounded asynchronous transports for local and explicitly enabled cloud AI."""

import asyncio
import json
import math
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from ipaddress import ip_address
from typing import Literal, Protocol, TypedDict, cast
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr


class AISettings(Protocol):
    @property
    def ai_provider(self) -> str: ...

    @property
    def ollama_base_url(self) -> str: ...

    @property
    def ollama_model(self) -> str: ...

    @property
    def mistral_api_key(self) -> SecretStr | None: ...

    @property
    def mistral_model(self) -> str: ...

    @property
    def allow_cloud_ai(self) -> bool: ...

    @property
    def ai_timeout_seconds(self) -> float: ...

    @property
    def ai_max_retries(self) -> int: ...

    @property
    def ai_max_response_bytes(self) -> int: ...

    @property
    def ai_max_output_tokens(self) -> int: ...

    @property
    def ai_max_input_tokens(self) -> int: ...

    @property
    def ai_max_cost_usd(self) -> float: ...

    @property
    def ai_input_cost_per_million(self) -> float: ...

    @property
    def ai_output_cost_per_million(self) -> float: ...


class ChatMessage(TypedDict):
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True, slots=True)
class Completion:
    content: str
    tokens_used: int
    model: str
    estimated_cost_usd: float = 0


class ProviderError(RuntimeError):
    """A provider failure; callers must persist a failed job, never a review."""


class ProviderConfigurationError(ProviderError):
    pass


class ProviderAuthenticationError(ProviderError):
    pass


class ProviderRateLimitError(ProviderError):
    pass


class ProviderTimeoutError(ProviderError):
    pass


class ProviderResponseError(ProviderError):
    pass


class ProviderBudgetError(ProviderError):
    pass


class ProviderPrivacyError(ProviderError):
    """Sensitive source must not leave the machine through a cloud provider."""


_SECRET_PREFIX = re.compile(
    r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"
    r"|\b(?:gh[pousr]_[A-Za-z0-9]{20,255}|github_pat_[A-Za-z0-9_]{20,255})\b"
    r"|\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"
    r"|\bAIza[A-Za-z0-9_-]{35}\b"
    r"|\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"
    r"|\bsk_live_[A-Za-z0-9]{20,}\b"
    r"|\bxox[baprs]-[A-Za-z0-9-]{10,}\b"
)
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"\b(?:[A-Za-z_]\w*_)?(?:api_?key|secret|secret_key|password|token)\b"
    r"\s*(?::[^\n=]{1,100})?=\s*(?:[rubf]{0,2})?"
    r"(?P<quote>['\"])(?P<value>[^'\"\r\n]{16,})(?P=quote)",
    re.IGNORECASE,
)


def _contains_credentials(text: str) -> bool:
    if _SECRET_PREFIX.search(text) or _CREDENTIAL_ASSIGNMENT.search(text):
        return True
    # Review inputs are JSON envelopes. Decode them before inspecting source so
    # escaped quotes/newlines cannot hide a literal credential assignment.
    try:
        decoded: object = json.loads(text)
    except ValueError, RecursionError:
        return False
    pending = [decoded]
    while pending:
        value = pending.pop()
        if isinstance(value, str):
            if _SECRET_PREFIX.search(value) or _CREDENTIAL_ASSIGNMENT.search(value):
                return True
        elif isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, dict):
            pending.extend(value.values())
    return False


def _mapping(value: object, message: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ProviderResponseError(message)
    return cast("Mapping[str, object]", value)


def _nonnegative_int(value: object, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProviderResponseError("Invalid token usage in provider response")
    return value


class AIProvider:
    """One pooled client per application lifespan; no source or secrets in logs."""

    def __init__(
        self,
        settings: AISettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        budget_reserver: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.settings = settings
        self._transport = transport
        self._budget_reserver = budget_reserver
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()
        self._budget_lock = asyncio.Lock()
        self._reserved_cost = 0.0
        self._closed = False
        self._cloud = settings.ai_provider == "mistral"
        if settings.ai_provider not in {"ollama", "mistral", "static"}:
            raise ProviderConfigurationError("Unknown AI provider")
        if self._cloud:
            if not settings.allow_cloud_ai or not settings.mistral_api_key:
                raise ProviderConfigurationError(
                    "Mistral requires explicit cloud consent and MISTRAL_API_KEY"
                )
            if (
                settings.ai_max_cost_usd <= 0
                or min(settings.ai_input_cost_per_million, settings.ai_output_cost_per_million) <= 0
            ):
                raise ProviderConfigurationError(
                    "Cloud AI requires a positive budget and explicit token prices"
                )
            self._url = "https://api.mistral.ai/v1/chat/completions"
            self.model = settings.mistral_model
            self._trust_env = True
        else:
            endpoint = urlsplit(settings.ollama_base_url)
            if (
                endpoint.scheme not in {"http", "https"}
                or not endpoint.hostname
                or endpoint.username
                or endpoint.password
                or endpoint.query
                or endpoint.fragment
            ):
                raise ProviderConfigurationError(
                    "OLLAMA_BASE_URL must be an HTTP(S) URL without credentials or query"
                )
            self._url = settings.ollama_base_url.rstrip("/") + "/chat/completions"
            self.model = settings.ollama_model
            # Bypass HTTP(S)_PROXY only for literal loopback hosts.
            try:
                loopback = ip_address(endpoint.hostname).is_loopback
            except ValueError:
                loopback = endpoint.hostname.rstrip(".").lower() == "localhost"
            self._trust_env = not loopback

    async def __aenter__(self) -> AIProvider:
        await self._get_client()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def _get_client(self) -> httpx.AsyncClient:
        async with self._client_lock:
            if self._closed:
                raise ProviderError("AI client is closed")
            if self._client is None:
                self._client = httpx.AsyncClient(
                    timeout=httpx.Timeout(
                        self.settings.ai_timeout_seconds,
                        connect=min(self.settings.ai_timeout_seconds, 10),
                    ),
                    limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
                    trust_env=self._trust_env,
                    follow_redirects=False,
                    transport=self._transport,
                )
            return self._client

    async def aclose(self) -> None:
        async with self._client_lock:
            self._closed = True
            if self._client is not None:
                await self._client.aclose()
                self._client = None

    async def _reserve(self, input_bound: int) -> float:
        if not self._cloud:
            return 0
        cost = (
            input_bound * self.settings.ai_input_cost_per_million
            + self.settings.ai_max_output_tokens * self.settings.ai_output_cost_per_million
        ) / 1_000_000
        async with self._budget_lock:
            # The application supplies a transactional UTC-day ledger shared by
            # all workers. Reserve before each attempt, including retries: an
            # ambiguous failed request may still be billed by the upstream.
            if self._budget_reserver is not None:
                await self._budget_reserver(cost)
            else:
                # Standalone transports retain an atomic process-lifetime cap.
                if self._reserved_cost + cost > self.settings.ai_max_cost_usd:
                    raise ProviderBudgetError("Configured cloud token budget would be exceeded")
                self._reserved_cost += cost
        return cost

    @staticmethod
    def _retry_delay(retry_after: str | None, attempt: int) -> float | None:
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                try:
                    when = parsedate_to_datetime(retry_after)
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=UTC)
                    delay = (when - datetime.now(UTC)).total_seconds()
                except TypeError, ValueError, OverflowError:
                    delay = 2**attempt
            if not math.isfinite(delay) or delay > 10:
                return None
            return max(0.0, delay)
        return min(float(2**attempt), 5.0)

    async def complete(self, messages: Sequence[ChatMessage]) -> Completion:
        if self.settings.ai_provider == "static":
            raise ProviderConfigurationError("Static mode does not generate model output")
        if self._cloud and any(_contains_credentials(message["content"]) for message in messages):
            raise ProviderPrivacyError(
                "Cloud request blocked: possible embedded credential; use local AI or remove credentials from source"
            )
        payload: dict[str, object] = {
            "model": self.model,
            "messages": list(messages),
            "temperature": 0,
            "max_tokens": self.settings.ai_max_output_tokens,
            "response_format": {"type": "json_object"},
        }
        # UTF-8 bytes conservatively bound input tokens including metadata.
        input_bound = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        if input_bound > self.settings.ai_max_input_tokens:
            raise ProviderBudgetError("Input exceeds the configured conservative token bound")
        headers: dict[str, str] = {"Accept-Encoding": "identity"}
        if self._cloud and self.settings.mistral_api_key is not None:
            headers["Authorization"] = "Bearer " + self.settings.mistral_api_key.get_secret_value()
        client = await self._get_client()
        total_reserved = 0.0
        for attempt in range(self.settings.ai_max_retries + 1):
            total_reserved += await self._reserve(input_bound)
            try:
                async with client.stream(
                    "POST", self._url, json=payload, headers=headers
                ) as response:
                    status = response.status_code
                    if status in {401, 403}:
                        raise ProviderAuthenticationError("AI provider rejected authentication")
                    if status in {408, 429, 500, 502, 503, 504}:
                        delay = self._retry_delay(response.headers.get("Retry-After"), attempt)
                        if attempt < self.settings.ai_max_retries and delay is not None:
                            await response.aclose()
                            await asyncio.sleep(delay)
                            continue
                        if status == 429:
                            raise ProviderRateLimitError("AI provider rate limit reached")
                        raise ProviderError(f"AI provider unavailable (HTTP {status})")
                    if status != 200:
                        raise ProviderError(f"AI provider request failed (HTTP {status})")
                    if (
                        response.headers.get("Content-Encoding", "identity").lower().strip()
                        != "identity"
                    ):
                        raise ProviderResponseError("Compressed AI responses are not accepted")
                    content_length = response.headers.get("Content-Length")
                    if content_length:
                        try:
                            if int(content_length) > self.settings.ai_max_response_bytes:
                                raise ProviderResponseError(
                                    "AI response exceeds the configured byte limit"
                                )
                        except ValueError as exc:
                            raise ProviderResponseError(
                                "Invalid AI response content length"
                            ) from exc
                    chunks = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(chunks) + len(chunk) > self.settings.ai_max_response_bytes:
                            raise ProviderResponseError(
                                "AI response exceeds the configured byte limit"
                            )
                        chunks.extend(chunk)
                try:
                    decoded: object = json.loads(chunks)
                except (ValueError, UnicodeDecodeError) as exc:
                    raise ProviderResponseError("AI provider returned invalid JSON") from exc
                data = _mapping(decoded, "AI response must be an object")
                choices = data.get("choices")
                if not isinstance(choices, list) or not choices:
                    raise ProviderResponseError("AI response has no completion")
                choice = _mapping(choices[0], "Invalid completion choice")
                if choice.get("finish_reason") == "length":
                    raise ProviderResponseError("AI response was truncated at its token limit")
                message = _mapping(choice.get("message"), "Invalid completion message")
                content = message.get("content")
                if not isinstance(content, str) or not content:
                    raise ProviderResponseError("AI response has empty content")
                usage = _mapping(data.get("usage", {}), "Invalid completion usage")
                input_tokens = _nonnegative_int(usage.get("prompt_tokens"), input_bound)
                output_tokens = _nonnegative_int(
                    usage.get("completion_tokens"), self.settings.ai_max_output_tokens
                )
                if input_tokens > input_bound or output_tokens > self.settings.ai_max_output_tokens:
                    raise ProviderResponseError(
                        "Provider token usage exceeds the reserved token limits"
                    )
                tokens = _nonnegative_int(usage.get("total_tokens"), input_tokens + output_tokens)
                if tokens != input_tokens + output_tokens:
                    raise ProviderResponseError("Provider returned inconsistent token accounting")
                return Completion(
                    content=content,
                    tokens_used=tokens,
                    model=self.model,
                    estimated_cost_usd=total_reserved,
                )
            except httpx.TimeoutException as exc:
                if attempt == self.settings.ai_max_retries:
                    raise ProviderTimeoutError("AI provider request timed out") from exc
            except httpx.TransportError as exc:
                if attempt == self.settings.ai_max_retries:
                    raise ProviderError("Could not connect to AI provider") from exc
            await asyncio.sleep(min(float(2**attempt), 5.0))
        raise ProviderError("AI provider retry limit exceeded")
