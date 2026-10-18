import asyncio
import json

import httpx
import pytest
from pydantic import SecretStr

from app.ai_client import (
    AIProvider,
    ProviderAuthenticationError,
    ProviderBudgetError,
    ProviderConfigurationError,
    ProviderError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
)
from app.settings import Settings


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None, data_dir=tmp_path, secret_key=SecretStr("r" * 64), ai_max_retries=0
    )


def completion(content='{"findings": [], "summary": "ok"}'):
    return {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
    }


async def test_local_transport_reuses_client_and_bypasses_proxy(settings):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=completion())

    async with AIProvider(settings, transport=httpx.MockTransport(handler)) as provider:
        first = await provider._get_client()
        a, b = await asyncio.gather(
            provider.complete([{"role": "user", "content": "a"}]),
            provider.complete([{"role": "user", "content": "b"}]),
        )
        assert await provider._get_client() is first
        assert provider._trust_env is False
        assert a.tokens_used == b.tokens_used == 20
    assert first.is_closed
    assert all(request.url.host == "127.0.0.1" for request in seen)
    assert all("authorization" not in request.headers for request in seen)
    with pytest.raises(ProviderError, match="closed"):
        await provider.complete([{"role": "user", "content": "again"}])


@pytest.mark.parametrize(
    "status,error",
    [
        (401, ProviderAuthenticationError),
        (403, ProviderAuthenticationError),
        (429, ProviderRateLimitError),
        (503, ProviderError),
    ],
)
async def test_http_errors_never_return_success(settings, status, error):
    async with AIProvider(
        settings,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(status, text="sensitive upstream body")
        ),
    ) as provider:
        with pytest.raises(error) as caught:
            await provider.complete([{"role": "user", "content": "review"}])
        assert "sensitive" not in str(caught.value)


async def test_rate_limit_retry_honors_retry_after_without_long_sleep(settings):
    count = 0

    def handler(_):
        nonlocal count
        count += 1
        return (
            httpx.Response(429, headers={"Retry-After": "0"})
            if count == 1
            else httpx.Response(200, json=completion())
        )

    settings.ai_max_retries = 1
    async with AIProvider(settings, transport=httpx.MockTransport(handler)) as provider:
        await provider.complete([{"role": "user", "content": "review"}])
    assert count == 2


async def test_long_retry_after_does_not_hammer_provider(settings):
    count = 0

    def handler(_):
        nonlocal count
        count += 1
        return httpx.Response(429, headers={"Retry-After": "120"})

    settings.ai_max_retries = 2
    async with AIProvider(settings, transport=httpx.MockTransport(handler)) as provider:
        with pytest.raises(ProviderRateLimitError):
            await provider.complete([{"role": "user", "content": "review"}])
    assert count == 1


async def test_timeout_is_typed(settings):
    def handler(request):
        raise httpx.ReadTimeout("secret request", request=request)

    async with AIProvider(settings, transport=httpx.MockTransport(handler)) as provider:
        with pytest.raises(ProviderTimeoutError, match="timed out"):
            await provider.complete([{"role": "user", "content": "review"}])


@pytest.mark.parametrize(
    "body", [b"invalid JSON", b"[]", b'{"choices": []}', json.dumps(completion("")).encode()]
)
async def test_invalid_or_empty_response_fails(settings, body):
    async with AIProvider(
        settings, transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))
    ) as provider:
        with pytest.raises(ProviderResponseError):
            await provider.complete([{"role": "user", "content": "review"}])


async def test_response_limit_and_input_limit(settings):
    settings.ai_max_response_bytes = 1024
    async with AIProvider(
        settings, transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 1025))
    ) as provider:
        with pytest.raises(ProviderResponseError, match="byte limit"):
            await provider.complete([{"role": "user", "content": "review"}])
        with pytest.raises(ProviderBudgetError):
            await provider.complete([{"role": "user", "content": "x" * 25000}])


def test_cloud_requires_consent_key_and_prices(settings):
    settings.ai_provider = "mistral"
    with pytest.raises(ProviderConfigurationError):
        AIProvider(settings)
    settings.allow_cloud_ai = True
    settings.mistral_api_key = SecretStr("test-key")
    with pytest.raises(ProviderConfigurationError, match="budget"):
        AIProvider(settings)


async def test_cloud_budget_reservation_is_atomic(settings):
    settings.ai_provider = "mistral"
    settings.allow_cloud_ai = True
    settings.mistral_api_key = SecretStr("test-key")
    settings.ai_input_cost_per_million = settings.ai_output_cost_per_million = 1
    settings.ai_max_cost_usd = 0.007
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=completion())

    async with AIProvider(settings, transport=httpx.MockTransport(handler)) as provider:
        assert provider._trust_env is True
        outcomes = await asyncio.gather(
            *(provider.complete([{"role": "user", "content": "review"}]) for _ in range(2)),
            return_exceptions=True,
        )
    assert sum(isinstance(item, ProviderBudgetError) for item in outcomes) == 1
    assert len(seen) == 1
    assert seen[0].headers["Authorization"] == "Bearer test-key"


async def test_compressed_response_is_rejected_before_decompression(settings):
    async with AIProvider(
        settings,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, headers={"Content-Encoding": "identity"}, json=completion()
            )
        ),
    ) as provider:
        assert (await provider.complete([{"role": "user", "content": "review"}])).tokens_used == 20

    async def compressed(_):
        # A stream keeps HTTPX from eagerly decoding a mocked response body.
        return httpx.Response(
            200, headers={"Content-Encoding": "gzip"}, stream=httpx.ByteStream(b"not-read")
        )

    async with AIProvider(settings, transport=httpx.MockTransport(compressed)) as provider:
        with pytest.raises(ProviderResponseError, match="Compressed"):
            await provider.complete([{"role": "user", "content": "review"}])


async def test_durable_budget_callback_reserves_before_every_attempt(settings):
    settings.ai_provider = "mistral"
    settings.allow_cloud_ai = True
    settings.mistral_api_key = SecretStr("test-key")
    settings.ai_input_cost_per_million = settings.ai_output_cost_per_million = 1
    settings.ai_max_cost_usd = 0.007
    settings.ai_max_retries = 1
    reservations = []
    requests = []

    async def reserve(amount):
        reservations.append(amount)

    def handler(request):
        requests.append(request)
        assert len(reservations) == len(requests)
        return (
            httpx.Response(429, headers={"Retry-After": "0"})
            if len(requests) == 1
            else httpx.Response(200, json=completion())
        )

    async with AIProvider(
        settings, transport=httpx.MockTransport(handler), budget_reserver=reserve
    ) as provider:
        result = await provider.complete([{"role": "user", "content": "review"}])
    assert len(reservations) == len(requests) == 2
    assert result.estimated_cost_usd == sum(reservations)


async def test_durable_budget_denial_prevents_network_request(settings):
    settings.ai_provider = "mistral"
    settings.allow_cloud_ai = True
    settings.mistral_api_key = SecretStr("test-key")
    settings.ai_input_cost_per_million = settings.ai_output_cost_per_million = 1
    settings.ai_max_cost_usd = 0.007
    requests = []

    async def deny(_):
        raise ProviderBudgetError("Daily account budget exhausted")

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=completion())

    async with AIProvider(
        settings, transport=httpx.MockTransport(handler), budget_reserver=deny
    ) as provider:
        with pytest.raises(ProviderBudgetError, match="Daily"):
            await provider.complete([{"role": "user", "content": "review"}])
    assert requests == []


@pytest.mark.parametrize(
    "source",
    [
        'API_KEY = "a-long-private-value-12345"\n',
        'api_key: str = "a-long-private-value-12345"\n',
        'service_password = "a-long-private-value-12345"\n',
        'secret = "-----BEGIN PRIVATE KEY-----\\nkeydata"\n',
        'client("ghp_abcdefghijklmnopqrstuv12345")\n',
        'client("sk-abcdefghijklmnopqrstuv12345")\n',
        'client("AKIA1234567890ABCDEF")\n',
    ],
)
async def test_cloud_secret_guard_prevents_egress_without_leaking_values(settings, source):
    from app.ai_client import ProviderPrivacyError

    settings.ai_provider = "mistral"
    settings.allow_cloud_ai = True
    settings.mistral_api_key = SecretStr("test-key")
    settings.ai_input_cost_per_million = settings.ai_output_cost_per_million = 1
    settings.ai_max_cost_usd = 1
    requests = []
    async with AIProvider(
        settings, transport=httpx.MockTransport(lambda request: requests.append(request))
    ) as provider:
        with pytest.raises(ProviderPrivacyError) as caught:
            await provider.complete(
                [
                    {
                        "role": "user",
                        "content": json.dumps({"files": [{"path": "code.py", "content": source}]}),
                    }
                ]
            )
    assert requests == []
    assert source not in str(caught.value)
    assert "use local AI" in str(caught.value)


async def test_normal_environment_bound_code_allowed_in_cloud(settings):
    settings.ai_provider = "mistral"
    settings.allow_cloud_ai = True
    settings.mistral_api_key = SecretStr("test-key")
    settings.ai_input_cost_per_million = settings.ai_output_cost_per_million = 1
    settings.ai_max_cost_usd = 1
    code = 'api_key = os.environ["MISTRAL_API_KEY"]\npassword = input()\n'
    async with AIProvider(
        settings, transport=httpx.MockTransport(lambda _: httpx.Response(200, json=completion()))
    ) as provider:
        await provider.complete(
            [
                {
                    "role": "user",
                    "content": json.dumps({"files": [{"path": "code.py", "content": code}]}),
                }
            ]
        )


async def test_local_provider_can_review_sensitive_source_privately(settings):
    code = 'API_KEY = "a-long-private-value-12345"\n'
    async with AIProvider(
        settings, transport=httpx.MockTransport(lambda _: httpx.Response(200, json=completion()))
    ) as provider:
        await provider.complete([{"role": "user", "content": code}])
