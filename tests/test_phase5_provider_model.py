from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from collections import UserDict
from contextlib import contextmanager

import pytest
import httpx
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import IntegrityError as SqlAlchemyIntegrityError

from bots5.core.application import BotsApplication
from bots5.core.errors import RevisionConflict, StateError
from bots5.core.events import EventBus
from bots5.core.generation import GenerationCompleted, GenerationDelta, GenerationDispatched, GenerationRequest
from bots5.core.provider_configuration import ProviderConfiguration, resolve_capability
from bots5.domain.clock import SystemClock
from bots5.domain.ids import Uuid7Factory
from bots5.domain.provider import (
    BackendType,
    CatalogueRefreshFailureClass,
    CatalogueRefreshStatus,
    CapabilityFact,
    CapabilityKey,
    CapabilityOverride,
    CapabilitySource,
    CapabilityState,
    CatalogueAvailability,
    CatalogueOrigin,
    CredentialSource,
    GenerationSettings,
    ProviderProfile,
    builtin_connection_definitions,
)
from bots5.domain.models import AttemptState, Chat, MessageState
from bots5.errors import ProviderError
from bots5.desktop.profile import DesktopSessionInfo
from bots5.desktop.window import MainWindow
from bots5.desktop.widgets import AddConnectionDialog, SettingsDialog, TopBar, TuneDialog
from bots5.infrastructure.generation.fake import FakeStreamingBackend
from bots5.infrastructure.persistence import migration_runner
from tests._authority_test_support import (
    SQLiteAppStateStore,
    phase7_guarded_raw_mutation,
    upgrade_database,
    upgrade_to as authority_upgrade_to,
)
from bots5.infrastructure.persistence.phase5_validation import validate_phase5_snapshot
from bots5.infrastructure.secrets import FakeSecretStore, SecretServiceStore, SecretStoreError
from bots5.providers.base import CompletionRequest
from bots5.providers.discovery import (
    DiscoveredModel,
    FakeModelDiscoverer,
    ModelDiscoveryError,
    OpenAICompatibleModelDiscoverer,
)
from bots5.providers.openrouter import OpenRouterProvider


REPO = Path(__file__).resolve().parents[1]
MIGRATIONS = REPO / "src/bots5/infrastructure/persistence/migrations"


def _upgrade_to(database: Path, revision: str) -> None:
    authority_upgrade_to(database, revision)


@contextmanager
def _engine_connection(store, *, transaction: bool = False):
    """Supply the explicit test grant required for private Engine probes."""
    with store.command_admission():
        scope = store.engine.begin() if transaction else store.engine.connect()
        with scope as connection:
            yield connection


def _configured_application(tmp_path: Path, backend=None, *, secret_store=None):
    database = tmp_path / "state.sqlite3"
    upgrade_database(database)
    ids = Uuid7Factory()
    clock = SystemClock()
    store = SQLiteAppStateStore.open(database)
    configuration = ProviderConfiguration(
        store,
        ids,
        clock,
        secret_stores=({CredentialSource.SECRET_SERVICE: secret_store} if secret_store is not None else None),
        phase6_enabled=False,
    )
    application = BotsApplication(
        store,
        EventBus(clock, ids, queue_size=64),
        backend or FakeStreamingBackend(),
        ids=ids,
        clock=clock,
        configuration=configuration,
    )
    return application, store


def test_fresh_phase5_seed_and_pre_phase5_chat_selection_required(tmp_path: Path):
    database = tmp_path / "state.sqlite3"
    _upgrade_to(database, "0006_phase4_workspace")
    legacy_store = SQLiteAppStateStore.open(database)
    try:
        now = datetime(2026, 9, 5, tzinfo=UTC)
        legacy_store.create_chat(Chat("old", "Old", now, now))
    finally:
        legacy_store.close()
    upgrade_database(database)
    store = SQLiteAppStateStore.open(database)
    try:
        connections = store.list_provider_connections()
        models = store.list_model_catalogue_entries()
        assert len(connections) == 1
        assert connections[0].backend_type is BackendType.FAKE
        assert models[0].provider_model_id == "fake-v0.1"
        assert store.get_chat_model_selection("old") is None
        with _engine_connection(store) as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0010_phase7_search_navigation"
    finally:
        store.close()


def test_selection_required_model_selector_can_choose_the_only_visible_model():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    bar = TopBar(DesktopSessionInfo(backend_id="fake", model="legacy"), phase5=True)
    selected: list[str] = []
    bar.model_selected.connect(selected.append)
    bar.set_models((("Built-in fake / fake-v0.1 [available]", "model-1"),), None)

    assert bar.model_selector.currentIndex() == -1
    bar.model_selector.setCurrentIndex(0)
    assert selected == ["model-1"]


def test_connection_catalogue_identity_revision_and_endpoint_invalidation(tmp_path: Path):
    async def scenario():
        application, store = _configured_application(tmp_path)
        try:
            connection = await application.create_provider_connection(
                name=" Local   Ollama ",
                backend_type=BackendType.OPENAI_COMPATIBLE_HTTP,
                profile=ProviderProfile.GENERIC,
                endpoint="http://192.168.50.223:11434/v1/",
            )
            assert connection.name == "Local Ollama"
            assert connection.endpoint == "http://192.168.50.223:11434/v1"
            with pytest.raises(StateError, match="already exists"):
                await application.create_provider_connection(
                    name="local ollama",
                    backend_type=BackendType.OPENAI_COMPATIBLE_HTTP,
                    profile=ProviderProfile.GENERIC,
                    endpoint="http://127.0.0.1:1/v1",
                )
            manual = await application.add_manual_model(
                connection_id=connection.id,
                provider_model_id="Qwen3.5:35B-A3B",
            )
            refreshed = await application.refresh_models(
                connection.id,
                FakeModelDiscoverer((DiscoveredModel("Qwen3.5:35B-A3B"),)),
            )
            confirmed = next(item for item in refreshed if item.provider_model_id == manual.provider_model_id)
            assert confirmed.id == manual.id
            assert confirmed.origin is CatalogueOrigin.MANUAL_CONFIRMED
            second = await application.create_provider_connection(
                name="Other endpoint",
                backend_type=BackendType.OPENAI_COMPATIBLE_HTTP,
                profile=ProviderProfile.GENERIC,
                endpoint="http://127.0.0.1:9000/v1",
            )
            other = await application.add_manual_model(connection_id=second.id, provider_model_id="Qwen3.5:35B-A3B")
            assert other.id != manual.id
            disabled = await application.set_provider_connection_enabled(
                second.id, False, expected_revision=second.revision
            )
            assert store.get_model_catalogue_entry(other.id).availability is CatalogueAvailability.DISCONNECTED
            enabled = await application.set_provider_connection_enabled(
                second.id, True, expected_revision=disabled.revision
            )
            assert enabled.revision == disabled.revision + 1
            assert store.get_model_catalogue_entry(other.id).availability is CatalogueAvailability.AVAILABLE
            edited = replace(connection, endpoint="http://192.168.50.223:11435/v1")
            updated = await application.edit_provider_connection(edited, expected_revision=connection.revision)
            assert updated.catalogue_revision == connection.catalogue_revision + 2
            changed = store.get_model_catalogue_entry(manual.id)
            assert changed is not None and changed.availability is CatalogueAvailability.STALE
            assert store.get_model_catalogue_entry(other.id).availability is CatalogueAvailability.AVAILABLE
        finally:
            await application.close()

    asyncio.run(scenario())


def test_new_fake_connection_refresh_seeds_fake_capabilities_for_generation(tmp_path: Path):
    async def scenario():
        application, store = _configured_application(tmp_path)
        try:
            connection = await application.create_provider_connection(
                name="Another deterministic fake",
                backend_type=BackendType.FAKE,
                profile=ProviderProfile.GENERIC,
            )
            await application.refresh_models(
                connection.id,
                FakeModelDiscoverer((DiscoveredModel("fake-v0.1"),)),
            )
            model = store.get_model_catalogue_entry_by_provider_id(connection.id, "fake-v0.1")
            assert model is not None
            chat = await application.create_chat()
            await application.select_model(chat.id, model.id)
            prepared, _snapshot = application._configuration.prepare_generation(
                chat_id=chat.id,
                user_message_id="01900000-0000-7000-8000-000000000007",
                prompt="hello",
                attempt_id="01900000-0000-7000-8000-000000000008",
            )
            assert prepared.request.backend_id == BackendType.FAKE.value
            assert {item.key for item in prepared.capabilities} >= {
                CapabilityKey.STREAMING.value,
                CapabilityKey.TEMPERATURE.value,
                CapabilityKey.MAX_OUTPUT_TOKENS.value,
            }
        finally:
            await application.close()

    asyncio.run(scenario())


def test_provider_connection_edit_cannot_mutate_lifecycle_or_resurrect_tombstone(tmp_path: Path):
    async def scenario():
        application, store = _configured_application(tmp_path)
        try:
            connection = await application.create_provider_connection(
                name="Tombstone",
                backend_type=BackendType.FAKE,
                profile=ProviderProfile.GENERIC,
            )
            retired = await application.retire_provider_connection(
                connection.id,
                expected_revision=connection.revision,
            )
            with pytest.raises(StateError, match="lifecycle"):
                await application.edit_provider_connection(
                    replace(retired, enabled=True, retired=False),
                    expected_revision=retired.revision,
                )
            assert store.get_provider_connection(connection.id).retired is True
        finally:
            await application.close()

    asyncio.run(scenario())


def test_application_default_connection_retirement_requires_replacement_at_public_and_sqlite_boundaries(
    tmp_path: Path,
):
    async def scenario():
        application, store = _configured_application(tmp_path)
        try:
            connection = await application.create_provider_connection(
                name="Default retirement",
                backend_type=BackendType.FAKE,
                profile=ProviderProfile.GENERIC,
            )
            model = await application.add_manual_model(
                connection_id=connection.id,
                provider_model_id="default-retirement-model",
            )
            await application.set_application_default_model(model.id)
            with pytest.raises(StateError, match="lifecycle"):
                await application.edit_provider_connection(
                    replace(connection, enabled=False, retired=True),
                    expected_revision=connection.revision,
                )
            assert store.get_application_generation_config()[1] == model.id
            with _engine_connection(store, transaction=True) as db:
                with pytest.raises(SqlAlchemyIntegrityError, match="replacement"):
                    db.execute(
                        text(
                            "UPDATE provider_connections SET enabled = 0, retired = 1 "
                            "WHERE id = :connection_id"
                        ),
                        {"connection_id": connection.id},
                    )
        finally:
            await application.close()

    asyncio.run(scenario())


def test_raw_retired_connection_cannot_be_resurrected_after_restart(tmp_path: Path):
    async def scenario():
        application, _store = _configured_application(tmp_path)
        try:
            connection = await application.create_provider_connection(
                name="Raw tombstone",
                backend_type=BackendType.FAKE,
                profile=ProviderProfile.GENERIC,
            )
            retired = await application.retire_provider_connection(
                connection.id,
                expected_revision=connection.revision,
            )
        finally:
            await application.close()

        reopened = SQLiteAppStateStore.open(tmp_path / "state.sqlite3")
        try:
            with _engine_connection(reopened, transaction=True) as database:
                with pytest.raises(SqlAlchemyIntegrityError, match="resurrected"):
                    database.execute(
                        text(
                            "UPDATE provider_connections SET retired = 0, enabled = 1, revision = :revision WHERE id = :connection_id"
                        ),
                        {"revision": retired.revision + 1, "connection_id": retired.id},
                    )
        finally:
            reopened.close()
        reopened = SQLiteAppStateStore.open(tmp_path / "state.sqlite3")
        try:
            current = reopened.get_provider_connection(retired.id)
            assert current is not None and current.retired and not current.enabled
        finally:
            reopened.close()

    asyncio.run(scenario())


def test_raw_connection_identity_edit_cannot_preserve_catalogue_authority(tmp_path: Path):
    async def scenario():
        application, store = _configured_application(tmp_path)
        try:
            connection = await application.create_provider_connection(
                name="Raw identity guard",
                backend_type=BackendType.OPENAI_COMPATIBLE_HTTP,
                profile=ProviderProfile.GENERIC,
                endpoint="http://127.0.0.1:9010/v1",
            )
            model = await application.add_manual_model(
                connection_id=connection.id,
                provider_model_id="identity-guard-model",
            )
            before = store.get_provider_connection(connection.id)
            assert before is not None
            with _engine_connection(store, transaction=True) as db:
                with pytest.raises(SqlAlchemyIntegrityError, match="core authority"):
                    db.execute(
                        text(
                            "UPDATE provider_connections SET endpoint = :endpoint, "
                            "revision = revision + 1, catalogue_revision = catalogue_revision + 1 "
                            "WHERE id = :connection_id"
                        ),
                        {"endpoint": "http://127.0.0.1:9011/v1", "connection_id": connection.id},
                    )
            after = store.get_provider_connection(connection.id)
            assert after is not None
            assert after.endpoint == before.endpoint
            assert after.catalogue_revision == before.catalogue_revision
            assert store.get_model_catalogue_entry(model.id).availability is CatalogueAvailability.AVAILABLE
        finally:
            await application.close()

    asyncio.run(scenario())


def test_raw_unavailable_model_cannot_become_application_default(tmp_path: Path):
    async def scenario():
        application, store = _configured_application(tmp_path)
        try:
            connection = await application.create_provider_connection(
                name="Unavailable default source",
                backend_type=BackendType.FAKE,
                profile=ProviderProfile.GENERIC,
            )
            model = await application.add_manual_model(
                connection_id=connection.id,
                provider_model_id="unavailable-default-model",
            )
            await application.retire_provider_connection(
                connection.id,
                expected_revision=connection.revision,
            )
            with _engine_connection(store, transaction=True) as db:
                with pytest.raises(SqlAlchemyIntegrityError, match="default model"):
                    db.execute(
                        text(
                            "UPDATE application_generation_config "
                            "SET default_model_entry_id = :model_id, revision = revision + 1 "
                            "WHERE id = 1"
                        ),
                        {"model_id": model.id},
                    )
            assert store.get_application_generation_config()[1] != model.id
        finally:
            await application.close()

    asyncio.run(scenario())


def test_openrouter_required_auth_is_enforced_by_core_and_raw_sqlite_authority(tmp_path: Path):
    async def scenario():
        application, store = _configured_application(tmp_path)
        try:
            with pytest.raises(StateError, match="credential source"):
                await application.create_provider_connection(
                    name="Unauthenticated OpenRouter",
                    backend_type=BackendType.OPENAI_COMPATIBLE_HTTP,
                    profile=ProviderProfile.OPENROUTER,
                    endpoint="https://openrouter.ai/api/v1",
                )
            connection = await application.create_provider_connection(
                name="OpenRouter auth",
                backend_type=BackendType.OPENAI_COMPATIBLE_HTTP,
                profile=ProviderProfile.OPENROUTER,
                endpoint="https://openrouter.ai/api/v1",
                credential_source=CredentialSource.ENVIRONMENT,
                credential_reference="PHASE5_TEST_KEY",
            )
            with pytest.raises(StateError, match="credential source"):
                await application.edit_provider_connection(
                    replace(
                        connection,
                        credential_source=CredentialSource.NONE,
                        credential_reference=None,
                    ),
                    expected_revision=connection.revision,
                )
            with _engine_connection(store, transaction=True) as db:
                with pytest.raises(SqlAlchemyIntegrityError, match="OpenRouter|provider connection"):
                    db.execute(
                        text(
                            "UPDATE provider_connections SET credential_source = 'none', "
                            "credential_reference = NULL WHERE id = :connection_id"
                        ),
                        {"connection_id": connection.id},
                    )
        finally:
            await application.close()

    asyncio.run(scenario())


def test_connection_identity_edit_stales_all_models_and_discards_old_capability_facts(tmp_path: Path):
    async def scenario():
        application, store = _configured_application(tmp_path)
        try:
            connection = store.get_provider_connection("01900000-0000-7000-8000-000000000005")
            assert connection is not None
            model = store.get_model_catalogue_entry("01900000-0000-7000-8000-000000000006")
            assert model is not None
            assert store.list_capability_facts(model.id)
            updated = await application.edit_provider_connection(
                replace(
                    connection,
                    backend_type=BackendType.OPENAI_COMPATIBLE_HTTP,
                    profile=ProviderProfile.GENERIC,
                    endpoint="http://127.0.0.1:9123/v1",
                ),
                expected_revision=connection.revision,
            )
            assert updated.catalogue_revision == connection.catalogue_revision + 1
            stale = store.get_model_catalogue_entry(model.id)
            assert stale is not None and stale.availability is CatalogueAvailability.STALE
            assert stale.revision == model.revision + 1
            assert store.list_capability_facts(model.id) == ()
        finally:
            await application.close()

    asyncio.run(scenario())


def test_catalogue_revision_is_store_derived_and_cannot_regress(tmp_path: Path):
    async def scenario():
        application, store = _configured_application(tmp_path)
        try:
            connection = (await application.list_provider_connections())[0]
            model = store.get_model_catalogue_entry("01900000-0000-0000-0000-000000000000")
            model = model or store.get_model_catalogue_entry("01900000-0000-7000-8000-000000000006")
            assert model is not None
            store.set_capability_fact(
                CapabilityFact(
                    model.id,
                    CapabilityKey.OUTPUT_TOKENS.value,
                    CapabilityState.SUPPORTED,
                    CapabilitySource.PROVIDER_METADATA,
                    source_revision=connection.catalogue_revision,
                    value=7,
                )
            )
            refreshed = await application.refresh_models(connection.id, FakeModelDiscoverer(()))
            current = (await application.list_provider_connections())[0]
            assert current.catalogue_revision == connection.catalogue_revision + 1
            assert {
                item.key: item for item in application._configuration.resolve_capabilities(model.id)
            }[CapabilityKey.OUTPUT_TOKENS.value].state is CapabilityState.UNKNOWN

            stale_object = replace(current, catalogue_revision=0)
            edited = await application.edit_provider_connection(
                stale_object,
                expected_revision=current.revision,
            )
            assert edited.catalogue_revision == current.catalogue_revision
            assert {
                item.key: item for item in application._configuration.resolve_capabilities(model.id)
            }[CapabilityKey.OUTPUT_TOKENS.value].state is CapabilityState.UNKNOWN
            with _engine_connection(store, transaction=True) as db:
                with pytest.raises(SqlAlchemyIntegrityError, match="catalogue revision"):
                    db.execute(
                        text(
                            "UPDATE provider_connections SET catalogue_revision = 0 "
                            "WHERE id = :connection_id"
                        ),
                        {"connection_id": connection.id},
                    )
        finally:
            await application.close()

    asyncio.run(scenario())


def test_generic_http_phase5_request_keeps_generic_profile_attribution(tmp_path: Path):
    async def scenario():
        application, store = _configured_application(tmp_path)
        try:
            connection = await application.create_provider_connection(
                name="Generic endpoint",
                backend_type=BackendType.OPENAI_COMPATIBLE_HTTP,
                profile=ProviderProfile.GENERIC,
                endpoint="http://127.0.0.1:9000/v1",
            )
            model = await application.add_manual_model(
                connection_id=connection.id,
                provider_model_id="generic-model",
            )
            for key in (
                CapabilityKey.STREAMING.value,
                CapabilityKey.TEMPERATURE.value,
                CapabilityKey.MAX_OUTPUT_TOKENS.value,
            ):
                await application.set_capability_override(
                    CapabilityOverride(model.id, key, CapabilityState.SUPPORTED)
                )
            chat = await application.create_chat()
            await application.select_model(chat.id, model.id)
            attempt = await application.send_message(chat.id, "hello")
            await asyncio.sleep(0.05)
            attempt = store.get_generation_attempt(attempt.id)
            assert attempt is not None
            assert attempt.provider_id == "generic"
            assert attempt.state is AttemptState.COMPLETE
            assert attempt.finish_reason == "stop"
            assert store.list_generation_attempts(chat.id)[0].provider_id == "generic"
        finally:
            await application.close()

    asyncio.run(scenario())


def test_phase5_complete_requires_exact_stop_in_authoritative_store(tmp_path: Path):
    class HoldingBackend:
        def __init__(self):
            self.release = asyncio.Event()

        async def stream(self, request):
            await self.release.wait()
            if False:
                yield GenerationDelta(request.attempt_id, "")

    async def scenario():
        backend = HoldingBackend()
        application, store = _configured_application(tmp_path, backend)
        try:
            chat = await application.create_chat()
            started = await application.send_message(chat.id, "complete")
            await asyncio.sleep(0.01)
            stored = store.get_generation_attempt(started.id)
            assistant = None if stored is None else store.get_message(stored.assistant_message_id)
            assert stored is not None and assistant is not None and stored.state is AttemptState.RUNNING
            with pytest.raises(StateError, match="finish_reason"):
                store.finalize_generation(
                    replace(assistant, state=MessageState.COMPLETE, content="answer"),
                    replace(
                        stored,
                        state=AttemptState.COMPLETE,
                        ended_at=stored.started_at,
                        finish_reason="length",
                    ),
                )
            with pytest.raises(StateError, match="finish_reason"):
                store.finalize_generation(
                    replace(assistant, state=MessageState.FAILED, content="partial"),
                    replace(
                        stored,
                        state=AttemptState.FAILED,
                        ended_at=stored.started_at,
                        error_type="provider_failure",
                        error_message="provider failed",
                        finish_reason="stop",
                    ),
                )
            with pytest.raises(StateError, match="unknown outcome"):
                store.finalize_generation(
                    replace(assistant, state=MessageState.COMPLETE, content="answer"),
                    replace(
                        stored,
                        state=AttemptState.COMPLETE,
                        ended_at=stored.started_at,
                        finish_reason="stop",
                        remote_outcome_unknown=True,
                    ),
                )
            assert store.get_generation_attempt(started.id).state is AttemptState.RUNNING
        finally:
            await application.close()

    asyncio.run(scenario())


def test_phase5_model_output_limit_is_checked_from_limits_capability(tmp_path: Path):
    async def scenario():
        application, store = _configured_application(tmp_path)
        try:
            chat = await application.create_chat()
            model_id = (await application.chat_model_selection(chat.id)).model_entry_id
            assert model_id is not None
            store.set_capability_fact(
                CapabilityFact(
                    model_id,
                    CapabilityKey.OUTPUT_TOKENS.value,
                    CapabilityState.SUPPORTED,
                    CapabilitySource.PROVIDER_METADATA,
                    source_revision=1,
                    value=8,
                )
            )
            await application.set_application_generation_settings(
                GenerationSettings(max_output_tokens=9)
            )
            with pytest.raises(StateError, match="resolved model limit"):
                await application.send_message(chat.id, "too much")
            assert store.list_generation_attempts(chat.id) == ()
        finally:
            await application.close()

    asyncio.run(scenario())


def test_manual_model_accepts_bounded_nonempty_metadata(tmp_path: Path):
    async def scenario():
        application, store = _configured_application(tmp_path)
        try:
            connection = (await application.list_provider_connections())[0]
            model = await application.add_manual_model(
                connection_id=connection.id,
                provider_model_id="manual-with-metadata",
                metadata={"owned_by": "operator"},
            )
            assert store.get_model_catalogue_entry(model.id).metadata == {"owned_by": "operator"}
        finally:
            await application.close()

    asyncio.run(scenario())


def test_manual_model_promotion_survives_failed_refresh_and_publishes_change(tmp_path: Path):
    class FailingDiscoverer:
        async def discover(self, connection, credential):
            raise RuntimeError("deterministic discovery failure")

    async def scenario():
        application, store = _configured_application(tmp_path)
        subscription = application.subscribe()
        try:
            connection = await application.create_provider_connection(
                name="Catalogue endpoint",
                backend_type=BackendType.OPENAI_COMPATIBLE_HTTP,
                profile=ProviderProfile.GENERIC,
                endpoint="http://127.0.0.1:9001/v1",
            )
            discovered = await application.refresh_models(
                connection.id,
                FakeModelDiscoverer((DiscoveredModel("same-model"),)),
            )
            original = next(item for item in discovered if item.provider_model_id == "same-model")
            promoted = await application.add_manual_model(
                connection_id=connection.id,
                provider_model_id="same-model",
            )
            assert promoted.id == original.id
            assert promoted.origin is CatalogueOrigin.MANUAL_CONFIRMED
            revision = promoted.revision
            with pytest.raises(RuntimeError, match="discovery failure"):
                await application.refresh_models(connection.id, FailingDiscoverer())
            current = store.get_model_catalogue_entry(promoted.id)
            assert current is not None
            assert current.origin is CatalogueOrigin.MANUAL_CONFIRMED
            assert current.availability is CatalogueAvailability.AVAILABLE
            assert current.revision == revision
            event = await asyncio.wait_for(subscription.__anext__(), timeout=1)
            while event.kind != "model_catalogue_changed":
                event = await asyncio.wait_for(subscription.__anext__(), timeout=1)
            assert event.payload["connection_id"] == connection.id
        finally:
            subscription.close()
            await application.close()

    asyncio.run(scenario())


def test_catalogue_refresh_outcome_is_truthful_across_success_failure_and_restart(tmp_path: Path):
    def discoverer_for(kind: str):
        async def handler(request):
            if kind == "refused":
                raise httpx.ConnectError("connection refused", request=request)
            if kind == "timeout":
                raise httpx.ReadTimeout("timed out", request=request)
            if kind == "http":
                return httpx.Response(500, json={"error": "provider failure"}, request=request)
            if kind == "json":
                return httpx.Response(200, content=b"not-json", request=request)
            if kind == "shape":
                return httpx.Response(200, json={"models": []}, request=request)
            raise AssertionError(kind)

        return OpenAICompatibleModelDiscoverer(transport=httpx.MockTransport(handler))

    async def scenario():
        application, store = _configured_application(tmp_path)
        try:
            successful = await application.create_provider_connection(
                name="Successful catalogue",
                backend_type=BackendType.OPENAI_COMPATIBLE_HTTP,
                profile=ProviderProfile.GENERIC,
                endpoint="http://127.0.0.1:9015/v1",
            )
            models = await application.refresh_models(
                successful.id,
                FakeModelDiscoverer((DiscoveredModel("known-good"),)),
            )
            assert [item.provider_model_id for item in models] == ["known-good"]
            state = store.get_provider_connection(successful.id)
            assert state is not None
            assert state.catalogue_refresh_status is CatalogueRefreshStatus.SUCCEEDED
            assert state.catalogue_refresh_revision == state.catalogue_revision == 1
            assert state.catalogue_refresh_failure_class is None
            assert state.catalogue_refresh_failure_message is None

            manual = await application.add_manual_model(
                connection_id=successful.id,
                provider_model_id="manual-model",
            )
            with pytest.raises(ProviderError):
                await application.refresh_models(successful.id, discoverer_for("http"))
            failed = store.get_provider_connection(successful.id)
            assert failed is not None
            assert failed.catalogue_refresh_status is CatalogueRefreshStatus.FAILED
            assert failed.catalogue_refresh_revision == failed.catalogue_revision == 2
            assert failed.catalogue_refresh_failure_class is CatalogueRefreshFailureClass.PROVIDER_HTTP
            assert failed.catalogue_refresh_failure_message == "provider returned an HTTP error"
            assert store.get_model_catalogue_entry(models[0].id).availability is CatalogueAvailability.STALE
            assert store.get_model_catalogue_entry(manual.id).availability is CatalogueAvailability.AVAILABLE

            refreshed = await application.refresh_models(
                successful.id,
                FakeModelDiscoverer(()),
            )
            current = store.get_provider_connection(successful.id)
            assert current is not None
            assert current.catalogue_refresh_status is CatalogueRefreshStatus.SUCCEEDED
            assert current.catalogue_refresh_revision == current.catalogue_revision == 3
            assert current.catalogue_refresh_failure_class is None
            assert current.catalogue_refresh_failure_message is None
            assert store.get_model_catalogue_entry(models[0].id).availability is CatalogueAvailability.UNAVAILABLE
            assert store.get_model_catalogue_entry(manual.id).availability is CatalogueAvailability.AVAILABLE
            assert refreshed

            empty = await application.create_provider_connection(
                name="Successful empty catalogue",
                backend_type=BackendType.OPENAI_COMPATIBLE_HTTP,
                profile=ProviderProfile.GENERIC,
                endpoint="http://127.0.0.1:9016/v1",
            )
            await application.refresh_models(empty.id, FakeModelDiscoverer(()))
            empty_state = store.get_provider_connection(empty.id)
            assert empty_state is not None
            assert empty_state.catalogue_refresh_status is CatalogueRefreshStatus.SUCCEEDED
            assert empty_state.catalogue_refresh_revision == 1

            classes = {
                "refused": CatalogueRefreshFailureClass.TRANSPORT,
                "timeout": CatalogueRefreshFailureClass.TIMEOUT,
                "http": CatalogueRefreshFailureClass.PROVIDER_HTTP,
                "json": CatalogueRefreshFailureClass.PROTOCOL,
                "shape": CatalogueRefreshFailureClass.PROTOCOL,
            }
            failure_ids: dict[str, str] = {}
            for index, (kind, expected_class) in enumerate(classes.items(), start=17):
                connection = await application.create_provider_connection(
                    name=f"Failure {kind}",
                    backend_type=BackendType.OPENAI_COMPATIBLE_HTTP,
                    profile=ProviderProfile.GENERIC,
                    endpoint=f"http://127.0.0.1:{index}/v1",
                )
                failure_ids[kind] = connection.id
                with pytest.raises(ProviderError):
                    await application.refresh_models(connection.id, discoverer_for(kind))
                observed = store.get_provider_connection(connection.id)
                assert observed is not None
                assert observed.catalogue_refresh_status is CatalogueRefreshStatus.FAILED
                assert observed.catalogue_refresh_revision == observed.catalogue_revision == 1
                assert observed.catalogue_refresh_failure_class is expected_class
                assert observed.catalogue_refresh_failure_message

            await application.close()
            reopened = SQLiteAppStateStore.open(tmp_path / "state.sqlite3")
            try:
                reopened_empty = reopened.get_provider_connection(empty.id)
                reopened_failed = reopened.get_provider_connection(failure_ids["http"])
                assert reopened_empty is not None and reopened_failed is not None
                assert reopened_empty.catalogue_refresh_status is CatalogueRefreshStatus.SUCCEEDED
                assert reopened_empty.catalogue_refresh_failure_class is None
                assert reopened_failed.catalogue_refresh_status is CatalogueRefreshStatus.FAILED
                assert reopened_failed.catalogue_refresh_failure_class is CatalogueRefreshFailureClass.PROVIDER_HTTP

                with _engine_connection(reopened) as database:
                    raw = database.execute(
                        text(
                            "SELECT connection_id, status, failure_class, failure_message "
                            "FROM catalogue_refresh_state"
                        )
                    ).fetchall()
                    by_connection = {row[0]: row[1:] for row in raw}
                    assert by_connection[empty.id] == ("succeeded", None, None)
                    assert by_connection[failure_ids["http"]] == (
                        "failed", "provider_http", "provider returned an HTTP error"
                    )
            finally:
                reopened.close()
            return
        finally:
            if not application._closed:
                await application.close()

    asyncio.run(scenario())


def test_failed_catalogue_refresh_survives_restart_and_requery_without_secret_leak(tmp_path: Path):
    async def scenario():
        secret = "SENTINEL_REFRESH_DIAGNOSTIC_SECRET"
        secret_store = FakeSecretStore({"refresh-ref": secret})
        application, store = _configured_application(tmp_path, secret_store=secret_store)
        try:
            connection = await application.create_provider_connection(
                name="Failed refresh restart",
                backend_type=BackendType.OPENAI_COMPATIBLE_HTTP,
                profile=ProviderProfile.GENERIC,
                endpoint="http://127.0.0.1:9019/v1",
                credential_source=CredentialSource.SECRET_SERVICE,
                credential_reference="refresh-ref",
            )

            async def handler(request):
                return httpx.Response(503, json={"error": "nope"}, request=request)

            with pytest.raises(ProviderError) as caught:
                await application.refresh_models(
                    connection.id,
                    OpenAICompatibleModelDiscoverer(transport=httpx.MockTransport(handler)),
                )
            assert secret not in str(caught.value)
            observed = store.get_provider_connection(connection.id)
            assert observed is not None
            assert observed.catalogue_refresh_status is CatalogueRefreshStatus.FAILED
            assert observed.catalogue_refresh_failure_class is CatalogueRefreshFailureClass.PROVIDER_HTTP
            assert secret not in repr(observed)
            with _engine_connection(store) as database:
                dump = " ".join(
                    str(value)
                    for row in database.execute(text("SELECT * FROM catalogue_refresh_state"))
                    for value in row
                )
                assert secret not in dump
                assert "refresh-ref" not in dump
        finally:
            await application.close()

        reopened = SQLiteAppStateStore.open(tmp_path / "state.sqlite3")
        try:
            state = reopened.get_provider_connection(connection.id)
            assert state is not None
            assert state.catalogue_refresh_status is CatalogueRefreshStatus.FAILED
            assert state.catalogue_refresh_failure_class is CatalogueRefreshFailureClass.PROVIDER_HTTP
            assert state.catalogue_refresh_failure_message == "provider returned an HTTP error"
            assert secret not in repr(state)
        finally:
            reopened.close()

    asyncio.run(scenario())


def test_raw_catalogue_refresh_outcome_cannot_be_forged_or_revised_in_place(tmp_path: Path):
    async def scenario():
        application, store = _configured_application(tmp_path)
        try:
            connection = await application.create_provider_connection(
                name="Raw refresh outcome guard",
                backend_type=BackendType.OPENAI_COMPATIBLE_HTTP,
                profile=ProviderProfile.GENERIC,
                endpoint="http://127.0.0.1:9020/v1",
            )
            await application.refresh_models(connection.id, FakeModelDiscoverer(()))
            current = store.get_provider_connection(connection.id)
            assert current is not None
            assert current.catalogue_refresh_status is CatalogueRefreshStatus.SUCCEEDED
            assert current.catalogue_refresh_revision == current.catalogue_revision == 1

            with _engine_connection(store, transaction=True) as database:
                with pytest.raises(SqlAlchemyIntegrityError, match="requires core authority"):
                    database.execute(
                        text(
                            "UPDATE catalogue_refresh_state SET status = 'failed', "
                            "failure_class = 'transport', "
                            "failure_message = 'provider is unreachable', "
                            "updated_at = 'not-a-timestamp' "
                            "WHERE connection_id = :connection_id"
                        ),
                        {"connection_id": connection.id},
                    )
            with _engine_connection(store, transaction=True) as database:
                with pytest.raises(SqlAlchemyIntegrityError, match="requires core authority"):
                    database.execute(
                        text(
                            "UPDATE provider_connections SET catalogue_revision = catalogue_revision + 1 "
                            "WHERE id = :connection_id"
                        ),
                        {"connection_id": connection.id},
                    )

            unchanged = store.get_provider_connection(connection.id)
            assert unchanged is not None
            assert unchanged.catalogue_refresh_status is CatalogueRefreshStatus.SUCCEEDED
            assert unchanged.catalogue_refresh_revision == 1
        finally:
            await application.close()

    asyncio.run(scenario())


def test_catalogue_refresh_outcome_event_requery_converges_between_store_views(tmp_path: Path):
    async def scenario():
        application, store = _configured_application(tmp_path)
        subscription = application.subscribe()
        try:
            connection = await application.create_provider_connection(
                name="Refresh convergence",
                backend_type=BackendType.OPENAI_COMPATIBLE_HTTP,
                profile=ProviderProfile.GENERIC,
                endpoint="http://127.0.0.1:9021/v1",
            )
            with pytest.raises(ProviderError):
                await application.refresh_models(
                    connection.id,
                    ModelFailingDiscoverer(
                        CatalogueRefreshFailureClass.TRANSPORT,
                    ),
                )
            event = await asyncio.wait_for(subscription.__anext__(), timeout=1)
            while event.kind != "model_catalogue_changed":
                event = await asyncio.wait_for(subscription.__anext__(), timeout=1)
            # Multiple windows share one core/store authority.  A second
            # independent store is intentionally rejected while the core
            # owns this root; re-query the authoritative store instead.
            observed = store.get_provider_connection(connection.id)
            assert observed is not None
            assert observed.catalogue_refresh_status is CatalogueRefreshStatus.FAILED
            assert observed.catalogue_refresh_failure_class is CatalogueRefreshFailureClass.TRANSPORT
            assert store.get_provider_connection(connection.id).catalogue_refresh_revision == observed.catalogue_refresh_revision
        finally:
            subscription.close()
            await application.close()

    class ModelFailingDiscoverer:
        def __init__(self, failure_class):
            self.failure_class = failure_class

        async def discover(self, connection, credential):
            raise ModelDiscoveryError(self.failure_class, "deterministic transport failure")

    asyncio.run(scenario())


def test_discovery_exception_is_sanitized_and_connection_cas_rejects_stale_result(tmp_path: Path):
    async def scenario():
        secret = "SENTINEL_DISCOVERY_SECRET"
        secret_store = FakeSecretStore({"old-ref": secret})
        application, store = _configured_application(tmp_path, secret_store=secret_store)
        try:
            connection = await application.create_provider_connection(
                name="Credentialed endpoint",
                backend_type=BackendType.OPENAI_COMPATIBLE_HTTP,
                profile=ProviderProfile.GENERIC,
                endpoint="http://127.0.0.1:9003/v1",
                credential_source=CredentialSource.SECRET_SERVICE,
                credential_reference="old-ref",
            )

            class LeakyDiscoverer:
                async def discover(self, connection, credential):
                    raise RuntimeError(f"transport saw Authorization: Bearer {credential}")

            with pytest.raises(ProviderError) as caught:
                await application.refresh_models(connection.id, LeakyDiscoverer())
            assert secret not in str(caught.value)
            assert "[REDACTED]" in str(caught.value)

            started = asyncio.Event()
            release = asyncio.Event()

            class BlockingDiscoverer:
                async def discover(self, connection, credential):
                    started.set()
                    await release.wait()
                    return (DiscoveredModel("old-tenant-model"),)

            refresh = asyncio.create_task(application.refresh_models(connection.id, BlockingDiscoverer()))
            await asyncio.wait_for(started.wait(), timeout=1)
            updated = await application.edit_provider_connection(
                replace(connection, credential_reference="new-ref"),
                expected_revision=connection.revision,
            )
            assert updated.revision == connection.revision + 1
            assert updated.catalogue_revision == connection.catalogue_revision + 2
            release.set()
            with pytest.raises(RevisionConflict, match="stale"):
                await refresh
            assert store.list_model_catalogue_entries(connection.id) == ()
        finally:
            await application.close()

    asyncio.run(scenario())


def test_discovery_echoed_credential_is_rejected_before_catalogue_persistence(tmp_path: Path):
    async def scenario():
        secret = "SENTINEL_ECHOED_DISCOVERY_CREDENTIAL"
        secret_store = FakeSecretStore({"echo-ref": secret})
        application, store = _configured_application(tmp_path, secret_store=secret_store)
        received_authorization: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            received_authorization.append(request.headers.get("Authorization"))
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "echoed-model",
                            "owned_by": secret,
                        }
                    ]
                },
            )

        try:
            connection = await application.create_provider_connection(
                name="Echoing catalogue endpoint",
                backend_type=BackendType.OPENAI_COMPATIBLE_HTTP,
                profile=ProviderProfile.GENERIC,
                endpoint="http://127.0.0.1:9005/v1",
                credential_source=CredentialSource.SECRET_SERVICE,
                credential_reference="echo-ref",
            )
            discoverer = OpenAICompatibleModelDiscoverer(
                transport=httpx.MockTransport(handler)
            )
            with pytest.raises(ProviderError, match="credential material") as caught:
                await application.refresh_models(connection.id, discoverer)
            assert secret not in str(caught.value)
            assert received_authorization == [f"Bearer {secret}"]
            assert store.list_model_catalogue_entries(connection.id) == ()
            with _engine_connection(store) as db:
                values = db.execute(
                    text(
                        "SELECT metadata_json, display_name, provider_model_id "
                        "FROM model_catalogue_entries"
                    )
                ).fetchall()
            assert secret not in repr(values)

            class MappingRecord:
                def as_record(self):
                    return UserDict(
                        {
                            "id": "mapping-" + secret,
                            "display_name": "safe",
                            "metadata": UserDict(),
                        }
                    )

            class MappingDiscoverer:
                async def discover(self, connection, credential):
                    return (MappingRecord(),)

            with pytest.raises(ProviderError, match="credential material") as caught:
                await application.refresh_models(connection.id, MappingDiscoverer())
            assert secret not in str(caught.value)
            assert store.list_model_catalogue_entries(connection.id) == ()
        finally:
            await application.close()

    asyncio.run(scenario())


def test_stale_credential_form_cannot_write_under_new_reference(tmp_path: Path):
    async def scenario():
        secret_store = FakeSecretStore()
        application, _store = _configured_application(tmp_path, secret_store=secret_store)
        try:
            connection = await application.create_provider_connection(
                name="Credential form",
                backend_type=BackendType.OPENAI_COMPATIBLE_HTTP,
                profile=ProviderProfile.GENERIC,
                endpoint="http://127.0.0.1:9004/v1",
                credential_source=CredentialSource.SECRET_SERVICE,
                credential_reference="old-ref",
            )
            updated = await application.edit_provider_connection(
                replace(connection, credential_reference="new-ref"),
                expected_revision=connection.revision,
            )
            await application.save_connection_credential(
                connection.id,
                "SENTINEL_CURRENT_SECRET",
                expected_revision=updated.revision,
                expected_credential_reference=updated.credential_reference,
            )
            with pytest.raises(RevisionConflict, match="stale"):
                await application.save_connection_credential(
                    connection.id,
                    "SENTINEL_STALE_FORM_SECRET",
                    expected_revision=connection.revision,
                    expected_credential_reference=connection.credential_reference,
                )
            with pytest.raises(RevisionConflict, match="stale"):
                await application.delete_connection_credential(
                    connection.id,
                    expected_revision=connection.revision,
                    expected_credential_reference=connection.credential_reference,
                )
            assert secret_store.values == {"new-ref": "SENTINEL_CURRENT_SECRET"}
        finally:
            await application.close()

    asyncio.run(scenario())


def test_failed_credential_save_does_not_retain_secret_in_exception_graph(tmp_path: Path):
    async def scenario():
        secret = "SENTINEL_FAILED_CREDENTIAL_SAVE"
        secret_store = FakeSecretStore(failures={"failed-ref"})
        application, _store = _configured_application(tmp_path, secret_store=secret_store)

        def assert_exception_graph_is_clean(error: BaseException) -> None:
            pending = [error]
            seen: set[int] = set()
            while pending:
                current = pending.pop()
                if id(current) in seen:
                    continue
                seen.add(id(current))
                assert secret not in repr(current)
                traceback = current.__traceback__
                while traceback is not None:
                    assert secret not in repr(traceback.tb_frame.f_locals)
                    traceback = traceback.tb_next
                if current.__cause__ is not None:
                    pending.append(current.__cause__)
                if current.__context__ is not None:
                    pending.append(current.__context__)

        try:
            connection = await application.create_provider_connection(
                name="Failed credential save",
                backend_type=BackendType.OPENAI_COMPATIBLE_HTTP,
                profile=ProviderProfile.GENERIC,
                endpoint="http://127.0.0.1:9018/v1",
                credential_source=CredentialSource.SECRET_SERVICE,
                credential_reference="failed-ref",
            )
            task = asyncio.create_task(
                application.save_connection_credential(
                    connection.id,
                    secret,
                    expected_revision=connection.revision,
                    expected_credential_reference=connection.credential_reference,
                )
            )
            failed = (await asyncio.gather(task, return_exceptions=True))[0]
            assert isinstance(failed, SecretStoreError)
            assert_exception_graph_is_clean(failed)

            stale_task = asyncio.create_task(
                application.save_connection_credential(
                    connection.id,
                    secret,
                    expected_revision=connection.revision + 1,
                    expected_credential_reference=connection.credential_reference,
                )
            )
            stale = (await asyncio.gather(stale_task, return_exceptions=True))[0]
            assert isinstance(stale, RevisionConflict)
            assert "stale" in str(stale)
            assert_exception_graph_is_clean(stale)
            assert secret_store.values == {}
        finally:
            await application.close()

    asyncio.run(scenario())


def test_phase5_snapshot_rejects_casefolded_nested_secret_and_bad_provenance(tmp_path: Path):
    async def scenario():
        application, store = _configured_application(tmp_path)
        try:
            chat = await application.create_chat()
            prepared, snapshot_text = application._configuration.prepare_generation(
                chat_id=chat.id,
                user_message_id="user-id",
                prompt="hello",
                attempt_id="phase5-snapshot-test",
            )
            payload = json.loads(snapshot_text)
            payload["capability_provenance"][CapabilityKey.STREAMING.value]["apiKey"] = "SENTINEL"
            with pytest.raises(ValueError, match="secret"):
                validate_phase5_snapshot(
                    json.dumps(payload),
                    attempt_id="phase5-snapshot-test",
                    chat_id=chat.id,
                    user_message_id="user-id",
                    backend_id=prepared.request.backend_id,
                    model=prepared.request.model,
                    provider_id=prepared.request.provider_id,
                )
            payload = json.loads(snapshot_text)
            payload["capability_provenance"][CapabilityKey.STREAMING.value] = {"source": "unknown", "unexpected": 1}
            with pytest.raises(ValueError, match="provenance"):
                validate_phase5_snapshot(
                    json.dumps(payload),
                    attempt_id="phase5-snapshot-test",
                    chat_id=chat.id,
                    user_message_id="user-id",
                    backend_id=prepared.request.backend_id,
                    model=prepared.request.model,
                    provider_id=prepared.request.provider_id,
                )
            payload = json.loads(snapshot_text)
            payload["capability_provenance"][CapabilityKey.STREAMING.value] = {
                "source": "trusted_registry",
                "catalogue_revision": 999,
            }
            with pytest.raises(ValueError, match="provenance"):
                validate_phase5_snapshot(
                    json.dumps(payload),
                    attempt_id="phase5-snapshot-test",
                    chat_id=chat.id,
                    user_message_id="user-id",
                    backend_id=prepared.request.backend_id,
                    model=prepared.request.model,
                    provider_id=prepared.request.provider_id,
                )
            payload = json.loads(snapshot_text)
            payload["credential_status"] = "available"
            with pytest.raises(ValueError, match="credential status"):
                validate_phase5_snapshot(
                    json.dumps(payload),
                    attempt_id="phase5-snapshot-test",
                    chat_id=chat.id,
                    user_message_id="user-id",
                    backend_id=prepared.request.backend_id,
                    model=prepared.request.model,
                    provider_id=prepared.request.provider_id,
                )
            payload = json.loads(snapshot_text)
            payload["omitted_settings"]["timeout_seconds"] = {"reason": "unset"}
            with pytest.raises(ValueError, match="omitted"):
                validate_phase5_snapshot(
                    json.dumps(payload),
                    attempt_id="phase5-snapshot-test",
                    chat_id=chat.id,
                    user_message_id="user-id",
                    backend_id=prepared.request.backend_id,
                    model=prepared.request.model,
                    provider_id=prepared.request.provider_id,
                )
        finally:
            await application.close()

    asyncio.run(scenario())


def test_tune_does_not_pin_an_inherited_timeout():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    dialog = TuneDialog()
    emitted = []
    dialog.save_requested.connect(emitted.append)
    dialog.set_settings(
        {
            "temperature": 0.0,
            "max_output_tokens": 1024,
            "reasoning_effort": None,
            "timeout_seconds": 4.0,
        },
        {
            "temperature": "application",
            "max_output_tokens": "application",
            "reasoning_effort": "application",
            "timeout_seconds": "application",
        },
    )
    dialog._save()
    assert emitted and emitted[-1]["timeout_seconds"] is None
    dialog.set_settings(
        {"temperature": 0.0, "max_output_tokens": 1024, "reasoning_effort": None},
        {"temperature": "chat_model", "max_output_tokens": "application", "reasoning_effort": "application"},
        timeout_override=7.5,
        model_entry_id="model-1",
    )
    dialog._save()
    assert emitted[-1]["timeout_seconds"] == 7.5
    inherited = []
    dialog.inherit_requested.connect(inherited.append)
    dialog._use_inherited()
    assert inherited[-1]["timeout_seconds"] is None
    dialog.close()
    app.processEvents()


def test_phase5_rejects_nonfinite_timeout_at_public_store_boundary(tmp_path: Path):
    async def scenario():
        application, store = _configured_application(tmp_path)
        try:
            for value in (float("nan"), float("inf"), float("-inf")):
                with pytest.raises(StateError, match="timeout"):
                    await application.set_application_generation_settings(
                        GenerationSettings(timeout_seconds=value)
                    )
            assert store.get_application_generation_settings().timeout_seconds is None
        finally:
            await application.close()

    asyncio.run(scenario())


def test_openrouter_emits_explicit_reasoning_none_field():
    provider = OpenRouterProvider("deterministic-secret")
    payload = provider._payload(
        CompletionRequest(
            model="openai/gpt-5",
            system="",
            user="hello",
            temperature=0.0,
            max_output_tokens=32,
            timeout_seconds=0.0,
            reasoning_effort="none",
        ),
        stream=True,
        include_empty_system=False,
    )
    assert payload["reasoning_effort"] == "none"


def test_stale_provider_metadata_capability_is_not_effective_after_refresh(tmp_path: Path):
    async def scenario():
        application, store = _configured_application(tmp_path)
        try:
            connection = (await application.list_provider_connections())[0]
            model = store.get_model_catalogue_entry("01900000-0000-7000-8000-000000000006")
            assert model is not None
            store.set_capability_fact(
                CapabilityFact(
                    model.id,
                    CapabilityKey.OUTPUT_TOKENS.value,
                    CapabilityState.SUPPORTED,
                    CapabilitySource.PROVIDER_METADATA,
                    source_revision=connection.catalogue_revision,
                    value=8,
                )
            )
            await application.refresh_models(connection.id, FakeModelDiscoverer(()))
            resolved = {
                item.key: item for item in application._configuration.resolve_capabilities(model.id)
            }
            assert resolved[CapabilityKey.OUTPUT_TOKENS.value].state is CapabilityState.UNKNOWN
            assert resolved[CapabilityKey.OUTPUT_TOKENS.value].value is None
        finally:
            await application.close()

    asyncio.run(scenario())


def test_endpoint_capability_facts_require_a_catalogue_revision(tmp_path: Path):
    application, store = _configured_application(tmp_path)
    try:
        model = store.get_model_catalogue_entry("01900000-0000-7000-8000-000000000006")
        assert model is not None
        for source in (CapabilitySource.PROVIDER_METADATA, CapabilitySource.CONFIRMED_ENDPOINT):
            with pytest.raises(StateError, match="malformed"):
                store.set_capability_fact(
                    CapabilityFact(
                        model.id,
                        CapabilityKey.OUTPUT_TOKENS.value,
                        CapabilityState.SUPPORTED,
                        source,
                        source_revision=None,
                        value=8,
                    )
                )
    finally:
        asyncio.run(application.close())


def test_raw_chat_model_selection_cannot_commit_a_contradictory_state(tmp_path: Path):
    database = tmp_path / "raw-chat-selection.sqlite3"
    upgrade_database(database)
    store = SQLiteAppStateStore.open(database)
    try:
        now = datetime(2026, 9, 5, tzinfo=UTC)
        store.create_chat(Chat("raw-selection", "Raw selection", now, now))
        with _engine_connection(store, transaction=True) as connection:
            connection.execute(
                text(
                    "INSERT INTO chat_model_selection "
                    "(chat_id, model_entry_id, selection_required, revision, updated_at) "
                    "VALUES ('raw-selection', :model_id, 0, 1, :updated_at)"
                ),
                {
                    "model_id": "01900000-0000-7000-8000-000000000006",
                    "updated_at": "2026-09-05T00:00:00.000Z",
                },
            )
            with pytest.raises(SqlAlchemyIntegrityError, match="selection"):
                connection.execute(
                    text(
                        "UPDATE chat_model_selection SET selection_required = 1 "
                        "WHERE chat_id = 'raw-selection'"
                    )
                )
    finally:
        store.close()


def test_current_phase5_open_rejects_missing_guard_or_malformed_settings(tmp_path: Path):
    missing_trigger_db = tmp_path / "missing-trigger.sqlite3"
    upgrade_database(missing_trigger_db)
    with sqlite3.connect(missing_trigger_db) as connection:
        connection.execute("DROP TRIGGER phase5_attempt_attribution_insert")
    with pytest.raises(RuntimeError, match="trigger"):
        SQLiteAppStateStore.open(missing_trigger_db)

    inert_trigger_db = tmp_path / "inert-trigger.sqlite3"
    upgrade_database(inert_trigger_db)
    with sqlite3.connect(inert_trigger_db) as connection:
        connection.execute("DROP TRIGGER phase5_provider_connection_validate_insert")
        connection.execute(
            """
            CREATE TRIGGER phase5_provider_connection_validate_insert
            BEFORE INSERT ON provider_connections
            WHEN abs(-0) = 1 AND bots5_valid_phase5_connection(
                NEW.name, NEW.name_key, NEW.backend_type, NEW.profile, NEW.endpoint,
                NEW.credential_source, NEW.credential_reference, NEW.enabled,
                NEW.retired, NEW.revision, NEW.catalogue_revision
            ) = 0
            BEGIN SELECT RAISE(ABORT, 'Phase 5 provider connection is malformed'); END
            """
        )
    with pytest.raises(RuntimeError, match="not enforcing|not migration-authoritative"):
        SQLiteAppStateStore.open(inert_trigger_db)

    inert_catalogue_trigger_db = tmp_path / "inert-catalogue-trigger.sqlite3"
    upgrade_database(inert_catalogue_trigger_db)
    with sqlite3.connect(inert_catalogue_trigger_db) as connection:
        connection.execute("DROP TRIGGER phase5_model_catalogue_metadata_validate_insert")
        connection.execute(
            """
            CREATE TRIGGER phase5_model_catalogue_metadata_validate_insert
            BEFORE INSERT ON model_catalogue_entries
            WHEN NOT (
                json_valid(NEW.metadata_json) = 1
                AND json_type(NEW.metadata_json) = 'object'
                AND NOT EXISTS (SELECT 1 FROM json_tree(NEW.metadata_json))
            ) AND (2 = 3)
            BEGIN SELECT RAISE(ABORT, 'Phase 5 model catalogue metadata is malformed'); END
            """
        )
    with pytest.raises(RuntimeError, match="not enforcing|not migration-authoritative"):
        SQLiteAppStateStore.open(inert_catalogue_trigger_db)

    inert_fact_trigger_db = tmp_path / "inert-fact-trigger.sqlite3"
    upgrade_database(inert_fact_trigger_db)
    with sqlite3.connect(inert_fact_trigger_db) as connection:
        connection.execute("DROP TRIGGER phase5_capability_fact_truth_validate_insert")
        connection.execute(
            """
            CREATE TRIGGER phase5_capability_fact_truth_validate_insert
            BEFORE INSERT ON capability_facts
            WHEN (2 = 3) AND NEW.capability_key NOT IN ('generation.streaming')
            BEGIN SELECT RAISE(ABORT, 'Phase 5 capability fact truth is malformed'); END
            """
        )
    with pytest.raises(RuntimeError, match="not enforcing|not migration-authoritative"):
        SQLiteAppStateStore.open(inert_fact_trigger_db)

    inert_observation_trigger_db = tmp_path / "inert-observation-trigger.sqlite3"
    upgrade_database(inert_observation_trigger_db)
    with sqlite3.connect(inert_observation_trigger_db) as connection:
        connection.execute("DROP TRIGGER phase5_capability_observation_truth_validate_insert")
        connection.execute(
            """
            CREATE TRIGGER phase5_capability_observation_truth_validate_insert
            BEFORE INSERT ON capability_observations
            WHEN (2 = 3) AND NEW.observed_state NOT IN ('supported')
            BEGIN SELECT RAISE(ABORT, 'Phase 5 capability observation truth is malformed'); END
            """
        )
    with pytest.raises(RuntimeError, match="not enforcing|not migration-authoritative"):
        SQLiteAppStateStore.open(inert_observation_trigger_db)

    disabled_trigger_db = tmp_path / "disabled-trigger.sqlite3"
    upgrade_database(disabled_trigger_db)
    with sqlite3.connect(disabled_trigger_db) as connection:
        connection.execute("DROP TRIGGER phase5_attempt_attribution_insert")
        connection.execute(
            """
            CREATE TRIGGER phase5_attempt_attribution_insert
            BEFORE INSERT ON generation_attempts
            WHEN json_extract(NEW.request_snapshot, '$.snapshot_version') = 2 AND 0
            BEGIN SELECT RAISE(ABORT, 'Phase 5 attempt attribution is inconsistent'); END
            """
        )
    with pytest.raises(RuntimeError, match="disabled trigger|not migration-authoritative"):
        SQLiteAppStateStore.open(disabled_trigger_db)

    disabled_completion_db = tmp_path / "disabled-completion-trigger.sqlite3"
    upgrade_database(disabled_completion_db)
    with sqlite3.connect(disabled_completion_db) as connection:
        connection.execute("DROP TRIGGER generation_attempt_phase5_completion_insert")
        connection.execute(
            """
            CREATE TRIGGER generation_attempt_phase5_completion_insert
            BEFORE INSERT ON generation_attempts
            WHEN (1 = 0) AND json_extract(NEW.request_snapshot, '$.snapshot_version') = 2
            BEGIN SELECT RAISE(ABORT, 'Phase 5 generation finish_reason is inconsistent with state'); END
            """
        )
    with pytest.raises(RuntimeError, match="disabled trigger|not migration-authoritative"):
        SQLiteAppStateStore.open(disabled_completion_db)

    malformed_settings_db = tmp_path / "malformed-settings.sqlite3"
    upgrade_database(malformed_settings_db)
    with sqlite3.connect(malformed_settings_db) as connection:
        connection.execute("DROP TRIGGER phase5_application_settings_validate_update")
        connection.execute(
            "UPDATE application_generation_config SET timeout_seconds = 'nan' WHERE id = 1"
        )
        connection.execute(
            """
            CREATE TRIGGER phase5_application_settings_validate_update
            BEFORE UPDATE OF temperature, max_output_tokens, reasoning_effort,
                timeout_seconds ON application_generation_config
            WHEN bots5_valid_phase5_settings(
                NEW.temperature, NEW.max_output_tokens, NEW.reasoning_effort,
                NEW.timeout_seconds, 1, 1
            ) = 0
            BEGIN SELECT RAISE(ABORT, 'Phase 5 generation settings are malformed'); END
            """
        )
    with pytest.raises(RuntimeError, match="rows|trigger"):
        SQLiteAppStateStore.open(malformed_settings_db)


def test_raw_phase5_metadata_and_current_declared_types_are_authoritative(tmp_path: Path):
    database = tmp_path / "raw-phase5-boundary.sqlite3"
    upgrade_database(database)
    with sqlite3.connect(database) as connection:
        for metadata in (
            '{"outer":{"api-Key":"SENTINEL_RAW_SECRET"}}',
            '{"outer":{"api$key":"SENTINEL_RAW_SECRET"}}',
            '{"outer":{"api•key":"SENTINEL_RAW_SECRET"}}',
            '{"outer":{"APİ-KEY":"SENTINEL_RAW_SECRET"}}',
            '{"outer":{"APIKEY":"SENTINEL_RAW_SECRET"}}',
            json.dumps({"outer": {"api\u0000key": "SENTINEL_RAW_SECRET"}}),
        ):
            with pytest.raises(sqlite3.IntegrityError, match="metadata"):
                connection.execute(
                    "UPDATE model_catalogue_entries SET metadata_json = ? WHERE id = ?",
                    (metadata, "01900000-0000-7000-8000-000000000006"),
                )
        connection.execute(
            "UPDATE model_catalogue_entries SET metadata_json = ? WHERE id = ?",
            ('{"outer":{"apricotKey":"benign"}}', "01900000-0000-7000-8000-000000000006"),
        )
        assert connection.execute(
            "SELECT metadata_json FROM model_catalogue_entries WHERE id = ?",
            ("01900000-0000-7000-8000-000000000006",),
        ).fetchone()[0] == '{"outer":{"apricotKey":"benign"}}'
        connection.execute(
            "UPDATE model_catalogue_entries SET metadata_json = '{}' WHERE id = ?",
            ("01900000-0000-7000-8000-000000000006",),
        )
        with pytest.raises(sqlite3.IntegrityError, match="provenance"):
            connection.execute(
                "UPDATE capability_facts SET provenance_json = ? WHERE id = ?",
                (
                    '{"nested":{"api$key":"SENTINEL_RAW_SECRET"}}',
                    "01900000-0000-7000-8000-000000000006-cap-generation-streaming",
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match="provenance"):
            connection.execute(
                "UPDATE capability_facts SET provenance_json = ? WHERE id = ?",
                (
                    '{"unexpected":1}',
                    "01900000-0000-7000-8000-000000000006-cap-generation-streaming",
                ),
            )
        assert connection.execute(
            "SELECT metadata_json FROM model_catalogue_entries WHERE id = ?",
            ("01900000-0000-7000-8000-000000000006",),
        ).fetchone()[0] == "{}"
        with pytest.raises(sqlite3.IntegrityError, match="identity"):
            connection.execute(
                "INSERT INTO capability_facts "
                "(id, model_entry_id, capability_key, state, source, source_revision, "
                "value, provenance_json, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "duplicate-capability-fact",
                    "01900000-0000-7000-8000-000000000006",
                    "generation.streaming",
                    "unsupported",
                    "trusted_registry",
                    1,
                    None,
                    "{}",
                    "2026-09-05T00:00:00.000Z",
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match="truth"):
            connection.execute(
                "INSERT INTO capability_facts "
                "(id, model_entry_id, capability_key, state, source, source_revision, "
                "value, provenance_json, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "manual-capability-fact",
                    "01900000-0000-7000-8000-000000000006",
                    "generation.streaming",
                    "supported",
                    "manual",
                    1,
                    None,
                    "{}",
                    "2026-09-05T00:00:00.000Z",
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match="truth"):
            connection.execute(
                "INSERT INTO capability_facts "
                "(id, model_entry_id, capability_key, state, source, source_revision, "
                "value, provenance_json, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "unsupported-limit-fact",
                    "01900000-0000-7000-8000-000000000006",
                    "limits.output_tokens",
                    "unsupported",
                    "provider_metadata",
                    1,
                    8,
                    "{}",
                    "2026-09-05T00:00:00.000Z",
                ),
            )
        for suffix, key, revision, observed_at in (
            ("vocabulary", "outside.closed.vocabulary", 1, "2026-09-05T00:00:00.000Z"),
            ("revision", "generation.streaming", -99, "2026-09-05T00:00:00.000Z"),
            ("timestamp", "generation.streaming", 1, "not-a-timestamp"),
        ):
            with pytest.raises(sqlite3.IntegrityError, match="truth"):
                connection.execute(
                    "INSERT INTO capability_facts "
                    "(id, model_entry_id, capability_key, state, source, source_revision, "
                    "value, provenance_json, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        f"invalid-capability-fact-{suffix}",
                        "01900000-0000-7000-8000-000000000006",
                        key,
                        "unknown",
                        "provider_metadata",
                        revision,
                        None,
                        "{}",
                        observed_at,
                    ),
                )
        connection.execute(
            "INSERT INTO capability_observations "
            "(id, model_entry_id, capability_key, observed_state, detail, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "probe-observation",
                "01900000-0000-7000-8000-000000000006",
                "generation.streaming",
                "unknown",
                "probe",
                "2026-09-05T00:00:00.000Z",
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="observation"):
            connection.execute(
                "UPDATE capability_observations SET observed_at = 'not-a-timestamp' "
                "WHERE id = 'probe-observation'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="truth"):
            connection.execute(
                "UPDATE capability_facts SET observed_at = 'not-a-timestamp' "
                "WHERE id = '01900000-0000-7000-8000-000000000006-cap-generation-streaming'"
            )
        connection.execute(
            "INSERT INTO capability_overrides "
            "(model_entry_id, capability_key, state, value, reason, revision, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "01900000-0000-7000-8000-000000000006",
                "generation.streaming",
                "supported",
                None,
                "valid",
                1,
                "2026-09-05T00:00:00.000Z",
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="truth"):
            connection.execute(
                "UPDATE capability_overrides SET reason = ? "
                "WHERE model_entry_id = ? AND capability_key = 'generation.streaming'",
                ("x" * 257, "01900000-0000-7000-8000-000000000006"),
            )

    malformed_types = tmp_path / "wrong-phase5-type.sqlite3"
    upgrade_database(malformed_types)
    with sqlite3.connect(malformed_types) as connection:
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            "UPDATE sqlite_master SET sql = replace(sql, "
            "'capability_key VARCHAR(128) NOT NULL', "
            "'capability_key BLOB NOT NULL') "
            "WHERE type = 'table' AND name = 'capability_overrides'"
        )
        schema_version = connection.execute("PRAGMA schema_version").fetchone()[0]
        connection.execute(f"PRAGMA schema_version = {schema_version + 1}")
        connection.execute("PRAGMA writable_schema = OFF")
    with pytest.raises(RuntimeError, match="capability_overrides"):
        SQLiteAppStateStore.open(malformed_types)

    malformed_selection_types = tmp_path / "wrong-selection-phase5-type.sqlite3"
    upgrade_database(malformed_selection_types)
    with sqlite3.connect(malformed_selection_types) as connection:
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            "UPDATE sqlite_master SET sql = replace(sql, "
            "'updated_at VARCHAR(40) NOT NULL', "
            "'updated_at BLOB NOT NULL') "
            "WHERE type = 'table' AND name = 'chat_model_selection'"
        )
        schema_version = connection.execute("PRAGMA schema_version").fetchone()[0]
        connection.execute(f"PRAGMA schema_version = {schema_version + 1}")
        connection.execute("PRAGMA writable_schema = OFF")
    with pytest.raises(RuntimeError, match="chat_model_selection"):
        SQLiteAppStateStore.open(malformed_selection_types)

    malformed_selection_nullability = tmp_path / "wrong-selection-phase5-nullability.sqlite3"
    upgrade_database(malformed_selection_nullability)
    with sqlite3.connect(malformed_selection_nullability) as connection:
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            "UPDATE sqlite_master SET sql = replace(sql, "
            "'updated_at VARCHAR(40) NOT NULL', "
            "'updated_at VARCHAR(40)') "
            "WHERE type = 'table' AND name = 'chat_model_selection'"
        )
        schema_version = connection.execute("PRAGMA schema_version").fetchone()[0]
        connection.execute(f"PRAGMA schema_version = {schema_version + 1}")
        connection.execute("PRAGMA writable_schema = OFF")
    with pytest.raises(RuntimeError, match="chat_model_selection"):
        SQLiteAppStateStore.open(malformed_selection_nullability)

    malformed_constraints = tmp_path / "wrong-phase5-constraints.sqlite3"
    upgrade_database(malformed_constraints)
    with sqlite3.connect(malformed_constraints) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DROP TABLE capability_observations")
        connection.execute(
            """
            CREATE TABLE capability_observations (
                id VARCHAR(64) NOT NULL,
                model_entry_id VARCHAR(64) NOT NULL,
                capability_key VARCHAR(128) NOT NULL,
                observed_state VARCHAR(32) NOT NULL,
                detail TEXT NOT NULL,
                observed_at VARCHAR(40) NOT NULL
            )
            """
        )
        schema_version = connection.execute("PRAGMA schema_version").fetchone()[0]
        connection.execute(f"PRAGMA schema_version = {schema_version + 1}")
    with pytest.raises(RuntimeError, match="capability_observations"):
        SQLiteAppStateStore.open(malformed_constraints)

    missing_check = tmp_path / "missing-phase5-check.sqlite3"
    upgrade_database(missing_check)
    with sqlite3.connect(missing_check) as connection:
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            "UPDATE sqlite_master SET sql = replace(sql, ?, ?) "
            "WHERE type = 'table' AND name = 'capability_observations'",
            (
                "ck_capability_observation_state",
                "ck_removed_capability_observation_state",
            ),
        )
        schema_version = connection.execute("PRAGMA schema_version").fetchone()[0]
        connection.execute(f"PRAGMA schema_version = {schema_version + 1}")
        connection.execute("PRAGMA writable_schema = OFF")
    with pytest.raises(RuntimeError, match="CHECK constraints|not migration-authoritative"):
        SQLiteAppStateStore.open(missing_check)

    wrong_literal = tmp_path / "wrong-phase5-check-literal.sqlite3"
    upgrade_database(wrong_literal)
    with sqlite3.connect(wrong_literal) as connection:
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            "UPDATE sqlite_master SET sql = replace(sql, ?, ?) "
            "WHERE type = 'table' AND name = 'capability_observations'",
            (
                "('supported', 'unsupported', 'unknown')",
                "('SUPPORTED', 'UNSUPPORTED', 'UNKNOWN')",
            ),
        )
        schema_version = connection.execute("PRAGMA schema_version").fetchone()[0]
        connection.execute(f"PRAGMA schema_version = {schema_version + 1}")
        connection.execute("PRAGMA writable_schema = OFF")
    with pytest.raises(RuntimeError, match="malformed CHECK constraints|not migration-authoritative"):
        SQLiteAppStateStore.open(wrong_literal)

    tautological_check = tmp_path / "tautological-phase5-check.sqlite3"
    upgrade_database(tautological_check)
    with sqlite3.connect(tautological_check) as connection:
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            "UPDATE sqlite_master SET sql = replace(sql, ?, ?) "
            "WHERE type = 'table' AND name = 'model_catalogue_entries'",
            (
                "origin IN ('manual', 'discovered', 'manual_confirmed')",
                "origin IN ('manual', 'discovered', 'manual_confirmed') OR 1 = 1",
            ),
        )
        schema_version = connection.execute("PRAGMA schema_version").fetchone()[0]
        connection.execute(f"PRAGMA schema_version = {schema_version + 1}")
        connection.execute("PRAGMA writable_schema = OFF")
    with pytest.raises(RuntimeError, match="not enforcing|not migration-authoritative"):
        SQLiteAppStateStore.open(tautological_check)


def test_raw_application_settings_revision_and_timestamp_are_authoritative(tmp_path: Path):
    database = tmp_path / "raw-application-settings.sqlite3"
    upgrade_database(database)
    store = SQLiteAppStateStore.open(database)
    try:
        with _engine_connection(store, transaction=True) as connection:
            with pytest.raises(SqlAlchemyIntegrityError, match="generation settings"):
                connection.execute(
                    text("UPDATE application_generation_config SET revision = 0 WHERE id = 1")
                )
            with pytest.raises(SqlAlchemyIntegrityError, match="generation settings"):
                connection.execute(
                    text("UPDATE application_generation_config SET updated_at = 'not-a-timestamp' WHERE id = 1")
                )
    finally:
        store.close()
    store = SQLiteAppStateStore.open(database)
    try:
        assert store.get_application_generation_config()[2] == 1
    finally:
        store.close()


def test_unavailable_application_default_remains_honest_and_restartable(tmp_path: Path):
    database = tmp_path / "unavailable-default.sqlite3"
    upgrade_database(database)
    store = SQLiteAppStateStore.open(database)
    try:
        connection = store.get_provider_connection("01900000-0000-7000-8000-000000000005")
        assert connection is not None
        disabled = store.set_provider_connection_enabled(
            connection.id,
            False,
            expected_revision=connection.revision,
        )
        assert disabled.enabled is False
        settings, default_model_id, _ = store.get_application_generation_config()
        assert settings.max_output_tokens == 1024
        assert default_model_id == "01900000-0000-7000-8000-000000000006"
        assert store.get_model_catalogue_entry(default_model_id).availability is CatalogueAvailability.DISCONNECTED
    finally:
        store.close()

    reopened = SQLiteAppStateStore.open(database)
    try:
        assert reopened.get_application_generation_config()[1] == "01900000-0000-7000-8000-000000000006"
    finally:
        reopened.close()


def test_application_generation_settings_compare_and_swap_is_not_lost(tmp_path: Path):
    database = tmp_path / "application-settings-cas.sqlite3"
    upgrade_database(database)
    store = SQLiteAppStateStore.open(database)
    try:
        initial_revision = store.get_application_generation_config()[2]
        assert store.set_application_generation_settings(
            GenerationSettings(temperature=0.25),
            expected_revision=initial_revision,
        ) == initial_revision + 1
        with pytest.raises(RevisionConflict, match="settings revision"):
            store.set_application_generation_settings(
                GenerationSettings(temperature=0.75),
                expected_revision=initial_revision,
            )
        assert store.get_application_generation_settings().temperature == 0.25
    finally:
        store.close()


def test_model_chat_and_capability_updates_use_revision_predicates(tmp_path: Path):
    async def scenario():
        application, store = _configured_application(tmp_path)
        listeners = []
        try:
            model = (await application.list_model_catalogue())[0]
            chat = await application.create_chat()

            store.set_model_generation_settings(
                model.id,
                GenerationSettings(temperature=0.1),
                expected_revision=0,
            )
            store.set_chat_model_generation_settings(
                chat.id,
                model.id,
                GenerationSettings(temperature=0.2),
                expected_revision=0,
            )
            store.set_capability_override(
                CapabilityOverride(model.id, CapabilityKey.TEMPERATURE, CapabilityState.SUPPORTED),
                expected_revision=0,
            )

            def interleave(table_name, update_sql, parameters):
                injected = {"value": False}

                def before_update(_connection, cursor, statement, _parameters, _context, _executemany):
                    if not injected["value"] and table_name in statement and statement.lstrip().upper().startswith("UPDATE"):
                        cursor.execute(update_sql, parameters)
                        injected["value"] = True

                event.listen(store.engine, "before_cursor_execute", before_update)
                listeners.append((before_update, injected))
                return injected

            model_interleaved = interleave(
                "model_generation_config",
                "UPDATE model_generation_config SET temperature = '0.9', "
                "revision = revision + 1, updated_at = '2000-01-01T00:00:00.000Z' "
                "WHERE model_entry_id = ?",
                (model.id,),
            )
            with pytest.raises(RevisionConflict, match="settings revision"):
                store.set_model_generation_settings(
                    model.id,
                    GenerationSettings(temperature=0.3),
                    expected_revision=1,
                )
            assert model_interleaved["value"] is True
            event.remove(store.engine, "before_cursor_execute", listeners.pop()[0])

            chat_interleaved = interleave(
                "chat_model_generation_config",
                "UPDATE chat_model_generation_config SET temperature = '0.8', "
                "revision = revision + 1, updated_at = '2000-01-01T00:00:00.000Z' "
                "WHERE chat_id = ? AND model_entry_id = ?",
                (chat.id, model.id),
            )
            with pytest.raises(RevisionConflict, match="settings revision"):
                store.set_chat_model_generation_settings(
                    chat.id,
                    model.id,
                    GenerationSettings(temperature=0.4),
                    expected_revision=1,
                )
            assert chat_interleaved["value"] is True
            event.remove(store.engine, "before_cursor_execute", listeners.pop()[0])

            override_interleaved = interleave(
                "capability_overrides",
                "UPDATE capability_overrides SET reason = 'interleaved', "
                "revision = revision + 1, updated_at = '2000-01-01T00:00:00.000Z' "
                "WHERE model_entry_id = ? AND capability_key = ?",
                (model.id, CapabilityKey.TEMPERATURE.value),
            )
            with pytest.raises(RevisionConflict, match="override revision"):
                store.set_capability_override(
                    CapabilityOverride(
                        model.id,
                        CapabilityKey.TEMPERATURE,
                        CapabilityState.UNSUPPORTED,
                    ),
                    expected_revision=1,
                )
            assert override_interleaved["value"] is True
            event.remove(store.engine, "before_cursor_execute", listeners.pop()[0])
        finally:
            for listener, _injected in listeners:
                event.remove(store.engine, "before_cursor_execute", listener)
            await application.close()

    asyncio.run(scenario())


def test_application_default_model_rejects_a_retired_connection(tmp_path: Path):
    async def scenario():
        application, store = _configured_application(tmp_path)
        try:
            builtin = (await application.list_model_catalogue())[0]
            connection = await application.create_provider_connection(
                name="Retire me",
                backend_type=BackendType.FAKE,
                profile=ProviderProfile.GENERIC,
            )
            model = await application.add_manual_model(
                connection_id=connection.id,
                provider_model_id="retired-default",
            )
            await application.set_application_default_model(model.id)
            await application.retire_provider_connection(
                connection.id,
                expected_revision=connection.revision,
                replacement_model_entry_id=builtin.id,
            )
            with pytest.raises(StateError, match="available"):
                await application.set_application_default_model(model.id)
            assert store.get_application_generation_config()[1] == builtin.id
        finally:
            await application.close()

    asyncio.run(scenario())


def test_failed_oversized_discovery_is_atomic_and_publishes_catalogue_change(tmp_path: Path):
    async def scenario():
        application, store = _configured_application(tmp_path)
        subscription = application.subscribe()
        try:
            connection = await application.create_provider_connection(
                name="Malformed discovery",
                backend_type=BackendType.OPENAI_COMPATIBLE_HTTP,
                profile=ProviderProfile.GENERIC,
                endpoint="http://127.0.0.1:9002/v1",
            )
            with pytest.raises(StateError, match="discovery metadata"):
                await application.refresh_models(
                    connection.id,
                    FakeModelDiscoverer(
                        (DiscoveredModel("oversized", metadata={"context_length": 2**80}),)
                    ),
                )
            current = store.get_provider_connection(connection.id)
            assert current is not None and current.catalogue_revision == 1
            assert store.list_model_catalogue_entries(connection.id) == ()
            event = await asyncio.wait_for(subscription.__anext__(), timeout=1)
            while event.kind != "model_catalogue_changed":
                event = await asyncio.wait_for(subscription.__anext__(), timeout=1)
            assert event.payload["connection_id"] == connection.id
        finally:
            subscription.close()
            await application.close()

    asyncio.run(scenario())


def test_settings_dialog_reloads_model_defaults_and_preserves_per_field_inheritance():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    dialog = SettingsDialog()
    add_requests: list[bool] = []
    dialog.add_connection_requested.connect(lambda: add_requests.append(True))
    dialog.add_connection_button.click()
    assert add_requests == [True]
    connection = SimpleNamespace(
        id="connection",
        name="Connection",
        profile=SimpleNamespace(value="generic"),
        available=True,
        retired=False,
    )
    available = SimpleNamespace(value="available")
    models = (
        SimpleNamespace(id="model-a", connection_id="connection", provider_model_id="A", availability=available),
        SimpleNamespace(id="model-b", connection_id="connection", provider_model_id="B", availability=available),
    )
    dialog.set_connections((connection,))
    dialog.set_models(models, {connection.id: connection})
    emitted: list[dict[str, object]] = []
    dialog.model_defaults_requested.connect(emitted.append)
    dialog.set_model_defaults(
        GenerationSettings(temperature=0.75, max_output_tokens=4096, reasoning_effort="none", timeout_seconds=9.0),
        revision=7,
    )
    dialog.model_list.setCurrentRow(1)
    dialog._save_model_defaults()
    assert emitted == []
    dialog.set_model_defaults(
        GenerationSettings(temperature=None, max_output_tokens=2048, reasoning_effort=None, timeout_seconds=9.0),
        revision=9,
    )
    dialog._save_model_defaults()
    assert emitted[-1]["model_entry_id"] == "model-b"
    assert emitted[-1]["temperature"] is None
    assert emitted[-1]["max_output_tokens"] == 2048
    assert emitted[-1]["reasoning_effort"] is None
    assert emitted[-1]["timeout_seconds"] == 9.0
    application_emitted: list[dict[str, object]] = []
    dialog.application_defaults_requested.connect(application_emitted.append)
    dialog.set_application_defaults(
        SimpleNamespace(
            temperature=0.0,
            max_output_tokens=1024,
            reasoning_effort="none",
            timeout_seconds=4.0,
        ),
        revision=3,
    )
    dialog._save_application_defaults()
    assert application_emitted[-1]["reasoning_effort"] == "none"
    capability_emitted: list[dict[str, object]] = []
    dialog.capability_override_requested.connect(capability_emitted.append)
    dialog.capability_key.setCurrentIndex(dialog.capability_key.findData("limits.output_tokens"))
    dialog.capability_state.setCurrentIndex(dialog.capability_state.findData("supported"))
    dialog.capability_value.setValue(8)
    dialog.capability_value_set.setChecked(True)
    dialog._set_capability_override()
    assert capability_emitted[-1]["key"] == "limits.output_tokens"
    assert capability_emitted[-1]["value"] == 8

    dialog.set_capability_overrides((SimpleNamespace(
        model_entry_id="model-a",
        key="limits.output_tokens",
        state=SimpleNamespace(value="supported"),
        value=777,
        reason="old reason",
        revision=1,
    ),))
    dialog.model_list.setCurrentRow(0)
    dialog.capability_key.setCurrentIndex(dialog.capability_key.findData("limits.output_tokens"))
    assert dialog.capability_value_set.isChecked()
    assert dialog.capability_value.value() == 777
    assert dialog.capability_reason.text() == "old reason"
    dialog.capability_key.setCurrentIndex(dialog.capability_key.findData("limits.context_tokens"))
    assert not dialog.capability_value_set.isChecked()
    assert dialog.capability_reason.text() == ""
    assert dialog.capability_value.value() == 0
    dialog.close()
    app.processEvents()


def test_add_connection_dialog_is_closed_adapter_aware_and_clears_secret_input():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    dialog = AddConnectionDialog()
    try:
        assert [
            dialog.connection_definition.itemData(index)
            for index in range(dialog.connection_definition.count())
        ] == [item.key for item in builtin_connection_definitions()]

        generic_index = dialog.connection_definition.findData("generic_openai_compatible")
        dialog.connection_definition.setCurrentIndex(generic_index)
        assert dialog.connection_backend_value.text() == BackendType.OPENAI_COMPATIBLE_HTTP.value
        assert dialog.connection_profile_value.text() == ProviderProfile.GENERIC.value
        assert not dialog.connection_endpoint.isHidden()
        assert dialog.connection_credential_source.findData(CredentialSource.NONE.value) >= 0
        assert dialog.connection_credential_source.findData(CredentialSource.ENVIRONMENT.value) >= 0
        assert dialog.connection_credential_source.findData(CredentialSource.SECRET_SERVICE.value) >= 0

        openrouter_index = dialog.connection_definition.findData("openrouter")
        dialog.connection_definition.setCurrentIndex(openrouter_index)
        assert dialog.connection_profile_value.text() == ProviderProfile.OPENROUTER.value
        assert dialog.connection_credential_source.findData(CredentialSource.NONE.value) == -1
        assert dialog.connection_credential_source.findData(CredentialSource.SECRET_SERVICE.value) >= 0

        local_index = dialog.connection_definition.findData("local_ollama")
        dialog.connection_definition.setCurrentIndex(local_index)
        assert dialog.connection_endpoint.text() == "http://localhost:11434/v1"
        assert dialog.connection_credential_source.findData(CredentialSource.NONE.value) >= 0

        emitted: list[dict[str, object]] = []
        dialog.save_requested.connect(emitted.append)
        dialog.connection_definition.setCurrentIndex(generic_index)
        dialog.connection_name.setText("Sentinel API")
        dialog.connection_endpoint.setText("http://127.0.0.1:9009/v1")
        secret_index = dialog.connection_credential_source.findData(CredentialSource.SECRET_SERVICE.value)
        dialog.connection_credential_source.setCurrentIndex(secret_index)
        dialog.connection_credential_reference.setText("sentinel-ref")
        sentinel = "phase5-dialog-sentinel"
        dialog.connection_credential_value.setText(sentinel)
        dialog.connection_credential_source.setCurrentIndex(
            dialog.connection_credential_source.findData(CredentialSource.NONE.value)
        )
        assert dialog.connection_credential_reference.text() == ""
        dialog.connection_credential_source.setCurrentIndex(secret_index)
        dialog.connection_credential_reference.setText("sentinel-ref")
        dialog.connection_credential_value.setText(sentinel)
        dialog.save_button.click()

        assert emitted[-1]["credential_value"] == sentinel
        assert dialog.connection_credential_value.text() == ""
        assert sentinel not in repr(dialog.connection_credential_value)
    finally:
        dialog.close()
        app.processEvents()


def test_add_connection_dialog_preserves_custom_name_across_definition_switches():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    dialog = AddConnectionDialog()
    try:
        generic_index = dialog.connection_definition.findData("generic_openai_compatible")
        openrouter_index = dialog.connection_definition.findData("openrouter")
        local_index = dialog.connection_definition.findData("local_ollama")
        dialog.connection_definition.setCurrentIndex(generic_index)
        dialog.connection_name.setText("Operator custom name")
        dialog.connection_definition.setCurrentIndex(openrouter_index)
        dialog.connection_definition.setCurrentIndex(local_index)
        assert dialog.connection_name.text() == "Operator custom name"
    finally:
        dialog.close()
        app.processEvents()


def test_add_connection_dialog_save_and_refresh_are_explicit_distinct_actions():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    dialog = AddConnectionDialog()
    try:
        generic_index = dialog.connection_definition.findData("generic_openai_compatible")
        dialog.connection_definition.setCurrentIndex(generic_index)
        dialog.connection_name.setText("No network on save")
        dialog.connection_endpoint.setText("http://127.0.0.1:9010/v1")
        actions: list[str] = []
        dialog.save_requested.connect(lambda _values: actions.append("save"))
        dialog.save_refresh_requested.connect(lambda _values: actions.append("save_refresh"))
        dialog.save_button.click()
        assert actions == ["save"]

        dialog.submission_failed("reset for deterministic action probe")
        dialog.connection_name.setText("Explicit refresh")
        dialog.save_refresh_button.click()
        assert actions == ["save", "save_refresh"]
    finally:
        dialog.close()
        app.processEvents()


def test_add_connection_dialog_validates_adapter_requirements_and_environment_is_reference_only():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    dialog = AddConnectionDialog()
    try:
        generic_index = dialog.connection_definition.findData("generic_openai_compatible")
        dialog.connection_definition.setCurrentIndex(generic_index)
        dialog.connection_name.setText("Needs endpoint")
        dialog.connection_endpoint.clear()
        emitted: list[dict[str, object]] = []
        dialog.save_requested.connect(emitted.append)
        dialog.save_button.click()
        assert emitted == []
        assert "base URL is required" in dialog.validation_label.text()

        dialog.connection_endpoint.setText("http://127.0.0.1:9014/v1")
        dialog.connection_credential_source.setCurrentIndex(
            dialog.connection_credential_source.findData(CredentialSource.ENVIRONMENT.value)
        )
        dialog.connection_credential_reference.setText("BOTS_TEST_API_KEY")
        dialog.save_button.click()
        assert emitted[-1]["credential_source"] == CredentialSource.ENVIRONMENT.value
        assert emitted[-1]["credential_value"] is None
    finally:
        dialog.close()
        app.processEvents()


def test_add_connection_secret_service_value_is_stored_only_through_the_fake_store(tmp_path: Path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    sentinel = "phase5-add-connection-secret"

    async def scenario():
        secret_store = FakeSecretStore()
        application, store = _configured_application(tmp_path, secret_store=secret_store)
        window = MainWindow(application)
        dialog = AddConnectionDialog(window)
        window._add_connection_dialog = dialog
        try:
            generic_index = dialog.connection_definition.findData("generic_openai_compatible")
            dialog.connection_definition.setCurrentIndex(generic_index)
            dialog.connection_name.setText("Secret-backed API")
            dialog.connection_endpoint.setText("http://127.0.0.1:9013/v1")
            dialog.connection_credential_source.setCurrentIndex(
                dialog.connection_credential_source.findData(CredentialSource.SECRET_SERVICE.value)
            )
            dialog.connection_credential_reference.setText("secret-backed-ref")
            dialog.connection_credential_value.setText(sentinel)
            payloads: list[dict[str, object]] = []
            dialog.save_requested.connect(payloads.append)
            dialog.save_button.click()
            assert dialog.connection_credential_value.text() == ""
            await window._create_connection_from_add(payloads[-1], refresh=False)

            connection = next(
                item for item in await application.list_provider_connections()
                if item.name == "Secret-backed API"
            )
            assert secret_store.values == {"secret-backed-ref": sentinel}
            assert connection.credential_reference == "secret-backed-ref"
            with sqlite3.connect(tmp_path / "state.sqlite3") as database:
                rows = database.execute("SELECT * FROM provider_connections").fetchall()
                assert sentinel not in repr(rows)
                assert sentinel not in database.execute(
                    "SELECT request_snapshot FROM generation_attempts"
                ).fetchall().__repr__()
            assert sentinel not in repr(connection)
        finally:
            dialog.close()
            window.deleteLater()
            await application.close()

    asyncio.run(scenario())


def test_add_connection_secret_is_cleared_before_explicit_refresh(tmp_path: Path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from bots5.desktop import window as window_module

    app = QApplication.instance() or QApplication([])
    sentinel = "phase5-refresh-secret-lifetime"

    async def scenario():
        secret_store = FakeSecretStore()
        application, _store = _configured_application(tmp_path, secret_store=secret_store)
        window = MainWindow(application)
        entered = asyncio.Event()
        release = asyncio.Event()

        class BlockingDiscoverer:
            async def discover(self, connection, credential):
                entered.set()
                await release.wait()
                return ()

        original = window_module.discoverer_for_connection
        window_module.discoverer_for_connection = lambda _connection: BlockingDiscoverer()
        payload = {
            "name": "Refresh lifetime",
            "backend_type": BackendType.OPENAI_COMPATIBLE_HTTP.value,
            "profile": ProviderProfile.GENERIC.value,
            "endpoint": "http://127.0.0.1:9015/v1",
            "credential_source": CredentialSource.SECRET_SERVICE.value,
            "credential_reference": "refresh-lifetime-ref",
            "credential_value": sentinel,
        }
        try:
            task = asyncio.create_task(window._create_connection_from_add(payload, refresh=True))
            await asyncio.wait_for(entered.wait(), timeout=1)
            assert all(
                frame.f_locals.get("credential_value") != sentinel
                for frame in task.get_stack()
            )
            release.set()
            await task
        finally:
            release.set()
            window_module.discoverer_for_connection = original
            window.deleteLater()
            await application.close()

    asyncio.run(scenario())


def test_add_connection_secret_is_cleared_before_status_event_backpressure(tmp_path: Path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    sentinel = "phase5-event-backpressure-secret"

    async def scenario():
        secret_store = FakeSecretStore()
        application, _store = _configured_application(tmp_path, secret_store=secret_store)
        window = MainWindow(application)
        subscription = None
        original_create = application.create_provider_connection
        queue_filled = asyncio.Event()

        async def create_and_fill_event_queue(**kwargs):
            nonlocal subscription
            created = await original_create(**kwargs)
            subscription = application.subscribe()
            for index in range(application._events._queue_size):
                await application._events.publish("filler", index=index)
            queue_filled.set()
            return created

        application.create_provider_connection = create_and_fill_event_queue
        payload = {
            "name": "Backpressure lifetime",
            "backend_type": BackendType.OPENAI_COMPATIBLE_HTTP.value,
            "profile": ProviderProfile.GENERIC.value,
            "endpoint": "http://127.0.0.1:9016/v1",
            "credential_source": CredentialSource.SECRET_SERVICE.value,
            "credential_reference": "backpressure-ref",
            "credential_value": sentinel,
        }
        try:
            task = asyncio.create_task(window._create_connection_from_add(payload, refresh=False))
            await asyncio.wait_for(queue_filled.wait(), timeout=2)
            await asyncio.sleep(0)
            if task.done():
                task.result()
            assert secret_store.values == {"backpressure-ref": sentinel}
            assert not task.done()
            for pending in asyncio.all_tasks():
                if pending is asyncio.current_task():
                    continue
                for frame in pending.get_stack():
                    assert sentinel not in repr(frame.f_locals)

            assert subscription is not None
            await subscription.__anext__()
            await asyncio.wait_for(task, timeout=2)
        finally:
            application.create_provider_connection = original_create
            if subscription is not None:
                subscription.close()
            window.deleteLater()
            await application.close()

    asyncio.run(scenario())


def test_add_connection_save_and_refresh_clears_secret_before_catalogue_event_backpressure(
    tmp_path: Path,
):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from bots5.desktop import window as window_module

    app = QApplication.instance() or QApplication([])
    sentinel = "phase5-save-refresh-catalogue-secret"

    async def scenario():
        secret_store = FakeSecretStore()
        application, store = _configured_application(tmp_path, secret_store=secret_store)
        window = MainWindow(application)
        subscription = None
        original_save = application.save_connection_credential
        original_discoverer = window_module.discoverer_for_connection
        queue_filled = asyncio.Event()

        async def save_and_fill_event_queue(*args, **kwargs):
            nonlocal subscription
            result = await original_save(*args, **kwargs)
            subscription = application.subscribe()
            for index in range(application._events._queue_size):
                await application._events.publish("filler", index=index)
            queue_filled.set()
            return result

        class ImmediateDiscoverer:
            async def discover(self, connection, credential):
                assert credential == sentinel
                return (DiscoveredModel("save-refresh-model"),)

        def assert_no_secret_in_await_chain(task: asyncio.Task[object]) -> None:
            seen: set[int] = set()
            current: object | None = task
            while current is not None and id(current) not in seen:
                seen.add(id(current))
                if isinstance(current, asyncio.Task):
                    for frame in current.get_stack():
                        assert sentinel not in repr(frame.f_locals)
                    current = current.get_coro()
                    continue
                frame = getattr(current, "cr_frame", None)
                if frame is not None:
                    assert sentinel not in repr(frame.f_locals)
                current = getattr(current, "cr_await", None)

        application.save_connection_credential = save_and_fill_event_queue
        window_module.discoverer_for_connection = lambda _connection: ImmediateDiscoverer()
        payload = {
            "name": "Save and refresh lifetime",
            "backend_type": BackendType.OPENAI_COMPATIBLE_HTTP.value,
            "profile": ProviderProfile.GENERIC.value,
            "endpoint": "http://127.0.0.1:9017/v1",
            "credential_source": CredentialSource.SECRET_SERVICE.value,
            "credential_reference": "save-refresh-ref",
            "credential_value": sentinel,
        }
        try:
            task = asyncio.create_task(window._create_connection_from_add(payload, refresh=True))
            await asyncio.wait_for(queue_filled.wait(), timeout=2)

            async def catalogue_persisted() -> None:
                while True:
                    connection = next(
                        item
                        for item in store.list_provider_connections()
                        if item.name == "Save and refresh lifetime"
                    )
                    if connection.catalogue_revision == 1:
                        return
                    await asyncio.sleep(0)

            await asyncio.wait_for(catalogue_persisted(), timeout=2)
            assert not task.done()
            assert secret_store.values == {"save-refresh-ref": sentinel}
            assert_no_secret_in_await_chain(task)
            with _engine_connection(store) as db:
                dump = repr(db.execute(text("SELECT * FROM provider_connections")).fetchall())
                dump += repr(db.execute(text("SELECT * FROM model_catalogue_entries")).fetchall())
            assert sentinel not in dump

            assert subscription is not None
            await subscription.__anext__()
            await asyncio.wait_for(task, timeout=2)
        finally:
            application.save_connection_credential = original_save
            window_module.discoverer_for_connection = original_discoverer
            if subscription is not None:
                subscription.close()
            window.deleteLater()
            await application.close()

    asyncio.run(scenario())


def test_phase5_attribution_cannot_be_added_to_legacy_attempt(tmp_path: Path):
    database = tmp_path / "legacy-attribution.sqlite3"
    _upgrade_to(database, "0001_desktop_state")
    engine = create_engine(f"sqlite:///{database}")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO chats (id, title, created_at, updated_at) VALUES "
                    "('legacy-chat', 'Legacy', '2026-09-05T00:00:00.000Z', "
                    "'2026-09-05T00:00:00.000Z')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO messages "
                    "(id, chat_id, parent_id, sequence, role, state, content, created_at) VALUES "
                    "('legacy-user', 'legacy-chat', NULL, 1, 'user', 'sent', 'hello', "
                    "'2026-09-05T00:00:00.000Z'), "
                    "('legacy-assistant', 'legacy-chat', 'legacy-user', 2, 'assistant', 'aborted', '', "
                    "'2026-09-05T00:00:00.000Z')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO generation_attempts "
                    "(id, chat_id, user_message_id, assistant_message_id, backend_id, model, "
                    "state, request_snapshot, started_at, ended_at) VALUES "
                    "('legacy-attempt', 'legacy-chat', 'legacy-user', 'legacy-assistant', "
                    "'fake', 'fake-v0.1', 'aborted', '{}', "
                    "'2026-09-05T00:00:00.000Z', '2026-09-05T00:00:01.000Z')"
                )
            )
    finally:
        engine.dispose()

    upgrade_database(database)
    with sqlite3.connect(database) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="Phase 5 attempt attribution"):
            with phase7_guarded_raw_mutation(
                connection,
                "legacy-attribution rejection fixture",
            ):
                connection.execute(
                    "UPDATE generation_attempts SET connection_id = ?, model_entry_id = ? "
                    "WHERE id = ?",
                    (
                        "01900000-0000-7000-8000-000000000005",
                        "01900000-0000-7000-8000-000000000006",
                        "legacy-attempt",
                    ),
                )
        assert connection.execute(
            "SELECT connection_id, model_entry_id FROM generation_attempts WHERE id = ?",
            ("legacy-attempt",),
        ).fetchone() == (None, None)
    store = SQLiteAppStateStore.open(database)
    try:
        assert store.get_generation_attempt("legacy-attempt") is not None
    finally:
        store.close()


def test_raw_delete_cannot_remove_model_referenced_by_phase5_history(tmp_path: Path):
    async def scenario():
        application, store = _configured_application(tmp_path)
        try:
            connection = (await application.list_provider_connections())[0]
            model = await application.add_manual_model(
                connection_id=connection.id,
                provider_model_id="historical-model",
            )
            for key in (
                CapabilityKey.STREAMING.value,
                CapabilityKey.TEMPERATURE.value,
                CapabilityKey.MAX_OUTPUT_TOKENS.value,
            ):
                await application.set_capability_override(
                    CapabilityOverride(model.id, key, CapabilityState.SUPPORTED)
                )
            chat = await application.create_chat()
            await application.select_model(chat.id, model.id)
            attempt = await application.send_message(chat.id, "historical")
            for _ in range(100):
                current = store.get_generation_attempt(attempt.id)
                if current is not None and current.state is not AttemptState.RUNNING:
                    attempt = current
                    break
                await asyncio.sleep(0.01)
            assert attempt.state is AttemptState.COMPLETE
        finally:
            await application.close()
        with sqlite3.connect(tmp_path / "state.sqlite3") as connection_db:
            connection_db.execute("DELETE FROM chat_model_selection WHERE chat_id = ?", (chat.id,))
            with pytest.raises(sqlite3.IntegrityError, match="referenced Phase 5 model"):
                connection_db.execute(
                    "DELETE FROM model_catalogue_entries WHERE id = ?", (model.id,)
                )
            assert connection_db.execute(
                "SELECT count(*) FROM generation_attempts WHERE id = ? AND model_entry_id = ?",
                (attempt.id, model.id),
            ).fetchone()[0] == 1

    asyncio.run(scenario())


def test_capability_precedence_and_settings_provenance(tmp_path: Path):
    async def scenario():
        application, store = _configured_application(tmp_path)
        try:
            chat = await application.create_chat()
            model_id = (await application.chat_model_selection(chat.id)).model_entry_id
            assert model_id is not None
            connection = (await application.list_provider_connections())[0]
            store.set_capability_fact(CapabilityFact(model_id, CapabilityKey.TEMPERATURE.value, CapabilityState.SUPPORTED, CapabilitySource.HEURISTIC))
            store.set_capability_fact(CapabilityFact(model_id, CapabilityKey.TEMPERATURE.value, CapabilityState.UNSUPPORTED, CapabilitySource.PROVIDER_METADATA, source_revision=connection.catalogue_revision))
            store.set_capability_fact(CapabilityFact(model_id, CapabilityKey.TEMPERATURE.value, CapabilityState.SUPPORTED, CapabilitySource.CONFIRMED_ENDPOINT, source_revision=connection.catalogue_revision))
            resolved = {item.key: item for item in application._configuration.resolve_capabilities(model_id)}
            assert resolved[CapabilityKey.TEMPERATURE.value].state is CapabilityState.SUPPORTED
            assert resolved[CapabilityKey.TEMPERATURE.value].source is CapabilitySource.CONFIRMED_ENDPOINT
            store.set_capability_fact(CapabilityFact(model_id, CapabilityKey.OUTPUT_TOKENS.value, CapabilityState.SUPPORTED, CapabilitySource.TRUSTED_REGISTRY, value=64))
            store.set_capability_fact(CapabilityFact(model_id, CapabilityKey.OUTPUT_TOKENS.value, CapabilityState.UNSUPPORTED, CapabilitySource.PROVIDER_METADATA, source_revision=1))
            store.set_capability_fact(CapabilityFact(model_id, CapabilityKey.OUTPUT_TOKENS.value, CapabilityState.UNSUPPORTED, CapabilitySource.CONFIRMED_ENDPOINT, source_revision=1))
            await application.refresh_models(connection.id, FakeModelDiscoverer(()))
            resolved = {item.key: item for item in application._configuration.resolve_capabilities(model_id)}
            assert resolved[CapabilityKey.OUTPUT_TOKENS.value].state is CapabilityState.SUPPORTED
            assert resolved[CapabilityKey.OUTPUT_TOKENS.value].source is CapabilitySource.TRUSTED_REGISTRY
            await application.set_application_generation_settings(GenerationSettings(temperature=0.4, max_output_tokens=2048, timeout_seconds=4.0))
            await application.set_model_defaults(model_id, GenerationSettings(temperature=0.7, max_output_tokens=1536))
            await application.set_chat_generation_settings(chat.id, GenerationSettings(temperature=1.1))
            settings = await application.resolve_chat_generation_settings(chat.id)
            assert settings.temperature == 1.1
            assert settings.max_output_tokens == 1536
            assert settings.timeout_seconds == 4.0
            assert settings.provenance["temperature"] == "chat_model"
            assert settings.provenance["max_output_tokens"] == "model"
            assert settings.provenance["timeout_seconds"] == "application"
            await application.select_model(chat.id, model_id)
        finally:
            await application.close()

    asyncio.run(scenario())


def test_equal_precedence_capability_resolution_uses_newest_revision():
    key = CapabilityKey.TEMPERATURE.value
    facts = (
        CapabilityFact(
            "model",
            key,
            CapabilityState.SUPPORTED,
            CapabilitySource.TRUSTED_REGISTRY,
            source_revision=10,
        ),
        CapabilityFact(
            "model",
            key,
            CapabilityState.UNSUPPORTED,
            CapabilitySource.TRUSTED_REGISTRY,
            source_revision=20,
        ),
    )

    for ordered in (facts, tuple(reversed(facts))):
        resolved = resolve_capability(ordered, None, key)
        assert resolved.state is CapabilityState.UNSUPPORTED
        assert resolved.source is CapabilitySource.TRUSTED_REGISTRY
        assert resolved.source_revision == 20


def test_tune_save_rejects_a_model_switch_before_writing_the_new_model(tmp_path: Path):
    async def scenario():
        application, store = _configured_application(tmp_path)
        try:
            connection = (await application.list_provider_connections())[0]
            model_a = store.get_model_catalogue_entry("01900000-0000-7000-8000-000000000006")
            assert model_a is not None
            model_b = await application.add_manual_model(
                connection_id=connection.id,
                provider_model_id="second-model",
            )
            chat = await application.create_chat()
            initial_selection = await application.chat_model_selection(chat.id)
            assert initial_selection is not None
            assert initial_selection.model_entry_id == model_a.id
            await application.select_model(
                chat.id,
                model_b.id,
                expected_revision=initial_selection.revision,
            )
            with pytest.raises(RevisionConflict, match="model selection changed"):
                await application.set_chat_generation_settings(
                    chat.id,
                    GenerationSettings(temperature=0.88),
                    expected_revision=0,
                    expected_model_entry_id=model_a.id,
                )
            assert store.get_chat_model_generation_settings(chat.id, model_a.id) is None
            assert store.get_chat_model_generation_settings(chat.id, model_b.id) is None
        finally:
            await application.close()

    asyncio.run(scenario())


def test_retirement_is_tombstone_and_requires_an_available_replacement(tmp_path: Path):
    async def scenario():
        application, store = _configured_application(tmp_path)
        try:
            fake = (await application.list_provider_connections())[0]
            replacement_connection = await application.create_provider_connection(
                name="Replacement fake",
                backend_type=BackendType.FAKE,
                profile=ProviderProfile.GENERIC,
            )
            replacement = await application.add_manual_model(
                connection_id=replacement_connection.id,
                provider_model_id="replacement-model",
            )
            retired = await application.retire_provider_connection(
                fake.id,
                expected_revision=fake.revision,
                replacement_model_entry_id=replacement.id,
            )
            assert retired.retired is True and retired.enabled is False
            assert store.get_provider_connection(fake.id) is not None
            assert store.get_application_generation_config()[1] == replacement.id
            assert store.get_model_catalogue_entry("01900000-0000-7000-8000-000000000006").availability is CatalogueAvailability.DISCONNECTED
        finally:
            await application.close()

    asyncio.run(scenario())


def test_openrouter_configuration_is_deterministic_without_public_network_activity(tmp_path: Path):
    async def scenario():
        secret = FakeSecretStore({"openrouter-ref": "SENTINEL_SECRET_MUST_NOT_PERSIST"})
        application, store = _configured_application(tmp_path, secret_store=secret)
        try:
            connection = await application.create_provider_connection(
                name="OpenRouter desktop",
                backend_type=BackendType.OPENAI_COMPATIBLE_HTTP,
                profile=ProviderProfile.OPENROUTER,
                endpoint="https://openrouter.ai/api/v1/",
                credential_source=CredentialSource.SECRET_SERVICE,
                credential_reference="openrouter-ref",
            )
            model = await application.add_manual_model(
                connection_id=connection.id,
                provider_model_id="openai/gpt-5",
            )
            for key in (
                CapabilityKey.STREAMING.value,
                CapabilityKey.TEMPERATURE.value,
                CapabilityKey.MAX_OUTPUT_TOKENS.value,
            ):
                await application.set_capability_override(
                    CapabilityOverride(
                        model.id,
                        key,
                        CapabilityState.SUPPORTED,
                        4096 if key == CapabilityKey.MAX_OUTPUT_TOKENS.value else None,
                        "operator-confirmed for deterministic configuration",
                    )
                )
            chat = await application.create_chat()
            await application.select_model(chat.id, model.id)
            prepared, snapshot_text = application._configuration.prepare_generation(
                chat_id=chat.id,
                user_message_id="user-id",
                prompt="hello",
                attempt_id="attempt-id",
            )
            assert prepared.request.backend_id == "openai_compatible_http"
            assert prepared.request.provider_id == "openrouter"
            assert prepared.request.base_url == "https://openrouter.ai/api/v1"
            assert prepared.request.credential_source == "secret_service"
            assert prepared.request.credential_reference == "openrouter-ref"
            assert prepared.request.credential_status == "available"
            assert "SENTINEL_SECRET" not in snapshot_text
            assert json.loads(snapshot_text)["model"] == "openai/gpt-5"
            assert store.get_model_catalogue_entry(model.id).connection_id == connection.id
        finally:
            await application.close()

    asyncio.run(scenario())


def test_secret_service_is_explicit_and_values_are_not_rendered_or_persisted():
    class Backend:
        def __init__(self):
            self.values = {}

        def get_password(self, service, username):
            return self.values.get((service, username))

        def set_password(self, service, username, value):
            self.values[(service, username)] = value

        def delete_password(self, service, username):
            self.values.pop((service, username), None)

    backend = Backend()
    store = SecretServiceStore(backend=backend)
    sentinel = "SENTINEL_SECRET_MUST_NOT_PERSIST"
    assert store.status("local").status == "missing"
    store.put("local", sentinel)
    assert store.status("local").status == "available"
    assert store.get("local") == sentinel
    assert "SENTINEL_SECRET" not in repr(store)
    store.delete("local")
    assert store.status("local").status == "missing"

    failing = SecretServiceStore(backend=type("Failing", (), {"get_password": lambda *args: (_ for _ in ()).throw(RuntimeError("secret backend unavailable")), "set_password": lambda *args: None, "delete_password": lambda *args: None})())
    assert failing.status("local").status == "unavailable"
    with pytest.raises(SecretStoreError, match="operation failed"):
        failing.get("local")


class _TimeoutBackend:
    def __init__(self):
        self.calls = 0

    async def stream(self, request: GenerationRequest):
        self.calls += 1
        yield GenerationDispatched(request.attempt_id)
        yield GenerationDelta(request.attempt_id, "partial")
        await asyncio.sleep(0.05)
        yield GenerationCompleted(request.attempt_id)


class _BackendNativeTimeout:
    async def stream(self, request: GenerationRequest):
        yield GenerationDispatched(request.attempt_id)
        raise asyncio.TimeoutError("backend-native timeout")


def test_backend_native_timeout_is_not_reported_as_bots_owned_deadline(tmp_path: Path):
    async def scenario():
        application, store = _configured_application(tmp_path, _BackendNativeTimeout())
        try:
            chat = await application.create_chat()
            created = await application.send_message(chat.id, "native timeout")
            await asyncio.sleep(0.15)
            attempt = store.get_generation_attempt(created.id)
            assert attempt is not None
            assert attempt.state is AttemptState.FAILED
            assert attempt.error_type == "TimeoutError"
            assert attempt.error_message == "backend-native timeout"
            assert attempt.error_type != "timeout"
        finally:
            await application.close()

    asyncio.run(scenario())


def test_backend_native_timeout_with_configured_deadline_keeps_backend_error(tmp_path: Path):
    async def scenario():
        application, store = _configured_application(tmp_path, _BackendNativeTimeout())
        try:
            await application.set_application_generation_settings(
                GenerationSettings(timeout_seconds=1.0)
            )
            chat = await application.create_chat()
            created = await application.send_message(chat.id, "native timeout with deadline")
            await asyncio.sleep(0.15)
            attempt = store.get_generation_attempt(created.id)
            assert attempt is not None
            assert attempt.state is AttemptState.FAILED
            assert attempt.error_type == "TimeoutError"
            assert attempt.error_message == "backend-native timeout"
        finally:
            await application.close()

    asyncio.run(scenario())


class _CancellationResistantStream:
    def __init__(self, attempt_id: str, cancelled: asyncio.Event, release: asyncio.Event):
        self.attempt_id = attempt_id
        self.step = 0
        self.cancelled = cancelled
        self.release = release

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.step == 0:
            self.step += 1
            return GenerationDispatched(self.attempt_id)
        if self.step == 1:
            self.step += 1
            return GenerationDelta(self.attempt_id, "partial")
        try:
            await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            self.cancelled.set()
            await self.release.wait()
            raise StopAsyncIteration
        raise StopAsyncIteration


class _CancellationResistantBackend:
    def __init__(self):
        self.calls = 0
        self.cancelled = asyncio.Event()
        self.release = asyncio.Event()

    def stream(self, request: GenerationRequest):
        self.calls += 1
        return _CancellationResistantStream(
            request.attempt_id, self.cancelled, self.release
        )


def test_bots_owned_timeout_does_not_wait_for_cancellation_resistant_backend(tmp_path: Path):
    async def scenario():
        backend = _CancellationResistantBackend()
        application, store = _configured_application(tmp_path, backend)
        try:
            await application.set_application_generation_settings(
                GenerationSettings(timeout_seconds=0.01)
            )
            chat = await application.create_chat()
            created = await application.send_message(chat.id, "resistant timeout")
            await asyncio.wait_for(backend.cancelled.wait(), timeout=2)
            attempt = None
            for _ in range(100):
                attempt = store.get_generation_attempt(created.id)
                if attempt is not None and attempt.state is not AttemptState.RUNNING:
                    break
                await asyncio.sleep(0)
            assert attempt is not None
            assert attempt.state is AttemptState.FAILED
            assert attempt.error_type == "timeout"
            assert attempt.remote_outcome_unknown is True
            message = store.get_message(attempt.assistant_message_id)
            assert message is not None and message.content == "partial"
            assert backend.calls == 1
        finally:
            backend.release.set()
            await asyncio.wait_for(application.close(), timeout=1)

    asyncio.run(scenario())


def test_bots_owned_timeout_preserves_partial_output_and_uncertainty_without_retry(tmp_path: Path):
    async def scenario():
        backend = _TimeoutBackend()
        application, store = _configured_application(tmp_path, backend)
        try:
            await application.set_application_generation_settings(
                GenerationSettings(temperature=0.0, max_output_tokens=1024, timeout_seconds=0.05)
            )
            chat = await application.create_chat()
            created = await application.send_message(chat.id, "timeout")
            attempt = None
            for _ in range(200):
                attempt = store.get_generation_attempt(created.id)
                if attempt is not None and attempt.state is not AttemptState.RUNNING:
                    break
                await asyncio.sleep(0.01)
            assert attempt is not None and attempt.state is not AttemptState.RUNNING
            message = store.get_message(attempt.assistant_message_id)
            assert backend.calls == 1
            assert attempt.state.value == "failed"
            assert attempt.error_type == "timeout"
            assert attempt.remote_outcome_unknown is True
            assert message is not None and message.content == "partial"
            parsed = json.loads(attempt.request_snapshot)
            validate_phase5_snapshot(
                attempt.request_snapshot,
                attempt_id=attempt.id,
                chat_id=attempt.chat_id,
                user_message_id=attempt.user_message_id,
                backend_id=attempt.backend_id,
                model=attempt.model,
                provider_id=attempt.provider_id,
            )
            assert parsed["effective_settings"]["timeout_seconds"] == 0.05
            assert "SENTINEL_SECRET" not in attempt.request_snapshot
        finally:
            await application.close()

    asyncio.run(scenario())
