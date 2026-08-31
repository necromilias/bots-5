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


@pytest.mark.parametrize("finish_reason", ["length", "content_filter"])
def test_provider_null_content_with_incomplete_finish_reason_preserves_metadata(
    finish_reason,
):
    async def handler(request):
        return httpx.Response(
            200,
            json={
                "id": "req-null",
                "model": "returned-model",
                "choices": [
                    {
                        "finish_reason": finish_reason,
                        "message": {
                            "content": None,
                            "reasoning": "must not become output",
                            "reasoning_details": [{"type": "reasoning"}],
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 22,
                    "total_tokens": 33,
                    "completion_tokens_details": {"reasoning_tokens": 17},
                    "cost": "0.0042",
                },
            },
        )

    provider = OpenRouterProvider("secret", _transport=httpx.MockTransport(handler))
    result = asyncio.run(provider.complete(req()))

    assert result.output_text == ""
    assert result.finish_reason == finish_reason
    assert result.returned_model == "returned-model"
    assert result.request_id == "req-null"
    assert result.prompt_tokens == 11
    assert result.completion_tokens == 22
    assert result.reasoning_tokens == 17
    assert result.total_tokens == 33
    assert result.known_cost_usd == Decimal("0.0042")
    assert not hasattr(result, "reasoning")
    assert not hasattr(result, "reasoning_details")


def test_provider_null_content_with_stop_is_empty_response():
    async def handler(request):
        return httpx.Response(
            200,
            json={"choices": [{"finish_reason": "stop", "message": {"content": None}}]},
        )

    provider = OpenRouterProvider("secret", _transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderResponseError, match="empty"):
        asyncio.run(provider.complete(req()))


@pytest.mark.parametrize(
    "choice",
    [
        {"message": {"content": None}},
        {"finish_reason": None, "message": {"content": None}},
        {"finish_reason": {"unexpected": True}, "message": {"content": None}},
    ],
)
def test_provider_null_content_without_string_finish_reason_is_malformed(choice):
    async def handler(request):
        return httpx.Response(200, json={"choices": [choice]})

    provider = OpenRouterProvider("secret", _transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderResponseError, match="malformed"):
        asyncio.run(provider.complete(req()))


def test_provider_missing_content_key_is_malformed_even_with_incomplete_finish():
    async def handler(request):
        return httpx.Response(
            200,
            json={"choices": [{"finish_reason": "length", "message": {}}]},
        )

    provider = OpenRouterProvider("secret", _transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderResponseError, match="malformed"):
        asyncio.run(provider.complete(req()))


@pytest.mark.parametrize("content", [[{"type": "text", "text": "ok"}], {"text": "ok"}, 3, 3.5, True])
def test_provider_unsupported_non_string_content_is_malformed(content):
    async def handler(request):
        return httpx.Response(
            200,
            json={"choices": [{"finish_reason": "length", "message": {"content": content}}]},
        )

    provider = OpenRouterProvider("secret", _transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderResponseError, match="malformed"):
        asyncio.run(provider.complete(req()))
