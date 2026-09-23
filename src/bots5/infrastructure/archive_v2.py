"""Strict Archive v2 wire validation and deterministic package writing.

Archive v1 remains owned by :mod:`archive_package`.  This module deliberately
does not reinterpret v1 rows: it owns the closed v2 additions needed to carry
import provenance and continuation evidence across another export/import hop.
"""

from __future__ import annotations

import io
import re
import stat
import zipfile
from dataclasses import dataclass
from typing import Mapping

from bots5.core.interchange import (
    InterchangeError,
    canonical_json_bytes,
    logical_content_digest,
    parse_jsonl,
    sha256_hex,
    strict_json_loads,
)


class ArchiveV2Error(ValueError):
    """The closed Archive v2 language was not satisfied."""


class ArchiveV2Unsupported(ArchiveV2Error):
    """A well-identified future v2 semantic cannot be interpreted here."""


FORMAT = "org.necromilias.bots5.chat-archive"
VERSION = 2
FEATURES = frozenset({
    "attachments", "chat-lineage", "context-plans", "generation-outcomes",
    "history-bindings-v1", "import-provenance-v1", "request-time-provenance",
    "continuation-history-v1",
})
_V2_SETTINGS_PROVENANCE = frozenset({
    "application", "model", "chat_model", "branch",
})
BASE_ENTRIES = frozenset({
    "manifest.json", "domain/chat.json", "domain/messages.jsonl",
    "domain/attempts.jsonl", "domain/context-plans.jsonl",
    "domain/attachments.jsonl", "domain/message-attachments.jsonl",
    "domain/attempt-attachments.jsonl", "domain/chat-configuration.json",
    "domain/provenance.json", "COMPLETED",
})
EXTRA_ENTRIES = frozenset({
    "domain/object-provenance.jsonl", "domain/continuation-history.json",
    "domain/history-bindings.jsonl",
})
REQUIRED_ENTRIES = BASE_ENTRIES | EXTRA_ENTRIES
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_FORBIDDEN = frozenset({
    "apikey", "apikeyvalue", "apitoken", "accesstoken", "authorization",
    "clientsecret", "password", "refreshtoken", "secret", "secretvalue", "token",
    "credentialreference", "credentialsource", "credentialstatus", "apikeyenv",
    "baseurl", "endpoint", "requestid",
})


@dataclass(frozen=True, slots=True)
class ArchiveV2Validation:
    archive_id: str
    logical_content_digest: str
    entry_count: int
    total_uncompressed_size: int


def _fail(message: str) -> None:
    raise ArchiveV2Error(message)


def _mapping(value: object, fields: set[str], label: str) -> Mapping[str, object]:
    if type(value) is not dict or set(value) != fields:
        _fail(f"{label} is not a closed schema")
    return value


def _text(value: object, label: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if type(value) is not str or not value or "\x00" in value or len(value) > 4096:
        _fail(f"{label} is invalid")
    return value


def _digest(value: object, label: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        _fail(f"{label} is not a SHA-256 digest")
    return value


def _no_secrets(value: object) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            compact = "".join(character for character in key.casefold() if character.isalnum())
            if compact in _FORBIDDEN:
                _fail("archive has secret-shaped data")
            _no_secrets(nested)
    elif isinstance(value, list):
        for item in value:
            _no_secrets(item)


def _json(contents: Mapping[str, bytes], name: str) -> Mapping[str, object]:
    try:
        value = strict_json_loads(contents[name])
    except (KeyError, InterchangeError) as exc:
        raise ArchiveV2Error(f"{name} is malformed") from exc
    if type(value) is not dict or canonical_json_bytes(value) != contents[name]:
        _fail(f"{name} is not canonical JSON")
    _no_secrets(value)
    return value


def _rows(contents: Mapping[str, bytes], name: str) -> tuple[Mapping[str, object], ...]:
    try:
        values = parse_jsonl(contents[name])
    except (KeyError, InterchangeError) as exc:
        raise ArchiveV2Error(f"{name} is malformed") from exc
    if any(type(value) is not dict for value in values):
        _fail(f"{name} contains a non-object row")
    for value in values:
        _no_secrets(value)
    return tuple(values)  # type: ignore[return-value]


def _inventory(manifest: Mapping[str, object], names: set[str], contents: Mapping[str, bytes]) -> None:
    inventory = manifest.get("entry_inventory")
    if type(inventory) is not list or not inventory:
        _fail("manifest inventory is invalid")
    declared: dict[str, Mapping[str, object]] = {}
    for row in inventory:
        row = _mapping(row, {"path", "media_type", "required", "uncompressed_size", "sha256"}, "inventory row")
        path = _text(row["path"], "inventory path")
        if path == "manifest.json" or path in declared or path not in names:
            _fail("inventory path is invalid")
        if row["required"] is not True or type(row["uncompressed_size"]) is not int or row["uncompressed_size"] < 0:
            _fail("inventory required/size is invalid")
        if type(row["media_type"]) is not str or not row["media_type"]:
            _fail("inventory media type is invalid")
        _digest(row["sha256"], "inventory digest")
        declared[path] = row
    if set(declared) != names - {"manifest.json"}:
        _fail("inventory does not exactly declare package members")
    tuples: list[tuple[str, int, str]] = []
    for path, row in declared.items():
        raw = contents[path]
        if len(raw) != row["uncompressed_size"] or sha256_hex(raw) != row["sha256"]:
            _fail("inventory integrity is invalid")
        tuples.append((path, row["uncompressed_size"], row["sha256"]))
    if _digest(manifest.get("logical_content_digest"), "logical content digest") != logical_content_digest(tuples):
        _fail("logical content digest is invalid")


def _validate_hop(value: object) -> Mapping[str, object]:
    row = _mapping(value, {"archive_id", "archive_version", "logical_content_digest", "object_id", "imported_at"}, "provenance hop")
    _text(row["archive_id"], "provenance archive ID")
    if row["archive_version"] not in {1, 2} or type(row["archive_version"]) is not int:
        _fail("provenance archive version is invalid")
    _digest(row["logical_content_digest"], "provenance digest")
    _text(row["object_id"], "provenance object ID")
    _text(row["imported_at"], "provenance import time")
    return row


def _validate_object_provenance(
    rows: tuple[Mapping[str, object], ...], object_ids: Mapping[str, set[str]], *,
    messages: Mapping[str, Mapping[str, object]], attempts: Mapping[str, Mapping[str, object]],
) -> None:
    seen: set[tuple[str, str]] = set()
    legal_kinds = set(object_ids)
    for row in rows:
        row = _mapping(row, {"object_kind", "object_id", "source", "derivation"}, "object provenance")
        kind = row["object_kind"]
        object_id = row["object_id"]
        if type(kind) is not str or kind not in legal_kinds or type(object_id) is not str or object_id not in object_ids[kind] or (kind, object_id) in seen:
            _fail("object provenance coverage is invalid")
        seen.add((kind, object_id))
        source = _mapping(row["source"], {"kind", "immediate", "prior_chain"}, "object source")
        source_kind = source["kind"]
        if source_kind not in {"native", "v1-bootstrap", "imported"} or type(source["prior_chain"]) is not list:
            _fail("object source is invalid")
        immediate = source["immediate"]
        if source_kind == "native":
            if immediate is not None or source["prior_chain"]:
                _fail("native object has imported source provenance")
        else:
            immediate_row = _validate_hop(immediate)
            if source_kind == "v1-bootstrap" and (immediate_row["archive_version"] != 1 or source["prior_chain"]):
                _fail("v1 bootstrap provenance is invalid")
            triplets = {(immediate_row["archive_id"], immediate_row["logical_content_digest"], immediate_row["object_id"])}
            for prior in source["prior_chain"]:
                hop = _validate_hop(prior)
                triplet = (hop["archive_id"], hop["logical_content_digest"], hop["object_id"])
                if triplet in triplets:
                    _fail("provenance chain repeats an object hop")
                triplets.add(triplet)
        derivation = _mapping(row["derivation"], {"kind", "predecessor"}, "object derivation")
        if derivation["kind"] == "root":
            if derivation["predecessor"] is not None:
                _fail("root derivation has a predecessor")
        elif derivation["kind"] == "local-continuation":
            predecessor = _mapping(derivation["predecessor"], {"object_kind", "object_id"}, "derivation predecessor")
            if (kind not in {"message", "attempt"}
                    or predecessor.get("object_kind") != "message"
                    or predecessor.get("object_id") not in object_ids["message"]):
                _fail("continuation derivation predecessor is invalid")
            predecessor_row = messages[str(predecessor["object_id"])]
            successor_sequence = (
                int(messages[str(object_id)]["sequence"])
                if kind == "message" else int(messages[str(attempts[str(object_id)]["assistant_message_id"])]["sequence"])
            )
            if int(predecessor_row["sequence"]) >= successor_sequence:
                _fail("continuation derivation is not graph ordered")
        else:
            _fail("derivation kind is invalid")
    expected = {(kind, object_id) for kind, values in object_ids.items() for object_id in values}
    if seen != expected:
        _fail("object provenance does not cover every identity-bearing object")


def _validate_continuation(value: Mapping[str, object], message_ids: set[str], attempt_ids: set[str], chat_head: object) -> None:
    data = _mapping(value, {"active_head_message_id", "anchors", "choices", "branches"}, "continuation history")
    head = data["active_head_message_id"]
    if head is not None and head not in message_ids:
        _fail("continuation active head is invalid")
    if chat_head is not None and head != chat_head:
        _fail("continuation active head contradicts chat")
    if type(data["anchors"]) is not list or type(data["choices"]) is not list or type(data["branches"]) is not list:
        _fail("continuation collections are invalid")
    anchors: set[str] = set()
    previous_anchor: str | None = None
    for item in data["anchors"]:
        item = _mapping(item, {"anchor_key", "base_message_id", "source_configuration", "resolution", "resolution_reason"}, "continuation anchor")
        key = _text(item["anchor_key"], "anchor key")
        base = item["base_message_id"]
        if key in anchors or item["resolution"] not in {"unresolved", "equivalent", "needs-operator", "unavailable"} or type(item["source_configuration"]) is not dict:
            _fail("continuation anchor is invalid")
        if (key == "empty") != (base is None) or (base is not None and (base not in message_ids or key != base)):
            _fail("continuation anchor base is invalid")
        if previous_anchor is not None and key <= previous_anchor:
            _fail("continuation anchors are not canonical")
        previous_anchor = key
        anchors.add(key)
    choices: set[tuple[str, int]] = set()
    previous_choice: tuple[str, int] | None = None
    for item in data["choices"]:
        item = _mapping(item, {"anchor_key", "choice_revision", "decision_kind", "mapped_model", "explicit_settings", "excluded_context_refs", "chosen_at"}, "continuation choice")
        key, revision = item["anchor_key"], item["choice_revision"]
        if key not in anchors or type(revision) is not int or revision < 1 or (key, revision) in choices or item["decision_kind"] not in {"equivalent", "operator-resolution"} or type(item["mapped_model"]) is not dict or type(item["explicit_settings"]) is not dict or type(item["excluded_context_refs"]) is not list:
            _fail("continuation choice is invalid")
        if set(item["explicit_settings"]) != {"temperature", "max_output_tokens", "reasoning_effort", "timeout_seconds"}:
            _fail("continuation explicit settings are not closed")
        seen_requirements: set[int] = set()
        for excluded in item["excluded_context_refs"]:
            excluded = _mapping(excluded, {"requirement_ordinal", "expected_digest", "attachment_id", "reason"}, "continuation exclusion")
            ordinal = excluded["requirement_ordinal"]
            if type(ordinal) is not int or ordinal < 0 or ordinal in seen_requirements or excluded["reason"] not in {"missing-external", "operator-excluded"}:
                _fail("continuation exclusion is invalid")
            _digest(excluded["expected_digest"], "continuation exclusion digest")
            if excluded["attachment_id"] is not None and excluded["attachment_id"] not in message_ids:
                # The v2 wire relationship has an attachment ID, which need
                # not be a message ID.  It is checked by the attachment graph
                # validator when that relation is available.
                _text(excluded["attachment_id"], "continuation exclusion attachment")
            seen_requirements.add(ordinal)
        pair = (key, revision)
        if previous_choice is not None and pair <= previous_choice:
            _fail("continuation choices are not canonical")
        previous_choice = pair
        choices.add((key, revision))
    branch_messages: set[str] = set()
    previous_message: str | None = None
    for item in data["branches"]:
        item = _mapping(item, {"anchor_key", "choice_revision", "first_message_id", "attempt_id", "created_at"}, "continuation branch")
        pair, message = (item["anchor_key"], item["choice_revision"]), item["first_message_id"]
        if pair not in choices or message not in message_ids or message in branch_messages or item["attempt_id"] not in attempt_ids:
            _fail("continuation branch is invalid")
        if previous_message is not None and message <= previous_message:
            _fail("continuation branches are not canonical")
        previous_message = message
        branch_messages.add(message)


def _validate_v2_attachments(
    rows: tuple[Mapping[str, object], ...],
    *,
    attachment_policy: object,
    contents: Mapping[str, bytes],
    manifest: Mapping[str, object],
    provenance_resources: list[object],
) -> dict[str, Mapping[str, object]]:
    """Validate the evolved attachment truth without changing frozen v1.

    A v2 missing external reference carries verified expected identity, never
    fabricated bytes.  All other metadata retains the closed v1 safe shape.
    """
    from .archive_package import (
        _validate_attachment,
        _validate_attachment_resources,
    )

    values: dict[str, Mapping[str, object]] = {}
    normalised: list[Mapping[str, object]] = []
    for row in rows:
        row = _mapping(
            row,
            {
                "source_id", "blob_digest", "filename", "filename_status",
                "source_kind", "source_kind_status", "text_representation_id",
                "text_digest", "text_eligibility", "ineligibility_reason",
                "created_at", "byte_size", "integrity_status",
                "payload_availability",
            },
            "v2 attachment",
        )
        availability = row["payload_availability"]
        if availability not in {"verified", "missing-external"}:
            _fail("v2 attachment availability is invalid")
        legacy = dict(row)
        del legacy["payload_availability"]
        if availability == "verified":
            _validate_attachment(legacy)
        else:
            if (
                attachment_policy != "external-reference"
                or row["integrity_status"] != "not-present"
                or f"payloads/sha256/{row['blob_digest']}" in contents
            ):
                _fail("v2 missing external attachment is contradictory")
            # Reuse the strict safe metadata validation while changing only
            # the frozen v1 integrity conclusion that v2 explicitly evolves.
            legacy["integrity_status"] = "verified"
            _validate_attachment(legacy)
        identifier = str(row["source_id"])
        if identifier in values:
            _fail("v2 attachment identities are duplicated")
        values[identifier] = row
        normalised.append(legacy)
    _validate_attachment_resources(manifest, contents, normalised, provenance_resources)
    return values


def validate_v2(manifest: Mapping[str, object], names: set[str], contents: Mapping[str, bytes], *, total_size: int) -> ArchiveV2Validation:
    """Validate the closed v2 additions after safe ZIP capture has completed."""
    fields = {"format", "archive_version", "archive_id", "created_at", "source_application_version", "source_db_migration_revision", "source_chat", "attachment_policy", "self_contained", "features", "entry_inventory", "external_resources", "logical_content_digest", "secret_exclusion"}
    manifest = _mapping(manifest, fields, "manifest")
    if manifest["format"] != FORMAT or manifest["archive_version"] != VERSION or type(manifest["archive_version"]) is not int:
        raise ArchiveV2Unsupported("archive format/version is unsupported")
    if not REQUIRED_ENTRIES <= names:
        _fail("archive v2 lacks a required member")
    unknown_members = names - {"manifest.json", "COMPLETED"} - REQUIRED_ENTRIES
    if any(not item.startswith("payloads/sha256/") for item in unknown_members):
        raise ArchiveV2Unsupported("archive v2 member is unsupported")
    if type(manifest["features"]) is not list or any(type(item) is not str for item in manifest["features"]):
        _fail("archive v2 feature set is invalid")
    if set(manifest["features"]) - FEATURES:
        raise ArchiveV2Unsupported("archive v2 feature is unsupported")
    if manifest["features"] != sorted(FEATURES):
        _fail("archive v2 feature set is invalid")
    _text(manifest["archive_id"], "archive ID")
    source_chat = _mapping(manifest["source_chat"], {"source_id", "title"}, "source chat")
    _text(source_chat["source_id"], "source chat ID")
    if type(source_chat["title"]) is not str:
        _fail("source chat title is invalid")
    if manifest["attachment_policy"] not in {"embedded", "external-reference"} or type(manifest["self_contained"]) is not bool or manifest["self_contained"] != (manifest["attachment_policy"] == "embedded"):
        _fail("attachment policy is invalid")
    _inventory(manifest, names, contents)
    # V2 retains the versioned v1 domain graph forms.  Reuse the shared
    # structural validators, then apply V2's independently closed source and
    # continuation evidence below.  This is deliberately not the v1 whole
    # archive validator: V2 has distinct provenance and availability rules.
    from .archive_package import (
        _validate_attempt, _validate_chat, _validate_configuration,
        _validate_message,
    )

    chat = _json(contents, "domain/chat.json")
    _validate_chat(chat)
    messages = _rows(contents, "domain/messages.jsonl")
    attempts = _rows(contents, "domain/attempts.jsonl")
    attachments = _rows(contents, "domain/attachments.jsonl")
    configuration = _json(contents, "domain/chat-configuration.json")
    _validate_configuration(configuration)
    provenance = _json(contents, "domain/provenance.json")
    if set(provenance) != {"source_origin", "import_origin", "attachment_policy", "resources"}:
        _fail("v2 archive provenance is not a closed schema")
    if provenance["attachment_policy"] != manifest["attachment_policy"] or type(provenance["resources"]) is not list:
        _fail("v2 archive provenance contradicts manifest")
    attachment_by_id = _validate_v2_attachments(
        attachments,
        attachment_policy=manifest["attachment_policy"],
        contents=contents,
        manifest=manifest,
        provenance_resources=provenance["resources"],
    )
    validated_messages = tuple(_validate_message(item, str(chat["source_id"])) for item in messages)
    message_by_id = {str(item["source_id"]): item for item in validated_messages}
    if len(message_by_id) != len(validated_messages):
        _fail("v2 message identities are duplicated")
    sequences = {int(item["sequence"]) for item in validated_messages}
    if sequences != set(range(1, len(validated_messages) + 1)):
        _fail("v2 message sequence is not contiguous")
    for message in validated_messages:
        parent = message["parent_id"]
        if parent is not None and (
            parent not in message_by_id
            or parent == message["source_id"]
            or int(message_by_id[parent]["sequence"]) >= int(message["sequence"])
        ):
            _fail("v2 message parent graph is invalid")
        supersedes = message["supersedes_id"]
        if supersedes is None:
            if int(message["revision"]) != 1:
                _fail("v2 initial message revision is invalid")
        else:
            prior = message_by_id.get(str(supersedes))
            if prior is None or prior["role"] != message["role"] or prior["lineage_id"] != message["lineage_id"] or int(prior["revision"]) + 1 != int(message["revision"]):
                _fail("v2 message supersession is invalid")
    for lineage_id in {str(item["lineage_id"]) for item in validated_messages}:
        revisions = sorted(int(item["revision"]) for item in validated_messages if item["lineage_id"] == lineage_id)
        if revisions != list(range(1, len(revisions) + 1)):
            _fail("v2 message lineage revisions are invalid")
    head = chat["head_message_id"]
    if head is None:
        if validated_messages:
            _fail("v2 nonempty chat lacks a head")
    elif head not in message_by_id or message_by_id[head]["role"] != "assistant" or any(item["parent_id"] == head for item in validated_messages):
        _fail("v2 chat head is invalid")
    validated_attempts = tuple(
        _validate_attempt(
            item,
            str(chat["source_id"]),
            message_by_id,
            settings_provenance_values=_V2_SETTINGS_PROVENANCE,
        )
        for item in attempts
    )
    if len({str(item["source_id"]) for item in validated_attempts}) != len(validated_attempts):
        _fail("v2 attempt identities are duplicated")
    attempt_by_id = {str(item["source_id"]): item for item in validated_attempts}
    assistant_owner: set[str] = set()
    for attempt in validated_attempts:
        assistant_id = str(attempt["assistant_message_id"])
        if assistant_id in assistant_owner:
            _fail("v2 assistant has more than one generation attempt")
        assistant_owner.add(assistant_id)
    if any(
        row["role"] == "assistant" and str(row["source_id"]) not in assistant_owner
        for row in validated_messages
    ):
        _fail("v2 assistant lacks its generation attempt")
    message_ids = {str(item.get("source_id")) for item in messages if type(item.get("source_id")) is str}
    attempt_ids = {str(item.get("source_id")) for item in attempts if type(item.get("source_id")) is str}
    attachment_ids = set(attachment_by_id)
    chat_id = chat.get("source_id")
    if type(chat_id) is not str or len(message_ids) != len(messages) or len(attempt_ids) != len(attempts) or len(attachment_ids) != len(attachments):
        _fail("v2 identity rows are malformed or duplicated")
    from .archive_package import _validate_relations
    message_references = _validate_relations(
        _rows(contents, "domain/message-attachments.jsonl"),
        "message_id", message_by_id, attachment_by_id,
    )
    # A context row is a sealed projection of exactly one v3 request.  It is
    # validated before attachment/provenance intake so malformed ownership or
    # a non-canonical timestamp cannot become a durable import candidate.
    from .archive_package import ArchivePackageError, _timestamp, _validate_safe_context
    context_attempts: set[str] = set()
    for context in _rows(contents, "domain/context-plans.jsonl"):
        context = _mapping(
            context,
            {"attempt_id", "plan_version", "canonical_digest", "wire_representation_digest",
             "budget_limit", "budget_semantics", "adapter_id", "adapter_version", "input_counts",
             "envelope_overhead", "output_reserve", "input_units", "total_units", "headroom", "created_at"},
            "v2 persisted context plan",
        )
        attempt_id = _text(context["attempt_id"], "v2 context attempt ID")
        if attempt_id in context_attempts or attempt_id not in attempt_by_id or context["plan_version"] != 3 or type(context["plan_version"]) is not int:
            _fail("v2 context plan ownership is invalid")
        context_attempts.add(attempt_id)
        request = attempt_by_id[attempt_id]["request_time_provenance"]
        if request.get("status") != "available" or request.get("snapshot_version") != 3:
            _fail("v2 context plan does not belong to a v3 attempt")
        try:
            safe = _validate_safe_context(request["context"])
            _timestamp(context["created_at"], "v2 persisted context timestamp", milliseconds=True)
        except ArchivePackageError as exc:
            raise ArchiveV2Error("v2 persisted context plan is malformed") from exc
        for key, safe_key in (
            ("canonical_digest", "canonical_digest"),
            ("wire_representation_digest", "wire_representation_sha256"),
            ("budget_limit", None), ("budget_semantics", None),
            ("adapter_id", None), ("adapter_version", None),
            ("input_counts", "input_counts"), ("envelope_overhead", None),
            ("output_reserve", None), ("input_units", None),
            ("total_units", None), ("headroom", None),
        ):
            expected = safe[safe_key] if safe_key is not None else safe["budget"][key.removeprefix("budget_")]
            if context[key] != expected or type(context[key]) is not type(expected):
                _fail("v2 context plan contradicts request-time provenance")
    for attempt_id, attempt in attempt_by_id.items():
        request = attempt["request_time_provenance"]
        if request.get("status") == "available" and request.get("snapshot_version") == 3 and attempt_id not in context_attempts:
            _fail("v2 v3 attempt lacks its persisted context plan")

    attempt_references = _validate_relations(
        _rows(contents, "domain/attempt-attachments.jsonl"),
        "attempt_id", attempt_by_id, attachment_by_id,
    )
    referenced = set().union(*message_references.values(), *attempt_references.values())
    if referenced != set(attachment_by_id):
        _fail("v2 archive contains an unreferenced attachment")
    provenance_rows = _rows(contents, "domain/object-provenance.jsonl")
    _validate_object_provenance(provenance_rows, {
        "chat": {chat_id}, "message": message_ids, "lineage": {str(row.get("lineage_id")) for row in messages if type(row.get("lineage_id")) is str}, "attachment": attachment_ids, "attempt": attempt_ids,
    }, messages=message_by_id, attempts=attempt_by_id)
    source_kinds = {
        str(row["source"]["kind"])
        for row in provenance_rows
        if type(row.get("source")) is dict and type(row["source"].get("kind")) is str
    }
    expected_origin = "native" if source_kinds == {"native"} else "imported"
    if provenance.get("source_origin") != expected_origin or provenance.get("import_origin") != ("not-recorded" if expected_origin == "native" else "recorded"):
        _fail("v2 archive provenance source origin contradicts object lineage")
    _validate_continuation(_json(contents, "domain/continuation-history.json"), message_ids, attempt_ids, chat.get("head_message_id"))
    provenance_by_identity = {
        (str(row["object_kind"]), str(row["object_id"])): row
        for row in provenance_rows
    }
    def retained_source_hops(kind: str, object_id: str) -> tuple[tuple[str, str, str], ...]:
        """Return every verified source hop for one current object.

        Bare object IDs are not globally meaningful: separate hops may
        legitimately reuse them.  A target can predate its importing attempt,
        so matching later compares the owner's oldest scope against every
        retained target hop rather than assuming both have the same oldest
        origin.
        """
        source = provenance_by_identity.get((kind, object_id), {}).get("source", {})
        if not isinstance(source, dict):
            return ()
        hops = [] if source.get("immediate") is None else [source["immediate"]]
        hops.extend(source.get("prior_chain", []))
        return tuple(
            (
                str(hop["archive_id"]),
                str(hop["logical_content_digest"]),
                str(hop["object_id"]),
            )
            for hop in hops
            if isinstance(hop, dict)
        )

    expected_bindings: dict[tuple[str, int], Mapping[str, object]] = {}
    for attempt_id, attempt in attempt_by_id.items():
        request = attempt["request_time_provenance"]
        if request.get("status") == "available" and request.get("snapshot_version") == 3:
            for ordinal, source in enumerate(request["context"]["sources"]):
                expected_bindings[(attempt_id, ordinal)] = source
    observed_bindings: dict[tuple[str, int], Mapping[str, object]] = {}
    previous_binding: tuple[str, int] | None = None
    for row in _rows(contents, "domain/history-bindings.jsonl"):
        _mapping(row, {"attempt_id", "binding_kind", "ordinal", "snapshot_source", "current_binding", "binding_state"}, "history binding")
        if row["attempt_id"] not in attempt_ids or row["binding_kind"] != "context-source" or type(row["ordinal"]) is not int or row["ordinal"] < 0 or row["binding_state"] not in {"bound", "historical-only", "unavailable"}:
            _fail("history binding is invalid")
        snapshot = _mapping(row["snapshot_source"], {"kind", "source_id", "source_id_status", "evidence_digest", "digest_kind"}, "history binding snapshot")
        if snapshot["kind"] not in {"history", "bots_instruction", "current_user", "attachment"} or snapshot["source_id_status"] not in {"available", "redacted"} or snapshot["digest_kind"] not in {"representation", "safe-source-record"}:
            _fail("history binding snapshot is invalid")
        _digest(snapshot["evidence_digest"], "history binding digest")
        binding = (str(row["attempt_id"]), int(row["ordinal"]))
        if previous_binding is not None and binding <= previous_binding:
            _fail("history bindings are not canonical")
        previous_binding = binding
        if binding in observed_bindings:
            _fail("history bindings are duplicated")
        observed_bindings[binding] = row
    if set(observed_bindings) != set(expected_bindings):
        _fail("history bindings do not cover the sealed safe context")
    for binding, source in expected_bindings.items():
        row = observed_bindings[binding]
        snapshot = row["snapshot_source"]
        expected_snapshot = {
            "kind": source["kind"], "source_id": source["source_id"],
            "source_id_status": source["source_id_status"],
            "evidence_digest": (
                source["representation_digest"] if source["kind"] == "attachment" else
                sha256_hex(canonical_json_bytes({key: source[key] for key in (
                    "kind", "role", "content", "state", "eligible", "selected",
                    "selection_reason", "representation_id", "representation_digest",
                )}))
            ),
            "digest_kind": "representation" if source["kind"] == "attachment" else "safe-source-record",
        }
        if snapshot != expected_snapshot:
            _fail("history binding snapshot contradicts safe context")
        current = row["current_binding"]
        expected_state = "historical-only" if source["kind"] == "bots_instruction" else "unavailable"
        if source["kind"] == "bots_instruction":
            if current is not None or row["binding_state"] != expected_state:
                _fail("instruction history binding is not historical-only")
            continue
        if current is None:
            if row["binding_state"] != expected_state:
                _fail("unbound history binding state is invalid")
            continue
        current = _mapping(current, {"object_kind", "object_id"}, "history current binding")
        target_kind, target_id = current["object_kind"], current["object_id"]
        if target_kind not in {"message", "attachment"} or type(target_id) is not str:
            _fail("history current binding is invalid")
        owner_hops = retained_source_hops("attempt", binding[0])
        target_hops = retained_source_hops(str(target_kind), str(target_id))
        if not owner_hops:
            # Native request-time evidence names current exporting identities
            # exactly, including a ready imported attachment reused by a
            # native request.  No foreign hop may stand in for that ID.
            if target_id != source["source_id"]:
                _fail(
                    "native history current binding lacks its exact current identity: "
                    f"{source['kind']}:{source['source_id']}->{target_kind}:{target_id}"
                )
        else:
            owner_scope = owner_hops[-1][:2]
            if any(
                target_scope[:2] == owner_scope
                and target_scope[2] == source["source_id"]
                for target_scope in target_hops
            ):
                pass
            else:
                _fail(
                    "history current binding lacks a retained source scope: "
                    f"{source['kind']}:{source['source_id']}->{target_kind}:{target_id}"
                )
        if source["kind"] == "attachment":
            if target_kind != "attachment" or target_id not in attempt_references.get(binding[0], set()):
                _fail("attachment history binding is not related to its attempt")
            target = attachment_by_id[target_id]
            if target.get("text_digest") != source["representation_digest"]:
                _fail("attachment history binding representation contradicts safe context")
        else:
            if target_kind != "message" or target_id not in message_by_id:
                _fail("message history binding target is invalid")
            target = message_by_id[target_id]
            if source["kind"] == "current_user" and target_id != attempt_by_id[binding[0]]["user_message_id"]:
                _fail("current-user history binding targets the wrong message")
            if target["role"] != source["role"] or target["content"] != source["content"]:
                _fail("message history binding contradicts safe context")
        if row["binding_state"] != "bound":
            _fail("typed history binding is not bound")
    return ArchiveV2Validation(str(manifest["archive_id"]), str(manifest["logical_content_digest"]), len(names), total_size)


def archive_v2_bytes(manifest_base: Mapping[str, object], entries: Mapping[str, bytes]) -> bytes:
    """Write deterministic v2 bytes and immediately close them under the reader."""
    if set(entries) & {"manifest.json", "COMPLETED"}:
        _fail("caller supplied reserved archive entry")
    logical = dict(entries)
    logical["COMPLETED"] = b""
    inventory = [
        {"path": path, "media_type": "application/octet-stream" if path.startswith("payloads/sha256/") or path == "COMPLETED" else ("application/x-ndjson" if path.endswith(".jsonl") else "application/json"), "required": True, "uncompressed_size": len(raw), "sha256": sha256_hex(raw)}
        for path, raw in sorted(logical.items())
    ]
    manifest = dict(manifest_base)
    manifest["entry_inventory"] = inventory
    manifest["logical_content_digest"] = logical_content_digest((item["path"], item["uncompressed_size"], item["sha256"]) for item in inventory)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED, strict_timestamps=True) as package:
        info = zipfile.ZipInfo("manifest.json", date_time=(1980, 1, 1, 0, 0, 0)); info.external_attr = (stat.S_IFREG | 0o600) << 16; info.create_system = 3
        package.writestr(info, canonical_json_bytes(manifest))
        for path, raw in sorted(logical.items(), key=lambda item: (item[0] == "COMPLETED", item[0])):
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0)); info.external_attr = (stat.S_IFREG | 0o600) << 16; info.create_system = 3
            package.writestr(info, raw)
    return stream.getvalue()
