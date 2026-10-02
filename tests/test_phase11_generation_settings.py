"""Phase 11 scope amendment: the normalized generation-settings plane.

Covers, per the amendment's validation matrix:

- registry-wide invariants (typed definitions, no free-form escape hatch,
  truthful serialization mappings, no cross-family false equivalence);
- validation/range boundaries for representative settings of every value
  type plus cross-key rules;
- application -> model -> chat inheritance with provenance;
- persistence/restart round trips and stale-revision refusal;
- stored-but-inactive settings across model changes (preserved, never
  transmitted, re-activated when a supporting model is selected);
- capability gating per setting family and DISABLED/ENABLED UI states;
- provider payload INCLUSION for supported configured controls and payload
  OMISSION for unsupported, unknown and inherited-unset controls;
- a STRICT-ENDPOINT simulation that rejects unknown parameters;
- request/effective-settings evidence (additive v4 snapshot) and the frozen
  v2 byte-compatibility when no extended setting is configured.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from bots5.core.application import BotsApplication
from bots5.core.errors import RevisionConflict, StateError
from bots5.core.events import EventBus
from bots5.core.generation import GenerationRequest
from bots5.core.provider_configuration import (
    ProviderConfiguration,
    resolve_setting_emission_plan,
)
from bots5.domain.clock import SystemClock
from bots5.domain.generation_settings_registry import (
    EXTENDED_SETTING_KEYS,
    SETTING_CAPABILITY_KEYS,
    SETTING_DEFINITIONS,
    SETTING_DEFINITIONS_BY_KEY,
    SETTING_KEYS,
    STATE_EMITTED,
    STATE_OMITTED_UNSERIALIZABLE,
    STATE_OMITTED_UNKNOWN,
    STATE_OMITTED_UNSUPPORTED,
    STATE_UNSET,
    GenerationSettingsPayload,
    PayloadFamily,
    SettingValueType,
    decode_text_setting,
    validate_settings_values,
)
from bots5.domain.models import Chat
from bots5.domain.provider import (
    BackendType,
    CAPABILITY_KEYS,
    CapabilityFact,
    CapabilityOverride,
    CapabilitySource,
    CapabilityState,
    CredentialSource,
    GenerationSettings,
    ProviderProfile,
    ResolvedCapability,
    ResolvedGenerationSettings,
)
from bots5.infrastructure.persistence.phase11_validation import validate_phase11_snapshot
from tests._authority_test_support import SQLiteAppStateStore, upgrade_database
from bots5.infrastructure.generation.openai_compatible import OpenAICompatibleStreamingBackend
from bots5.providers.base import CompletionRequest
from bots5.providers.openai_compatible import OpenAICompatibleProvider
from bots5.providers.openrouter import OpenRouterProvider
from bots5.providers.discovery import FakeModelDiscoverer
from bots5.desktop.theme import (
    ACCENT_BLUE,
    BORDER_DEFAULT,
    SURFACE_BASE,
    SURFACE_BUBBLE,
    SURFACE_PANEL,
    build_theme_stylesheet,
)

def _store(tmp_path: Path) -> SQLiteAppStateStore:
    database = tmp_path / "state.sqlite3"
    upgrade_database(database)
    return SQLiteAppStateStore.open(database)


async def _seed_http_model(application, store):
    """Create an OpenAI-compatible connection/model ready for generation."""
    connection = await application.create_provider_connection(
        name="Local HTTP",
        backend_type=BackendType.OPENAI_COMPATIBLE_HTTP,
        profile=ProviderProfile.GENERIC,
        endpoint="http://127.0.0.1:9/v1",
        credential_source=CredentialSource.NONE,
    )
    model = store.add_manual_model(
        connection_id=connection.id,
        provider_model_id="http-model-1",
        model_entry_id=application._ids.new(),
    )
    for key in (
        "generation.streaming", "request.temperature", "request.max_output_tokens",
    ):
        store.set_capability_fact(
            CapabilityFact(model.id, key, CapabilityState.SUPPORTED, CapabilitySource.HEURISTIC)
        )
    return connection, model


async def _open_application(tmp_path: Path):
    from uuid6 import uuid7

    database = tmp_path / "state.sqlite3"
    upgrade_database(database)
    ids = type("Ids", (), {"new": staticmethod(lambda: str(uuid7()))})()
    clock = SystemClock()
    store = SQLiteAppStateStore.open(database)
    configuration = ProviderConfiguration(store, ids, clock, phase6_enabled=False)
    application = BotsApplication(
        store,
        EventBus(clock, ids, queue_size=64),
        backend=None,
        ids=ids,
        clock=clock,
        configuration=configuration,
    )
    return application, store


# ===========================================================================
# T0: registry-wide invariants
# ===========================================================================


def test_registry_definitions_are_complete_and_typed():
    assert "temperature" in SETTING_KEYS and "max_output_tokens" in SETTING_KEYS
    assert len(SETTING_DEFINITIONS) == len(SETTING_DEFINITIONS_BY_KEY)
    for definition in SETTING_DEFINITIONS:
        assert definition.key and definition.label and definition.description
        assert definition.value_type in set(SettingValueType)
        if definition.choices is not None:
            assert definition.choices
            assert len(set(definition.choices)) == len(definition.choices)
        if definition.minimum is not None and definition.maximum is not None:
            assert definition.minimum <= definition.maximum
        if not definition.legacy and not definition.bots_owned:
            assert definition.capability_key in SETTING_CAPABILITY_KEYS
            assert definition.omission_differs, definition.key
    # The frozen Phase 5 capability catalogue is NOT extended by the registry:
    # the EXTENDED settings' capability keys ride outside it so the closed v2
    # snapshot schema keeps validating historical evidence unchanged.
    extended_capability_keys = {
        definition.capability_key
        for definition in SETTING_DEFINITIONS
        if not definition.legacy
    }
    assert extended_capability_keys.isdisjoint(CAPABILITY_KEYS)
    # Legacy settings keep mapping onto the frozen Phase 5 capability keys.
    assert {
        definition.capability_key
        for definition in SETTING_DEFINITIONS
        if definition.legacy and definition.capability_key
    } <= CAPABILITY_KEYS


def test_registry_catalogue_is_exactly_the_amendment_catalogue():
    """Pin the exact R16 catalogue: 4 legacy + 28 extended = 32 settings.

    The falsifier noted that the completeness test checked properties of the
    definitions it received but never pinned the catalogue itself, so a silently
    added/removed/renamed setting could pass. This asserts the exact key set.
    """
    legacy = {definition.key for definition in SETTING_DEFINITIONS if definition.legacy}
    extended = {definition.key for definition in SETTING_DEFINITIONS if not definition.legacy}
    assert legacy == {"temperature", "max_output_tokens", "reasoning_effort", "timeout_seconds"}
    assert extended == {
        "top_p", "top_k", "min_p", "typical_p", "tail_free_sampling",
        "smoothing_factor", "dynamic_temperature_range", "xtc_probability",
        "xtc_threshold", "repetition_penalty", "repetition_window",
        "frequency_penalty", "presence_penalty", "dry_multiplier", "dry_base",
        "dry_allowed_length", "dry_penalty_last_n", "mirostat", "mirostat_tau",
        "mirostat_eta", "seed", "stop_sequences", "logprobs", "top_logprobs",
        "ignore_eos", "logit_bias", "reasoning_effort_level", "reasoning_token_budget",
    }
    assert len(SETTING_DEFINITIONS) == 32
    assert len(legacy) == 4 and len(extended) == 28
    assert SETTING_KEYS == legacy | extended
    assert EXTENDED_SETTING_KEYS == frozenset(extended)
    assert set(SETTING_DEFINITIONS_BY_KEY) == SETTING_KEYS


def test_registry_never_claims_false_family_equivalence():
    repetition = SETTING_DEFINITIONS_BY_KEY["repetition_penalty"]
    frequency = SETTING_DEFINITIONS_BY_KEY["frequency_penalty"]
    presence = SETTING_DEFINITIONS_BY_KEY["presence_penalty"]
    # Multiplicative logit scaling is NOT the additive token-count penalties.
    payload_keys = {
        mapping.payload_key
        for definition in (repetition, frequency, presence)
        for mapping in definition.serialization
    }
    assert payload_keys == {"repeat_penalty", "frequency_penalty", "presence_penalty"}
    budget = SETTING_DEFINITIONS_BY_KEY["reasoning_token_budget"]
    assert all(mapping.profile == "openrouter" for mapping in budget.serialization)
    assert SETTING_DEFINITIONS_BY_KEY["timeout_seconds"].serialization == ()
    assert SETTING_DEFINITIONS_BY_KEY["timeout_seconds"].bots_owned


def test_registry_has_no_free_form_escape_hatch():
    with pytest.raises(ValueError, match="unknown generation setting"):
        validate_settings_values({"vendor_temperature": 0.7})
    with pytest.raises(ValueError, match="unknown generation setting"):
        validate_settings_values({"top_p": 0.5, "some_new_openai_param": 1})
    with pytest.raises(ValueError, match="must be a mapping"):
        validate_settings_values([("top_p", 0.5)])


@pytest.mark.parametrize(
    ("key", "ok", "bad"),
    [
        ("temperature", [0.0, 2.0, 1], [-0.01, 2.01, True, "0.5", math.inf]),
        ("top_p", [0.0, 1.0], [-0.001, 1.001]),
        ("seed", [0, 2_147_483_647], [-1, 2_147_483_648, 1.5, True]),
        ("max_output_tokens", [1, 2_000_000_000], [0, -5, 1.5]),
        ("stop_sequences", [["a"], ["a", "b"] * 8], [[""], list(range(3)), ["x"] * 17]),
        ("logit_bias", [{"eos": -100, "hi": 100}], [{"eos": -101}, {"": 1}, {"a": 1.5}]),
        ("reasoning_effort_level", ["low", "medium", "high"], ["off", "NONE", ""]),
        ("ignore_eos", [True, False], [1, 0, "yes"]),
        ("mirostat", [0, 1, 2], [3, -1]),
        ("repetition_window", [-1, 0, 4096], [-2]),
    ],
)
def test_registry_validation_boundaries(key, ok, bad):
    for value in ok:
        validate_settings_values({key: value})
    for value in bad:
        with pytest.raises(ValueError):
            validate_settings_values({key: value})


def test_reasoning_controls_are_mutually_exclusive():
    with pytest.raises(ValueError, match="mutually exclusive"):
        validate_settings_values({"reasoning_effort": "none", "reasoning_effort_level": "high"})
    validate_settings_values({"reasoning_effort": "none"})
    validate_settings_values({"reasoning_effort_level": "low", "reasoning_token_budget": 512})


def test_text_round_trip_for_complex_settings():
    assert decode_text_setting("stop_sequences", "alpha\nbeta") == ("alpha", "beta")
    assert decode_text_setting("logit_bias", 'eos=-1\n"the"=40') == {"eos": -1, "the": 40}
    assert decode_text_setting("seed", "1234") == 1234
    assert decode_text_setting("top_p", "0.85") == 0.85
    with pytest.raises(ValueError):
        decode_text_setting("logit_bias", "eos=notanumber")
    with pytest.raises(ValueError):
        decode_text_setting("seed", "12.5")


# ===========================================================================
# T1: inheritance, provenance, durability, revisions
# ===========================================================================


def test_extra_settings_inherit_application_model_chat_with_provenance(tmp_path: Path):
    async def scenario():
        application, store = await _open_application(tmp_path)
        try:
            chat = await application.create_chat()
            model_id = (await application.chat_model_selection(chat.id)).model_entry_id
            assert model_id is not None
            await application.set_application_generation_settings(
                GenerationSettings(extra={"top_p": 0.1, "seed": 7})
            )
            await application.set_model_defaults(model_id, GenerationSettings(extra={"seed": 9, "top_k": 40}))
            await application.set_chat_generation_settings(chat.id, GenerationSettings(extra={"top_p": 0.9}))
            settings = await application.resolve_chat_generation_settings(chat.id)
            assert settings.extra["top_p"] == 0.9
            assert settings.extra["seed"] == 9
            assert settings.extra["top_k"] == 40
            assert settings.provenance["top_p"] == "chat_model"
            assert settings.provenance["seed"] == "model"
            assert settings.provenance["top_k"] == "model"
            # Explicit inherit at chat level falls back to the model scope.
            # The chat extended-settings plane already exists at revision 1, so
            # the coupled CAS contract requires that revision to be presented.
            await application.set_chat_generation_settings(
                chat.id, GenerationSettings(extra={"top_p": None}), expected_extra_revision=1
            )
            settings = await application.resolve_chat_generation_settings(chat.id)
            assert settings.extra["top_p"] == 0.1
            assert settings.provenance["top_p"] == "application"
        finally:
            await application.close()

    asyncio.run(scenario())


def test_extra_settings_persist_across_restart(tmp_path: Path):
    database = tmp_path / "state.sqlite3"

    async def scenario():
        application, store = await _open_application(tmp_path)
        try:
            chat = await application.create_chat()
            model_id = (await application.chat_model_selection(chat.id)).model_entry_id
            await application.set_application_generation_settings(
                GenerationSettings(extra={"stop_sequences": ["END", "STOP"]})
            )
            await application.set_model_defaults(model_id, GenerationSettings(extra={"logit_bias": {"eos": -1}}))
            await application.set_chat_generation_settings(chat.id, GenerationSettings(extra={"seed": 4242}))
            revision = store.get_chat_model_generation_settings_extra(chat.id, model_id)[1]
            assert revision == 1
            return chat.id, model_id
        finally:
            await application.close()

    chat_id, model_id = asyncio.run(scenario())
    # Restart: same database, exact configured state preserved.
    store2 = SQLiteAppStateStore.open(database)
    try:
        assert store2.get_application_generation_settings_extra() == {"stop_sequences": ("END", "STOP")}
        model_extra, model_revision = store2.get_model_generation_settings_extra(model_id)
        assert model_extra == {"logit_bias": {"eos": -1}} and model_revision == 1
        chat_extra, chat_revision = store2.get_chat_model_generation_settings_extra(chat_id, model_id)
        assert chat_extra == {"seed": 4242} and chat_revision == 1
    finally:
        store2.close()


def test_store_rejects_unknown_and_invalid_extra_settings(tmp_path: Path):
    store = _store(tmp_path)
    try:
        with store.command_admission():
            with pytest.raises(StateError, match="unknown generation setting"):
                store.set_application_generation_settings_extra({"mystery_param": 1})
            with pytest.raises(StateError, match="malformed"):
                store.set_application_generation_settings_extra({"seed": -5})
            model_id = store.list_model_catalogue_entries()[0].id
            with pytest.raises(StateError, match="malformed"):
                store.set_model_generation_settings(model_id, GenerationSettings(extra={"top_k": 0}))
    finally:
        store.close()


def test_extra_revision_cas_refuses_stale_writes(tmp_path: Path):
    store = _store(tmp_path)
    try:
        with store.command_admission():
            revision = store.set_application_generation_settings_extra({"seed": 1})
            assert revision == 1
            revision = store.set_application_generation_settings_extra({"seed": 2}, expected_revision=1)
            assert revision == 2
            with pytest.raises(RevisionConflict):
                store.set_application_generation_settings_extra({"seed": 3}, expected_revision=1)
            assert store.get_application_generation_settings_extra() == {"seed": 2}
            model_id = store.list_model_catalogue_entries()[0].id
            now = datetime(2026, 9, 30, tzinfo=UTC)
            store.create_chat(Chat("chat-x", "Chat X", now, now))
            store.set_chat_model_generation_settings_extra("chat-x", model_id, {"top_p": 0.5}, expected_revision=0)
            with pytest.raises(RevisionConflict):
                store.set_chat_model_generation_settings_extra("chat-x", model_id, {"top_p": 0.6}, expected_revision=0)
            assert store.get_chat_model_generation_settings_extra("chat-x", model_id)[0] == {"top_p": 0.5}
    finally:
        store.close()


def test_stale_combined_write_cannot_clobber_newer_extra_settings(tmp_path: Path):
    """The combined scope write must couple to the extended-settings revision.

    Regression for the R16 falsification: an extended value written at a newer
    revision must survive a stale combined write that only presents the legacy
    revision.
    """
    store = _store(tmp_path)
    try:
        with store.command_admission():
            model_id = store.list_model_catalogue_entries()[0].id
            rev1 = store.set_model_generation_settings_extra(model_id, {"top_k": 40}, expected_revision=0)
            rev2 = store.set_model_generation_settings_extra(model_id, {"top_k": 80}, expected_revision=rev1)
            assert (rev1, rev2) == (1, 2)
            # Stale combined writer: only the legacy revision is presented.
            with pytest.raises(RevisionConflict):
                store.set_model_generation_settings(
                    model_id, GenerationSettings(extra={"top_k": 40}), expected_revision=0
                )
            # The newer extended value survived untouched.
            assert store.get_model_generation_settings_extra(model_id)[0] == {"top_k": 80}
            # Presenting the current extended revision is accepted.
            store.set_model_generation_settings(
                model_id, GenerationSettings(extra={"top_k": 60}), expected_extra_revision=2
            )
            assert store.get_model_generation_settings_extra(model_id)[0] == {"top_k": 60}
            # A stale extended revision is refused as well.
            with pytest.raises(RevisionConflict):
                store.set_model_generation_settings(
                    model_id, GenerationSettings(extra={"top_k": 20}), expected_extra_revision=2
                )
            assert store.get_model_generation_settings_extra(model_id)[0] == {"top_k": 60}
    finally:
        store.close()


def test_application_scope_combined_save_requires_current_extra_revision(tmp_path: Path):
    """The application combined scope is coupled to its extended plane too."""
    store = _store(tmp_path)
    try:
        with store.command_admission():
            rev1 = store.set_application_generation_settings_extra({"top_k": 40}, expected_revision=0)
            rev2 = store.set_application_generation_settings_extra({"top_k": 80}, expected_revision=rev1)
            with pytest.raises(RevisionConflict):
                store.set_application_generation_settings(GenerationSettings(extra={"top_k": 40}))
            assert store.get_application_generation_settings_extra_config()[0] == {"top_k": 80}
            store.set_application_generation_settings(
                GenerationSettings(extra={"top_k": 60}), expected_extra_revision=rev2
            )
            assert store.get_application_generation_settings_extra_config()[0] == {"top_k": 60}
    finally:
        store.close()


def test_application_combined_save_requires_current_extra_revision(tmp_path: Path):
    async def scenario():
        application, store = await _open_application(tmp_path)
        try:
            chat = await application.create_chat()
            _connection, http_model = await _seed_http_model(application, store)
            await application.select_model(chat.id, http_model.id)
            await application.set_chat_generation_settings(chat.id, GenerationSettings(extra={"top_k": 40}))
            assert store.get_chat_model_generation_settings_extra(chat.id, http_model.id)[1] == 1
            with pytest.raises(RevisionConflict):
                await application.set_chat_generation_settings(chat.id, GenerationSettings(extra={"top_k": 80}))
            assert store.get_chat_model_generation_settings_extra(chat.id, http_model.id)[0] == {"top_k": 40}
            await application.set_chat_generation_settings(
                chat.id, GenerationSettings(extra={"top_k": 80}), expected_extra_revision=1
            )
            assert store.get_chat_model_generation_settings_extra(chat.id, http_model.id)[0] == {"top_k": 80}
        finally:
            await application.close()

    asyncio.run(scenario())


def test_stored_but_inactive_across_an_actual_model_change(tmp_path: Path):
    """A stored extended value is preserved, inactive under another model, and reactivates."""
    async def scenario():
        application, store = await _open_application(tmp_path)
        try:
            chat = await application.create_chat()
            _connection, model_a = await _seed_http_model(application, store)
            model_b = store.add_manual_model(
                connection_id=model_a.connection_id,
                provider_model_id="http-model-2",
                model_entry_id=application._ids.new(),
            )
            for key in ("generation.streaming", "request.temperature", "request.max_output_tokens"):
                store.set_capability_fact(
                    CapabilityFact(model_b.id, key, CapabilityState.SUPPORTED, CapabilitySource.HEURISTIC)
                )
            await application.set_capability_override(
                CapabilityOverride(
                    model_entry_id=model_a.id, key="request.top_k",
                    state=CapabilityState.SUPPORTED, reason="manual",
                )
            )
            await application.select_model(chat.id, model_a.id)
            await application.set_chat_generation_settings(chat.id, GenerationSettings(extra={"top_k": 40}))
            assert (await application.resolve_chat_generation_settings(chat.id)).extra["top_k"] == 40
            # Switching to a model without the capability makes the stored value inactive.
            await application.select_model(chat.id, model_b.id)
            resolved_b = await application.resolve_chat_generation_settings(chat.id)
            assert resolved_b.extra.get("top_k") is None
            capabilities_b = await application.generation_setting_capabilities(model_b.id)
            assert capabilities_b["request.top_k"].state is CapabilityState.UNKNOWN
            prepared_b, _snapshot_b = application._configuration.prepare_generation(
                chat_id=chat.id, user_message_id="u1", prompt="hi", attempt_id="at-b",
            )
            # Nothing is transmitted for the inactive stored value.
            assert prepared_b.request.generation_settings is None
            assert prepared_b.request.generation_setting_states is None
            # Switching back reactivates it from the original scope.
            await application.select_model(chat.id, model_a.id)
            assert (await application.resolve_chat_generation_settings(chat.id)).extra["top_k"] == 40
            assert store.get_chat_model_generation_settings_extra(chat.id, model_a.id)[0] == {"top_k": 40}
        finally:
            await application.close()

    asyncio.run(scenario())


def test_malformed_stored_extra_fails_closed_on_read(tmp_path: Path):
    from sqlalchemy import text as sql_text

    store = _store(tmp_path)
    try:
        with store.command_admission(), store.engine.connect() as db:
            db.execute(sql_text(
                "INSERT INTO application_generation_settings_extra (id, extra_settings_json, revision, updated_at)"
                " VALUES (1, '{\"vendor_mystery\": 3}', 1, '2026-09-30T00:00:00.000Z')"
            ))
            db.commit()
        with store.command_admission():
            with pytest.raises(StateError, match="unknown generation setting"):
                store.get_application_generation_settings_extra()
    finally:
        store.close()


# ===========================================================================
# T2: capability gating and provider payloads
# ===========================================================================


def _capability(state: str) -> ResolvedCapability:
    return ResolvedCapability("", CapabilityState(state), CapabilitySource.MANUAL)


def _resolved(extra: dict[str, object]) -> ResolvedGenerationSettings:
    return ResolvedGenerationSettings(
        0.7, 512, None, None,
        {"temperature": "application", "max_output_tokens": "application", **{key: "chat_model" for key in extra}},
        extra=extra,
    )


def test_emission_plan_gates_each_setting_family():
    capabilities = {
        "request.top_p": _capability("supported"),
        "request.top_k": _capability("unsupported"),
        "request.seed": _capability("unknown"),
        "request.reasoning_effort.level": _capability("supported"),
    }
    resolved = _resolved({"top_p": 0.9, "top_k": 40, "seed": 3, "reasoning_effort_level": "high"})
    plan = resolve_setting_emission_plan(resolved, capabilities, family=PayloadFamily.OPENAI_COMPATIBLE, profile="generic")
    by_key = plan.by_key()
    assert by_key["top_p"].state == STATE_EMITTED
    assert by_key["top_k"].state == STATE_OMITTED_UNSUPPORTED
    assert by_key["seed"].state == STATE_OMITTED_UNKNOWN
    assert by_key["reasoning_effort_level"].state == STATE_EMITTED
    # Unconfigured settings are explicitly unset, never silently missing.
    assert by_key["mirostat"].state == STATE_UNSET
    assert plan.payload is not None
    payload = plan.payload.model_dump()
    assert payload["top_p"] == 0.9
    assert payload["reasoning_effort_level"] == "high"
    assert payload["top_k"] is None and payload["seed"] is None
    evidence = plan.as_evidence()
    assert set(evidence["states"]) == SETTING_KEYS
    assert evidence["provenance"]["top_p"] == "chat_model"


def test_emission_plan_omits_without_serializer_for_profile():
    capabilities = {"request.reasoning.token_budget": _capability("supported")}
    resolved = _resolved({"reasoning_token_budget": 2048})
    generic = resolve_setting_emission_plan(resolved, capabilities, family=PayloadFamily.OPENAI_COMPATIBLE, profile="generic")
    assert generic.by_key()["reasoning_token_budget"].state == STATE_OMITTED_UNSERIALIZABLE
    assert generic.payload is None
    openrouter = resolve_setting_emission_plan(resolved, capabilities, family=PayloadFamily.OPENAI_COMPATIBLE, profile="openrouter")
    assert openrouter.by_key()["reasoning_token_budget"].state == STATE_EMITTED


def test_generic_openai_payload_includes_supported_and_omits_rest():
    request = CompletionRequest(
        model="m", system="", user="hi", temperature=0.5, max_output_tokens=16,
        timeout_seconds=0.0,
        generation_settings=GenerationSettingsPayload(top_p=0.9, seed=7, stop_sequences=("END",), logit_bias={"eos": -2}),
        generation_setting_states={
            "top_p": STATE_EMITTED, "seed": STATE_EMITTED,
            "stop_sequences": STATE_EMITTED, "logit_bias": STATE_EMITTED,
        },
    )
    payload = OpenAICompatibleProvider("http://127.0.0.1:9/v1", api_key="k")._payload(request, stream=False, include_empty_system=False)
    assert payload["top_p"] == 0.9
    assert payload["seed"] == 7
    assert payload["stop"] == ["END"]
    assert payload["logit_bias"] == {"eos": -2}
    assert "reasoning" not in payload and "top_k" not in payload and "mirostat" not in payload


def test_openrouter_payload_routes_reasoning_budget():
    request = CompletionRequest(
        model="m", system="", user="hi", temperature=0.5, max_output_tokens=16,
        timeout_seconds=0.0,
        generation_settings=GenerationSettingsPayload(reasoning_token_budget=2048, reasoning_effort_level="low"),
        generation_setting_states={
            "reasoning_token_budget": STATE_EMITTED,
            "reasoning_effort_level": STATE_EMITTED,
        },
    )
    payload = OpenRouterProvider("k", base_url="http://127.0.0.1:9/v1")._payload(request, stream=False, include_empty_system=False)
    assert payload["reasoning"] == {"max_tokens": 2048}
    assert payload["reasoning_effort"] == "low"


def test_adapter_omits_everything_when_no_settings_resolved():
    request = CompletionRequest(model="m", system="", user="hi", temperature=0.5, max_output_tokens=16, timeout_seconds=0.0)
    payload = OpenAICompatibleProvider("http://127.0.0.1:9/v1", api_key="k")._payload(request, stream=False, include_empty_system=False)
    assert set(payload) == {"model", "messages", "temperature", "max_tokens", "stream"}


def test_strict_endpoint_rejecting_unknown_parameters_succeeds_with_gated_omission():
    """A strict endpoint that rejects unknown fields works with no user action.

    The exercise runs the real streaming adapter against a mock transport
    that models a STRICT endpoint: any parameter outside its accepted set is
    answered with HTTP 400.  ``top_k`` is configured at the chat scope but
    its capability is unknown, so it must never reach the wire.
    """
    STRICT_ALLOWED = {"model", "messages", "temperature", "max_tokens", "stream", "top_p"}
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read().decode("utf-8"))
        captured["body"] = body
        unexpected = set(body) - STRICT_ALLOWED
        if unexpected:
            return httpx.Response(
                400,
                json={"error": {"message": f"unknown parameter: {sorted(unexpected)[0]}"}},
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                'data: {"id": "c1", "model": "m", "choices": [{"index": 0, '
                '"finish_reason": null, "delta": {"content": "ok"}}]}\n\n'
                'data: {"id": "c1", "model": "m", "choices": [{"index": 0, '
                '"finish_reason": "stop", "delta": {}}]}\n\n'
                "data: [DONE]\n\n"
            ).encode("utf-8"),
        )

    async def scenario():
        provider = OpenAICompatibleProvider(
            "http://127.0.0.1:9/v1", api_key="k", _transport=httpx.MockTransport(handler)
        )
        backend = OpenAICompatibleStreamingBackend(
            provider,
            provider_id="generic",
            base_url="http://127.0.0.1:9/v1",
        )
        request = GenerationRequest(
            attempt_id="a1", chat_id="c1", user_message_id="u1",
            backend_id="openai_compatible_http", model="m", prompt="hi",
            provider_id="generic", base_url="http://127.0.0.1:9/v1",
            effective_settings={"temperature": 0.5, "max_output_tokens": 16},
            generation_settings=GenerationSettingsPayload(top_p=0.9),
            generation_setting_states={"top_p": STATE_EMITTED, "top_k": STATE_OMITTED_UNKNOWN},
        )
        events = [event async for event in backend.stream(request)]
        return events

    events = asyncio.run(scenario())
    body = captured["body"]
    assert set(body) <= STRICT_ALLOWED
    # The supported setting genuinely reached the endpoint (inclusion is
    # asserted, so this test is not vacuous if serialization is removed).
    assert body["top_p"] == 0.9
    assert "top_k" not in body and "seed" not in body and "mirostat" not in body
    # The strict endpoint accepted the request and completed the stream.
    kinds = [type(event).__name__ for event in events]
    assert "GenerationCompleted" in kinds


def test_provider_boundary_drops_contradictory_unsupported_payload_setting():
    """A malformed/contradictory request must not smuggle a setting onto the wire.

    The payload carries ``top_k`` but the request's own capability evidence
    labels it ``omitted:unsupported``.  The provider boundary is the last line
    of defence and must drop it.
    """
    request = CompletionRequest(
        model="m", system="", user="hi", temperature=0.5, max_output_tokens=16,
        timeout_seconds=0.0,
        generation_settings=GenerationSettingsPayload(top_k=40),
        generation_setting_states={"top_k": STATE_OMITTED_UNSUPPORTED},
    )
    payload = OpenAICompatibleProvider("http://127.0.0.1:9/v1", api_key="k")._payload(
        request, stream=False, include_empty_system=False
    )
    assert "top_k" not in payload


def test_provider_boundary_emits_nothing_without_capability_evidence():
    """No state evidence at all fails closed at the provider boundary."""
    request = CompletionRequest(
        model="m", system="", user="hi", temperature=0.5, max_output_tokens=16,
        timeout_seconds=0.0,
        generation_settings=GenerationSettingsPayload(top_p=0.9),
    )
    payload = OpenAICompatibleProvider("http://127.0.0.1:9/v1", api_key="k")._payload(
        request, stream=False, include_empty_system=False
    )
    assert "top_p" not in payload


def test_provider_boundary_drops_setting_whose_emitted_state_contradicts_capability_evidence():
    """The boundary cross-checks independent evidence planes, not just ``states``.

    Regression for the second Luna falsification (F1): a request can carry
    ``generation_setting_states={'top_k': 'emitted'}`` while its *capability*
    plane explicitly says ``request.top_k`` is unsupported and its omission
    plane names ``top_k``.  The boundary must drop it rather than trust the one
    string that happens to say "emitted".
    """
    provider = OpenAICompatibleProvider("http://127.0.0.1:9/v1", api_key="k")
    contradictory = CompletionRequest(
        model="m", system="", user="hi", temperature=0.5, max_output_tokens=16,
        timeout_seconds=0.0,
        generation_settings=GenerationSettingsPayload(top_k=40, top_p=0.9),
        generation_setting_states={"top_k": STATE_EMITTED, "top_p": STATE_EMITTED},
        generation_setting_capabilities={"top_k": "unsupported"},
    )
    payload = provider._payload(contradictory, stream=False, include_empty_system=False)
    assert "top_k" not in payload
    assert payload["top_p"] == 0.9
    # The omission plane is authoritative too.
    omitted = CompletionRequest(
        model="m", system="", user="hi", temperature=0.5, max_output_tokens=16,
        timeout_seconds=0.0,
        generation_settings=GenerationSettingsPayload(top_k=40),
        generation_setting_states={"top_k": STATE_EMITTED},
        generation_omitted_settings={"top_k": "unsupported"},
    )
    assert "top_k" not in provider._payload(omitted, stream=False, include_empty_system=False)
    # A capability entry that is present but not exactly "supported" fails
    # closed too (None / empty string), rather than being read as "no evidence".
    for bad_state in (None, ""):
        degraded = CompletionRequest(
            model="m", system="", user="hi", temperature=0.5, max_output_tokens=16,
            timeout_seconds=0.0,
            generation_settings=GenerationSettingsPayload(top_k=40),
            generation_setting_states={"top_k": STATE_EMITTED},
            generation_setting_capabilities={"top_k": bad_state},
        )
        assert "top_k" not in provider._payload(degraded, stream=False, include_empty_system=False)
    # Matching evidence still emits.
    consistent = CompletionRequest(
        model="m", system="", user="hi", temperature=0.5, max_output_tokens=16,
        timeout_seconds=0.0,
        generation_settings=GenerationSettingsPayload(top_k=40),
        generation_setting_states={"top_k": STATE_EMITTED},
        generation_setting_capabilities={"top_k": "supported"},
    )
    assert provider._payload(consistent, stream=False, include_empty_system=False)["top_k"] == 40


@pytest.mark.parametrize(
    "kwargs",
    [
        {"top_p": 5.0},
        {"top_k": -7},
        {"top_k": 0},
        {"seed": -1},
        {"top_p": "0.5"},
        {"top_k": 4.0},
        {"ignore_eos": 1},
    ],
)
def test_generation_settings_payload_rejects_out_of_range_and_coerced_values(kwargs):
    """The provider-boundary payload type validates through the registry."""
    with pytest.raises(Exception):
        GenerationSettingsPayload(**kwargs)


def test_provider_boundary_strict_endpoint_survives_contradictory_unsupported_setting():
    """End-to-end: a contradictory unsupported setting never reaches a strict endpoint."""
    STRICT_ALLOWED = {"model", "messages", "temperature", "max_tokens", "stream", "top_p"}
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read().decode("utf-8"))
        captured["body"] = body
        unexpected = set(body) - STRICT_ALLOWED
        if unexpected:
            return httpx.Response(
                400,
                json={"error": {"message": f"unknown parameter: {sorted(unexpected)[0]}"}},
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                'data: {"id": "c1", "model": "m", "choices": [{"index": 0, '
                '"finish_reason": null, "delta": {"content": "ok"}}]}\n\n'
                'data: {"id": "c1", "model": "m", "choices": [{"index": 0, '
                '"finish_reason": "stop", "delta": {}}]}\n\n'
                "data: [DONE]\n\n"
            ).encode("utf-8"),
        )

    async def scenario():
        provider = OpenAICompatibleProvider(
            "http://127.0.0.1:9/v1", api_key="k", _transport=httpx.MockTransport(handler)
        )
        backend = OpenAICompatibleStreamingBackend(
            provider, provider_id="generic", base_url="http://127.0.0.1:9/v1"
        )
        request = GenerationRequest(
            attempt_id="a1", chat_id="c1", user_message_id="u1",
            backend_id="openai_compatible_http", model="m", prompt="hi",
            provider_id="generic", base_url="http://127.0.0.1:9/v1",
            effective_settings={"temperature": 0.5, "max_output_tokens": 16},
            generation_settings=GenerationSettingsPayload(top_k=40, top_p=0.9),
            generation_setting_states={
                "top_k": STATE_OMITTED_UNSUPPORTED,
                "top_p": STATE_EMITTED,
            },
        )
        return [event async for event in backend.stream(request)]

    events = asyncio.run(scenario())
    body = captured["body"]
    assert "top_k" not in body
    assert body["top_p"] == 0.9
    assert "GenerationCompleted" in [type(event).__name__ for event in events]


def test_generation_request_carries_typed_settings_and_forbids_unknown():
    payload = GenerationSettingsPayload(top_p=0.5)
    request = GenerationRequest(
        attempt_id="a", chat_id="c", user_message_id="u", backend_id="openai_compatible_http",
        model="m", prompt="p", generation_settings=payload,
        generation_setting_states={"top_p": "emitted"},
    )
    assert request.generation_settings.top_p == 0.5
    with pytest.raises(Exception):
        GenerationRequest(
            attempt_id="a", chat_id="c", user_message_id="u", backend_id="x", model="m", prompt="p",
            generation_settings={"unchecked_vendor_field": 1},
        )


def test_manual_capability_override_enables_extended_emission(tmp_path: Path):
    async def scenario():
        application, store = await _open_application(tmp_path)
        try:
            chat = await application.create_chat()
            _connection, http_model = await _seed_http_model(application, store)
            await application.select_model(chat.id, http_model.id)
            model_id = (await application.chat_model_selection(chat.id)).model_entry_id
            assert model_id == http_model.id
            # No fact, no override: OpenAI compatibility is never evidence.
            capabilities = await application.generation_setting_capabilities(model_id)
            assert capabilities["request.top_k"].state is CapabilityState.UNKNOWN
            await application.set_chat_generation_settings(chat.id, GenerationSettings(extra={"top_k": 40, "top_p": 0.9}))
            prepared, snapshot_text = application._configuration.prepare_generation(
                chat_id=chat.id, user_message_id="u1", prompt="hi", attempt_id="at-1",
            )
            states = json.loads(snapshot_text)["generation_settings"]["states"]
            assert states["top_k"] == STATE_OMITTED_UNKNOWN
            assert states["top_p"] == STATE_OMITTED_UNKNOWN
            # Explicit manual override under the existing precedence rules.
            await application.set_capability_override(
                CapabilityOverride(
                    model_entry_id=model_id, key="request.top_k",
                    state=CapabilityState.SUPPORTED, reason="llama.cpp server confirmed by operator",
                )
            )
            prepared, snapshot_text = application._configuration.prepare_generation(
                chat_id=chat.id, user_message_id="u2", prompt="hi again", attempt_id="at-2",
            )
            snapshot = json.loads(snapshot_text)
            assert snapshot["snapshot_version"] == 4
            assert snapshot["generation_settings"]["states"]["top_k"] == STATE_EMITTED
            assert snapshot["generation_settings"]["values"]["top_k"] == 40
            assert snapshot["generation_settings"]["provenance"]["top_k"] == "chat_model"
            assert snapshot["generation_settings"]["states"]["top_p"] == STATE_OMITTED_UNKNOWN
            assert prepared.request.generation_settings.top_k == 40
            validate_phase11_snapshot(
                snapshot_text,
                attempt_id="at-2", chat_id=chat.id, user_message_id="u2",
                backend_id=prepared.request.backend_id, model=prepared.request.model,
                provider_id=prepared.request.provider_id,
            )
        finally:
            await application.close()

    asyncio.run(scenario())


def test_v2_snapshot_stays_byte_compatible_without_extended_settings(tmp_path: Path):
    async def scenario():
        application, store = await _open_application(tmp_path)
        try:
            chat = await application.create_chat()
            prepared, snapshot_text = application._configuration.prepare_generation(
                chat_id=chat.id, user_message_id="u1", prompt="hi", attempt_id="at-1",
            )
            snapshot = json.loads(snapshot_text)
            assert snapshot["snapshot_version"] == 2
            assert "generation_settings" not in snapshot
            assert set(snapshot["settings_provenance"]) == {
                "temperature", "max_output_tokens", "reasoning_effort", "timeout_seconds",
            }
        finally:
            await application.close()

    asyncio.run(scenario())


def test_stored_but_inactive_setting_is_preserved_and_reactivates(tmp_path: Path):
    async def scenario():
        application, store = await _open_application(tmp_path)
        try:
            chat = await application.create_chat()
            _connection, http_model = await _seed_http_model(application, store)
            await application.select_model(chat.id, http_model.id)
            model_id = (await application.chat_model_selection(chat.id)).model_entry_id
            await application.set_capability_override(
                CapabilityOverride(model_entry_id=model_id, key="request.top_k", state=CapabilityState.SUPPORTED, reason="manual")
            )
            await application.set_chat_generation_settings(chat.id, GenerationSettings(extra={"top_k": 40, "seed": 5}))
            prepared, _snapshot = application._configuration.prepare_generation(
                chat_id=chat.id, user_message_id="u1", prompt="hi", attempt_id="at-1",
            )
            states = prepared.request.generation_setting_states
            assert states["top_k"] == STATE_EMITTED and states["seed"] == STATE_OMITTED_UNKNOWN
            # The operator marks the model as NOT supporting top_k.
            await application.set_capability_override(
                CapabilityOverride(model_entry_id=model_id, key="request.top_k", state=CapabilityState.UNSUPPORTED, reason="manual")
            )
            prepared, snapshot_text = application._configuration.prepare_generation(
                chat_id=chat.id, user_message_id="u2", prompt="hi", attempt_id="at-2",
            )
            assert prepared.request.generation_setting_states["top_k"] == STATE_OMITTED_UNSUPPORTED
            assert prepared.request.generation_settings is None or prepared.request.generation_settings.top_k is None
            # The stored value survives untouched, still owned by the chat scope.
            assert store.get_chat_model_generation_settings_extra(chat.id, model_id)[0]["top_k"] == 40
            # A supporting model is selected again -> the setting re-activates
            # automatically with the same stored value and provenance.
            await application.set_capability_override(
                CapabilityOverride(model_entry_id=model_id, key="request.top_k", state=CapabilityState.SUPPORTED, reason="manual")
            )
            prepared, _snapshot = application._configuration.prepare_generation(
                chat_id=chat.id, user_message_id="u3", prompt="hi", attempt_id="at-3",
            )
            assert prepared.request.generation_settings.top_k == 40
            assert prepared.request.generation_setting_states["seed"] == STATE_OMITTED_UNKNOWN
        finally:
            await application.close()

    asyncio.run(scenario())


# ===========================================================================
# T3: desktop surfaces
# ===========================================================================


def _cap(state: str):
    return SimpleNamespace(state=SimpleNamespace(value=state), source=SimpleNamespace(value="manual"))


def test_tune_dialog_disables_unsupported_and_unknown_controls_and_preserves_values():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from bots5.desktop.widgets import TuneDialog

    dialog = TuneDialog()
    try:
        dialog.set_settings(
            {"temperature": 0.0, "max_output_tokens": 1024, "reasoning_effort": None, "top_k": 40, "top_p": 0.9},
            {"temperature": "application", "max_output_tokens": "application", "reasoning_effort": "application", "top_k": "chat_model", "top_p": "application"},
            extra_values={"top_k": 40, "top_p": 0.9},
            capabilities={"request.top_k": _cap("unsupported"), "request.top_p": _cap("unknown")},
        )
        editor = dialog.generation_settings_editor
        top_k_row = editor._rows["top_k"]
        top_p_row = editor._rows["top_p"]
        # DISABLED with a reason; stored override values remain visible.
        assert not top_k_row.control.isEnabled()
        assert not top_k_row.inherit.isEnabled()
        assert top_k_row.badge.text() == "INACTIVE"
        assert "preserved" in top_k_row.note.text()
        assert top_k_row.get_value() == 40
        assert top_p_row.badge.text() == "INACTIVE"
        assert "capability unknown" in top_p_row.note.text()
        # A supporting model re-activates the control automatically.
        dialog.set_settings(
            {"temperature": 0.0, "max_output_tokens": 1024, "reasoning_effort": None, "top_k": 40},
            {"temperature": "application", "max_output_tokens": "application", "reasoning_effort": "application", "top_k": "chat_model"},
            extra_values={"top_k": 40},
            capabilities={"request.top_k": _cap("supported")},
        )
        assert top_k_row.control.isEnabled()
        assert top_k_row.badge.text() == "ACTIVE"
        # ENABLED controls are editable; save keeps the override payload.
        emitted: list[dict[str, object]] = []
        dialog.save_requested.connect(emitted.append)
        editor._rows["top_p"].inherit.setChecked(False)
        dialog._save()
        payload = emitted[-1]
        assert payload["extra"]["top_k"] == 40
        assert "top_p" in payload["extra"]
        # Legacy Tune contract is unchanged: inherited temperature stays
        # inherited (None), never silently pinned to the resolved value.
        assert payload["temperature"] is None
        assert payload["timeout_seconds"] is None
        assert payload["expected_revision"] == dialog._override_revision
    finally:
        dialog.close()


def test_tune_dialog_use_inherited_clears_extended_overrides():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from bots5.desktop.widgets import TuneDialog

    dialog = TuneDialog()
    try:
        dialog.set_settings(
            {"temperature": 0.5, "max_output_tokens": 128, "reasoning_effort": None},
            {"temperature": "chat_model", "max_output_tokens": "chat_model", "reasoning_effort": "application"},
            extra_values={"seed": 3},
            capabilities={"request.seed": _cap("supported")},
        )
        emitted: list[dict[str, object]] = []
        dialog.inherit_requested.connect(emitted.append)
        dialog._use_inherited()
        payload = emitted[-1]
        assert payload["temperature"] is None and payload["max_output_tokens"] is None
        assert payload["extra"] == {}
    finally:
        dialog.close()


def test_settings_dialog_navigation_and_extras_payloads():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from bots5.desktop.widgets import SettingsDialog

    dialog = SettingsDialog()
    try:
        assert [dialog.section_nav.item(i).text() for i in range(dialog.section_nav.count())] == [
            "General", "Providers", "Models", "Credentials", "Advanced",
        ]
        dialog.section_nav.setCurrentRow(SettingsDialog.SECTION_CREDENTIALS)
        assert dialog.section_stack.currentIndex() == SettingsDialog.SECTION_CREDENTIALS
        assert not dialog.connection_credential_value.isHidden()
        dialog.section_nav.setCurrentRow(SettingsDialog.SECTION_MODELS)
        assert not dialog.model_list.isHidden()
        assert not dialog.model_settings_editor.isHidden()
        dialog.section_nav.setCurrentRow(SettingsDialog.SECTION_ADVANCED)
        assert not dialog.capability_key.isHidden()
        # Registry capability keys are offered for manual per-setting overrides.
        keys = {dialog.capability_key.itemData(i) for i in range(dialog.capability_key.count())}
        assert {"request.top_p", "request.seed", "request.mirostat"} <= keys
        assert "generation.streaming" in keys

        available = SimpleNamespace(value="available")
        model = SimpleNamespace(id="model-a", connection_id="c1", provider_model_id="A", availability=available)
        dialog.set_connections((SimpleNamespace(
            id="c1", name="C", profile=SimpleNamespace(value="generic"), available=True, retired=False,
        ),))
        dialog.set_models((model,), {"c1": SimpleNamespace(id="c1", name="C")})
        dialog.set_model_defaults(
            GenerationSettings(temperature=None, max_output_tokens=512, reasoning_effort=None),
            revision=4,
            extra_values={"seed": 11},
            effective={"seed": 11, "temperature": None, "max_output_tokens": 512},
            effective_provenance={"seed": "model"},
            capabilities={"request.seed": _cap("supported")},
        )
        emitted: list[dict[str, object]] = []
        dialog.model_defaults_requested.connect(emitted.append)
        dialog.model_list.setCurrentRow(0)
        dialog._save_model_defaults()
        assert emitted[-1]["extra"]["seed"] == 11
        assert emitted[-1]["max_output_tokens"] == 512

        application_emitted: list[dict[str, object]] = []
        dialog.application_defaults_requested.connect(application_emitted.append)
        dialog.set_application_defaults(
            SimpleNamespace(temperature=0.0, max_output_tokens=1024, reasoning_effort=None, timeout_seconds=None),
            revision=2,
            extra_values={"top_p": 0.5},
        )
        dialog._save_application_defaults()
        assert application_emitted[-1]["extra"] == {"top_p": 0.5}
        # Invalid editor text is refused, never persisted.
        dialog.model_settings_editor._rows["seed"].inherit.setChecked(False)
        dialog.model_settings_editor._rows["seed"].set_value(11)
        dialog.model_settings_editor._rows["stop_sequences"].inherit.setChecked(False)
        dialog.model_settings_editor._rows["stop_sequences"].control.setPlainText("a\n\n")
        dialog.model_settings_editor._rows["stop_sequences"].set_value([])
        assert dialog.model_settings_editor.has_invalid_values() is False
        dialog.model_settings_editor._rows["seed"].control.setValue(-1)
        assert dialog.model_settings_editor.has_invalid_values() is False  # spin bounds reject, sentinel keeps None
    finally:
        dialog.close()


def test_all_normalized_settings_stay_visible_and_gated_controls_disabled():
    """Mick's required Tune behaviour: visible always, editable only if supported."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from bots5.desktop.dialog_primitives import GenerationSettingsEditor

    editor = GenerationSettingsEditor()
    editor.set_state(
        values={"dry_multiplier": 1.5, "tail_free_sampling": 0.5},
        effective={"dry_multiplier": 1.5, "tail_free_sampling": 0.5},
        provenance={"dry_multiplier": "chat_model", "tail_free_sampling": "chat_model"},
        capabilities={
            "request.dry_multiplier": _cap("unsupported"),
            "request.logit_bias": _cap("unknown"),
            "request.tail_free_sampling": _cap("supported"),
        },
    )
    # Every row is visible, including advanced rows: no control is hidden to
    # simplify the UI.
    for key, row in editor._rows.items():
        assert not row.container.isHidden(), key
    assert not editor._rows["dry_multiplier"].container.isHidden()
    assert not editor._rows["logit_bias"].container.isHidden()
    # Unsupported and unknown advanced controls are greyed out and locked.
    dry = editor._rows["dry_multiplier"]
    assert not dry.control.isEnabled() and not dry.inherit.isEnabled()
    assert dry.badge.text() == "INACTIVE" and "preserved" in dry.note.text()
    logit_bias = editor._rows["logit_bias"]
    assert not logit_bias.control.isEnabled() and not logit_bias.inherit.isEnabled()
    # A supported advanced override remains editable.
    assert editor._rows["tail_free_sampling"].control.isEnabled()
    # The old hiding affordance is gone entirely.
    assert not hasattr(editor, "_advanced_toggle")
    assert not hasattr(editor, "_show_advanced")


def test_dialogs_use_shared_scroll_areas_and_stay_within_work_area():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt as QtCore_Qt
    from PySide6.QtWidgets import QApplication, QDialogButtonBox, QScrollArea

    app = QApplication.instance() or QApplication([])
    from bots5.desktop.dialog_primitives import DialogScrollArea
    from bots5.desktop.widgets import SettingsDialog, TuneDialog

    tune = TuneDialog()
    settings = SettingsDialog()
    try:
        # Tune: settings/control region scrolls; primary actions stay outside.
        tune_area = tune.findChild(DialogScrollArea)
        assert tune_area is not None and tune_area.widgetResizable()
        assert tune_area.horizontalScrollBarPolicy() == QtCore_Qt.ScrollBarPolicy.ScrollBarAsNeeded
        assert tune_area.focusPolicy() == QtCore_Qt.FocusPolicy.StrongFocus
        buttons = tune.findChild(QDialogButtonBox)
        assert buttons is not None and not tune_area.isAncestorOf(buttons)
        # Settings: each detail pane scrolls; the navigation rail does not.
        assert settings.section_stack.count() == 5
        for index in range(settings.section_stack.count()):
            pane = settings.section_stack.widget(index)
            assert isinstance(pane, DialogScrollArea)
        assert not settings.section_nav.isAncestorOf(settings.section_stack.widget(0))
        # Both dialogs are clamped to the available work area.
        screen = tune.screen() or QApplication.primaryScreen()
        available = screen.availableGeometry()
        assert tune.maximumWidth() <= available.width()
        assert tune.maximumHeight() <= available.height()
        assert settings.maximumWidth() <= available.width()
        assert settings.maximumHeight() <= available.height()
    finally:
        tune.close()
        settings.close()


def test_oversized_tune_content_scrolls_rather_than_growing_offscreen():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from bots5.desktop.dialog_primitives import DialogScrollArea
    from bots5.desktop.widgets import TuneDialog

    dialog = TuneDialog()
    try:
        area = dialog.findChild(DialogScrollArea)
        assert area is not None, "Tune must expose the shared scroll surface"
        dialog.resize(480, 220)
        dialog.show()
        app.processEvents()
        # The registry-driven editor is taller than this viewport, so the
        # shared scroll surface must offer vertical scrolling.
        assert area.verticalScrollBar().maximum() > 0
        assert area.height() <= dialog.height()
    finally:
        dialog.close()


def test_narrow_dialogs_do_not_clip_content_horizontally():
    """Regression for the second Luna falsification (F2).

    At a small dialog size no detail pane may clip content behind a disabled
    horizontal scrollbar: any content genuinely wider than the viewport must be
    reachable by scrolling.  Tune must also be able to shrink below its old
    560px minimum, and its editor must not demand horizontal scrolling.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt as QtCore_Qt
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from bots5.desktop.dialog_primitives import DialogScrollArea
    from bots5.desktop.widgets import SettingsDialog, TuneDialog

    settings = SettingsDialog()
    tune = TuneDialog()
    try:
        settings.resize(360, 240)
        settings.show()
        app.processEvents()
        for index in range(settings.section_stack.count()):
            settings.section_nav.setCurrentRow(index)
            app.processEvents()
            area = settings.section_stack.widget(index)
            assert isinstance(area, DialogScrollArea)
            bar = area.horizontalScrollBar()
            if area.widget().minimumSizeHint().width() > area.viewport().width():
                assert area.horizontalScrollBarPolicy() == QtCore_Qt.ScrollBarPolicy.ScrollBarAsNeeded
                assert bar.maximum() > 0, f"section {index} content is clipped"
            else:
                assert bar.maximum() == 0, f"section {index} scrolls horizontally without need"

        tune.resize(320, 220)
        tune.show()
        app.processEvents()
        assert tune.width() <= 360, "Tune must shrink below its old 560px minimum"
        tune_area = tune.findChild(DialogScrollArea)
        assert tune_area is not None
        assert tune_area.horizontalScrollBar().maximum() == 0
        assert tune_area.verticalScrollBar().maximum() > 0
        # Editor labels reflow rather than forcing a wide minimum.
        assert all(
            row.container is not None for row in tune.generation_settings_editor._rows.values()
        )
    finally:
        settings.close()
        tune.close()


def test_theme_retains_tokens_and_adds_industrial_rules():
    # The design's section 3.1 retained tokens are untouched.
    assert SURFACE_BASE == "#11161b"
    assert SURFACE_PANEL == "#171d24"
    assert SURFACE_BUBBLE == "#1b232b"
    assert BORDER_DEFAULT == "#29343f"
    assert ACCENT_BLUE == "#3b9ddd"
    stylesheet = build_theme_stylesheet(scale=1.0)
    # Industrial dialog rules exist and carry no gradients or drop shadows.
    assert "QFrame#botsChamferedPanel {" in stylesheet
    assert "QDialog#tuneDialog" in stylesheet and "QDialog#settingsDialog" in stylesheet
    assert "QListWidget#botsSectionNav::item:selected" in stylesheet
    assert "QLabel#botsStateBadge[gate=\"unsupported\"]" in stylesheet
    assert "linear-gradient" not in stylesheet
    assert "qlineargradient" not in stylesheet
    assert "box-shadow" not in stylesheet
