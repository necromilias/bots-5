from __future__ import annotations

import re
import time
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from ..errors import ProviderError, ProviderHttpError, ProviderResponseError
from .base import CompletionRequest, CompletionResult


_BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+")


def _optional_int(value: Any) -> int | None:
    if type(value) is int and value >= 0:
        return value
    return None


def _optional_cost(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not result.is_finite() or result < 0:
        return None
    return result


class OpenRouterProvider:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://openrouter.ai/api/v1",
        *,
        _transport: httpx.AsyncBaseTransport | None = None,
    ):
        if not api_key:
            raise ProviderError("OPENROUTER_API_KEY is not set")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._transport = _transport

    def _sanitize(self, text: str) -> str:
        text = text.replace(self._api_key, "[REDACTED]")
        text = _BEARER_RE.sub("Bearer [REDACTED]", text)
        return text[:500]

    def _normalize(
        self,
        data: Any,
        *,
        requested_model: str,
        duration_seconds: float,
    ) -> CompletionResult:
        if type(data) is not dict:
            raise ProviderResponseError("malformed_provider_response")
        choices = data.get("choices")
        if type(choices) is not list or not choices:
            raise ProviderResponseError("empty_model_response")
        first = choices[0]
        if type(first) is not dict:
            raise ProviderResponseError("malformed_provider_response")
        message = first.get("message")
        if type(message) is not dict:
            raise ProviderResponseError("malformed_provider_response")
        content = message.get("content")
        if type(content) is not str:
            raise ProviderResponseError("malformed_provider_response")
        if not content.strip():
            raise ProviderResponseError("empty_model_response")

        usage = data.get("usage")
        if type(usage) is not dict:
            usage = {}
        details = usage.get("completion_tokens_details")
        if type(details) is not dict:
            details = {}

        returned_model = data.get("model") if type(data.get("model")) is str else None
        request_id = data.get("id") if type(data.get("id")) is str else None
        finish_reason = first.get("finish_reason")
        if type(finish_reason) is not str:
            finish_reason = None

        return CompletionResult(
            output_text=content,
            requested_model=requested_model,
            finish_reason=finish_reason,
            returned_model=returned_model,
            request_id=request_id,
            prompt_tokens=_optional_int(usage.get("prompt_tokens")),
            completion_tokens=_optional_int(usage.get("completion_tokens")),
            reasoning_tokens=_optional_int(details.get("reasoning_tokens")),
            total_tokens=_optional_int(usage.get("total_tokens")),
            known_cost_usd=_optional_cost(usage.get("cost")),
            duration_seconds=duration_seconds,
        )

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        payload = {
            "model": request.model,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
            "temperature": request.temperature,
            "max_tokens": request.max_output_tokens,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(
                timeout=None,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    f"{self._base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                )
        except httpx.HTTPError as exc:
            raise ProviderError(f"provider_transport_error: {self._sanitize(str(exc))}") from None

        duration = time.monotonic() - started
        if not 200 <= response.status_code < 300:
            body = self._sanitize(response.text.replace("\n", " "))[:200]
            raise ProviderHttpError(
                response.status_code,
                f"provider_http_error status={response.status_code} body={body!r}",
            )
        try:
            data = response.json()
        except ValueError:
            raise ProviderResponseError("malformed_provider_response") from None
        return self._normalize(
            data,
            requested_model=request.model,
            duration_seconds=duration,
        )
