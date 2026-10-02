"""Phase 11 scope amendment: the normalized generation-settings registry.

This module is the single typed authority describing every generation setting
B.O.T.S. can represent.  It replaces the closed three-field
``GenerationSettings`` contract with a capability-driven plane that future
versions can extend by ADDING definitions — no subsystem redesign required.

Design rules enforced here (Phase 11 scope amendment §C/§D):

- Every setting has a stable key, display label, semantic description, value
  type, legal range/choices, default/inherit behaviour, capability key,
  serialization mapping for the supported provider families, omission
  semantics, provenance vocabulary, validation and UI presentation metadata.
- There is NO free-form JSON escape hatch.  A value for an unknown key is
  rejected; unknown vendor parameters stay unsupported until they are
  deliberately represented in this registry.
- Normalized keys are only defined where a truthful, stable semantic mapping
  exists for at least one supported provider family.  Two families exposing
  similarly named controls with materially different semantics are NEVER
  cross-mapped (``repetition_penalty`` is multiplicative logit scaling,
  ``frequency_penalty``/``presence_penalty`` are additive token-count
  penalties — they remain distinct settings with distinct payload keys).
- ``min_output_tokens`` and sampler-ordering controls are deliberately ABSENT:
  the families B.O.T.S. speaks (OpenAI-compatible chat and llama.cpp-style
  OpenAI-compatible local servers) expose no stable, truthful semantic for
  them, and inventing one would lie about the payload.

Capability keys introduced here live OUTSIDE the frozen Phase 5
``CAPABILITY_KEYS`` set so that the closed Phase 5 request-snapshot evidence
schema is untouched.  Support for an extended setting is established only by
the existing capability machinery (manual override today; confirmed endpoint
or provider metadata when a discoverer records such a fact) — never inferred
merely because an endpoint is "OpenAI-compatible".
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, model_validator


class SettingValueType(StrEnum):
    FLOAT = "float"
    INT = "int"
    STR = "str"
    BOOL = "bool"
    STR_LIST = "str_list"
    TOKEN_BIAS = "token_bias"


class SettingGroup(StrEnum):
    SAMPLING = "sampling"
    REPETITION = "repetition"
    OUTPUT = "output"
    DETERMINISM = "determinism"
    REASONING = "reasoning"
    LOGPROBS = "logprobs"
    ADVANCED_SAMPLING = "advanced_sampling"
    RUNTIME = "runtime"


class PayloadFamily(StrEnum):
    OPENAI_COMPATIBLE = "openai_compatible"


#: UI control kinds for the generated Tune/Settings editors.
UI_KIND_SPIN = "spin"
UI_KIND_CHOICES = "choices"
UI_KIND_CHECK = "check"
UI_KIND_TEXT = "text"


@dataclass(frozen=True, slots=True)
class PayloadMapping:
    """One truthful translation of a normalized setting into a provider payload."""

    family: PayloadFamily
    payload_key: str
    #: Optional provider-profile restriction.  ``None`` means every profile of
    #: the family may carry the key; a profile value restricts emission to
    #: connections with exactly that profile.
    profile: str | None = None
    #: Human-readable dialect note recording why this mapping is truthful and
    #: how it differs from similarly named controls in other families.
    note: str = ""


@dataclass(frozen=True, slots=True)
class SettingDefinition:
    """Typed definition of one normalized generation setting."""

    key: str
    label: str
    description: str
    group: SettingGroup
    value_type: SettingValueType
    capability_key: str | None
    #: ``None`` means the setting is unset unless explicitly configured.
    default: Any = None
    #: Whether OMITTING the setting from a provider request differs
    #: semantically from sending an explicit value.
    omission_differs: bool = False
    serialization: tuple[PayloadMapping, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] | None = None
    #: Legacy settings ride the frozen pre-amendment persistence columns and
    #: keep their exact pre-amendment emission contract.
    legacy: bool = False
    #: UI metadata.
    ui_kind: str = UI_KIND_SPIN
    ui_step: float = 1.0
    ui_decimals: int = 2
    ui_placeholder: str = ""
    ui_advanced: bool = False
    #: True when the setting is a B.O.T.S.-owned control that is never
    #: serialized into a provider payload.
    bots_owned: bool = False

    def validate(self, value: Any) -> None:
        """Validate one raw value; raise ``ValueError`` when it is illegal."""
        if value is None:
            return
        if self.value_type is SettingValueType.FLOAT:
            if type(value) not in {int, float} or isinstance(value, bool):
                raise ValueError(f"{self.key} must be a number")
            number = float(value)
            if not math.isfinite(number):
                raise ValueError(f"{self.key} must be finite")
            if self.minimum is not None and number < self.minimum:
                raise ValueError(f"{self.key} must be >= {self.minimum}")
            if self.maximum is not None and number > self.maximum:
                raise ValueError(f"{self.key} must be <= {self.maximum}")
            return
        if self.value_type is SettingValueType.INT:
            if type(value) is not int or isinstance(value, bool):
                raise ValueError(f"{self.key} must be an integer")
            if self.minimum is not None and value < self.minimum:
                raise ValueError(f"{self.key} must be >= {self.minimum}")
            if self.maximum is not None and value > self.maximum:
                raise ValueError(f"{self.key} must be <= {self.maximum}")
            return
        if self.value_type is SettingValueType.STR:
            if type(value) is not str or not value:
                raise ValueError(f"{self.key} must be a non-empty string")
            if self.choices is not None and value not in self.choices:
                raise ValueError(f"{self.key} must be one of {', '.join(self.choices)}")
            return
        if self.value_type is SettingValueType.BOOL:
            if type(value) is not bool:
                raise ValueError(f"{self.key} must be a boolean")
            return
        if self.value_type is SettingValueType.STR_LIST:
            if type(value) not in {list, tuple} or not all(type(item) is str for item in value):
                raise ValueError(f"{self.key} must be a list of strings")
            if any(not item for item in value):
                raise ValueError(f"{self.key} must not contain empty sequences")
            if len(value) > 16:
                raise ValueError(f"{self.key} must hold at most 16 sequences")
            if any(len(item) > 256 for item in value):
                raise ValueError(f"{self.key} entries must hold at most 256 characters")
            return
        if self.value_type is SettingValueType.TOKEN_BIAS:
            if type(value) is not dict:
                raise ValueError(f"{self.key} must map token text to a bias")
            if not value:
                raise ValueError(f"{self.key} must not be empty when set")
            if len(value) > 64:
                raise ValueError(f"{self.key} must hold at most 64 tokens")
            for token, bias in value.items():
                if type(token) is not str or not token or len(token) > 64:
                    raise ValueError(f"{self.key} tokens must be 1-64 characters")
                if type(bias) is not int or isinstance(bias, bool) or not -100 <= bias <= 100:
                    raise ValueError(f"{self.key} biases must be integers in [-100, 100]")
            return
        raise ValueError(f"{self.key} has an unsupported value type")

    def normalize(self, value: Any) -> Any:
        """Return the canonical typed form of a validated value."""
        if value is None:
            return None
        if self.value_type is SettingValueType.FLOAT:
            return float(value)
        if self.value_type is SettingValueType.INT:
            return int(value)
        if self.value_type is SettingValueType.STR_LIST:
            return tuple(value)
        if self.value_type is SettingValueType.TOKEN_BIAS:
            return dict(value)
        return value

    def serialization_for(self, family: PayloadFamily, profile: str | None) -> PayloadMapping | None:
        """Return the payload mapping for a family/profile, when truthful."""
        for mapping in self.serialization:
            if mapping.family is not family:
                continue
            if mapping.profile is not None and mapping.profile != profile:
                continue
            return mapping
        return None


_OPENAI = PayloadFamily.OPENAI_COMPATIBLE


def _openai(payload_key: str, *, profile: str | None = None, note: str = "") -> tuple[PayloadMapping, ...]:
    return (PayloadMapping(_OPENAI, payload_key, profile=profile, note=note),)


LLAMACPP_NOTE = (
    "llama.cpp-style OpenAI-compatible local servers accept this extended "
    "sampling field; support must be established per endpoint/model, it is "
    "never inferred from OpenAI compatibility"
)

SETTING_DEFINITIONS: tuple[SettingDefinition, ...] = (
    # --- Legacy plane (frozen pre-amendment contract) --------------------
    SettingDefinition(
        key="temperature",
        label="Temperature",
        description="Sampling temperature. Omitting it lets the provider apply its own default.",
        group=SettingGroup.SAMPLING,
        value_type=SettingValueType.FLOAT,
        capability_key="request.temperature",
        default=0.0,
        omission_differs=True,
        minimum=0.0,
        maximum=2.0,
        ui_step=0.1,
        ui_decimals=2,
        legacy=True,
    ),
    SettingDefinition(
        key="max_output_tokens",
        label="Max output tokens",
        description="Upper bound on generated tokens. Omitting it leaves the provider unbounded.",
        group=SettingGroup.OUTPUT,
        value_type=SettingValueType.INT,
        capability_key="request.max_output_tokens",
        default=1024,
        omission_differs=True,
        minimum=1,
        maximum=2_000_000_000,
        ui_step=1,
        ui_decimals=0,
        legacy=True,
    ),
    SettingDefinition(
        key="reasoning_effort",
        label="Reasoning (disable)",
        description=(
            "Explicitly disables extended reasoning by sending the legacy "
            "'none' effort marker. Mutually exclusive with a reasoning level."
        ),
        group=SettingGroup.REASONING,
        value_type=SettingValueType.STR,
        capability_key="request.reasoning_effort.none",
        choices=("none",),
        omission_differs=True,
        serialization=_openai("reasoning_effort"),
        ui_kind=UI_KIND_CHECK,
        legacy=True,
    ),
    SettingDefinition(
        key="timeout_seconds",
        label="Request timeout",
        description=(
            "B.O.T.S.-owned attempt deadline. Never serialized into a provider "
            "payload; omission means the request has no configured deadline."
        ),
        group=SettingGroup.RUNTIME,
        value_type=SettingValueType.FLOAT,
        capability_key=None,
        omission_differs=True,
        minimum=0.001,
        maximum=86_400.0,
        ui_step=1.0,
        ui_decimals=2,
        bots_owned=True,
        legacy=True,
    ),
    # --- Sampling ---------------------------------------------------------
    SettingDefinition(
        key="top_p",
        label="Top P",
        description="Nucleus sampling probability mass. OpenAI-compatible chat endpoints.",
        group=SettingGroup.SAMPLING,
        value_type=SettingValueType.FLOAT,
        capability_key="request.top_p",
        omission_differs=True,
        minimum=0.0,
        maximum=1.0,
        ui_step=0.01,
        serialization=_openai("top_p"),
    ),
    SettingDefinition(
        key="top_k",
        label="Top K",
        description="Restricts sampling to the K most likely tokens.",
        group=SettingGroup.SAMPLING,
        value_type=SettingValueType.INT,
        capability_key="request.top_k",
        omission_differs=True,
        minimum=1,
        maximum=200,
        ui_step=1,
        ui_decimals=0,
        serialization=_openai("top_k", note=LLAMACPP_NOTE),
    ),
    SettingDefinition(
        key="min_p",
        label="Min P",
        description="Keeps tokens whose probability is at least this fraction of the top token.",
        group=SettingGroup.SAMPLING,
        value_type=SettingValueType.FLOAT,
        capability_key="request.min_p",
        omission_differs=True,
        minimum=0.0,
        maximum=1.0,
        ui_step=0.01,
        serialization=_openai("min_p", note=LLAMACPP_NOTE),
    ),
    SettingDefinition(
        key="typical_p",
        label="Typical P",
        description="Locally typical sampling mass (1.0 disables the restriction).",
        group=SettingGroup.SAMPLING,
        value_type=SettingValueType.FLOAT,
        capability_key="request.typical_p",
        omission_differs=True,
        minimum=0.0,
        maximum=1.0,
        ui_step=0.01,
        serialization=_openai("typical_p", note=LLAMACPP_NOTE),
    ),
    SettingDefinition(
        key="tail_free_sampling",
        label="Tail free sampling (z)",
        description="Tail-free sampling z; 1.0 disables it.",
        group=SettingGroup.SAMPLING,
        value_type=SettingValueType.FLOAT,
        capability_key="request.tail_free_sampling",
        omission_differs=True,
        minimum=0.0,
        maximum=1.0,
        ui_step=0.01,
        ui_advanced=True,
        serialization=_openai("tfs_z", note=LLAMACPP_NOTE),
    ),
    SettingDefinition(
        key="smoothing_factor",
        label="Smoothing factor",
        description="Smoothing factor applied to the sampling distribution (0 disables).",
        group=SettingGroup.SAMPLING,
        value_type=SettingValueType.FLOAT,
        capability_key="request.smoothing_factor",
        omission_differs=True,
        minimum=0.0,
        maximum=10.0,
        ui_step=0.05,
        ui_advanced=True,
        serialization=_openai("smoothing_factor", note=LLAMACPP_NOTE),
    ),
    # --- Repetition / token penalties ------------------------------------
    SettingDefinition(
        key="frequency_penalty",
        label="Frequency penalty",
        description="Additive penalty proportional to how often a token already appeared.",
        group=SettingGroup.REPETITION,
        value_type=SettingValueType.FLOAT,
        capability_key="request.frequency_penalty",
        omission_differs=True,
        minimum=-2.0,
        maximum=2.0,
        ui_step=0.1,
        serialization=_openai("frequency_penalty"),
    ),
    SettingDefinition(
        key="presence_penalty",
        label="Presence penalty",
        description="Additive penalty applied once a token has appeared at all.",
        group=SettingGroup.REPETITION,
        value_type=SettingValueType.FLOAT,
        capability_key="request.presence_penalty",
        omission_differs=True,
        minimum=-2.0,
        maximum=2.0,
        ui_step=0.1,
        serialization=_openai("presence_penalty"),
    ),
    SettingDefinition(
        key="repetition_penalty",
        label="Repetition penalty",
        description=(
            "Multiplicative logit penalty for repeated tokens. NOT equivalent to "
            "the additive frequency/presence penalties."
        ),
        group=SettingGroup.REPETITION,
        value_type=SettingValueType.FLOAT,
        capability_key="request.repetition_penalty",
        omission_differs=True,
        minimum=0.5,
        maximum=2.0,
        ui_step=0.05,
        serialization=_openai("repeat_penalty", note=LLAMACPP_NOTE),
    ),
    SettingDefinition(
        key="repetition_window",
        label="Repetition window",
        description=(
            "Number of recent tokens the repetition penalty considers; 0 disables, "
            "-1 means the whole context."
        ),
        group=SettingGroup.REPETITION,
        value_type=SettingValueType.INT,
        capability_key="request.repetition_window",
        omission_differs=True,
        minimum=-1,
        maximum=2_000_000_000,
        ui_step=1,
        ui_decimals=0,
        ui_advanced=True,
        serialization=_openai("repeat_last_n", note=LLAMACPP_NOTE),
    ),
    # --- Output / stopping ------------------------------------------------
    SettingDefinition(
        key="stop_sequences",
        label="Stop sequences",
        description="Up to 16 literal sequences that end generation when produced.",
        group=SettingGroup.OUTPUT,
        value_type=SettingValueType.STR_LIST,
        capability_key="request.stop_sequences",
        omission_differs=True,
        ui_kind=UI_KIND_TEXT,
        ui_placeholder="one sequence per line",
        serialization=_openai("stop"),
    ),
    SettingDefinition(
        key="ignore_eos",
        label="Ignore EOS",
        description="Continue generation past an end-of-sequence token (local servers only).",
        group=SettingGroup.OUTPUT,
        value_type=SettingValueType.BOOL,
        capability_key="request.ignore_eos",
        omission_differs=True,
        ui_kind=UI_KIND_CHECK,
        ui_advanced=True,
        serialization=_openai("ignore_eos", note=LLAMACPP_NOTE),
    ),
    # --- Determinism --------------------------------------------------------
    SettingDefinition(
        key="seed",
        label="Seed",
        description="Deterministic sampling seed where the endpoint honours one.",
        group=SettingGroup.DETERMINISM,
        value_type=SettingValueType.INT,
        capability_key="request.seed",
        omission_differs=True,
        minimum=0,
        maximum=2_147_483_647,
        ui_step=1,
        ui_decimals=0,
        serialization=_openai("seed"),
    ),
    # --- Reasoning ----------------------------------------------------------
    SettingDefinition(
        key="reasoning_effort_level",
        label="Reasoning effort",
        description=(
            "Extended-reasoning effort level for models that expose one. Mutually "
            "exclusive with the legacy reasoning disable marker."
        ),
        group=SettingGroup.REASONING,
        value_type=SettingValueType.STR,
        capability_key="request.reasoning_effort.level",
        choices=("low", "medium", "high"),
        omission_differs=True,
        serialization=_openai("reasoning_effort"),
        ui_kind=UI_KIND_CHOICES,
    ),
    SettingDefinition(
        key="reasoning_token_budget",
        label="Reasoning token budget",
        description=(
            "Maximum tokens spent on reasoning. Routed as the OpenRouter "
            "reasoning.max_tokens control; it has no truthful generic-OpenAI "
            "mapping and is omitted elsewhere."
        ),
        group=SettingGroup.REASONING,
        value_type=SettingValueType.INT,
        capability_key="request.reasoning.token_budget",
        omission_differs=True,
        minimum=1,
        maximum=2_000_000_000,
        ui_step=1,
        ui_decimals=0,
        serialization=_openai("reasoning.max_tokens", profile="openrouter"),
        ui_kind=UI_KIND_TEXT,
        ui_placeholder="tokens",
    ),
    # --- Logprobs -----------------------------------------------------------
    SettingDefinition(
        key="logprobs",
        label="Return logprobs",
        description="Ask the endpoint to return token logprobs with the response.",
        group=SettingGroup.LOGPROBS,
        value_type=SettingValueType.BOOL,
        capability_key="request.logprobs",
        omission_differs=True,
        ui_kind=UI_KIND_CHECK,
        ui_advanced=True,
        serialization=_openai("logprobs"),
    ),
    SettingDefinition(
        key="top_logprobs",
        label="Top logprobs",
        description="Number of most-likely tokens to report logprobs for (0-20).",
        group=SettingGroup.LOGPROBS,
        value_type=SettingValueType.INT,
        capability_key="request.top_logprobs",
        omission_differs=True,
        minimum=0,
        maximum=20,
        ui_step=1,
        ui_decimals=0,
        ui_advanced=True,
        serialization=_openai("top_logprobs"),
    ),
    SettingDefinition(
        key="logit_bias",
        label="Logit bias",
        description=(
            "Token-text to bias map (integer -100..100) applied before sampling. "
            "Token text is normalized by the endpoint tokenizer."
        ),
        group=SettingGroup.LOGPROBS,
        value_type=SettingValueType.TOKEN_BIAS,
        capability_key="request.logit_bias",
        omission_differs=True,
        ui_kind=UI_KIND_TEXT,
        ui_placeholder='" token"=offset per line, bias -100..100',
        ui_advanced=True,
        serialization=_openai("logit_bias"),
    ),
    # --- Advanced sampling (local/model endpoints) --------------------------
    SettingDefinition(
        key="mirostat",
        label="Mirostat mode",
        description="Mirostat sampling mode: 0 off, 1 mirostat v1, 2 mirostat v2.",
        group=SettingGroup.ADVANCED_SAMPLING,
        value_type=SettingValueType.INT,
        capability_key="request.mirostat",
        omission_differs=True,
        minimum=0,
        maximum=2,
        ui_step=1,
        ui_decimals=0,
        ui_advanced=True,
        serialization=_openai("mirostat", note=LLAMACPP_NOTE),
    ),
    SettingDefinition(
        key="mirostat_tau",
        label="Mirostat tau",
        description="Mirostat entropy target tau.",
        group=SettingGroup.ADVANCED_SAMPLING,
        value_type=SettingValueType.FLOAT,
        capability_key="request.mirostat_tau",
        omission_differs=True,
        minimum=0.0,
        maximum=10.0,
        ui_step=0.1,
        ui_advanced=True,
        serialization=_openai("mirostat_tau", note=LLAMACPP_NOTE),
    ),
    SettingDefinition(
        key="mirostat_eta",
        label="Mirostat eta",
        description="Mirostat learning rate eta.",
        group=SettingGroup.ADVANCED_SAMPLING,
        value_type=SettingValueType.FLOAT,
        capability_key="request.mirostat_eta",
        omission_differs=True,
        minimum=0.0,
        maximum=1.0,
        ui_step=0.01,
        ui_advanced=True,
        serialization=_openai("mirostat_eta", note=LLAMACPP_NOTE),
    ),
    SettingDefinition(
        key="dynamic_temperature_range",
        label="Dynamic temperature range",
        description="Dynamic-temperature adjustment range (0 disables).",
        group=SettingGroup.ADVANCED_SAMPLING,
        value_type=SettingValueType.FLOAT,
        capability_key="request.dynamic_temperature_range",
        omission_differs=True,
        minimum=0.0,
        maximum=10.0,
        ui_step=0.1,
        ui_advanced=True,
        serialization=_openai("dynatemp_range", note=LLAMACPP_NOTE),
    ),
    SettingDefinition(
        key="xtc_probability",
        label="XTC probability",
        description="XTC sampler: probability of excluding top tokens each step.",
        group=SettingGroup.ADVANCED_SAMPLING,
        value_type=SettingValueType.FLOAT,
        capability_key="request.xtc_probability",
        omission_differs=True,
        minimum=0.0,
        maximum=1.0,
        ui_step=0.01,
        ui_advanced=True,
        serialization=_openai("xtc_probability", note=LLAMACPP_NOTE),
    ),
    SettingDefinition(
        key="xtc_threshold",
        label="XTC threshold",
        description="XTC sampler: minimum probability under which top tokens are excluded.",
        group=SettingGroup.ADVANCED_SAMPLING,
        value_type=SettingValueType.FLOAT,
        capability_key="request.xtc_threshold",
        omission_differs=True,
        minimum=0.0,
        maximum=1.0,
        ui_step=0.01,
        ui_advanced=True,
        serialization=_openai("xtc_threshold", note=LLAMACPP_NOTE),
    ),
    SettingDefinition(
        key="dry_multiplier",
        label="DRY multiplier",
        description="DRY repetition sampler strength (0 disables DRY).",
        group=SettingGroup.ADVANCED_SAMPLING,
        value_type=SettingValueType.FLOAT,
        capability_key="request.dry_multiplier",
        omission_differs=True,
        minimum=0.0,
        maximum=10.0,
        ui_step=0.05,
        ui_advanced=True,
        serialization=_openai("dry_multiplier", note=LLAMACPP_NOTE),
    ),
    SettingDefinition(
        key="dry_base",
        label="DRY base",
        description="DRY repetition sampler exponential base.",
        group=SettingGroup.ADVANCED_SAMPLING,
        value_type=SettingValueType.FLOAT,
        capability_key="request.dry_base",
        omission_differs=True,
        minimum=1.0,
        maximum=10.0,
        ui_step=0.1,
        ui_advanced=True,
        serialization=_openai("dry_base", note=LLAMACPP_NOTE),
    ),
    SettingDefinition(
        key="dry_allowed_length",
        label="DRY allowed length",
        description="DRY sampler: longest repeat that remains unpunished.",
        group=SettingGroup.ADVANCED_SAMPLING,
        value_type=SettingValueType.INT,
        capability_key="request.dry_allowed_length",
        omission_differs=True,
        minimum=0,
        maximum=4_096,
        ui_step=1,
        ui_decimals=0,
        ui_advanced=True,
        serialization=_openai("dry_allowed_length", note=LLAMACPP_NOTE),
    ),
    SettingDefinition(
        key="dry_penalty_last_n",
        label="DRY window",
        description="DRY sampler sequence window; -1 means the whole context.",
        group=SettingGroup.ADVANCED_SAMPLING,
        value_type=SettingValueType.INT,
        capability_key="request.dry_penalty_last_n",
        omission_differs=True,
        minimum=-1,
        maximum=2_000_000_000,
        ui_step=1,
        ui_decimals=0,
        ui_advanced=True,
        serialization=_openai("dry_penalty_last_n", note=LLAMACPP_NOTE),
    ),
)

SETTING_DEFINITIONS_BY_KEY: dict[str, SettingDefinition] = {
    definition.key: definition for definition in SETTING_DEFINITIONS
}
SETTING_KEYS: frozenset[str] = frozenset(SETTING_DEFINITIONS_BY_KEY)
LEGACY_SETTING_KEYS: frozenset[str] = frozenset(
    definition.key for definition in SETTING_DEFINITIONS if definition.legacy
)
EXTENDED_SETTING_KEYS: frozenset[str] = SETTING_KEYS - LEGACY_SETTING_KEYS
SETTING_CAPABILITY_KEYS: frozenset[str] = frozenset(
    definition.capability_key
    for definition in SETTING_DEFINITIONS
    if definition.capability_key is not None
)
#: capability key -> setting key, for reconciling request capability evidence
#: with the normalized generation-settings plane at the provider boundary.
SETTING_KEY_BY_CAPABILITY_KEY: dict[str, str] = {
    definition.capability_key: definition.key
    for definition in SETTING_DEFINITIONS
    if definition.capability_key is not None
}
#: Capability keys whose support may gate a Tune control.  Extended keys are
#: additive to the frozen Phase 5 capability catalogue and never appear in a
#: Phase 5 snapshot's capability fact list.
GATEABLE_CAPABILITY_KEYS: frozenset[str] = SETTING_CAPABILITY_KEYS

# Emission order used when building provider payloads: deterministic and
# grouped exactly like the catalogue above.
EMISSION_ORDER: tuple[str, ...] = tuple(
    definition.key for definition in SETTING_DEFINITIONS
)

#: Reason vocabulary recorded for per-setting emission decisions.
STATE_EMITTED = "emitted"
STATE_UNSET = "unset"
STATE_OMITTED_UNSUPPORTED = "omitted:unsupported"
STATE_OMITTED_UNKNOWN = "omitted:unknown_capability"
STATE_OMITTED_UNSERIALIZABLE = "omitted:unserializable"
STATE_OMITTED_INVALID = "omitted:invalid"
SETTING_STATES: frozenset[str] = frozenset({
    STATE_EMITTED,
    STATE_UNSET,
    STATE_OMITTED_UNSUPPORTED,
    STATE_OMITTED_UNKNOWN,
    STATE_OMITTED_UNSERIALIZABLE,
    STATE_OMITTED_INVALID,
})


def setting_definition(key: str) -> SettingDefinition:
    definition = SETTING_DEFINITIONS_BY_KEY.get(key)
    if definition is None:
        raise ValueError(f"unknown generation setting: {key}")
    return definition


def validate_setting_value(key: str, value: Any) -> None:
    setting_definition(key).validate(value)


def validate_settings_values(values: Any) -> dict[str, Any]:
    """Validate a mapping of setting values; return the canonical mapping.

    Unknown keys are rejected (fail closed): there is no free-form escape
    hatch.  ``None`` entries mean "inherit" and are dropped.
    """
    if not isinstance(values, dict):
        raise ValueError("generation settings must be a mapping")
    result: dict[str, Any] = {}
    for key, value in values.items():
        if value is None:
            continue
        if key not in SETTING_KEYS:
            raise ValueError(f"unknown generation setting: {key}")
        definition = SETTING_DEFINITIONS_BY_KEY[key]
        definition.validate(value)
        result[key] = definition.normalize(value)
    _validate_cross_rules(result)
    return result


def _validate_cross_rules(values: dict[str, Any]) -> None:
    if values.get("reasoning_effort") is not None and values.get("reasoning_effort_level") is not None:
        raise ValueError(
            "reasoning_effort and reasoning_effort_level are mutually exclusive"
        )


class GenerationSettingsPayload(BaseModel):
    """Typed, validated, capability-resolved provider payload settings.

    Only capability-confirmed settings are populated.  ``None`` fields are
    omitted from the wire payload; the model refuses unknown fields and
    refuses type coercion, so an adapter can never forward unchecked data.

    The model also re-validates every populated field through the closed
    domain registry.  The normal emission planner already validates, but the
    payload is a provider-boundary type: a contradictory or hand-built
    payload must not be able to carry an out-of-range value past it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    top_p: float | None = None
    top_k: int | None = None
    min_p: float | None = None
    typical_p: float | None = None
    tail_free_sampling: float | None = None
    smoothing_factor: float | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    repetition_penalty: float | None = None
    repetition_window: int | None = None
    stop_sequences: tuple[str, ...] | None = None
    ignore_eos: bool | None = None
    seed: int | None = None
    reasoning_effort_level: str | None = None
    reasoning_token_budget: int | None = None
    logprobs: bool | None = None
    top_logprobs: int | None = None
    logit_bias: dict[str, int] | None = None
    mirostat: int | None = None
    mirostat_tau: float | None = None
    mirostat_eta: float | None = None
    dynamic_temperature_range: float | None = None
    xtc_probability: float | None = None
    xtc_threshold: float | None = None
    dry_multiplier: float | None = None
    dry_base: float | None = None
    dry_allowed_length: int | None = None
    dry_penalty_last_n: int | None = None

    @model_validator(mode="after")
    def _validate_against_registry(self) -> "GenerationSettingsPayload":
        for key in EMISSION_ORDER:
            if key not in EXTENDED_SETTING_KEYS:
                continue
            value = getattr(self, key)
            if value is None:
                continue
            SETTING_DEFINITIONS_BY_KEY[key].validate(value)
        return self

    def configured(self) -> tuple[str, ...]:
        """Return the keys carrying a value, in catalogue emission order."""
        return tuple(
            key
            for key in EMISSION_ORDER
            if key in EXTENDED_SETTING_KEYS and getattr(self, key) is not None
        )

    def emittable_keys(
        self,
        states: "Mapping[str, str] | None",
        capabilities: "Mapping[str, str] | None" = None,
        omitted: "Mapping[str, str] | None" = None,
    ) -> tuple[str, ...]:
        """Return the configured keys the boundary is allowed to emit.

        The provider boundary is the last place a normalized setting can be
        stopped.  A setting may cross it only when ALL of the following hold:

        - the request carries an explicit ``emitted`` state for it (``states``);
        - any capability evidence the request also carries (``capabilities``,
          keyed by setting key) is ``supported`` — a present capability entry
          that is not exactly ``supported`` (including ``None`` or an empty
          string) fails closed, while an absent entry defers to ``states``;
        - it is not named as non-emitted in ``omitted`` (``omitted_settings``).

        A request with no state evidence at all fails closed and emits nothing.
        This defends against a malformed or contradictory upstream request in
        which the independent evidence planes disagree.
        """
        if states is None:
            return ()
        blocked = set((omitted or {}).keys())
        capability_states = capabilities or {}
        missing = object()
        allowed: list[str] = []
        for key in self.configured():
            if states.get(key) != STATE_EMITTED:
                continue
            if key in blocked:
                continue
            # A key that is PRESENT in the capability plane must say
            # "supported"; a present-but-None/empty/unknown value fails
            # closed.  Only an absent key defers to the state plane.
            capability_state = capability_states.get(key, missing)
            if capability_state is not missing and capability_state != "supported":
                continue
            allowed.append(key)
        return tuple(allowed)



def _decode_text_setting(definition: SettingDefinition, raw: str) -> Any:
    """Parse the text representation used by the generated editors."""
    text = raw.strip()
    if not text:
        return None
    if definition.value_type is SettingValueType.STR_LIST:
        items = [line.strip() for line in text.splitlines()]
        return [item for item in items if item]
    if definition.value_type is SettingValueType.TOKEN_BIAS:
        result: dict[str, int] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if "=" not in line:
                raise ValueError(f"{definition.key} entries must look like token=bias")
            token, _, bias_text = line.partition("=")
            token = token.strip().strip('"').strip("'")
            try:
                bias = int(bias_text.strip())
            except ValueError:
                raise ValueError(f"{definition.key} bias must be an integer") from None
            result[token] = bias
        return result
    if definition.value_type is SettingValueType.INT:
        try:
            return int(text)
        except ValueError:
            raise ValueError(f"{definition.key} must be an integer") from None
    if definition.value_type is SettingValueType.FLOAT:
        try:
            return float(text)
        except ValueError:
            raise ValueError(f"{definition.key} must be a number") from None
    return text


def encode_text_setting(key: str, value: Any) -> str:
    """Render a value as editor text (inverse of ``decode_text_setting``)."""
    definition = setting_definition(key)
    if value is None:
        return ""
    if definition.value_type is SettingValueType.STR_LIST:
        return "\n".join(str(item) for item in value)
    if definition.value_type is SettingValueType.TOKEN_BIAS:
        return "\n".join(f"{token}={bias}" for token, bias in value.items())
    return str(value)


def decode_text_setting(key: str, raw: str) -> Any:
    value = _decode_text_setting(setting_definition(key), raw)
    validate_setting_value(key, value)
    return setting_definition(key).normalize(value) if value is not None else None


@dataclass(frozen=True, slots=True)
class SettingResolution:
    """One setting's resolved value, provenance and emission decision."""

    key: str
    value: Any
    provenance: str
    #: capability state governing emission ("supported"|"unsupported"|"unknown"
    #: |"none" for B.O.T.S.-owned settings|"unset" when nothing is configured)
    capability_state: str
    state: str
    reason: str = ""


@dataclass(frozen=True, slots=True)
class SettingsEmissionPlan:
    """The complete per-setting decision plan for one generation request."""

    resolutions: tuple[SettingResolution, ...] = field(default_factory=tuple)
    payload: GenerationSettingsPayload | None = None

    def by_key(self) -> dict[str, SettingResolution]:
        return {item.key: item for item in self.resolutions}

    def emitted_keys(self) -> tuple[str, ...]:
        return tuple(item.key for item in self.resolutions if item.state == STATE_EMITTED)

    def as_evidence(self) -> dict[str, object]:
        """Durable, closed evidence mapping for request-time settings."""
        values: dict[str, object] = {}
        provenance: dict[str, str] = {}
        states: dict[str, str] = {}
        for item in self.resolutions:
            states[item.key] = item.state
            if item.value is not None:
                values[item.key] = item.value
                provenance[item.key] = item.provenance
        return {"values": values, "provenance": provenance, "states": states}
