from __future__ import annotations

import os
import json
import re
import time
from collections.abc import AsyncIterator
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from ..core.urls import canonical_http_base_url
from ..errors import ProviderError, ProviderHttpError, ProviderResponseError
from .base import CompletionRequest, CompletionResult, CompletionStreamEvent


_BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+")
_SQLITE_INTEGER_MAX = 2**63 - 1


def _optional_int(value: Any) -> int | None:
    if type(value) is int and 0 <= value <= _SQLITE_INTEGER_MAX:
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


def _validated_base_url(value: str) -> str:
    return canonical_http_base_url(
        value,
        error_type=ProviderError,
        error_message="local_openai base_url must be an HTTP/HTTPS API base URL",
    )


class OpenAICompatibleProvider:
    """Built-in provider for an operator-supplied OpenAI-compatible API."""

    def __init__(
        self,
        base_url: str,
        api_key_env: str | None = None,
        *,
        _transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._base_url = _validated_base_url(base_url)
        if api_key_env is not None and (type(api_key_env) is not str or not api_key_env.strip()):
            raise ProviderError(
                "local_openai api_key_env must be a non-empty environment variable name"
            )
        self._api_key_env = api_key_env
        self._api_key = None
        if api_key_env is not None:
            api_key = os.environ.get(api_key_env)
            if not api_key:
                raise ProviderError(f"{api_key_env} is not set")
            self._api_key = api_key
        self._transport = _transport

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def api_key_env(self) -> str | None:
        return self._api_key_env

    def _sanitize(self, text: str) -> str:
        if self._api_key:
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

        finish_reason = first.get("finish_reason")
        if type(finish_reason) is not str:
            finish_reason = None

        if "content" not in message:
            raise ProviderResponseError("malformed_provider_response")
        content = message["content"]
        if content is None:
            if finish_reason is None:
                raise ProviderResponseError("malformed_provider_response")
            if finish_reason == "stop":
                raise ProviderResponseError("empty_model_response")
            content = ""
        elif type(content) is not str:
            raise ProviderResponseError("malformed_provider_response")
        elif not content.strip():
            raise ProviderResponseError("empty_model_response")

        usage = data.get("usage")
        if type(usage) is not dict:
            usage = {}
        details = usage.get("completion_tokens_details")
        if type(details) is not dict:
            details = {}

        returned_model = data.get("model") if type(data.get("model")) is str else None
        request_id = data.get("id") if type(data.get("id")) is str else None

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

    def _normalize_stream_chunk(self, data: Any) -> CompletionStreamEvent:
        if type(data) is not dict:
            raise ProviderResponseError("malformed_provider_response")
        choices = data.get("choices")
        if type(choices) is not list:
            raise ProviderResponseError("malformed_provider_response")

        text = ""
        finish_reason = None
        if choices:
            first = choices[0]
            if type(first) is not dict:
                raise ProviderResponseError("malformed_provider_response")
            finish_reason = first.get("finish_reason")
            if finish_reason is not None and (
                type(finish_reason) is not str or not finish_reason
            ):
                raise ProviderResponseError("malformed_provider_response")
            delta = first.get("delta")
            if delta is not None and type(delta) is not dict:
                raise ProviderResponseError("malformed_provider_response")
            if delta is not None and "content" in delta:
                content = delta["content"]
                if content is not None and type(content) is not str:
                    raise ProviderResponseError("malformed_provider_response")
                text = "" if content is None else content

        usage = data.get("usage")
        if type(usage) is not dict:
            usage = {}
        details = usage.get("completion_tokens_details")
        if type(details) is not dict:
            details = {}
        returned_model = data.get("model") if type(data.get("model")) is str else None
        request_id = data.get("id") if type(data.get("id")) is str else None
        return CompletionStreamEvent(
            text=text,
            finish_reason=finish_reason,
            returned_model=returned_model,
            request_id=request_id,
            prompt_tokens=_optional_int(usage.get("prompt_tokens")),
            completion_tokens=_optional_int(usage.get("completion_tokens")),
            reasoning_tokens=_optional_int(details.get("reasoning_tokens")),
            total_tokens=_optional_int(usage.get("total_tokens")),
            known_cost_usd=_optional_cost(usage.get("cost")),
        )

    def _payload(
        self,
        request: CompletionRequest,
        *,
        stream: bool,
        include_empty_system: bool = True,
    ) -> dict[str, Any]:
        messages = []
        if request.system or include_empty_system:
            messages.append({"role": "system", "content": request.system})
        messages.append({"role": "user", "content": request.user})
        return {
            "model": request.model,
            "messages": messages,
            "temperature": request.temperature,
            "max_tokens": request.max_output_tokens,
            "stream": stream,
        }

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key is not None:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        payload = self._payload(request, stream=False)
        headers = self._headers()
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

    async def stream(self, request: CompletionRequest) -> AsyncIterator[CompletionStreamEvent]:
        """Stream ordinary OpenAI-compatible SSE chunks without alternate request shapes."""
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(
                timeout=None,
                transport=self._transport,
            ) as client:
                async with client.stream(
                    "POST",
                    f"{self._base_url}/chat/completions",
                    headers=self._headers(),
                    json=self._payload(request, stream=True, include_empty_system=False),
                ) as response:
                    if not 200 <= response.status_code < 300:
                        body = (await response.aread()).decode("utf-8", errors="replace")
                        body = self._sanitize(body.replace("\n", " "))[:200]
                        raise ProviderHttpError(
                            response.status_code,
                            f"provider_http_error status={response.status_code} body={body!r}",
                        )
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if not payload:
                            continue
                        if payload == "[DONE]":
                            return
                        try:
                            data = json.loads(payload)
                        except ValueError:
                            raise ProviderResponseError("malformed_provider_response") from None
                        yield self._normalize_stream_chunk(data)
        except httpx.HTTPError as exc:
            raise ProviderError(f"provider_transport_error: {self._sanitize(str(exc))}") from None
