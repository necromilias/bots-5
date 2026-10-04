"""OpenRouter advertisements must enter the normal fail-closed capability path."""
from __future__ import annotations

import asyncio
import httpx

from bots5.domain.provider import BackendType, CredentialSource, ProviderProfile
from bots5.providers.discovery import OpenAICompatibleModelDiscoverer
from bots5.infrastructure.secrets import FakeSecretStore
from tests.test_phase5_provider_model import _configured_application
from dataclasses import replace
from datetime import UTC, datetime
import json
import pytest
from bots5.core.errors import StateError
from bots5.core.provider_configuration import resolve_setting_emission_plan
from bots5.domain.provider import (
    CapabilityKey, CapabilitySource, CapabilityState, CatalogueAvailability,
    CatalogueOrigin, ModelCatalogueEntry, ProviderConnection, ResolvedCapability,
    ResolvedGenerationSettings, CapabilityOverride,
)
from bots5.domain.openrouter_capabilities import sanitize_openrouter_metadata, openrouter_capability_facts
from bots5.domain.generation_settings_registry import GenerationSettingsPayload, PayloadFamily, STATE_OMITTED_INVALID
from bots5.providers.base import CompletionRequest
from bots5.providers.openrouter import OpenRouterProvider
from bots5.providers.openai_compatible import OpenAICompatibleProvider
from bots5.domain.models import AttemptState
from bots5.infrastructure.generation.router import BuiltinProviderRouter



def test_advertised_openrouter_model_can_prepare_generation(tmp_path):
    async def scenario():
        application, store = _configured_application(tmp_path, secret_store=FakeSecretStore({"test-ref": "synthetic-test-credential"}))
        try:
            connection = await application.create_provider_connection(
                name="OpenRouter test", backend_type=BackendType.OPENAI_COMPATIBLE_HTTP,
                profile=ProviderProfile.OPENROUTER, endpoint="https://openrouter.ai/api/v1",
                credential_source=CredentialSource.SECRET_SERVICE,
                credential_reference="test-ref",
            )
            def handler(request):
                assert request.url.path == "/api/v1/models"
                assert request.headers["Authorization"] == "Bearer synthetic-test-credential"
                return httpx.Response(200, json={"data": [{
                    "id": "test/text-model", "supported_parameters": ["temperature", "max_tokens", "top_p"],
                    "context_length": 32000, "top_provider": {"max_completion_tokens": 4096},
                }]})
            models = await application.refresh_models(
                connection.id, OpenAICompatibleModelDiscoverer(transport=httpx.MockTransport(handler)),
            )
            chat = await application.create_chat()
            await application.select_model(chat.id, models[0].id)
            prepared, snapshot = application._configuration.prepare_generation(
                chat_id=chat.id, user_message_id="u1", prompt="Hello", attempt_id="a1",
            )
            assert prepared.request.model == "test/text-model"
            assert models[0].metadata["supported_parameters"] == ["max_tokens", "temperature", "top_p"]
        finally:
            await application.close()

    asyncio.run(scenario())



def _evidence(metadata):
    connection = ProviderConnection("c", "OpenRouter", BackendType.OPENAI_COMPATIBLE_HTTP,
                                    ProviderProfile.OPENROUTER, "https://openrouter.ai/api/v1", catalogue_revision=2)
    model = ModelCatalogueEntry("m", "c", "test/text-model", "Text", CatalogueOrigin.DISCOVERED,
                                CatalogueAvailability.AVAILABLE, 2, datetime(2026, 10, 3, tzinfo=UTC), metadata)
    return connection, model


@pytest.mark.parametrize("bad", [None, "temperature", {}, ["temperature", 1], ["temperature", True],
                                       ["temperature", "bad name"], ["a"] * 129, ["a" * 65]])
def test_malformed_parameter_list_never_partially_establishes_support(bad):
    metadata = {"supported_parameters": bad}
    assert sanitize_openrouter_metadata(metadata) == {}
    facts = openrouter_capability_facts(*_evidence(metadata))
    assert [fact.key for fact in facts] == ["generation.streaming"]


def test_advertisements_map_only_semantically_matching_keys_and_provenance():
    parameters = ["temperature", "max_completion_tokens", "top_p", "top_k", "min_p", "frequency_penalty",
                  "presence_penalty", "repetition_penalty", "seed", "stop", "logprobs", "top_logprobs",
                  "reasoning", "logit_bias", "unknown_future_parameter", "typical_p"]
    metadata = {"supported_parameters": parameters, "reasoning": {
        "supported_efforts": ["none", "low", "medium", "high"], "mandatory": False, "supports_max_tokens": True}}
    facts = {f.key: f for f in openrouter_capability_facts(*_evidence(metadata))}
    expected = {"generation.streaming", "request.temperature", "request.max_output_tokens", "request.top_p",
                "request.top_k", "request.min_p", "request.frequency_penalty", "request.presence_penalty",
                "request.repetition_penalty", "request.seed", "request.stop_sequences", "request.logprobs",
                "request.top_logprobs", "request.reasoning_effort.level", "request.reasoning_effort.none",
                "request.reasoning.token_budget"}
    assert set(facts) == expected
    assert facts["generation.streaming"].source is CapabilitySource.TRUSTED_REGISTRY
    assert "streaming" in facts["generation.streaming"].provenance["reason"]
    assert "field" not in facts["generation.streaming"].provenance
    for key, fact in facts.items():
        assert fact.state is CapabilityState.SUPPORTED
        assert fact.source_revision == 2
        if key != "generation.streaming":
            assert fact.source is CapabilitySource.PROVIDER_METADATA
            assert fact.provenance["catalogue_revision"] == 2
    assert "unknown_future_parameter" in sanitize_openrouter_metadata(metadata)["supported_parameters"]


@pytest.mark.parametrize("reasoning,expected", [
    ({}, set()),
    ({"supported_efforts": ["high"]}, set()),
    ({"supported_efforts": None}, {"request.reasoning_effort.level"}),
    ({"supported_efforts": ["none"], "mandatory": True}, set()),
    ({"supported_efforts": ["none"], "mandatory": "false"}, set()),
    ({"supports_max_tokens": False}, set()),
    ({"supports_max_tokens": True}, {"request.reasoning.token_budget"}),
])
def test_reasoning_advertisement_does_not_imply_every_control(reasoning, expected):
    facts = openrouter_capability_facts(*_evidence({"supported_parameters": ["reasoning"], "reasoning": reasoning}))
    assert {f.key for f in facts if f.key != "generation.streaming"} == expected


@pytest.mark.parametrize("change", ["generic", "fake", "disabled", "retired", "stale", "unavailable", "manual", "old", "unproven", "foreign"])
def test_untrusted_or_stale_catalogue_never_inherits_openrouter_support(change):
    connection, model = _evidence({"supported_parameters": ["temperature", "max_tokens", "top_p"]})
    if change == "generic": connection = replace(connection, profile=ProviderProfile.GENERIC)
    if change == "fake": connection = replace(connection, backend_type=BackendType.FAKE)
    if change == "disabled": connection = replace(connection, enabled=False)
    if change == "retired": connection = replace(connection, retired=True)
    if change == "stale": model = replace(model, availability=CatalogueAvailability.STALE)
    if change == "unavailable": model = replace(model, availability=CatalogueAvailability.UNAVAILABLE)
    if change == "manual": model = replace(model, origin=CatalogueOrigin.MANUAL)
    if change == "old": model = replace(model, discovery_revision=1)
    if change == "unproven": model = replace(model, discovery_revision=None)
    if change == "foreign": model = replace(model, connection_id="other")
    assert openrouter_capability_facts(connection, model) == ()


def _request(**extra):
    return CompletionRequest(model="test/text-model", system="", user="hi", temperature=0.0,
                             max_output_tokens=32, timeout_seconds=10, **extra)


def test_openrouter_payload_truthful_mapping_and_independent_gates():
    settings = GenerationSettingsPayload(repetition_penalty=1.1, top_p=0.8, top_k=10, min_p=0.1,
                                         logit_bias={"word": 1}, logprobs=True, top_logprobs=3)
    states = {key: "emitted" for key in settings.configured()}
    request = _request(generation_settings=settings, generation_setting_states=states,
                       generation_setting_capabilities={"top_p": "unknown", "top_k": "unsupported"},
                       generation_omitted_settings={"min_p": "omitted_unknown"})
    payload = OpenRouterProvider("synthetic")._payload(request, stream=True)
    assert payload["provider"] == {"require_parameters": True}
    assert payload["repetition_penalty"] == 1.1
    assert payload["logprobs"] is True and payload["top_logprobs"] == 3
    assert not {"repeat_penalty", "logit_bias", "top_p", "top_k", "min_p"} & payload.keys()
    generic = OpenAICompatibleProvider("http://localhost/v1")._payload(request, stream=True)
    assert generic["repeat_penalty"] == 1.1
    assert "provider" not in generic and "repetition_penalty" not in generic
    unproven = OpenRouterProvider("synthetic")._payload(replace(request, generation_setting_states=None), stream=True)
    assert not set(settings.configured()) & unproven.keys()


@pytest.mark.parametrize("logprobs,capstate", [(None, "supported"), (False, "supported"), (True, "unknown")])
def test_top_logprobs_dependency_agrees_between_plan_and_provider(logprobs, capstate):
    extra = {"top_logprobs": 3}
    if logprobs is not None: extra["logprobs"] = logprobs
    resolved = ResolvedGenerationSettings(0.0, 32, None, None, {}, extra=extra)
    caps = {key: ResolvedCapability(key, CapabilityState(state), CapabilitySource.PROVIDER_METADATA)
            for key, state in (("request.top_logprobs", "supported"), ("request.logprobs", capstate))}
    plan = resolve_setting_emission_plan(resolved, caps, family=PayloadFamily.OPENAI_COMPATIBLE, profile="openrouter")
    assert plan.by_key()["top_logprobs"].state == STATE_OMITTED_INVALID
    forged = _request(generation_settings=GenerationSettingsPayload(**extra),
                      generation_setting_states={key: "emitted" for key in extra},
                      generation_setting_capabilities={"logprobs": capstate})
    assert "top_logprobs" not in OpenRouterProvider("synthetic")._payload(forged, stream=True)


async def _setup(application, *, profile=ProviderProfile.OPENROUTER):
    connection = await application.create_provider_connection(name="Catalogue test", backend_type=BackendType.OPENAI_COMPATIBLE_HTTP,
        profile=profile, endpoint="https://openrouter.ai/api/v1", credential_source=CredentialSource.SECRET_SERVICE,
        credential_reference="test-ref")
    return connection


def _discoverer(parameters, *, failure=False):
    def handler(request):
        if failure: return httpx.Response(503)
        return httpx.Response(200, json={"data": [{"id": "test/text-model", "context_length": 32000,
            "supported_parameters": parameters, "top_provider": {"max_completion_tokens": 4096}}]})
    return OpenAICompatibleModelDiscoverer(transport=httpx.MockTransport(handler))


def test_refresh_revokes_removed_support_manual_metadata_cannot_forge_it(tmp_path):
    async def scenario():
        app, store = _configured_application(tmp_path, secret_store=FakeSecretStore({"test-ref": "synthetic"}))
        try:
            connection = await _setup(app)
            model, = await app.refresh_models(connection.id, _discoverer(["temperature", "max_tokens", "top_p"]))
            assert store.get_model_catalogue_entry(model.id).metadata["supported_parameters"] == ["max_tokens", "temperature", "top_p"]
            assert store.list_generation_setting_capability_overrides(model.id) == ()
            assert app._configuration.resolve_generation_setting_capabilities(model.id)["request.top_p"].state is CapabilityState.SUPPORTED
            await app.refresh_models(connection.id, _discoverer(["max_tokens"]))
            assert app._configuration.resolve_generation_setting_capabilities(model.id)["request.top_p"].state is CapabilityState.UNKNOWN
            assert {f.key: f for f in app._configuration.resolve_capabilities(model.id)}["request.temperature"].state is CapabilityState.UNKNOWN
            await app.add_manual_model(connection_id=connection.id, provider_model_id=model.provider_model_id,
                                       metadata={"supported_parameters": ["top_p", "temperature"]})
            assert app._configuration.resolve_generation_setting_capabilities(model.id)["request.top_p"].state is CapabilityState.UNKNOWN
            manual_legacy = {f.key: f for f in app._configuration.resolve_capabilities(model.id)}
            assert manual_legacy["request.max_output_tokens"].state is CapabilityState.UNKNOWN
            assert manual_legacy["generation.streaming"].state is CapabilityState.UNKNOWN
            await app.refresh_models(connection.id, _discoverer(["temperature", "max_tokens", "top_p"]))
            await app.set_capability_override(CapabilityOverride(model.id, "request.temperature", CapabilityState.UNSUPPORTED))
            assert {f.key: f for f in app._configuration.resolve_capabilities(model.id)}["request.temperature"].state is CapabilityState.UNSUPPORTED
            with pytest.raises(Exception, match="model discovery failed"):
                await app.refresh_models(connection.id, _discoverer([], failure=True))
            assert app._configuration.resolve_generation_setting_capabilities(model.id)["request.top_p"].state is CapabilityState.UNKNOWN
        finally:
            await app.close()
    asyncio.run(scenario())


def test_generic_discovery_and_fake_facts_remain_unchanged(tmp_path):
    async def scenario():
        app, store = _configured_application(tmp_path, secret_store=FakeSecretStore({"test-ref": "synthetic"}))
        try:
            fake = next(m for m in store.list_model_catalogue_entries() if m.provider_model_id == "fake-v0.1")
            before = app._configuration.resolve_capabilities(fake.id)
            connection = await _setup(app, profile=ProviderProfile.GENERIC)
            model, = await app.refresh_models(connection.id, _discoverer(["temperature", "max_tokens", "top_p"]))
            assert "supported_parameters" not in model.metadata
            assert app._configuration.resolve_generation_setting_capabilities(model.id)["request.top_p"].state is CapabilityState.UNKNOWN
            assert {f.key: f for f in app._configuration.resolve_capabilities(model.id)}["generation.streaming"].state is CapabilityState.UNKNOWN
            assert app._configuration.resolve_capabilities(fake.id) == before
        finally:
            await app.close()
    asyncio.run(scenario())


def test_generic_desktop_exact_gate_remains_fail_closed(tmp_path):
    async def scenario():
        app, store = _configured_application(tmp_path, secret_store=FakeSecretStore({"test-ref": "synthetic"}))
        app._configuration.phase6_enabled = True  # same setting as desktop bootstrap
        try:
            connection = await _setup(app, profile=ProviderProfile.GENERIC)
            model, = await app.refresh_models(connection.id, _discoverer(["temperature", "max_tokens"]))
            for key in ('generation.streaming','request.temperature','request.max_output_tokens'):
                await app.set_capability_override(CapabilityOverride(model.id, key, CapabilityState.SUPPORTED))
            chat = await app.create_chat()
            await app.select_model(chat.id, model.id)
            with pytest.raises(StateError, match="no exact Phase 6 accounting adapter is registered for this backend"):
                await app.send_message(chat.id, "Hello")
            assert store.list_generation_attempts(chat.id) == ()
        finally:
            await app.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("max_parameter", ["max_tokens", "max_completion_tokens"])
def test_legacy_supported_http_path_streams_with_discovered_facts(tmp_path, max_parameter):
    async def scenario():
        secrets = FakeSecretStore({"test-ref": "synthetic"})
        router = BuiltinProviderRouter(secret_stores={"secret_service": secrets})
        app, store = _configured_application(tmp_path, backend=router, secret_store=secrets)
        sent = []
        try:
            connection = await _setup(app)
            model, = await app.refresh_models(connection.id, _discoverer(["temperature", max_parameter]))
            def stream_handler(request):
                payload = json.loads(request.content)
                sent.append(payload)
                return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"Hello"},"finish_reason":null}]}\n\ndata: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n', headers={"content-type": "text/event-stream"})
            router._transports[connection.id] = httpx.MockTransport(stream_handler)
            chat = await app.create_chat()
            await app.select_model(chat.id, model.id)
            attempt = await app.send_message(chat.id, "Hello")
            for _ in range(100):
                current = store.get_generation_attempt(attempt.id)
                if current.state is not AttemptState.RUNNING: break
                await asyncio.sleep(0.01)
            assert current.state is AttemptState.COMPLETE
            assert current.finish_reason == "stop"
            assert len(sent) == 1 and sent[0]["stream"] is True
            assert set(sent[0]) == {"model", "messages", "temperature", max_parameter, "stream", "provider"}
        finally:
            await app.close()
    asyncio.run(scenario())


def test_persisted_catalogue_drives_tune_and_retains_inactive_values(tmp_path):
    from PySide6.QtWidgets import QApplication
    from bots5.desktop.widgets import TuneDialog
    from bots5.core.provider_configuration import ProviderConfiguration
    from bots5.domain.clock import SystemClock
    from bots5.domain.ids import Uuid7Factory
    from tests._authority_test_support import SQLiteAppStateStore
    QApplication.instance() or QApplication([])

    async def setup():
        app, store = _configured_application(tmp_path, secret_store=FakeSecretStore({"test-ref": "synthetic"}))
        try:
            connection = await _setup(app)
            model, = await app.refresh_models(connection.id, _discoverer(["temperature", "max_completion_tokens", "top_p"]))
            return model.id
        finally:
            await app.close()

    model_id = asyncio.run(setup())
    store = SQLiteAppStateStore.open(tmp_path / "state.sqlite3")
    dialog = TuneDialog()
    try:
        config = ProviderConfiguration(store, Uuid7Factory(), SystemClock())
        capabilities = config.resolve_generation_setting_capabilities(model_id)
        assert capabilities["request.top_p"].source is CapabilitySource.PROVIDER_METADATA
        assert store.list_generation_setting_capability_overrides(model_id) == ()
        legacy = {item.key: item for item in config.resolve_capabilities(model_id)}
        assert legacy["request.max_output_tokens"].state is CapabilityState.SUPPORTED
        assert legacy["limits.output_tokens"].value == 4096
        values = {"top_p": 0.8, "top_k": 10}
        dialog.set_settings({"temperature": 0.0, "max_output_tokens": 1024, "reasoning_effort": None, **values},
                            {key: "chat_model" for key in values}, extra_values=values, capabilities=capabilities)
        rows = dialog.generation_settings_editor._rows
        assert rows["top_p"].control.isEnabled()
        assert rows["top_p"].badge.text() == "ACTIVE"
        assert not rows["top_k"].control.isEnabled()
        assert rows["top_k"].badge.text() == "INACTIVE"
        assert rows["top_k"].get_value() == 10
        assert "preserved" in rows["top_k"].note.text()
    finally:
        dialog.close()
        store.close()


def test_output_token_alias_is_closed_at_provider_boundary():
    from bots5.errors import ProviderError
    with pytest.raises(ProviderError, match="unsupported output-token parameter"):
        OpenRouterProvider("synthetic")._payload(_request(max_output_parameter="arbitrary_vendor_field"), stream=True)
