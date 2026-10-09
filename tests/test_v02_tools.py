"""v0.2 tools: T0 exact declaration/invocation/result + workspace effects,
T1 subsystem (registry, invoker, application commands)."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from bots5.core.application import BotsApplication
from bots5.core.capabilities import (
    CapabilityAuthority,
    DenialReason,
    DirectoryScope,
    GrantRequest,
    Subject,
    WORKSPACE_READ,
)
from bots5.core.errors import StateError
from bots5.core.events import EventBus
from bots5.core.tools import (
    ToolCollision,
    ToolDefinition,
    ToolEffectError,
    ToolExecutor,
    ToolInvoker,
    ToolNotFound,
    ToolRegistry,
    ToolResult,
    ToolSchemaError,
    ToolState,
    ToolUncertainOutcome,
    bind_workspace_read_tool,
    workspace_read_file,
    workspace_read_tool_definition,
)
from bots5.domain.clock import SystemClock
from bots5.domain.ids import Uuid7Factory
from bots5.infrastructure.data_root_authority import DataRootAuthority
from bots5.infrastructure.generation.fake import FakeStreamingBackend


def _grant(ca: CapabilityAuthority, root: Path, *, effects: int = 8):
    return ca.grant(
        GrantRequest(
            subject=Subject(kind="tool", identity="test-tool"),
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=root),
            ttl_seconds=60.0,
            max_effects=effects,
        )
    )


def _registry(root: Path) -> ToolRegistry:
    registry = ToolRegistry()
    bind_workspace_read_tool(registry, root=root)
    return registry


def _ids():
    counter = {"n": 0}

    def factory() -> str:
        counter["n"] += 1
        return f"inv-{counter['n']}"

    return factory


def _invoker(root: Path):
    registry = _registry(root)
    ca = CapabilityAuthority()
    invoker = ToolInvoker(registry=registry, authority=ca, id_factory=_ids())
    return invoker, ca, _grant(ca, root)


def _application(root: Path, *, capability_authority=None, workspace_root=None):
    authority = DataRootAuthority(root.absolute()).acquire()
    store = authority.open_store()
    ids = Uuid7Factory()
    clock = SystemClock()
    events = EventBus(clock, ids, queue_size=32)
    application = BotsApplication(
        store,
        events,
        FakeStreamingBackend(),
        ids=ids,
        clock=clock,
        capability_authority=capability_authority,
        workspace_root=workspace_root,
    )
    return authority, store, application, events


# ---------------------------------------------------------------------------
# T0 exact: declaration surface (fail-closed)
# ---------------------------------------------------------------------------


def test_valid_definition_has_pinned_digest(tmp_path: Path):
    definition = workspace_read_tool_definition()
    assert definition.tool_id == "bots5.workspace_read"
    assert definition.capability_kind == "workspace-read"
    assert len(definition.digest) == 64
    assert int(definition.digest, 16) >= 0


def test_invalid_schema_is_refused_at_construction():
    with pytest.raises(ToolSchemaError):
        ToolDefinition(
            tool_id="bad.tool",
            version=1,
            description="x",
            schema={"type": "array"},
            capability_kind="workspace-read",
        )
    with pytest.raises(ToolSchemaError):
        ToolDefinition(
            tool_id="bad.tool",
            version=1,
            description="x",
            schema={
                "type": "object",
                "properties": {"p": {"type": "object"}},
            },
            capability_kind="workspace-read",
        )
    with pytest.raises(ToolSchemaError):
        ToolDefinition(
            tool_id="bad.tool",
            version=1,
            description="x",
            schema={"type": "object", "unknown_key": 1},
            capability_kind="workspace-read",
        )


def test_invalid_tool_id_is_refused():
    for bad in ("", "no-dots-but-bad char!", "a..b", "a b", "x" * 200):
        with pytest.raises(ToolSchemaError):
            ToolDefinition(
                tool_id=bad,
                version=1,
                description="x",
                schema={"type": "object"},
                capability_kind="workspace-read",
            )


# ---------------------------------------------------------------------------
# T0 exact: registry collision / lookup
# ---------------------------------------------------------------------------


def test_registry_duplicate_is_collision_never_last_wins(tmp_path: Path):
    registry = _registry(tmp_path)
    with pytest.raises(ToolCollision):
        registry.register(
            workspace_read_tool_definition(),
            ToolExecutor("bots5.workspace_read", lambda a, g: {}),
        )


def test_registry_unknown_tool_lookup(tmp_path: Path):
    registry = _registry(tmp_path)
    with pytest.raises(ToolNotFound):
        registry.definition("bots5.absent")
    with pytest.raises(ToolNotFound):
        registry.executor("bots5.absent")


def test_registry_executor_definition_mismatch(tmp_path: Path):
    registry = ToolRegistry()
    with pytest.raises(ToolSchemaError):
        registry.register(
            workspace_read_tool_definition(),
            ToolExecutor("bots5.other", lambda a, g: {}),
        )


# ---------------------------------------------------------------------------
# T0 exact: invocation results and refusals
# ---------------------------------------------------------------------------


def test_invocation_success_consumes_one_budget_unit(tmp_path: Path):
    (tmp_path / "hello.txt").write_text("hello world", encoding="utf-8")
    invoker, _ca, grant = _invoker(tmp_path)
    before = grant.remaining_effects
    result = invoker.invoke(
        "bots5.workspace_read", {"path": "hello.txt"}, grant=grant, workspace_root=tmp_path
    )
    assert result.state is ToolState.SUCCEEDED
    assert result.payload is not None
    assert result.payload["content"] == "hello world"
    assert result.payload["sha256"] == hashlib.sha256(b"hello world").hexdigest()
    assert grant.remaining_effects == before - 1


def test_unknown_tool_is_refused_without_effect(tmp_path: Path):
    invoker, _ca, grant = _invoker(tmp_path)
    before = grant.remaining_effects
    result = invoker.invoke(
        "bots5.absent", {"path": "x"}, grant=grant, workspace_root=tmp_path
    )
    assert result.state is ToolState.REFUSED
    assert result.refusal_reason is DenialReason.NO_GRANT
    assert grant.remaining_effects == before


def test_malformed_arguments_are_refused(tmp_path: Path):
    invoker, _ca, grant = _invoker(tmp_path)
    before = grant.remaining_effects
    unknown_arg = invoker.invoke(
        "bots5.workspace_read", {"nope": 1}, grant=grant, workspace_root=tmp_path
    )
    assert unknown_arg.state is ToolState.REFUSED
    missing = invoker.invoke(
        "bots5.workspace_read", {}, grant=grant, workspace_root=tmp_path
    )
    assert missing.state is ToolState.REFUSED
    wrong_type = invoker.invoke(
        "bots5.workspace_read", {"path": 42}, grant=grant, workspace_root=tmp_path
    )
    assert wrong_type.state is ToolState.REFUSED
    assert grant.remaining_effects == before


def test_scope_escape_is_refused_before_effect(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    invoker, _ca, grant = _invoker(workspace)
    before = grant.remaining_effects
    result = invoker.invoke(
        "bots5.workspace_read", {"path": "../escape.txt"}, grant=grant, workspace_root=workspace
    )
    assert result.state is ToolState.REFUSED
    assert result.refusal_reason is DenialReason.SCOPE_MISMATCH
    assert grant.remaining_effects == before


def test_exhausted_budget_is_refused(tmp_path: Path):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    invoker, ca, _ = _invoker(tmp_path)
    tiny = _grant(ca, tmp_path, effects=1)
    first = invoker.invoke(
        "bots5.workspace_read", {"path": "a.txt"}, grant=tiny, workspace_root=tmp_path
    )
    assert first.state is ToolState.SUCCEEDED
    second = invoker.invoke(
        "bots5.workspace_read", {"path": "a.txt"}, grant=tiny, workspace_root=tmp_path
    )
    assert second.state is ToolState.REFUSED
    assert second.refusal_reason is DenialReason.EXHAUSTED


def test_foreign_grant_is_refused(tmp_path: Path):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    invoker, ca, _ = _invoker(tmp_path)
    other_ca = CapabilityAuthority()
    foreign = _grant(other_ca, tmp_path)
    result = invoker.invoke(
        "bots5.workspace_read", {"path": "a.txt"}, grant=foreign, workspace_root=tmp_path
    )
    assert result.state is ToolState.REFUSED
    assert result.refusal_reason is DenialReason.NO_GRANT


def test_executor_failure_is_failed_and_unknown_is_truthful(tmp_path: Path):
    registry = ToolRegistry()

    def boom(_args, _grant):
        raise ToolEffectError("bounded effect failed")

    registry.register(
        ToolDefinition(
            tool_id="test.boom",
            version=1,
            description="always fails",
            schema={"type": "object", "properties": {}},
            capability_kind="workspace-read",
        ),
        ToolExecutor("test.boom", boom),
    )
    ca = CapabilityAuthority()
    grant = _grant(ca, tmp_path)
    invoker = ToolInvoker(registry=registry, authority=ca, id_factory=_ids())
    failed = invoker.invoke("test.boom", {}, grant=grant, workspace_root=tmp_path)
    assert failed.state is ToolState.FAILED
    assert "bounded effect failed" in (failed.error or "")

    def uncertain(_args, _grant):
        raise ToolUncertainOutcome("outcome not established")

    registry2 = ToolRegistry()
    registry2.register(
        ToolDefinition(
            tool_id="test.uncertain",
            version=1,
            description="unknown outcome",
            schema={"type": "object", "properties": {}},
            capability_kind="workspace-read",
        ),
        ToolExecutor("test.uncertain", uncertain),
    )
    invoker2 = ToolInvoker(registry=registry2, authority=ca, id_factory=_ids())
    unknown = invoker2.invoke("test.uncertain", {}, grant=grant, workspace_root=tmp_path)
    assert unknown.state is ToolState.UNKNOWN
    assert unknown.error is None
    # UNKNOWN is terminal and never rewritten.
    with pytest.raises(ToolSchemaError):
        ToolResult(
            invocation_id="x",
            tool_id="test.uncertain",
            state=ToolState.SUCCEEDED,
            error="late rewrite",
        )


# ---------------------------------------------------------------------------
# T0 exact: workspace-bounded local effects
# ---------------------------------------------------------------------------


def test_workspace_read_nested_and_sha256(tmp_path: Path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("nested content", encoding="utf-8")
    payload = workspace_read_file(tmp_path, "sub/b.txt")
    assert payload["content"] == "nested content"
    assert payload["bytes"] == len("nested content")
    assert payload["sha256"] == hashlib.sha256(b"nested content").hexdigest()


def test_workspace_read_refuses_traversal_and_absolute(tmp_path: Path):
    with pytest.raises((ToolEffectError, Exception)):
        workspace_read_file(tmp_path, "../x")
    with pytest.raises(Exception):
        workspace_read_file(tmp_path, "/etc/passwd")
    with pytest.raises(Exception):
        workspace_read_file(tmp_path, "..")
    with pytest.raises(Exception):
        workspace_read_file(tmp_path, "")
    with pytest.raises(Exception):
        workspace_read_file(tmp_path, "a\x00b")


def test_workspace_read_refuses_symlink_escape(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "link").symlink_to(outside / "secret.txt")
    with pytest.raises(ToolEffectError):
        workspace_read_file(workspace, "link")


def test_workspace_read_refuses_non_regular_and_directory(tmp_path: Path):
    (tmp_path / "adir").mkdir()
    with pytest.raises(ToolEffectError):
        workspace_read_file(tmp_path, "adir")
    import os as _os

    fifo = tmp_path / "afifo"
    _os.mkfifo(fifo)
    with pytest.raises(ToolEffectError):
        workspace_read_file(tmp_path, "afifo")
    with pytest.raises(ToolEffectError):
        workspace_read_file(tmp_path, "missing.txt")


def test_workspace_read_enforces_byte_bound(tmp_path: Path):
    (tmp_path / "big.txt").write_bytes(b"a" * 4096)
    with pytest.raises(ToolEffectError):
        workspace_read_file(tmp_path, "big.txt", max_bytes=1024)
    payload = workspace_read_file(tmp_path, "big.txt", max_bytes=4096)
    assert payload["bytes"] == 4096


def test_workspace_read_refuses_non_utf8(tmp_path: Path):
    (tmp_path / "bin.dat").write_bytes(b"\xff\xfe\x00binary")
    with pytest.raises(ToolEffectError):
        workspace_read_file(tmp_path, "bin.dat")


# ---------------------------------------------------------------------------
# T1 subsystem: application wiring
# ---------------------------------------------------------------------------


def _application(root: Path, *, capability_authority=None, workspace_root=None):
    authority = DataRootAuthority(root.absolute()).acquire()
    store = authority.open_store()
    ids = Uuid7Factory()
    clock = SystemClock()
    events = EventBus(clock, ids, queue_size=32)
    application = BotsApplication(
        store,
        events,
        FakeStreamingBackend(),
        ids=ids,
        clock=clock,
        capability_authority=capability_authority,
        workspace_root=workspace_root,
    )
    return authority, store, application, events


def test_application_without_capability_authority_exposes_no_tools(tmp_path: Path):
    authority, store, application, _events = _application(tmp_path / "root")
    try:
        assert asyncio.run(application.list_tools()) == ()
        with pytest.raises(StateError, match="not configured"):
            asyncio.run(
                application.invoke_tool(
                    "bots5.workspace_read", {"path": "x"}, grant=None
                )
            )
    finally:
        asyncio.run(application.close())


def test_application_invokes_tool_and_publishes_event(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "hello.txt").write_text("hi workspace", encoding="utf-8")
    ca = CapabilityAuthority()
    authority, store, application, events = _application(
        tmp_path / "root", capability_authority=ca, workspace_root=workspace
    )
    try:
        tools = asyncio.run(application.list_tools())
        assert [tool.tool_id for tool in tools] == ["bots5.workspace_read"]
        grant = _grant(ca, workspace)
        subscription = events.subscribe()

        async def scenario():
            result = await application.invoke_tool(
                "bots5.workspace_read",
                {"path": "hello.txt"},
                grant=grant,
                workspace_root=workspace,
            )
            assert result.state is ToolState.SUCCEEDED
            assert result.payload is not None
            assert result.payload["content"] == "hi workspace"
            refusal = await application.invoke_tool(
                "bots5.workspace_read",
                {"path": "../escape"},
                grant=grant,
                workspace_root=workspace,
            )
            assert refusal.state is ToolState.REFUSED
            first = await asyncio.wait_for(subscription.__anext__(), 5)
            second = await asyncio.wait_for(subscription.__anext__(), 5)
            return result, refusal, first, second

        result, refusal, first, second = asyncio.run(scenario())
        assert first.kind == "tool_invoked"
        assert first.payload["state"] == "SUCCEEDED"
        assert first.payload["invocation_id"] == result.invocation_id
        assert second.kind == "tool_invoked"
        assert second.payload["state"] == "REFUSED"
        assert second.payload["refusal_reason"] == "SCOPE_MISMATCH"
        assert second.payload["invocation_id"] == refusal.invocation_id
        subscription.close()
    finally:
        asyncio.run(application.close())
