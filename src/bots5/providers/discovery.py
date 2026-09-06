from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from bots5.core.secrets import sanitize_secret_error
from bots5.domain.provider import (
    BackendType,
    CatalogueRefreshFailureClass,
    ProviderConnection,
)
from bots5.errors import ProviderError


@dataclass(frozen=True, slots=True)
class DiscoveredModel:
    provider_model_id: str
    display_name: str | None = None
    metadata: dict[str, object] | None = None

    def as_record(self) -> dict[str, object]:
        return {
            "id": self.provider_model_id,
            "display_name": self.display_name,
            "metadata": dict(self.metadata or {}),
        }


class ModelDiscoverer(Protocol):
    async def discover(
        self,
        connection: ProviderConnection,
        credential: str | None,
    ) -> tuple[DiscoveredModel, ...]:
        ...


class ModelDiscoveryError(ProviderError):
    """A bounded, sanitized classification from a built-in discovery adapter."""

    def __init__(self, failure_class: CatalogueRefreshFailureClass, message: str) -> None:
        self.failure_class = failure_class
        super().__init__(message)


class FakeModelDiscoverer:
    def __init__(self, models: tuple[DiscoveredModel, ...] = ()) -> None:
        self.models = models
        self.calls = 0

    async def discover(self, connection: ProviderConnection, credential: str | None):
        self.calls += 1
        return self.models


class OpenAICompatibleModelDiscoverer:
    """Explicitly invoked /models discovery; never constructed by startup."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def discover(
        self,
        connection: ProviderConnection,
        credential: str | None,
    ) -> tuple[DiscoveredModel, ...]:
        try:
            if not connection.endpoint:
                raise ModelDiscoveryError(
                    CatalogueRefreshFailureClass.PROTOCOL,
                    "connection has no discovery endpoint",
                )
            headers = {"Content-Type": "application/json"}
            if credential:
                headers["Authorization"] = "Bearer " + credential
            async with httpx.AsyncClient(timeout=None, transport=self._transport) as client:
                response = await client.get(f"{connection.endpoint}/models", headers=headers)
            if not 200 <= response.status_code < 300:
                raise ModelDiscoveryError(
                    CatalogueRefreshFailureClass.PROVIDER_HTTP,
                    f"model discovery failed with HTTP status {response.status_code}",
                )
            try:
                payload: Any = response.json()
            except ValueError:
                raise ModelDiscoveryError(
                    CatalogueRefreshFailureClass.PROTOCOL,
                    "model discovery returned malformed JSON",
                ) from None
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise ModelDiscoveryError(
                    CatalogueRefreshFailureClass.PROTOCOL,
                    "model discovery returned malformed catalogue",
                )
            result: list[DiscoveredModel] = []
            for item in payload["data"]:
                if not isinstance(item, dict) or type(item.get("id")) is not str or not item["id"]:
                    raise ModelDiscoveryError(
                        CatalogueRefreshFailureClass.PROTOCOL,
                        "model discovery returned malformed catalogue",
                    )
                metadata: dict[str, object] = {}
                for key in ("owned_by", "context_length", "max_output_tokens"):
                    value = item.get(key)
                    if type(value) in {str, int} and not isinstance(value, bool):
                        metadata[key] = value
                result.append(DiscoveredModel(item["id"], item["id"], metadata))
            return tuple(result)
        except ProviderError:
            raise
        except httpx.TimeoutException:
            raise ModelDiscoveryError(
                CatalogueRefreshFailureClass.TIMEOUT,
                "model discovery transport failed",
            ) from None
        except httpx.HTTPError:
            raise ModelDiscoveryError(
                CatalogueRefreshFailureClass.TRANSPORT,
                "model discovery transport failed",
            ) from None
        except Exception as exc:
            raise ProviderError(
                f"model discovery failed: {sanitize_secret_error(exc, credential)}"
            ) from None


def discoverer_for_connection(connection: ProviderConnection) -> ModelDiscoverer:
    """Resolve the closed built-in catalogue adapter for one connection."""

    if connection.backend_type is BackendType.FAKE:
        return FakeModelDiscoverer((DiscoveredModel("fake-v0.1", "fake-v0.1"),))
    if connection.backend_type is BackendType.OPENAI_COMPATIBLE_HTTP:
        return OpenAICompatibleModelDiscoverer()
    raise ProviderError("connection has no built-in discovery adapter")
