from __future__ import annotations

import asyncio
from decimal import Decimal

import httpx
import pytest

from bots5.errors import ProviderHttpError, ProviderResponseError
from bots5.providers.base import CompletionRequest
from bots5.providers.openrouter import OpenRouterProvider


def req():
    return CompletionRequest(
        model="model-x",
        system="sys",
        user="user",
        temperature=0.1,
        max_output_tokens=100,
        timeout_seconds=1.0,
    )


def test_provider_success_normalization():
    async def handler(request):
        return httpx.Response(
            200,
            json={
                "id": "abc",
                "model": "returned-x",
                "choices": [{"finish_reason": "stop", "message": {"content": "hello"}}],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 2,
                    "total_tokens": 3,
                    "completion_tokens_details": {"reasoning_tokens": 1},
                    "cost": "0.0012",
                },
            },
        )

    provider = OpenRouterProvider("secret", _transport=httpx.MockTransport(handler))
    result = asyncio.run(provider.complete(req()))
    assert result.output_text == "hello"
    assert result.finish_reason == "stop"
    assert result.known_cost_usd == Decimal("0.0012")
    assert result.reasoning_tokens == 1


@pytest.mark.parametrize(
    ("provider_reason", "normalized_reason"),
    [
        ("length", "length"),
        ("content_filter", "content_filter"),
        ("refusal", "refusal"),
        ("future_reason", "future_reason"),
        (None, None),
        ({"malformed": True}, None),
    ],
)
def test_provider_finish_reason_normalization(provider_reason, normalized_reason):
    async def handler(request):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": provider_reason,
                        "message": {"content": "hello"},
                    }
                ]
            },
        )

    provider = OpenRouterProvider("secret", _transport=httpx.MockTransport(handler))
    result = asyncio.run(provider.complete(req()))
    assert result.finish_reason == normalized_reason


def test_provider_non_2xx_is_sanitized():
    async def handler(request):
        return httpx.Response(500, text="failed Bearer secret")

    provider = OpenRouterProvider("secret", _transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderHttpError) as caught:
        asyncio.run(provider.complete(req()))
    assert "secret" not in str(caught.value)
    assert "[REDACTED]" in str(caught.value)


def test_provider_malformed_response():
    async def handler(request):
        return httpx.Response(200, content=b"not json")

    provider = OpenRouterProvider("secret", _transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderResponseError, match="malformed"):
        asyncio.run(provider.complete(req()))


def test_provider_empty_response():
    async def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "   "}}]})

    provider = OpenRouterProvider("secret", _transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderResponseError, match="empty"):
        asyncio.run(provider.complete(req()))
