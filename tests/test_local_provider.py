from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from bots5.errors import ProviderError, ProviderHttpError, ProviderResponseError
from bots5.providers.base import CompletionRequest
from bots5.providers.openai_compatible import OpenAICompatibleProvider
from bots5.providers.openrouter import OpenRouterProvider


def req(*, reasoning_effort=None) -> CompletionRequest:
    return CompletionRequest(
        model="model-x",
        system="sys",
        user="user",
        temperature=0.1,
        max_output_tokens=100,
        timeout_seconds=1.0,
        reasoning_effort=reasoning_effort,
    )


def test_local_provider_builds_non_streaming_request_and_appends_endpoint():
    seen = {}

    async def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["json"] = request.read().decode()
        return httpx.Response(
            200,
            json={"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]},
        )

    provider = OpenAICompatibleProvider(
        "http://127.0.0.1:8000/v1/",
        _transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(provider.complete(req()))

    payload = json.loads(seen["json"])
    assert seen["url"] == "http://127.0.0.1:8000/v1/chat/completions"
    assert "authorization" not in seen["headers"]
    assert payload["model"] == "model-x"
    assert payload["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "user"},
    ]
    assert payload["temperature"] == 0.1
    assert payload["max_tokens"] == 100
    assert payload["stream"] is False
    assert result.output_text == "ok"


def test_local_provider_emits_explicit_reasoning_effort_without_changing_other_fields():
    seen = {}

    async def handler(request: httpx.Request):
        seen["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]},
        )

    provider = OpenAICompatibleProvider(
        "http://127.0.0.1:8000/v1", _transport=httpx.MockTransport(handler)
    )
    asyncio.run(provider.complete(req(reasoning_effort="none")))

    assert seen["payload"] == {
        "model": "model-x",
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "user"},
        ],
        "temperature": 0.1,
        "max_tokens": 100,
        "stream": False,
        "reasoning_effort": "none",
    }


def test_local_provider_uses_only_configured_auth_environment(monkeypatch):
    monkeypatch.setenv("BOTS5_LOCAL_KEY", "local-secret")
    seen = {}

    async def handler(request: httpx.Request):
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]},
        )

    provider = OpenAICompatibleProvider(
        "https://localhost:1234/api/v1",
        api_key_env="BOTS5_LOCAL_KEY",
        _transport=httpx.MockTransport(handler),
    )
    asyncio.run(provider.complete(req()))

    assert seen["authorization"] == "Bearer local-secret"


def test_local_provider_missing_configured_auth_fails_before_request(monkeypatch):
    monkeypatch.delenv("BOTS5_LOCAL_KEY", raising=False)
    with pytest.raises(ProviderError, match="BOTS5_LOCAL_KEY is not set"):
        OpenAICompatibleProvider("http://127.0.0.1:8000/v1", api_key_env="BOTS5_LOCAL_KEY")


@pytest.mark.parametrize(
    "base_url",
    [
        "ftp://127.0.0.1:8000/v1",
        "http://127.0.0.1:8000/v1?secret=not-allowed",
        "http://user:password@127.0.0.1:8000/v1",
        "not-a-url",
    ],
)
def test_local_provider_rejects_non_http_api_base(base_url):
    with pytest.raises(ProviderError, match="HTTP/HTTPS"):
        OpenAICompatibleProvider(base_url)


def test_local_provider_parses_usage_and_leaves_unknown_cost_unknown():
    async def handler(request: httpx.Request):
        return httpx.Response(
            200,
            json={
                "id": "local-request",
                "model": "returned-local",
                "choices": [{"finish_reason": "stop", "message": {"content": "ok"}}],
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 22,
                    "total_tokens": 33,
                    "completion_tokens_details": {"reasoning_tokens": 17},
                },
            },
        )

    provider = OpenAICompatibleProvider(
        "http://127.0.0.1:8000/v1", _transport=httpx.MockTransport(handler)
    )
    result = asyncio.run(provider.complete(req()))

    assert result.returned_model == "returned-local"
    assert result.request_id == "local-request"
    assert result.prompt_tokens == 11
    assert result.completion_tokens == 22
    assert result.reasoning_tokens == 17
    assert result.total_tokens == 33
    assert result.known_cost_usd is None
    assert not hasattr(result, "reasoning")
    assert not hasattr(result, "reasoning_details")


def test_local_provider_redacts_auth_from_http_error(monkeypatch):
    secret = "local-secret-value"
    monkeypatch.setenv("BOTS5_LOCAL_KEY", secret)

    async def handler(request: httpx.Request):
        return httpx.Response(401, text=f"failed Bearer {secret} {secret}")

    provider = OpenAICompatibleProvider(
        "http://127.0.0.1:8000/v1",
        api_key_env="BOTS5_LOCAL_KEY",
        _transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ProviderHttpError) as caught:
        asyncio.run(provider.complete(req()))
    assert secret not in str(caught.value)
    assert "[REDACTED]" in str(caught.value)


def test_local_provider_transport_and_json_errors_are_provider_errors():
    async def transport_error(request: httpx.Request):
        raise httpx.ConnectError("connection failed")

    provider = OpenAICompatibleProvider(
        "http://127.0.0.1:8000/v1",
        _transport=httpx.MockTransport(transport_error),
    )
    with pytest.raises(ProviderError, match="transport"):
        asyncio.run(provider.complete(req()))

    async def malformed(request: httpx.Request):
        return httpx.Response(200, content=b"not-json")

    provider = OpenAICompatibleProvider(
        "http://127.0.0.1:8000/v1", _transport=httpx.MockTransport(malformed)
    )
    with pytest.raises(ProviderResponseError, match="malformed"):
        asyncio.run(provider.complete(req()))


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            {"choices": [{"finish_reason": "stop", "message": {"content": "hello"}}]},
            ("ok", "hello", "stop"),
        ),
        (
            {"choices": [{"finish_reason": "length", "message": {"content": None}}]},
            ("ok", "", "length"),
        ),
        (
            {
                "choices": [
                    {"finish_reason": "content_filter", "message": {"content": None}}
                ]
            },
            ("ok", "", "content_filter"),
        ),
        (
            {"choices": [{"finish_reason": "stop", "message": {"content": None}}]},
            ("error", "empty_model_response"),
        ),
        (
            {"choices": [{"message": {"content": None}}]},
            ("error", "malformed_provider_response"),
        ),
        (
            {"choices": [{"finish_reason": {"unexpected": True}, "message": {"content": None}}]},
            ("error", "malformed_provider_response"),
        ),
        (
            {"choices": [{"finish_reason": "length", "message": {}}]},
            ("error", "malformed_provider_response"),
        ),
        (
            {"choices": [{"finish_reason": "length", "message": {"content": []}}]},
            ("error", "malformed_provider_response"),
        ),
        (
            {"choices": [{"finish_reason": "stop", "message": {"content": "  "}}]},
            ("error", "empty_model_response"),
        ),
    ],
)
def test_openrouter_and_local_normalization_parity(response, expected):
    async def handler(request: httpx.Request):
        return httpx.Response(200, json=response)

    providers = [
        OpenRouterProvider("router-secret", _transport=httpx.MockTransport(handler)),
        OpenAICompatibleProvider(
            "http://127.0.0.1:8000/v1", _transport=httpx.MockTransport(handler)
        ),
    ]
    observations = []
    for provider in providers:
        try:
            result = asyncio.run(provider.complete(req()))
        except ProviderResponseError as exc:
            observations.append(("error", str(exc)))
        else:
            observations.append(("ok", result.output_text, result.finish_reason))

    assert observations == [expected, expected]
