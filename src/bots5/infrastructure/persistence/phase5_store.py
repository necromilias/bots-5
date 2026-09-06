from __future__ import annotations

import json
import math
import re
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError
from uuid6 import uuid7

from bots5.core.errors import RevisionConflict, StateError
from bots5.core.secrets import is_forbidden_secret_key
from bots5.core.urls import canonical_http_base_url
from bots5.domain.clock import parse_utc, utc_iso
from bots5.domain.provider import (
    BackendType,
    CAPABILITY_KEYS,
    CapabilityFact,
    CapabilityOverride,
    CapabilitySource,
    CapabilityState,
    CATALOGUE_REFRESH_FAILURE_MESSAGES,
    CatalogueRefreshFailureClass,
    CatalogueRefreshStatus,
    CatalogueAvailability,
    CatalogueOrigin,
    CredentialSource,
    GenerationSettings,
    ModelCatalogueEntry,
    ModelSelection,
    ProviderConnection,
    ProviderProfile,
    validate_capability_value,
)

from .schema import (
    application_generation_config,
    capability_facts,
    capability_observations,
    capability_overrides,
    chat_model_generation_config,
    chat_model_selection,
    catalogue_refresh_state,
    model_catalogue_entries,
    model_generation_config,
    provider_connections,
)
from .transition_guard import (
    arm_phase5_connection_identity_update,
    arm_phase5_catalogue_refresh,
    clear_phase5_catalogue_refresh,
    clear_phase5_connection_identity_update,
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _json_object(value: object, message: str) -> dict[str, object]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError) as exc:
        raise StateError(message) from exc
    if not isinstance(parsed, dict):
        raise StateError(message)
    def contains_forbidden(item: object) -> bool:
        if isinstance(item, dict):
            return any(
                is_forbidden_secret_key(key)
                or contains_forbidden(nested)
                for key, nested in item.items()
            )
        if isinstance(item, list):
            return any(contains_forbidden(nested) for nested in item)
        return False

    if contains_forbidden(parsed):
        raise StateError(message)
    return parsed


def _capability_provenance(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or len(value) > 8:
        raise StateError("capability provenance is malformed")
    allowed = {"reason", "field", "catalogue_revision"}
    if set(value) - allowed:
        raise StateError("capability provenance is malformed")
    for key in ("reason", "field"):
        item = value.get(key)
        if item is not None and (type(item) is not str or len(item) > 256):
            raise StateError("capability provenance is malformed")
    revision = value.get("catalogue_revision")
    if revision is not None and (type(revision) is not int or revision < 0):
        raise StateError("capability provenance is malformed")
    return dict(value)


def _discovery_metadata(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise StateError("model discovery metadata is malformed")
    allowed = {"owned_by", "context_length", "max_output_tokens"}
    result: dict[str, object] = {}
    for key, item in list(value.items())[:32]:
        key = str(key)
        if is_forbidden_secret_key(key):
            raise StateError("model discovery metadata contains a forbidden field")
        if key not in allowed:
            continue
        if key == "owned_by":
            if type(item) is not str:
                raise StateError("model discovery metadata is malformed")
            result[key] = item[:256]
        elif key in {"context_length", "max_output_tokens"}:
            if type(item) is not int or not 0 <= item <= 2**63 - 1:
                raise StateError("model discovery metadata is malformed")
            result[key] = item
    return result


def _settings(mapping: Any) -> GenerationSettings:
    def number(name: str):
        value = mapping[name]
        return None if value is None else float(value)

    try:
        settings = GenerationSettings(
            temperature=number("temperature"),
            max_output_tokens=mapping["max_output_tokens"],
            reasoning_effort=mapping["reasoning_effort"],
            timeout_seconds=number("timeout_seconds"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise StateError("generation settings are malformed") from exc
    if settings.temperature is not None and (
        not math.isfinite(settings.temperature) or not 0 <= settings.temperature <= 2
    ):
        raise StateError("generation settings are malformed")
    if settings.max_output_tokens is not None and (
        type(settings.max_output_tokens) is not int or settings.max_output_tokens < 1
    ):
        raise StateError("generation settings are malformed")
    if settings.reasoning_effort not in {None, "none"}:
        raise StateError("generation settings are malformed")
    if settings.timeout_seconds is not None and (
        not math.isfinite(settings.timeout_seconds) or settings.timeout_seconds <= 0
    ):
        raise StateError("generation settings are malformed")
    return settings


def _validate_settings_value(value: GenerationSettings) -> None:
    if not isinstance(value, GenerationSettings):
        raise StateError("generation settings are malformed")
    if value.temperature is not None and (
        type(value.temperature) not in {int, float}
        or isinstance(value.temperature, bool)
        or not math.isfinite(value.temperature)
        or not 0 <= value.temperature <= 2
    ):
        raise StateError("generation settings are malformed")
    if value.max_output_tokens is not None and (
        type(value.max_output_tokens) is not int or value.max_output_tokens < 1
    ):
        raise StateError("generation settings are malformed")
    if value.reasoning_effort not in {None, "none"}:
        raise StateError("generation settings are malformed")
    if value.timeout_seconds is not None and (
        type(value.timeout_seconds) not in {int, float}
        or isinstance(value.timeout_seconds, bool)
        or not math.isfinite(value.timeout_seconds)
        or value.timeout_seconds <= 0
    ):
        raise StateError("generation settings are malformed")


def _connection(row: Any) -> ProviderConnection:
    m = row._mapping
    try:
        if (
            type(m["id"]) is not str
            or type(m["name"]) is not str
            or type(m["name_key"]) is not str
            or type(m["enabled"]) not in {bool, int}
            or m["enabled"] not in {False, True, 0, 1}
            or type(m["retired"]) not in {bool, int}
            or m["retired"] not in {False, True, 0, 1}
            or type(m["revision"]) is not int
            or type(m["catalogue_revision"]) is not int
        ):
            raise StateError("provider connection is malformed")
        refresh_status = CatalogueRefreshStatus(
            m.get("catalogue_refresh_status", CatalogueRefreshStatus.NEVER.value)
        )
        refresh_revision = m.get("catalogue_refresh_revision", 0)
        refresh_at = m.get("catalogue_refresh_at")
        failure_class_value = m.get("catalogue_refresh_failure_class")
        failure_message = m.get("catalogue_refresh_failure_message")
        if type(refresh_revision) is not int or refresh_revision < 0:
            raise StateError("provider connection refresh state is malformed")
        if refresh_at is not None and type(refresh_at) is not str:
            raise StateError("provider connection refresh state is malformed")
        if failure_class_value is not None:
            failure_class = CatalogueRefreshFailureClass(failure_class_value)
        else:
            failure_class = None
        if failure_message is not None and type(failure_message) is not str:
            raise StateError("provider connection refresh state is malformed")
        if refresh_status is CatalogueRefreshStatus.FAILED:
            if failure_class is None or failure_message is None:
                raise StateError("provider connection refresh state is malformed")
        elif failure_class is not None or failure_message is not None:
            raise StateError("provider connection refresh state is malformed")
        value = ProviderConnection(
            id=m["id"],
            name=m["name"],
            backend_type=BackendType(m["backend_type"]),
            profile=ProviderProfile(m["profile"]),
            endpoint=m["endpoint"],
            credential_source=CredentialSource(m["credential_source"]),
            credential_reference=m["credential_reference"],
            enabled=bool(m["enabled"]),
            retired=bool(m["retired"]),
            revision=int(m["revision"]),
            catalogue_revision=int(m["catalogue_revision"]),
            created_at=parse_utc(m["created_at"]),
            updated_at=parse_utc(m["updated_at"]),
            catalogue_refresh_status=refresh_status,
            catalogue_refresh_revision=refresh_revision,
            catalogue_refresh_at=None if refresh_at is None else parse_utc(refresh_at),
            catalogue_refresh_failure_class=failure_class,
            catalogue_refresh_failure_message=failure_message,
        )
        if value.name.casefold() != m["name_key"] or not value.name.strip():
            raise StateError("provider connection name normalization is invalid")
        if value.retired and value.enabled:
            raise StateError("retired provider connections cannot be enabled")
        if value.revision < 1 or value.catalogue_revision < 0:
            raise StateError("provider connection revisions are invalid")
        if value.backend_type is BackendType.FAKE:
            if (
                value.profile is not ProviderProfile.GENERIC
                or value.endpoint is not None
                or value.credential_source is not CredentialSource.NONE
                or value.credential_reference is not None
            ):
                raise StateError("fake provider connection is malformed")
        else:
            canonical = canonical_http_base_url(
                value.endpoint,
                error_type=StateError,
                error_message="provider connection endpoint is malformed",
            )
            if value.endpoint != canonical:
                raise StateError("provider connection endpoint is not normalized")
            if value.profile is ProviderProfile.OPENROUTER and value.credential_source is CredentialSource.NONE:
                raise StateError("OpenRouter provider connection requires a credential source")
        if value.credential_source is CredentialSource.NONE and value.credential_reference is not None:
            raise StateError("provider connection credential reference is malformed")
        if value.credential_source is not CredentialSource.NONE and not value.credential_reference:
            raise StateError("provider connection credential reference is malformed")
        if value.credential_source is CredentialSource.ENVIRONMENT and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value.credential_reference or "") is None:
            raise StateError("provider connection environment reference is malformed")
        return value
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise StateError("provider connection is malformed") from exc


def _model(row: Any, connected: bool = True) -> ModelCatalogueEntry:
    m = row._mapping
    try:
        if (
            type(m["id"]) is not str
            or type(m["connection_id"]) is not str
            or type(m["provider_model_id"]) is not str
            or type(m["display_name"]) is not str
            or type(m["revision"]) is not int
            or m["discovery_revision"] is not None
            and type(m["discovery_revision"]) is not int
        ):
            raise StateError("model catalogue entry is malformed")
        availability = CatalogueAvailability(m["availability"])
        if not connected:
            availability = CatalogueAvailability.DISCONNECTED
        value = ModelCatalogueEntry(
            id=m["id"],
            connection_id=m["connection_id"],
            provider_model_id=m["provider_model_id"],
            display_name=m["display_name"],
            origin=CatalogueOrigin(m["origin"]),
            availability=availability,
            discovery_revision=m["discovery_revision"],
            discovered_at=None if m["discovered_at"] is None else parse_utc(m["discovered_at"]),
            metadata=_json_object(m["metadata_json"], "model catalogue metadata is malformed"),
            revision=m["revision"],
            created_at=parse_utc(m["created_at"]),
            updated_at=parse_utc(m["updated_at"]),
        )
        if not value.connection_id or not value.provider_model_id or not value.display_name:
            raise StateError("model catalogue entry is malformed")
        if value.revision < 1 or value.discovery_revision is not None and value.discovery_revision < 0:
            raise StateError("model catalogue entry is malformed")
        return value
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise StateError("model catalogue entry is malformed") from exc


class Phase5StoreMixin:
    """Persistence operations for additive Phase 5 tables.

    The mixin deliberately talks only to the existing SQLAlchemy engine.  It
    does not add a second authority or a second database connection policy.
    """

    def list_provider_connections(self) -> tuple[ProviderConnection, ...]:
        self._ensure_open()
        with self._engine.connect() as db:
            rows = db.execute(self._provider_connection_statement().order_by(provider_connections.c.name_key)).fetchall()
        return tuple(_connection(row) for row in rows)

    def get_provider_connection(self, connection_id: str) -> ProviderConnection | None:
        self._ensure_open()
        with self._engine.connect() as db:
            row = db.execute(self._provider_connection_statement().where(provider_connections.c.id == connection_id)).first()
        return None if row is None else _connection(row)

    @staticmethod
    def _provider_connection_statement():
        return select(
            provider_connections,
            catalogue_refresh_state.c.status.label("catalogue_refresh_status"),
            catalogue_refresh_state.c.refresh_revision.label("catalogue_refresh_revision"),
            catalogue_refresh_state.c.updated_at.label("catalogue_refresh_at"),
            catalogue_refresh_state.c.failure_class.label("catalogue_refresh_failure_class"),
            catalogue_refresh_state.c.failure_message.label("catalogue_refresh_failure_message"),
        ).select_from(
            provider_connections.outerjoin(
                catalogue_refresh_state,
                catalogue_refresh_state.c.connection_id == provider_connections.c.id,
            )
        )

    def create_provider_connection(self, value: ProviderConnection) -> ProviderConnection:
        self._ensure_open()
        if value.revision != 1 or value.retired:
            raise StateError("new provider connections must start unretired")
        now = utc_iso(value.created_at or datetime.now(UTC))
        try:
            with self._engine.begin() as db:
                db.execute(insert(provider_connections).values(
                    id=value.id, name=value.name, name_key=value.name.casefold(),
                    backend_type=value.backend_type.value, profile=value.profile.value,
                    endpoint=value.endpoint, credential_source=value.credential_source.value,
                    credential_reference=value.credential_reference, enabled=value.enabled,
                    retired=value.retired, revision=1, catalogue_revision=value.catalogue_revision,
                    created_at=now, updated_at=now,
                ))
        except IntegrityError:
            raise StateError("provider connection name or identity already exists") from None
        return value

    def update_provider_connection(self, value: ProviderConnection, *, expected_revision: int) -> ProviderConnection:
        self._ensure_open()
        if value.revision != expected_revision + 1:
            raise RevisionConflict(f"provider connection revision must advance by one: {value.id}")
        current = self.get_provider_connection(value.id)
        if current is None:
            raise StateError(f"provider connection not found: {value.id}")
        if value.retired != current.retired:
            raise StateError("provider connection retirement requires the retirement command")
        if (value.retired and value.enabled) or (current.retired and value.enabled):
            raise StateError("retired provider connections cannot be enabled")
        catalogue_revision = current.catalogue_revision
        catalogue_identity_changed = (
            current.endpoint != value.endpoint
            or current.backend_type != value.backend_type
            or current.profile != value.profile
            or current.credential_source != value.credential_source
            or current.credential_reference != value.credential_reference
        )
        if catalogue_identity_changed:
            catalogue_revision = current.catalogue_revision + 1
        value = replace_provider(value, catalogue_revision=catalogue_revision)
        with self._engine.begin() as db:
            if catalogue_identity_changed:
                arm_phase5_connection_identity_update(
                    db,
                    value.id,
                    expected_revision,
                    current.catalogue_revision,
                )
            try:
                if catalogue_identity_changed:
                    db.execute(
                        update(model_catalogue_entries)
                        .where(
                            model_catalogue_entries.c.connection_id == value.id,
                        )
                        .values(
                            availability=CatalogueAvailability.STALE.value,
                            revision=model_catalogue_entries.c.revision + 1,
                            updated_at=utc_iso(value.updated_at or datetime.now(UTC)),
                        )
                    )
                    db.execute(
                        delete(capability_facts).where(
                            capability_facts.c.model_entry_id.in_(
                                select(model_catalogue_entries.c.id).where(
                                    model_catalogue_entries.c.connection_id == value.id
                                )
                            )
                        )
                    )
                result = db.execute(update(provider_connections).where(
                    provider_connections.c.id == value.id,
                    provider_connections.c.revision == expected_revision,
                ).values(
                    name=value.name, name_key=value.name.casefold(),
                    backend_type=value.backend_type.value, profile=value.profile.value,
                    endpoint=value.endpoint, credential_source=value.credential_source.value,
                    credential_reference=value.credential_reference, enabled=value.enabled,
                    retired=value.retired, revision=value.revision,
                    catalogue_revision=catalogue_revision,
                    updated_at=utc_iso(value.updated_at or datetime.now(UTC)),
                ))
                if result.rowcount != 1:
                    raise RevisionConflict(f"provider connection changed: {value.id}")
            finally:
                if catalogue_identity_changed:
                    clear_phase5_connection_identity_update(db)
        return value

    def set_provider_connection_enabled(self, connection_id: str, enabled: bool, *, expected_revision: int) -> ProviderConnection:
        current = self.get_provider_connection(connection_id)
        if current is None:
            raise StateError(f"provider connection not found: {connection_id}")
        if current.retired and enabled:
            raise StateError("retired provider connections cannot be enabled")
        return self.update_provider_connection(
            replace_provider(current, enabled=bool(enabled), revision=expected_revision + 1, updated_at=datetime.now(UTC)),
            expected_revision=expected_revision,
        )

    def retire_provider_connection(self, connection_id: str, *, expected_revision: int, replacement_model_entry_id: str | None = None) -> ProviderConnection:
        current = self.get_provider_connection(connection_id)
        if current is None:
            raise StateError(f"provider connection not found: {connection_id}")
        if current.revision != expected_revision:
            raise RevisionConflict(f"provider connection revision changed: {connection_id}")
        now = datetime.now(UTC)
        with self._engine.begin() as db:
            default_id = db.execute(
                select(application_generation_config.c.default_model_entry_id).where(
                    application_generation_config.c.id == 1
                )
            ).scalar_one_or_none()
            if default_id is not None:
                default_connection_id = db.execute(
                    select(model_catalogue_entries.c.connection_id).where(
                        model_catalogue_entries.c.id == default_id
                    )
                ).scalar_one_or_none()
                if default_connection_id == connection_id:
                    if replacement_model_entry_id is None:
                        raise StateError("retiring the application-default connection requires a replacement")
                    replacement_exists = db.execute(
                        select(model_catalogue_entries.c.id)
                        .select_from(
                            model_catalogue_entries.join(
                                provider_connections,
                                provider_connections.c.id == model_catalogue_entries.c.connection_id,
                            )
                        )
                        .where(
                            model_catalogue_entries.c.id == replacement_model_entry_id,
                            model_catalogue_entries.c.connection_id != connection_id,
                            model_catalogue_entries.c.availability == CatalogueAvailability.AVAILABLE.value,
                            provider_connections.c.enabled.is_(True),
                            provider_connections.c.retired.is_(False),
                        )
                    ).first()
                    if replacement_exists is None:
                        raise StateError("retirement replacement must be an available model on another connection")
                    db.execute(
                        update(application_generation_config)
                        .where(application_generation_config.c.id == 1)
                        .values(
                            default_model_entry_id=replacement_model_entry_id,
                            revision=application_generation_config.c.revision + 1,
                            updated_at=utc_iso(now),
                        )
                    )
            result = db.execute(
                update(provider_connections)
                .where(
                    provider_connections.c.id == connection_id,
                    provider_connections.c.revision == expected_revision,
                )
                .values(
                    enabled=False,
                    retired=True,
                    revision=expected_revision + 1,
                    updated_at=utc_iso(now),
                )
            )
            if result.rowcount != 1:
                raise RevisionConflict(f"provider connection changed: {connection_id}")
        return self.get_provider_connection(connection_id)  # type: ignore[return-value]

    def _model_statement(self, connection_id: str | None = None):
        statement = select(model_catalogue_entries, provider_connections.c.enabled, provider_connections.c.retired).select_from(
            model_catalogue_entries.join(provider_connections, provider_connections.c.id == model_catalogue_entries.c.connection_id)
        )
        if connection_id is not None:
            statement = statement.where(model_catalogue_entries.c.connection_id == connection_id)
        return statement.order_by(provider_connections.c.name_key, model_catalogue_entries.c.provider_model_id)

    def list_model_catalogue_entries(self, connection_id: str | None = None) -> tuple[ModelCatalogueEntry, ...]:
        self._ensure_open()
        with self._engine.connect() as db:
            rows = db.execute(self._model_statement(connection_id)).fetchall()
        return tuple(_model(row, bool(row._mapping["enabled"]) and not bool(row._mapping["retired"])) for row in rows)

    def get_model_catalogue_entry(self, model_entry_id: str) -> ModelCatalogueEntry | None:
        self._ensure_open()
        with self._engine.connect() as db:
            row = db.execute(self._model_statement().where(model_catalogue_entries.c.id == model_entry_id)).first()
        return None if row is None else _model(row, bool(row._mapping["enabled"]) and not bool(row._mapping["retired"]))

    def get_model_catalogue_entry_by_provider_id(self, connection_id: str, provider_model_id: str) -> ModelCatalogueEntry | None:
        self._ensure_open()
        with self._engine.connect() as db:
            row = db.execute(self._model_statement(connection_id).where(model_catalogue_entries.c.provider_model_id == provider_model_id)).first()
        return None if row is None else _model(row, bool(row._mapping["enabled"]) and not bool(row._mapping["retired"]))

    def add_manual_model(self, *, connection_id: str, provider_model_id: str, model_entry_id: str, display_name: str | None = None, metadata: dict[str, object] | None = None) -> ModelCatalogueEntry:
        if type(provider_model_id) is not str or not provider_model_id.strip():
            raise StateError("provider model ID must not be empty")
        existing = self.get_model_catalogue_entry_by_provider_id(connection_id, provider_model_id)
        now = _now()
        metadata = metadata or {}
        if not isinstance(metadata, dict) or len(metadata) > 32:
            raise StateError("model catalogue metadata is malformed")
        if any(is_forbidden_secret_key(key) for key in metadata):
            raise StateError("model catalogue metadata contains a forbidden field")
        try:
            encoded = json.dumps(metadata, sort_keys=True, separators=(",", ":"))
            _json_object(encoded, "model catalogue metadata is malformed")
        except (TypeError, ValueError) as exc:
            raise StateError("model catalogue metadata is malformed") from exc
        with self._engine.begin() as db:
            if existing is None:
                db.execute(insert(model_catalogue_entries).values(
                    id=model_entry_id, connection_id=connection_id, provider_model_id=provider_model_id,
                    display_name=(display_name or provider_model_id)[:256], origin=CatalogueOrigin.MANUAL.value,
                    availability=CatalogueAvailability.AVAILABLE.value, discovery_revision=None,
                    discovered_at=None, metadata_json=encoded, revision=1, created_at=now, updated_at=now,
                ))
            else:
                origin = existing.origin.value
                if origin is CatalogueOrigin.DISCOVERED.value:
                    origin = CatalogueOrigin.MANUAL_CONFIRMED.value
                db.execute(update(model_catalogue_entries).where(model_catalogue_entries.c.id == existing.id).values(
                    display_name=(display_name or existing.display_name)[:256], metadata_json=encoded,
                    origin=origin, availability=CatalogueAvailability.AVAILABLE.value,
                    revision=existing.revision + 1, updated_at=now,
                ))
        return self.get_model_catalogue_entry_by_provider_id(connection_id, provider_model_id)  # type: ignore[return-value]

    def refresh_model_catalogue(self, connection_id: str, discovered: tuple[dict[str, object], ...], *, success: bool, failure_class: CatalogueRefreshFailureClass | None = None, expected_catalogue_revision: int | None = None, expected_connection_revision: int | None = None) -> tuple[ModelCatalogueEntry, ...]:
        if success and failure_class is not None:
            raise StateError("successful catalogue refresh cannot have a failure class")
        if not success:
            try:
                failure_class = CatalogueRefreshFailureClass(failure_class or CatalogueRefreshFailureClass.UNKNOWN)
            except ValueError:
                raise StateError("catalogue refresh failure class is malformed") from None
        failure_message = None if success else CATALOGUE_REFRESH_FAILURE_MESSAGES[failure_class]
        current = self.get_provider_connection(connection_id)
        if current is None:
            raise StateError(f"provider connection not found: {connection_id}")
        if expected_catalogue_revision is not None and current.catalogue_revision != expected_catalogue_revision:
            raise RevisionConflict("model catalogue refresh is stale")
        if expected_connection_revision is not None and current.revision != expected_connection_revision:
            raise RevisionConflict("model catalogue refresh is stale")
        now = _now()
        revision = current.catalogue_revision + 1
        seen: set[str] = set()
        with self._engine.begin() as db:
            arm_phase5_catalogue_refresh(
                db,
                connection_id,
                current.catalogue_revision,
                current.catalogue_refresh_revision,
                revision,
            )
            criteria = [provider_connections.c.id == connection_id]
            if expected_catalogue_revision is not None:
                criteria.append(provider_connections.c.catalogue_revision == expected_catalogue_revision)
            if expected_connection_revision is not None:
                criteria.append(provider_connections.c.revision == expected_connection_revision)
            try:
                result = db.execute(update(provider_connections).where(*criteria).values(catalogue_revision=revision, updated_at=now))
                if result.rowcount != 1:
                    raise RevisionConflict("model catalogue refresh is stale")
                state_result = db.execute(
                    update(catalogue_refresh_state)
                    .where(catalogue_refresh_state.c.connection_id == connection_id)
                    .values(
                        status=(CatalogueRefreshStatus.SUCCEEDED.value if success else CatalogueRefreshStatus.FAILED.value),
                        refresh_revision=revision,
                        failure_class=None if success else failure_class.value,
                        failure_message=failure_message,
                        updated_at=now,
                    )
                )
                if state_result.rowcount != 1:
                    raise StateError("catalogue refresh state is missing")
            finally:
                clear_phase5_catalogue_refresh(db)
            if success:
                for item in discovered:
                    model_id = item.get("id")
                    if type(model_id) is not str or not model_id:
                        raise StateError("model discovery returned a malformed model ID")
                    seen.add(model_id)
                    metadata = _discovery_metadata(item.get("metadata", {}))
                    encoded = json.dumps(metadata, sort_keys=True, separators=(",", ":"))
                    row = db.execute(select(model_catalogue_entries).where(model_catalogue_entries.c.connection_id == connection_id, model_catalogue_entries.c.provider_model_id == model_id)).first()
                    if row is None:
                        db.execute(insert(model_catalogue_entries).values(
                            id=str(uuid7()), connection_id=connection_id, provider_model_id=model_id,
                            display_name=str(item.get("display_name") or model_id)[:256],
                            origin=CatalogueOrigin.DISCOVERED.value, availability=CatalogueAvailability.AVAILABLE.value,
                            discovery_revision=revision, discovered_at=now, metadata_json=encoded,
                            revision=1, created_at=now, updated_at=now,
                        ))
                    else:
                        origin = row._mapping["origin"]
                        if origin == CatalogueOrigin.MANUAL.value:
                            origin = CatalogueOrigin.MANUAL_CONFIRMED.value
                        db.execute(update(model_catalogue_entries).where(model_catalogue_entries.c.id == row._mapping["id"]).values(
                            display_name=str(item.get("display_name") or row._mapping["display_name"])[:256],
                            origin=origin, availability=CatalogueAvailability.AVAILABLE.value,
                            discovery_revision=revision, discovered_at=now, metadata_json=encoded,
                            revision=int(row._mapping["revision"]) + 1, updated_at=now,
                        ))
                rows = db.execute(select(model_catalogue_entries).where(model_catalogue_entries.c.connection_id == connection_id, model_catalogue_entries.c.origin == CatalogueOrigin.DISCOVERED.value)).fetchall()
                for row in rows:
                    if row._mapping["provider_model_id"] not in seen:
                        db.execute(update(model_catalogue_entries).where(model_catalogue_entries.c.id == row._mapping["id"]).values(availability=CatalogueAvailability.UNAVAILABLE.value, revision=int(row._mapping["revision"]) + 1, updated_at=now))
            else:
                db.execute(update(model_catalogue_entries).where(model_catalogue_entries.c.connection_id == connection_id, model_catalogue_entries.c.origin == CatalogueOrigin.DISCOVERED.value).values(availability=CatalogueAvailability.STALE.value, revision=model_catalogue_entries.c.revision + 1, updated_at=now))
        return self.list_model_catalogue_entries(connection_id)

    def list_capability_facts(self, model_entry_id: str) -> tuple[CapabilityFact, ...]:
        self._ensure_open()
        with self._engine.connect() as db:
            rows = db.execute(select(capability_facts).where(capability_facts.c.model_entry_id == model_entry_id)).fetchall()
        result = []
        for row in rows:
            m = row._mapping
            try:
                if m["capability_key"] not in CAPABILITY_KEYS:
                    raise StateError("capability fact key is invalid")
                if m["source"] in {
                    CapabilitySource.CONFIRMED_ENDPOINT.value,
                    CapabilitySource.PROVIDER_METADATA.value,
                } and m["source_revision"] is None:
                    raise StateError("capability fact revision is missing")
                result.append(CapabilityFact(m["model_entry_id"], m["capability_key"], CapabilityState(m["state"]), CapabilitySource(m["source"]), m["source_revision"], m["value"], _capability_provenance(json.loads(m["provenance_json"])), parse_utc(m["observed_at"])))
            except (KeyError, TypeError, ValueError) as exc:
                raise StateError("capability fact is malformed") from exc
        return tuple(result)

    def set_capability_fact(self, fact: CapabilityFact) -> None:
        self._ensure_open()
        if (
            fact.key not in CAPABILITY_KEYS
            or fact.source_revision is not None
            and (type(fact.source_revision) is not int or fact.source_revision < 0)
            or fact.source is CapabilitySource.MANUAL
            or fact.source in {
                CapabilitySource.CONFIRMED_ENDPOINT,
                CapabilitySource.PROVIDER_METADATA,
            }
            and fact.source_revision is None
        ):
            raise StateError("capability fact is malformed")
        try:
            validate_capability_value(fact.key, fact.state, fact.value)
        except ValueError as exc:
            raise StateError("capability fact is malformed") from exc
        try:
            provenance = _capability_provenance(fact.provenance)
            provenance_json = json.dumps(provenance, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise StateError("capability provenance is malformed") from exc
        values = dict(model_entry_id=fact.model_entry_id, capability_key=fact.key, state=fact.state.value, source=fact.source.value, source_revision=fact.source_revision, value=fact.value, provenance_json=provenance_json, observed_at=utc_iso(fact.observed_at or datetime.now(UTC)))
        fact_id = str(uuid7())
        with self._engine.begin() as db:
            criteria = [
                capability_facts.c.model_entry_id == fact.model_entry_id,
                capability_facts.c.capability_key == fact.key,
                capability_facts.c.source == fact.source.value,
            ]
            if fact.source_revision is None:
                criteria.append(capability_facts.c.source_revision.is_(None))
            else:
                criteria.append(capability_facts.c.source_revision == fact.source_revision)
            existing = db.execute(select(capability_facts).where(*criteria)).first()
            if existing is None:
                db.execute(insert(capability_facts).values(id=fact_id, **values))
            else:
                db.execute(
                    update(capability_facts)
                    .where(capability_facts.c.id == existing._mapping["id"])
                    .values(**values)
                )

    def list_capability_overrides(self, model_entry_id: str) -> tuple[CapabilityOverride, ...]:
        self._ensure_open()
        with self._engine.connect() as db:
            rows = db.execute(select(capability_overrides).where(capability_overrides.c.model_entry_id == model_entry_id)).fetchall()
        result = []
        for row in rows:
            if row._mapping["capability_key"] not in CAPABILITY_KEYS:
                raise StateError("capability override key is invalid")
            if row._mapping["reason"] is not None and (
                type(row._mapping["reason"]) is not str
                or len(row._mapping["reason"]) > 256
            ):
                raise StateError("capability override reason is invalid")
            result.append(CapabilityOverride(row._mapping["model_entry_id"], row._mapping["capability_key"], CapabilityState(row._mapping["state"]), row._mapping["value"], row._mapping["reason"], int(row._mapping["revision"]), parse_utc(row._mapping["updated_at"])))
        return tuple(result)

    def set_capability_override(self, value: CapabilityOverride, *, expected_revision: int | None = None) -> CapabilityOverride:
        self._ensure_open()
        if (
            value.key not in CAPABILITY_KEYS
            or value.reason is not None
            and (type(value.reason) is not str or len(value.reason) > 256)
            or type(value.revision) is not int
            or value.revision < 1
        ):
            raise StateError("capability override is malformed")
        try:
            validate_capability_value(value.key, value.state, value.value)
        except ValueError as exc:
            raise StateError("capability override is malformed") from exc
        now = utc_iso(value.updated_at or datetime.now(UTC))
        with self._engine.begin() as db:
            row = db.execute(select(capability_overrides).where(capability_overrides.c.model_entry_id == value.model_entry_id, capability_overrides.c.capability_key == value.key)).first()
            current_revision = 0 if row is None else int(row._mapping["revision"])
            if expected_revision is not None and current_revision != expected_revision:
                raise RevisionConflict("capability override revision changed")
            revision = current_revision + 1
            values = dict(model_entry_id=value.model_entry_id, capability_key=value.key, state=value.state.value, value=value.value, reason=value.reason, revision=revision, updated_at=now)
            if row is None:
                try:
                    db.execute(insert(capability_overrides).values(**values))
                except IntegrityError:
                    raise RevisionConflict("capability override was concurrently created") from None
            else:
                result = db.execute(update(capability_overrides).where(
                    capability_overrides.c.model_entry_id == value.model_entry_id,
                    capability_overrides.c.capability_key == value.key,
                    capability_overrides.c.revision == current_revision,
                ).values(**values))
                if result.rowcount != 1:
                    raise RevisionConflict("capability override revision changed")
        return replace(value, revision=revision, updated_at=parse_utc(now))

    def add_capability_observation(self, observation: dict[str, object]) -> None:
        self._ensure_open()
        with self._engine.begin() as db:
            db.execute(insert(capability_observations).values(id=observation["id"], model_entry_id=observation["model_entry_id"], capability_key=observation["key"], observed_state=observation["state"], detail=str(observation.get("detail", ""))[:500], observed_at=observation.get("observed_at", _now())))

    def get_application_generation_config(self) -> tuple[GenerationSettings, str | None, int]:
        self._ensure_open()
        with self._engine.connect() as db:
            row = db.execute(select(application_generation_config).where(application_generation_config.c.id == 1)).first()
        if row is None:
            raise StateError("application generation configuration is missing")
        return _settings(row._mapping), row._mapping["default_model_entry_id"], int(row._mapping["revision"])

    def get_application_generation_settings(self) -> GenerationSettings:
        return self.get_application_generation_config()[0]

    def set_application_generation_settings(self, value: GenerationSettings, *, expected_revision: int | None = None) -> int:
        _validate_settings_value(value)
        with self._engine.begin() as db:
            row = db.execute(
                select(application_generation_config).where(application_generation_config.c.id == 1)
            ).first()
            if row is None:
                raise StateError("application generation configuration is missing")
            current_revision = int(row._mapping["revision"])
            if expected_revision is not None and current_revision != expected_revision:
                raise RevisionConflict("application generation settings revision changed")
            revision = current_revision + 1
            result = db.execute(update(application_generation_config).where(
                application_generation_config.c.id == 1,
                application_generation_config.c.revision == current_revision,
            ).values(
                temperature=str(0.0 if value.temperature is None else value.temperature), max_output_tokens=(1024 if value.max_output_tokens is None else value.max_output_tokens),
                reasoning_effort=value.reasoning_effort, timeout_seconds=None if value.timeout_seconds is None else str(value.timeout_seconds),
                revision=revision, updated_at=_now(),
            ))
            if result.rowcount != 1:
                raise RevisionConflict("application generation settings revision changed")
        return revision

    def set_application_default_model(self, model_entry_id: str, *, expected_revision: int | None = None) -> int:
        with self._engine.begin() as db:
            model = db.execute(
                select(
                    model_catalogue_entries.c.id,
                    model_catalogue_entries.c.availability,
                    provider_connections.c.enabled,
                    provider_connections.c.retired,
                )
                .select_from(
                    model_catalogue_entries.join(
                        provider_connections,
                        provider_connections.c.id == model_catalogue_entries.c.connection_id,
                    )
                )
                .where(model_catalogue_entries.c.id == model_entry_id)
            ).first()
            if (
                model is None
                or model._mapping["availability"] != CatalogueAvailability.AVAILABLE.value
                or model._mapping["enabled"] is not True
                or model._mapping["retired"] is not False
            ):
                raise StateError("application default model must be available")
            row = db.execute(
                select(application_generation_config).where(application_generation_config.c.id == 1)
            ).first()
            if row is None:
                raise StateError("application generation configuration is missing")
            current_revision = int(row._mapping["revision"])
            if expected_revision is not None and current_revision != expected_revision:
                raise RevisionConflict("application default model revision changed")
            revision = current_revision + 1
            result = db.execute(
                update(application_generation_config)
                .where(
                    application_generation_config.c.id == 1,
                    application_generation_config.c.revision == current_revision,
                )
                .values(default_model_entry_id=model_entry_id, revision=revision, updated_at=_now())
            )
            if result.rowcount != 1:
                raise RevisionConflict("application default model revision changed")
        return revision

    def get_model_generation_settings(self, model_entry_id: str) -> GenerationSettings | None:
        return self.get_model_generation_config(model_entry_id)[0]

    def get_model_generation_config(self, model_entry_id: str) -> tuple[GenerationSettings | None, int | None]:
        self._ensure_open()
        with self._engine.connect() as db:
            row = db.execute(select(model_generation_config).where(model_generation_config.c.model_entry_id == model_entry_id)).first()
        return (None, None) if row is None else (_settings(row._mapping), int(row._mapping["revision"]))

    def set_model_generation_settings(self, model_entry_id: str, value: GenerationSettings, *, expected_revision: int | None = None) -> int:
        return self._set_generation_config(model_generation_config, {"model_entry_id": model_entry_id}, value, expected_revision=expected_revision)

    def get_chat_model_generation_settings(self, chat_id: str, model_entry_id: str) -> GenerationSettings | None:
        return self.get_chat_model_generation_config(chat_id, model_entry_id)[0]

    def get_chat_model_generation_config(self, chat_id: str, model_entry_id: str) -> tuple[GenerationSettings | None, int | None]:
        self._ensure_open()
        with self._engine.connect() as db:
            row = db.execute(select(chat_model_generation_config).where(chat_model_generation_config.c.chat_id == chat_id, chat_model_generation_config.c.model_entry_id == model_entry_id)).first()
        return (None, None) if row is None else (_settings(row._mapping), int(row._mapping["revision"]))

    def set_chat_model_generation_settings(self, chat_id: str, model_entry_id: str, value: GenerationSettings, *, expected_revision: int | None = None) -> int:
        return self._set_generation_config(chat_model_generation_config, {"chat_id": chat_id, "model_entry_id": model_entry_id}, value, expected_revision=expected_revision)

    def _set_generation_config(self, table, identity: dict[str, object], value: GenerationSettings, *, expected_revision: int | None = None) -> int:
        self._ensure_open()
        _validate_settings_value(value)
        with self._engine.begin() as db:
            where = [getattr(table.c, key) == item for key, item in identity.items()]
            row = db.execute(select(table).where(*where)).first()
            current_revision = 0 if row is None else int(row._mapping["revision"])
            if expected_revision is not None and current_revision != expected_revision:
                raise RevisionConflict("generation settings revision changed")
            revision = current_revision + 1
            values = dict(identity, temperature=None if value.temperature is None else str(value.temperature), max_output_tokens=value.max_output_tokens, reasoning_effort=value.reasoning_effort, timeout_seconds=None if value.timeout_seconds is None else str(value.timeout_seconds), revision=revision, updated_at=_now())
            if row is None:
                try:
                    db.execute(insert(table).values(**values))
                except IntegrityError:
                    raise RevisionConflict("generation settings were concurrently created") from None
            else:
                result = db.execute(
                    update(table)
                    .where(*where, table.c.revision == current_revision)
                    .values(**values)
                )
                if result.rowcount != 1:
                    raise RevisionConflict("generation settings revision changed")
        return revision

    def get_chat_model_selection(self, chat_id: str) -> ModelSelection | None:
        self._ensure_open()
        with self._engine.connect() as db:
            row = db.execute(select(chat_model_selection).where(chat_model_selection.c.chat_id == chat_id)).first()
        if row is None:
            return None
        return ModelSelection(row._mapping["chat_id"], row._mapping["model_entry_id"], bool(row._mapping["selection_required"]), int(row._mapping["revision"]))

    def set_chat_model_selection(self, chat_id: str, model_entry_id: str | None, *, expected_revision: int | None = None) -> ModelSelection:
        self._ensure_open()
        with self._engine.begin() as db:
            if model_entry_id is not None:
                model_exists = db.execute(
                    select(model_catalogue_entries.c.id).where(
                        model_catalogue_entries.c.id == model_entry_id
                    )
                ).first()
                if model_exists is None:
                    raise StateError("selected model entry does not exist")
            values = {
                "model_entry_id": model_entry_id,
                "selection_required": model_entry_id is None,
                "updated_at": _now(),
            }
            if expected_revision is None:
                result = db.execute(
                    update(chat_model_selection)
                    .where(chat_model_selection.c.chat_id == chat_id)
                    .values(**values, revision=chat_model_selection.c.revision + 1)
                )
                if result.rowcount == 0:
                    try:
                        db.execute(
                            insert(chat_model_selection).values(
                                chat_id=chat_id,
                                revision=1,
                                **values,
                            )
                        )
                    except IntegrityError:
                        raise RevisionConflict("chat model selection was concurrently created") from None
            elif expected_revision == 0:
                try:
                    db.execute(
                        insert(chat_model_selection).values(
                            chat_id=chat_id,
                            revision=1,
                            **values,
                        )
                    )
                except IntegrityError:
                    raise RevisionConflict("chat model selection revision changed") from None
            else:
                result = db.execute(
                    update(chat_model_selection)
                    .where(
                        chat_model_selection.c.chat_id == chat_id,
                        chat_model_selection.c.revision == expected_revision,
                    )
                    .values(**values, revision=expected_revision + 1)
                )
                if result.rowcount != 1:
                    raise RevisionConflict("chat model selection revision changed")
            row = db.execute(
                select(chat_model_selection).where(chat_model_selection.c.chat_id == chat_id)
            ).first()
            if row is None:
                raise StateError("chat model selection disappeared")
            return ModelSelection(
                row._mapping["chat_id"],
                row._mapping["model_entry_id"],
                bool(row._mapping["selection_required"]),
                int(row._mapping["revision"]),
            )


def replace_provider(value: ProviderConnection, **changes) -> ProviderConnection:
    from dataclasses import replace

    return replace(value, **changes)
