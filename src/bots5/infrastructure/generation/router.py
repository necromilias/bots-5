from __future__ import annotations

from collections.abc import AsyncIterator

from bots5.core.generation import GenerationBackend, GenerationEvent, GenerationRequest
from bots5.infrastructure.secrets import (
    EnvironmentSecretStore,
    SecretServiceStore,
    SecretStore,
    SecretStoreError,
)
from bots5.providers.openai_compatible import OpenAICompatibleProvider
from bots5.providers.openrouter import OpenRouterProvider

from .fake import FakeStreamingBackend
from .openai_compatible import OpenAICompatibleStreamingBackend


class BuiltinProviderRouter(GenerationBackend):
    """Closed Phase 5 backend factory with request-specific frozen routing."""

    def __init__(
        self,
        *,
        fake_backend: GenerationBackend | None = None,
        secret_stores: dict[str, SecretStore] | None = None,
        transports: dict[str, object] | None = None,
    ) -> None:
        self._fake = fake_backend or FakeStreamingBackend()
        self._secret_stores = dict(secret_stores or {})
        self._transports = dict(transports or {})

    def _secret(self, request: GenerationRequest) -> str | None:
        if request.credential_source in {None, "none"}:
            return None
        source = request.credential_source
        store = self._secret_stores.get(source)
        if store is None:
            if source == "environment":
                store = EnvironmentSecretStore()
            elif source == "secret_service":
                store = SecretServiceStore()
            else:
                raise SecretStoreError("unsupported credential source")
            self._secret_stores[source] = store
        return store.get(request.credential_reference or "")

    def _openai_backend(self, request: GenerationRequest) -> GenerationBackend:
        if request.base_url is None or request.provider_profile not in {"generic", "openrouter"}:
            raise ValueError("Phase 5 HTTP request has incomplete connection truth")
        secret = self._secret(request)
        transport = self._transports.get(request.connection_id or "")
        if request.provider_profile == "openrouter":
            if not secret:
                raise SecretStoreError("OpenRouter credential is missing")
            provider = OpenRouterProvider(
                secret,
                base_url=request.base_url,
                _transport=transport,
            )
        else:
            provider = OpenAICompatibleProvider(
                request.base_url,
                api_key=secret,
                _transport=transport,
            )
        return OpenAICompatibleStreamingBackend(
            provider,
            provider_id=request.provider_id or "generic",
            base_url=request.base_url,
        )

    async def stream(self, request: GenerationRequest) -> AsyncIterator[GenerationEvent]:
        if request.backend_id == "fake":
            async for event in self._fake.stream(request):
                yield event
            return
        if request.backend_id != "openai_compatible_http":
            raise ValueError("unsupported Phase 5 backend")
        backend = self._openai_backend(request)
        async for event in backend.stream(request):
            yield event
