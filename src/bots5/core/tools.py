"""Bounded local tool invocation as a consumer of the shared control plane.

This module is the v0.2 reference implementation of the "tool declaration /
invocation / result" and "workspace-bounded local effects" contracts.  It is
deliberately narrow:

* ``ToolDefinition`` / ``ToolRegistry`` — a closed, fail-closed declaration
  surface (no open enums, no free-form pass-through, collision is an error).
* ``ToolInvoker`` — one invocation path that **must** pass through a
  :class:`~bots5.core.capabilities.CapabilityAuthority` grant before any
  effect runs.  There is no ambient bypass: a tool that has not been granted
  a scoped capability is refused with a structured ``DenialReason``.
* ``ToolResult`` / ``InvocationRecord`` — an append-only, truth-preserving
  journal.  ``UNKNOWN`` is never rewritten to success or failure.
* ``workspace_read_file`` — the reference read-only local tool: it inspects
  exactly one file inside an explicitly granted workspace root, refuses
  traversal/symlink escape, and enforces a byte bound.

Persistence (durability of the invocation journal) is intentionally not owned
here: the record is produced as data and published through the application's
existing event/command admission so the queue/receipts workstream can settle it
later.  This keeps the module a pure consumer of the control plane rather than
a second authority.

Threat model (parent campaign locked decision): accidental, defective,
compromised-in-scope and out-of-authority behaviour — not a hostile
arbitrary-native-code sandbox.  The checks below fail closed on ordinary
mistakes (missing grant, path escape, oversized read, unknown tool, malformed
arguments); they are not a security boundary against a malicious in-process
attacker.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from bots5.core.capabilities import (
    CapabilityAuthority,
    CapabilityDenied,
    CapabilityGrant,
    DenialReason,
    DirectoryScope,
    WORKSPACE_READ,
)
from bots5.core.errors import CoreError


class ToolError(CoreError):
    """A bounded tool declaration/invocation failure (fail-closed)."""


class ToolNotFound(ToolError):
    """No registered tool with the requested ``tool_id``."""


class ToolCollision(ToolError):
    """A duplicate ``tool_id`` was registered (never last-wins)."""


class ToolSchemaError(ToolError):
    """A definition or argument payload failed strict validation."""


class ToolArgumentError(ToolError):
    """Validated arguments are structurally wrong for the bound tool."""


class ToolEffectError(ToolError):
    """The tool's bounded effect failed without establishing an outcome."""


class ToolRefused(CoreError):
    """Authorization refused the invocation; carries the structured reason."""

    def __init__(self, reason: DenialReason, message: str) -> None:
        self.reason = reason
        self.message = message
        super().__init__(f"{reason.value}: {message}")


# Closed vocabulary.  Open enums would let untrusted input define its own
# authority vocabulary, so both the state and the schema dialect are closed.
TOOL_SCHEMA_DIALECT = "bots5.tool-schema.v1"
TOOL_MAX_ARGUMENTS = 32
TOOL_MAX_STRING_ARGUMENT = 4096
WORKSPACE_READ_MAX_BYTES = 1 << 20  # 1 MiB bounded read


class ToolState(StrEnum):
    """Closed invocation state vocabulary (terminal states are final)."""

    PROPOSED = "PROPOSED"
    ADMITTED = "ADMITTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    REFUSED = "REFUSED"
    # UNKNOWN is terminal and must never be rewritten to SUCCEEDED/FAILED
    # when the effect outcome cannot be established.
    UNKNOWN = "UNKNOWN"


_TERMINAL_STATES = frozenset(
    {ToolState.SUCCEEDED, ToolState.FAILED, ToolState.REFUSED, ToolState.UNKNOWN}
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ToolSchemaError(message)


def _validate_tool_id(tool_id: str) -> str:
    """Namespaced, closed-form tool identity (``contributor.name``)."""
    _require(type(tool_id) is str and bool(tool_id), "tool_id must be a nonempty string")
    _require(len(tool_id) <= 128, "tool_id exceeds 128 characters")
    _require("\x00" not in tool_id, "tool_id must not contain NUL")
    parts = tool_id.split(".")
    _require(
        1 <= len(parts) <= 3 and all(parts),
        "tool_id must be dot-namespaced with nonempty segments",
    )
    for segment in parts:
        for char in segment:
            _require(
                char.isalnum() or char in {"-", "_"},
                f"tool_id has a forbidden character: {char!r}",
            )
    return tool_id


def _validate_schema(schema: Mapping[str, object]) -> Mapping[str, object]:
    """Strict, closed schema validation (fail-closed; no free-form pass-through)."""
    _require(isinstance(schema, Mapping), "tool schema must be a mapping")
    keys = set(schema)
    _require(
        keys <= {"type", "properties", "required", "description"},
        f"tool schema has unsupported keys: {sorted(keys)}",
    )
    _require(schema.get("type") == "object", "tool schema type must be 'object'")
    properties = schema.get("properties", {})
    _require(
        isinstance(properties, Mapping),
        "tool schema properties must be a mapping",
    )
    _require(
        len(properties) <= TOOL_MAX_ARGUMENTS,
        "tool schema declares too many properties",
    )
    for name, spec in properties.items():
        _require(
            isinstance(name, str) and name.isidentifier(),
            f"tool schema property name is invalid: {name!r}",
        )
        _require(isinstance(spec, Mapping), f"property {name!r} spec must be a mapping")
        spec_keys = set(spec) - {"description", "enum"}
        _require(
            spec_keys <= {"type"},
            f"property {name!r} has unsupported spec keys: {sorted(spec_keys)}",
        )
        _require(
            spec.get("type") in {"string", "integer", "number", "boolean"},
            f"property {name!r} has a closed-typed, unsupported or missing type",
        )
    required = schema.get("required", ())
    _require(isinstance(required, (list, tuple)), "tool schema required must be a list")
    _require(
        all(isinstance(item, str) for item in required),
        "tool schema required entries must be strings",
    )
    _require(
        len(set(required)) == len(required),
        "tool schema required entries must be unique",
    )
    _require(
        all(item in properties for item in required),
        "tool schema required entries must exist in properties",
    )
    return schema


def _validate_arguments(
    schema: Mapping[str, object], arguments: Mapping[str, object]
) -> dict[str, object]:
    """Validate and canonicalise one argument payload against a closed schema."""
    if not isinstance(arguments, Mapping):
        raise ToolSchemaError("tool arguments must be a mapping")
    properties = dict(schema.get("properties", {}))
    required = list(schema.get("required", ()))
    provided = set(arguments)
    allowed = set(properties)
    unknown = provided - allowed
    _require(not unknown, f"unknown tool arguments: {sorted(unknown)}")
    missing = [name for name in required if name not in provided]
    if missing:
        raise ToolArgumentError(f"missing required tool arguments: {missing}")

    validated: dict[str, object] = {}
    for name, value in arguments.items():
        expected = properties[name].get("type")
        if expected == "string":
            _require(type(value) is str, f"argument {name!r} must be a string")
            _require(
                len(value) <= TOOL_MAX_STRING_ARGUMENT,
                f"argument {name!r} exceeds the string bound",
            )
            _require(
                "\x00" not in value,
                f"argument {name!r} must not contain NUL",
            )
            # An empty required string is structurally meaningless for every
            # shipped tool (paths, ids), so refuse it at the argument plane
            # before any effect is attempted.
            _require(
                bool(value),
                f"argument {name!r} must be a nonempty string",
            )
        elif expected == "integer":
            _require(type(value) is int and not isinstance(value, bool),
                     f"argument {name!r} must be an integer")
        elif expected == "number":
            _require(
                isinstance(value, (int, float)) and not isinstance(value, bool),
                f"argument {name!r} must be a number",
            )
        elif expected == "boolean":
            _require(type(value) is bool, f"argument {name!r} must be a boolean")
        else:
            # Unreachable: _validate_schema enforces a closed type set.
            raise ToolSchemaError(f"argument {name!r} has no closed type")
        validated[name] = value
    return validated


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """One closed, digest-pinned tool declaration (the model-facing plane).

    Visibility of a definition confers no execution permission: the definition
    crosses into model context only after validation, and execution still
    requires a scoped capability grant at invocation time (permission plane),
    with any human approval on a separate surface (UI plane).
    """

    tool_id: str
    version: int
    description: str
    schema: Mapping[str, object]
    capability_kind: str
    definition_version: int = 1

    def __post_init__(self) -> None:
        _validate_tool_id(self.tool_id)
        if type(self.version) is not int or self.version < 1:
            raise ToolSchemaError("tool definition version must be a positive integer")
        if type(self.definition_version) is not int or self.definition_version != 1:
            raise ToolSchemaError("unsupported tool definition version")
        _require(
            isinstance(self.description, str) and bool(self.description.strip()),
            "tool description must be a nonempty string",
        )
        _require(
            len(self.description) <= 2048,
            "tool description exceeds 2048 characters",
        )
        _validate_schema(self.schema)
        _require(
            isinstance(self.capability_kind, str) and bool(self.capability_kind),
            "tool capability_kind must be a nonempty string",
        )

    @property
    def digest(self) -> str:
        """sha256 of the canonical declaration (pinned into every invocation)."""
        payload = (
            f"{TOOL_SCHEMA_DIALECT}\n{self.definition_version}\n{self.tool_id}\n"
            f"{self.version}\n{self.capability_kind}\n{self.description}\n"
            f"{sorted(self.schema.get('properties', {}))}\n"
            f"{sorted(self.schema.get('required', ()))}\n"
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ToolGrantBinding:
    """A scoped capability grant pinned to one tool invocation attempt."""

    grant: CapabilityGrant
    workspace_root: Path

    def __post_init__(self) -> None:
        if not isinstance(self.workspace_root, Path):
            raise ToolSchemaError("workspace root must be a Path")
        if not self.workspace_root.is_absolute():
            raise ToolSchemaError("workspace root must be an absolute path")
        if ".." in self.workspace_root.parts:
            raise ToolSchemaError("workspace root must not contain '..'")


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    """One append-only invocation request record (evidence envelope)."""

    invocation_id: str
    tool_id: str
    definition_digest: str
    arguments: Mapping[str, object]
    grant_id: str
    policy_version: int
    requested_state: ToolState = ToolState.PROPOSED

    def __post_init__(self) -> None:
        if not self.invocation_id:
            raise ToolSchemaError("invocation_id must be nonempty")
        _validate_tool_id(self.tool_id)
        if len(self.definition_digest) != 64:
            raise ToolSchemaError("definition digest must be a sha256 hex string")
        if type(self.policy_version) is not int or self.policy_version < 1:
            raise ToolSchemaError("policy_version must be a positive integer")
        if self.requested_state not in {ToolState.PROPOSED, ToolState.ADMITTED}:
            raise ToolSchemaError("an invocation may be requested only as PROPOSED/ADMITTED")


@dataclass(frozen=True, slots=True)
class ToolResult:
    """One settled (or truthfully unsettled) invocation outcome."""

    invocation_id: str
    tool_id: str
    state: ToolState
    payload: Mapping[str, object] | None = None
    refusal_reason: DenialReason | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if self.state not in _TERMINAL_STATES:
            raise ToolSchemaError("a tool result must be in a terminal state")
        if self.state is ToolState.REFUSED and self.refusal_reason is None:
            raise ToolSchemaError("a REFUSED result must carry a refusal reason")
        if self.state is not ToolState.REFUSED and self.refusal_reason is not None:
            raise ToolSchemaError("only a REFUSED result carries a refusal reason")
        if self.state is ToolState.SUCCEEDED and self.error is not None:
            raise ToolSchemaError("a SUCCEEDED result must not carry an error")
        if self.state is ToolState.FAILED and not self.error:
            raise ToolSchemaError("a FAILED result must carry an error")
        if self.state is ToolState.UNKNOWN and self.error is not None:
            raise ToolSchemaError(
                "an UNKNOWN result must not assert an error; the outcome is unknown"
            )


class ToolExecutor:
    """The narrow declared seam between authorization and one bounded effect.

    An executor is bound to exactly one registered tool and receives only the
    validated arguments plus the already-authorized capability grant.  It never
    performs its own authority decisions; refusal happens before dispatch.
    """

    def __init__(self, tool_id: str, invoke: Callable[[Mapping[str, object], CapabilityGrant], Mapping[str, object]]) -> None:
        _validate_tool_id(tool_id)
        if not callable(invoke):
            raise ToolSchemaError("executor invoke must be callable")
        self._tool_id = tool_id
        self._invoke = invoke

    @property
    def tool_id(self) -> str:
        return self._tool_id

    def run(self, arguments: Mapping[str, object], grant: CapabilityGrant) -> Mapping[str, object]:
        return self._invoke(arguments, grant)


class ToolRegistry:
    """Closed tool registry: explicit registration, collision is an error."""

    def __init__(self) -> None:
        self._definitions: dict[str, ToolDefinition] = {}
        self._executors: dict[str, ToolExecutor] = {}

    def register(
        self,
        definition: ToolDefinition,
        executor: ToolExecutor,
        *,
        replace: bool = False,
    ) -> None:
        """Register one definition/executor pair (never silently last-wins)."""
        if not isinstance(definition, ToolDefinition):
            raise ToolSchemaError("definition must be a ToolDefinition")
        if not isinstance(executor, ToolExecutor):
            raise ToolSchemaError("executor must be a ToolExecutor")
        if definition.tool_id != executor.tool_id:
            raise ToolSchemaError(
                f"executor tool_id {executor.tool_id!r} does not match "
                f"definition {definition.tool_id!r}"
            )
        if definition.tool_id in self._definitions and not replace:
            raise ToolCollision(
                f"tool_id already registered (collision, never last-wins): {definition.tool_id}"
            )
        self._definitions[definition.tool_id] = definition
        self._executors[definition.tool_id] = executor

    def definition(self, tool_id: str) -> ToolDefinition:
        try:
            return self._definitions[tool_id]
        except KeyError:
            raise ToolNotFound(f"no registered tool: {tool_id}") from None

    def executor(self, tool_id: str) -> ToolExecutor:
        try:
            return self._executors[tool_id]
        except KeyError:
            raise ToolNotFound(f"no registered tool executor: {tool_id}") from None

    def tool_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._definitions))


def workspace_read_file(root: Path, relative: str, *, max_bytes: int = WORKSPACE_READ_MAX_BYTES) -> dict[str, object]:
    """Reference read-only tool: one bounded file inside a granted workspace.

    The caller must already hold a ``workspace-read`` capability grant whose
    ``DirectoryScope`` root covers ``root``; this function only performs the
    bounded, escape-proof read itself.  It refuses:

    * absolute paths and ``..`` traversal (no escape from the granted root),
    * symlinked intermediate directories and symlinked final leaves,
    * non-regular files (directories, fifos, sockets, devices),
    * reads larger than ``max_bytes`` (bounded resource use).

    Fail-closed: any unexpected OS error becomes :class:`ToolEffectError`
    without asserting a durable outcome.
    """
    if not isinstance(root, Path) or not root.is_absolute():
        raise ToolSchemaError("workspace root must be an absolute Path")
    if ".." in root.parts:
        raise ToolSchemaError("workspace root must not contain '..'")
    if type(relative) is not str or not relative or "\x00" in relative:
        raise ToolArgumentError("workspace path must be a nonempty string without NUL")
    if relative.startswith("/"):
        raise ToolArgumentError("workspace path must be relative to the granted root")
    parts = [part for part in relative.split("/") if part not in {"", "."}]
    if not parts:
        raise ToolArgumentError("workspace path must name a file inside the root")
    if any(part == ".." for part in parts):
        raise ToolArgumentError("workspace path traversal is forbidden")
    if type(max_bytes) is not int or max_bytes < 1:
        raise ToolSchemaError("max_bytes must be a positive integer")

    # Walk component-by-component with O_NOFOLLOW so a symlink anywhere in the
    # chain cannot redirect the read outside the granted workspace.
    try:
        current_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError as exc:
        raise ToolEffectError(f"workspace root cannot be opened: {exc}") from exc
    opened = [current_fd]
    try:
        for index, part in enumerate(parts):
            final = index == len(parts) - 1
            # O_NONBLOCK keeps a FIFO/socket from blocking the open forever;
            # the regular-file check below then refuses it.  O_NOFOLLOW
            # refuses a symlink at every component, including the leaf.
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
            if not final:
                flags |= os.O_DIRECTORY
            try:
                next_fd = os.open(part, flags, dir_fd=current_fd)
            except OSError as exc:
                raise ToolEffectError(
                    f"workspace path cannot be opened: {part} ({exc.strerror})"
                ) from exc
            opened.append(next_fd)
            current_fd = next_fd
        file_fd = current_fd
        info = os.fstat(file_fd)
        if not stat_isreg(info.st_mode):
            raise ToolEffectError("workspace path is not a regular file")
        if info.st_size > max_bytes:
            raise ToolEffectError(
                f"workspace file exceeds the {max_bytes}-byte read bound"
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(file_fd, min(65536, max_bytes + 1 - total)) if total <= max_bytes else b""
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ToolEffectError(
                    f"workspace file exceeds the {max_bytes}-byte read bound"
                )
            chunks.append(chunk)
        raw = b"".join(chunks)
    finally:
        for descriptor in reversed(opened):
            try:
                os.close(descriptor)
            except OSError:
                pass

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolEffectError("workspace file is not valid UTF-8 text") from exc
    return {
        "path": "/".join(parts),
        "bytes": len(raw),
        "content": text,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def stat_isreg(mode: int) -> bool:
    import stat as _stat

    return _stat.S_ISREG(mode)


class ToolInvoker:
    """One invocation path: declare → authorize → execute → settle → journal.

    The invoker never performs an effect without a live capability grant issued
    by the injected :class:`CapabilityAuthority`, and it never retries or
    rewrites an ``UNKNOWN`` outcome.
    """

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        authority: CapabilityAuthority,
        id_factory: Callable[[], str],
        policy_version: int = 1,
    ) -> None:
        if not isinstance(registry, ToolRegistry):
            raise ToolSchemaError("registry must be a ToolRegistry")
        if not isinstance(authority, CapabilityAuthority):
            raise ToolSchemaError("authority must be a CapabilityAuthority")
        if not callable(id_factory):
            raise ToolSchemaError("id_factory must be callable")
        if type(policy_version) is not int or policy_version < 1:
            raise ToolSchemaError("policy_version must be a positive integer")
        self._registry = registry
        self._authority = authority
        self._id_factory = id_factory
        self._policy_version = policy_version
        self._journal: list[ToolResult] = []

    @property
    def policy_version(self) -> int:
        return self._policy_version

    def journal(self) -> tuple[ToolResult, ...]:
        """Append-only journal view (reruns add records; nothing is rewritten)."""
        return tuple(self._journal)

    def invoke(
        self,
        tool_id: str,
        arguments: Mapping[str, object],
        *,
        grant: CapabilityGrant,
        workspace_root: Path | None = None,
    ) -> ToolResult:
        """Invoke one registered tool under an explicit capability grant.

        Refusal happens **before** any effect: unknown tool, malformed
        arguments, or a ``CapabilityDenied`` from the authority all produce a
        terminal ``REFUSED`` result carrying the structured reason, with zero
        filesystem effects.  Executor exceptions produce ``FAILED``; an
        executor that cannot establish its outcome must raise
        :class:`ToolUncertainOutcome` (or be reported as ``UNKNOWN`` by the
        caller) — this path never converts uncertainty into success.
        """
        invocation_id = self._id_factory()
        definition = None
        try:
            definition = self._registry.definition(tool_id)
        except ToolNotFound as exc:
            result = ToolResult(
                invocation_id=invocation_id,
                tool_id=tool_id,
                state=ToolState.REFUSED,
                refusal_reason=DenialReason.NO_GRANT,
                error=str(exc),
            )
            self._journal.append(result)
            return result

        try:
            validated = _validate_arguments(definition.schema, arguments)
        except (ToolSchemaError, ToolArgumentError) as exc:
            result = ToolResult(
                invocation_id=invocation_id,
                tool_id=tool_id,
                state=ToolState.REFUSED,
                refusal_reason=DenialReason.INVALID_UNITS,
                error=str(exc),
            )
            self._journal.append(result)
            return result

        binding_root = workspace_root
        if binding_root is not None and not isinstance(binding_root, Path):
            result = ToolResult(
                invocation_id=invocation_id,
                tool_id=tool_id,
                state=ToolState.REFUSED,
                refusal_reason=DenialReason.SCOPE_MISMATCH,
                error="workspace root must be a Path",
            )
            self._journal.append(result)
            return result

        invocation = ToolInvocation(
            invocation_id=invocation_id,
            tool_id=tool_id,
            definition_digest=definition.digest,
            arguments=validated,
            grant_id=grant.grant_id,
            policy_version=self._policy_version,
            requested_state=ToolState.ADMITTED,
        )

        # Structural binding of the workspace target happens BEFORE the
        # capability effect is entered, so a malformed target (degenerate
        # path, missing file name, NUL) is refused with zero budget consumed.
        # The capability authority remains the source of truth for scope,
        # expiry, budget and grant ownership.
        target: Path | None = None
        if binding_root is not None and "path" in validated:
            candidate = validated["path"]
            if isinstance(candidate, str):
                if candidate.startswith("/") or "\x00" in candidate:
                    result = ToolResult(
                        invocation_id=invocation.invocation_id,
                        tool_id=tool_id,
                        state=ToolState.REFUSED,
                        refusal_reason=DenialReason.SCOPE_MISMATCH,
                        error="workspace path must be relative and must not contain NUL",
                    )
                    self._journal.append(result)
                    return result
                relative = [
                    part
                    for part in candidate.split("/")
                    if part not in {"", "."}
                ]
                if not relative or any(part == ".." for part in relative):
                    result = ToolResult(
                        invocation_id=invocation.invocation_id,
                        tool_id=tool_id,
                        state=ToolState.REFUSED,
                        refusal_reason=DenialReason.SCOPE_MISMATCH,
                        error="workspace path must name a file inside the granted root",
                    )
                    self._journal.append(result)
                    return result
                target = binding_root.joinpath(*relative)

        # One authorization/effect span for the whole invocation: the target
        # pins the exact file when a workspace root is bound (per-file scope
        # check, not whole-directory), and budget is consumed exactly once.
        # Any CapabilityDenied here is a pre-effect refusal with zero
        # filesystem effects.
        executor = self._registry.executor(tool_id)
        try:
            with self._authority.effect(grant, units=1, target=target, kind=WORKSPACE_READ):
                payload = executor.run(validated, grant)
        except CapabilityDenied as exc:
            result = ToolResult(
                invocation_id=invocation.invocation_id,
                tool_id=tool_id,
                state=ToolState.REFUSED,
                refusal_reason=exc.reason,
                error=exc.message,
            )
            self._journal.append(result)
            return result
        except ToolUncertainOutcome as exc:
            # Truthful UNKNOWN: never rewritten to SUCCEEDED or FAILED.
            result = ToolResult(
                invocation_id=invocation.invocation_id,
                tool_id=tool_id,
                state=ToolState.UNKNOWN,
                error=None,
            )
            _ = exc
            self._journal.append(result)
            return result
        except ToolArgumentError as exc:
            # A structural argument problem discovered while binding the path
            # (degenerate/relative-to-root confusion) is a refusal of the
            # request, not a failed effect: no effect was established.
            result = ToolResult(
                invocation_id=invocation.invocation_id,
                tool_id=tool_id,
                state=ToolState.REFUSED,
                refusal_reason=DenialReason.SCOPE_MISMATCH,
                error=str(exc),
            )
            self._journal.append(result)
            return result
        except ToolError as exc:
            result = ToolResult(
                invocation_id=invocation.invocation_id,
                tool_id=tool_id,
                state=ToolState.FAILED,
                error=str(exc),
            )
            self._journal.append(result)
            return result
        except Exception as exc:  # noqa: BLE001 - bounded, fail-closed boundary
            result = ToolResult(
                invocation_id=invocation.invocation_id,
                tool_id=tool_id,
                state=ToolState.FAILED,
                error=f"{type(exc).__name__}: {exc}",
            )
            self._journal.append(result)
            return result

        if not isinstance(payload, Mapping):
            result = ToolResult(
                invocation_id=invocation.invocation_id,
                tool_id=tool_id,
                state=ToolState.FAILED,
                error="tool executor returned a non-mapping payload",
            )
            self._journal.append(result)
            return result

        result = ToolResult(
            invocation_id=invocation.invocation_id,
            tool_id=tool_id,
            state=ToolState.SUCCEEDED,
            payload=dict(payload),
        )
        self._journal.append(result)
        return result


class ToolUncertainOutcome(ToolError):
    """The executor could not establish its outcome; settle as UNKNOWN."""

    def __init__(self, message: str = "tool outcome could not be established") -> None:
        super().__init__(message)


def workspace_read_tool_definition() -> ToolDefinition:
    """The reference read-only local tool declaration (closed schema)."""
    return ToolDefinition(
        tool_id="bots5.workspace_read",
        version=1,
        description=(
            "Read one UTF-8 text file inside an explicitly granted workspace "
            "root. Read-only; refuses traversal and symlink escape."
        ),
        schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path relative to the granted workspace root.",
                },
            },
            "required": ["path"],
        },
        capability_kind="workspace-read",
    )


def bind_workspace_read_tool(
    registry: ToolRegistry, *, root: Path, max_bytes: int = WORKSPACE_READ_MAX_BYTES
) -> None:
    """Register the reference read tool with a root-bound bounded executor.

    The executor closes over ``root`` so the effect is physically confined to
    the granted workspace even if a caller supplies a misleading scope; the
    capability grant remains the authorization source of truth.
    """
    definition = workspace_read_tool_definition()

    def _invoke(arguments: Mapping[str, object], grant: CapabilityGrant) -> Mapping[str, object]:
        relative = arguments.get("path")
        if not isinstance(relative, str):
            raise ToolArgumentError("workspace read requires a string 'path' argument")
        return workspace_read_file(root, relative, max_bytes=max_bytes)

    registry.register(definition, ToolExecutor(definition.tool_id, _invoke))


__all__ = [
    "ToolCollision",
    "ToolError",
    "ToolExecutor",
    "ToolEffectError",
    "ToolInvocation",
    "ToolInvoker",
    "ToolNotFound",
    "ToolRefused",
    "ToolRegistry",
    "ToolResult",
    "ToolSchemaError",
    "ToolState",
    "ToolUncertainOutcome",
    "ToolDefinition",
    "TOOL_SCHEMA_DIALECT",
    "WORKSPACE_READ_MAX_BYTES",
    "ToolGrantBinding",
    "ToolArgumentError",
    "bind_workspace_read_tool",
    "workspace_read_file",
    "workspace_read_tool_definition",
]
