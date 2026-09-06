from __future__ import annotations

import os
from dataclasses import dataclass

from bots5.core.secrets import SecretStore, SecretStoreError
from bots5.domain.provider import CredentialSource, CredentialStatus


@dataclass(frozen=True, slots=True)
class EnvironmentSecretStore:
    """Explicit environment-variable credentials for headless operation."""

    source: CredentialSource = CredentialSource.ENVIRONMENT

    def status(self, reference: str | None) -> CredentialStatus:
        if not reference:
            return CredentialStatus(self.source, None, "not_configured")
        if os.environ.get(reference):
            return CredentialStatus(self.source, reference, "available")
        return CredentialStatus(self.source, reference, "missing")

    def get(self, reference: str) -> str:
        value = os.environ.get(reference)
        if not value:
            raise SecretStoreError("environment credential is missing")
        return value

    def put(self, reference: str, value: str) -> None:
        raise SecretStoreError("environment credentials are read-only")

    def delete(self, reference: str) -> None:
        raise SecretStoreError("environment credentials are read-only")


class SecretServiceStore:
    """Strict Linux Secret Service adapter.

    This intentionally instantiates ``keyring.backends.SecretService.Keyring``
    instead of using keyring's global backend selection or fallback chain.
    ``backend`` is an internal seam for deterministic tests and is never
    persisted or rendered.
    """

    source = CredentialSource.SECRET_SERVICE
    _service_name = "bots5"

    def __init__(self, *, backend=None) -> None:
        if backend is None:
            try:
                from keyring.backends.SecretService import Keyring
            except Exception as exc:  # pragma: no cover - depends on host packages
                raise SecretStoreError(
                    "Linux Secret Service backend is unavailable"
                ) from None
            try:
                backend = Keyring()
            except Exception as exc:  # pragma: no cover - depends on host DBus
                raise SecretStoreError(
                    "Linux Secret Service backend could not be initialized"
                ) from None
        self._backend = backend

    def _safe_call(self, operation, *args):
        failure: SecretStoreError | None = None
        try:
            result = operation(*args)
        except Exception:
            failure = SecretStoreError("Linux Secret Service operation failed")
        finally:
            args = ()
        if failure is not None:
            raise failure
        return result

    def status(self, reference: str | None) -> CredentialStatus:
        if not reference:
            return CredentialStatus(self.source, None, "not_configured")
        try:
            value = self._safe_call(
                self._backend.get_password,
                self._service_name,
                reference,
            )
        except SecretStoreError:
            return CredentialStatus(self.source, reference, "unavailable")
        return CredentialStatus(
            self.source,
            reference,
            "available" if value else "missing",
        )

    def get(self, reference: str) -> str:
        if not reference:
            raise SecretStoreError("Secret Service credential reference is missing")
        value = self._safe_call(
            self._backend.get_password,
            self._service_name,
            reference,
        )
        if not value:
            raise SecretStoreError("Secret Service credential is missing")
        return str(value)

    def put(self, reference: str, value: str) -> None:
        try:
            if not reference or not value:
                raise SecretStoreError("Secret Service credential reference or value is missing")
            self._safe_call(
                self._backend.set_password,
                self._service_name,
                reference,
                value,
            )
        finally:
            value = None

    def delete(self, reference: str) -> None:
        if not reference:
            raise SecretStoreError("Secret Service credential reference is missing")
        self._safe_call(
            self._backend.delete_password,
            self._service_name,
            reference,
        )


class FakeSecretStore:
    """Deterministic in-memory SecretStore for tests; never a production fallback."""

    source = CredentialSource.SECRET_SERVICE

    def __init__(self, values: dict[str, str] | None = None, *, failures: set[str] | None = None):
        self.values = dict(values or {})
        self.failures = set(failures or ())

    def _check(self, reference: str) -> None:
        if reference in self.failures:
            raise SecretStoreError("fake Secret Service operation failed")

    def status(self, reference: str | None) -> CredentialStatus:
        if not reference:
            return CredentialStatus(self.source, None, "not_configured")
        try:
            self._check(reference)
        except SecretStoreError:
            return CredentialStatus(self.source, reference, "unavailable")
        return CredentialStatus(
            self.source,
            reference,
            "available" if reference in self.values else "missing",
        )

    def get(self, reference: str) -> str:
        self._check(reference)
        try:
            return self.values[reference]
        except KeyError:
            raise SecretStoreError("fake Secret Service credential is missing") from None

    def put(self, reference: str, value: str) -> None:
        try:
            self._check(reference)
            self.values[reference] = value
        finally:
            value = None

    def delete(self, reference: str) -> None:
        self._check(reference)
        self.values.pop(reference, None)


def secret_store_for(source: CredentialSource, *, backend=None) -> SecretStore:
    if source is CredentialSource.ENVIRONMENT:
        return EnvironmentSecretStore()
    if source is CredentialSource.SECRET_SERVICE:
        return SecretServiceStore(backend=backend)
    raise SecretStoreError("no credential store is configured")
