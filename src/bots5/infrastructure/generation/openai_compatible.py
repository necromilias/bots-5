from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace

from bots5.core.generation import (
    GenerationCompleted,
    GenerationDelta,
    GenerationDispatched,
    GenerationEvent,
    GenerationFailed,
    GenerationMetadata,
    GenerationRequest,
)
from bots5.core.urls import canonical_http_base_url
from bots5.errors import ProviderError, ProviderHttpError, ProviderResponseError
from bots5.providers.base import CompletionRequest, CompletionStreamEvent, StreamingProvider


class OpenAICompatibleStreamingBackend:
    """B.O.T.S.-owned streaming adapter over an OpenAI-compatible provider seam."""

    backend_id = "openai_compatible_http"

    def __init__(
        self,
        provider: StreamingProvider,
        *,
        provider_id: str,
        base_url: str,
        api_key_env: str | None = None,
        temperature: float = 0.0,
        max_output_tokens: int = 1024,
    ) -> None:
        if provider_id not in {"local_openai", "openrouter"}:
            raise ValueError(f"unsupported OpenAI-compatible provider ID: {provider_id}")
        base_url = canonical_http_base_url(
            base_url,
            error_type=ValueError,
            error_message="base_url must be a normalized non-empty URL",
        )
        if temperature < 0 or temperature > 2:
            raise ValueError("temperature must be between 0 and 2")
        if max_output_tokens < 1:
            raise ValueError("max_output_tokens must be positive")
        self._provider = provider
        self.provider_id = provider_id
        self.base_url = base_url
        self.api_key_env = api_key_env
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens

    @staticmethod
    def _merge_metadata(
        current: CompletionStreamEvent,
        update: CompletionStreamEvent,
    ) -> CompletionStreamEvent:
        return replace(
            current,
            returned_model=update.returned_model or current.returned_model,
            request_id=update.request_id or current.request_id,
            prompt_tokens=(
                update.prompt_tokens
                if update.prompt_tokens is not None
                else current.prompt_tokens
            ),
            completion_tokens=(
                update.completion_tokens
                if update.completion_tokens is not None
                else current.completion_tokens
            ),
            reasoning_tokens=(
                update.reasoning_tokens
                if update.reasoning_tokens is not None
                else current.reasoning_tokens
            ),
            total_tokens=(
                update.total_tokens
                if update.total_tokens is not None
                else current.total_tokens
            ),
            known_cost_usd=(
                update.known_cost_usd
                if update.known_cost_usd is not None
                else current.known_cost_usd
            ),
        )

    @staticmethod
    def _provider_failure_is_uncertain(error: ProviderError) -> bool:
        if isinstance(error, ProviderHttpError):
            return error.status_code >= 500
        return True

    async def stream(self, request: GenerationRequest) -> AsyncIterator[GenerationEvent]:
        if request.backend_id != self.backend_id:
            raise ValueError("generation request uses the wrong backend")
        if request.provider_id != self.provider_id:
            raise ValueError("generation request uses the wrong provider")
        if request.base_url != self.base_url:
            raise ValueError("generation request uses the wrong base URL")
        if request.api_key_env != self.api_key_env:
            raise ValueError("generation request uses the wrong authentication provenance")

        yield GenerationDispatched(attempt_id=request.attempt_id)
        completion_request = CompletionRequest(
            model=request.model,
            system="",
            user=request.prompt,
            temperature=self.temperature,
            max_output_tokens=self.max_output_tokens,
            timeout_seconds=0.0,
        )
        metadata = CompletionStreamEvent()
        finish_reason: str | None = None
        output_text = ""
        try:
            async for chunk in self._provider.stream(completion_request):
                if (
                    chunk.finish_reason is not None
                    and finish_reason is not None
                    and chunk.finish_reason != finish_reason
                ):
                    raise ProviderResponseError("conflicting_finish_reason")
                metadata = self._merge_metadata(metadata, chunk)
                if any(
                    value is not None
                    for value in (
                        metadata.returned_model,
                        metadata.request_id,
                        metadata.prompt_tokens,
                        metadata.completion_tokens,
                        metadata.reasoning_tokens,
                        metadata.total_tokens,
                        metadata.known_cost_usd,
                    )
                ):
                    yield GenerationMetadata(
                        attempt_id=request.attempt_id,
                        returned_model=metadata.returned_model,
                        request_id=metadata.request_id,
                        prompt_tokens=metadata.prompt_tokens,
                        completion_tokens=metadata.completion_tokens,
                        reasoning_tokens=metadata.reasoning_tokens,
                        total_tokens=metadata.total_tokens,
                        known_cost_usd=metadata.known_cost_usd,
                    )
                if chunk.text:
                    output_text += chunk.text
                    yield GenerationDelta(attempt_id=request.attempt_id, text=chunk.text)
                if chunk.finish_reason is not None:
                    finish_reason = chunk.finish_reason
        except ProviderError as exc:
            yield GenerationFailed(
                attempt_id=request.attempt_id,
                error_type=type(exc).__name__,
                error_message=str(exc)[:500],
                remote_outcome_unknown=self._provider_failure_is_uncertain(exc),
            )
            return

        if finish_reason == "stop" and not output_text.strip():
            yield GenerationFailed(
                attempt_id=request.attempt_id,
                error_type="empty_model_response",
                error_message="provider completed without non-empty output",
                remote_outcome_unknown=False,
            )
            return
        if finish_reason is not None:
            yield GenerationCompleted(
                attempt_id=request.attempt_id,
                finish_reason=finish_reason,
                returned_model=metadata.returned_model,
                request_id=metadata.request_id,
                prompt_tokens=metadata.prompt_tokens,
                completion_tokens=metadata.completion_tokens,
                reasoning_tokens=metadata.reasoning_tokens,
                total_tokens=metadata.total_tokens,
                known_cost_usd=metadata.known_cost_usd,
            )
