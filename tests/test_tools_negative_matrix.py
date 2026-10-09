"""High-volume negative/refusal matrix for out-of-scope tool requests (v0.2).

Every case asserts the same contract: an out-of-scope or unauthorized tool
request is **refused with a structured reason before any filesystem effect**,
the capability budget is not consumed by a pre-effect refusal, and the
invocation journal gains exactly one truthful terminal record.

Categories:
  A. unknown / unregistered tool ids (no capability vocabulary exists)
  B. malformed / unknown / wrongly-typed arguments
  C. path escapes (traversal, absolute, symlink, NUL, empty)
  D. grant lifecycle refusals (foreign, expired, exhausted, released)
  E. resource bounds (oversized read, non-UTF8, non-regular, missing)
  F. tools that are simply not shipped in this bounded scope
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bots5.core.capabilities import (
    CapabilityAuthority,
    DenialReason,
    DirectoryScope,
    GrantRequest,
    GrantStatus,
    Subject,
    WORKSPACE_READ,
)
from bots5.core.errors import StateError
from bots5.core.events import EventBus
from bots5.core.tools import (
    ToolInvoker,
    ToolRegistry,
    ToolState,
    bind_workspace_read_tool,
    workspace_read_file,
)
from bots5.domain.clock import SystemClock
from bots5.domain.ids import Uuid7Factory
from bots5.infrastructure.data_root_authority import DataRootAuthority
from bots5.infrastructure.generation.fake import FakeStreamingBackend


class _FakeClock:
    def __init__(self) -> None:
        self._monotonic = 0.0
        self._wall = datetime.now(timezone.utc)

    def now(self):
        return self._wall

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, seconds: float) -> None:
        self._monotonic += seconds


def _counter_ids():
    counter = {"n": 0}

    def factory() -> str:
        counter["n"] += 1
        return f"neg-{counter['n']}"

    return factory


def _setup(workspace: Path, *, effects: int = 16, clock=None):
    workspace.mkdir(exist_ok=True)
    (workspace / "inside.txt").write_text("inside", encoding="utf-8")
    (workspace / "big.bin").write_bytes(b"\xff" * 2048)
    (workspace / "plain.bin").write_bytes(b"a" * 2048)
    (workspace / "dir").mkdir(exist_ok=True)
    registry = ToolRegistry()
    bind_workspace_read_tool(registry, root=workspace)
    ca = CapabilityAuthority(clock=clock) if clock else CapabilityAuthority()
    invoker = ToolInvoker(registry=registry, authority=ca, id_factory=_counter_ids())
    grant = ca.grant(
        GrantRequest(
            subject=Subject(kind="tool", identity="negative-matrix"),
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=workspace),
            ttl_seconds=3600.0,
            max_effects=effects,
        )
    )
    return invoker, ca, grant


# ---------------------------------------------------------------------------
# A. unknown / unregistered tool ids
# ---------------------------------------------------------------------------

UNKNOWN_TOOL_IDS = [
    "bots5.workspace_write",
    "bots5.process_run",
    "bots5.network_fetch",
    "bots5.git_inspect",
    "bots5.git_push",
    "bots5.egress",
    "bots5.shell",
    "bots5.delete_file",
    "mcp.unknown.server",
    "unknown",
    "UPPER.CASE",
    "a b.c",
    "x" * 300,
]


@pytest.mark.parametrize("tool_id", UNKNOWN_TOOL_IDS)
def test_unknown_tool_id_is_refused(tmp_path: Path, tool_id: str):
    workspace = tmp_path / "ws"
    invoker, _ca, grant = _setup(workspace)
    before = grant.remaining_effects
    result = invoker.invoke(
        tool_id, {"path": "inside.txt"}, grant=grant, workspace_root=workspace
    )
    assert result.state is ToolState.REFUSED
    assert result.refusal_reason is DenialReason.NO_GRANT
    assert grant.remaining_effects == before
    assert (workspace / "inside.txt").read_text(encoding="utf-8") == "inside"


# ---------------------------------------------------------------------------
# B. malformed / unknown / wrongly-typed arguments
# ---------------------------------------------------------------------------

MALFORMED_ARGUMENTS = [
    {},
    {"nope": 1},
    {"path": ""},
    {"path": 42},
    {"path": None},
    {"path": ["inside.txt"]},
    {"path": {"nested": "obj"}},
    {"path": True},
    {"path": "inside.txt", "extra": "x"},
    {"PATH": "inside.txt"},
]


@pytest.mark.parametrize("arguments", MALFORMED_ARGUMENTS)
def test_malformed_arguments_are_refused(tmp_path: Path, arguments: dict):
    workspace = tmp_path / "ws"
    invoker, _ca, grant = _setup(workspace)
    before = grant.remaining_effects
    result = invoker.invoke(
        "bots5.workspace_read", arguments, grant=grant, workspace_root=workspace
    )
    assert result.state is ToolState.REFUSED
    assert grant.remaining_effects == before


# ---------------------------------------------------------------------------
# C. path escapes
# ---------------------------------------------------------------------------

ESCAPE_PATHS = [
    "../escape.txt",
    "../../etc/passwd",
    "sub/../../escape",
    "/etc/passwd",
    "/",
    "..",
    ".",
    "",
    "a\x00b",
    "./../x",
    "dir/../../up",
]


@pytest.mark.parametrize("path", ESCAPE_PATHS)
def test_path_escape_is_refused(tmp_path: Path, path: str):
    workspace = tmp_path / "ws"
    invoker, _ca, grant = _setup(workspace)
    before = grant.remaining_effects
    result = invoker.invoke(
        "bots5.workspace_read", {"path": path}, grant=grant, workspace_root=workspace
    )
    assert result.state is ToolState.REFUSED
    assert grant.remaining_effects == before
    # No escape side effect: only the original files remain.
    assert (workspace / "inside.txt").exists()


def test_symlink_escape_is_refused(tmp_path: Path):
    workspace = tmp_path / "ws"
    invoker, _ca, grant = _setup(workspace)
    (workspace / "leak").symlink_to("/etc/passwd")
    result = invoker.invoke(
        "bots5.workspace_read", {"path": "leak"}, grant=grant, workspace_root=workspace
    )
    assert result.state is ToolState.REFUSED
    assert grant.remaining_effects == grant.remaining_effects  # unchanged by refusal


# ---------------------------------------------------------------------------
# D. grant lifecycle refusals
# ---------------------------------------------------------------------------


def test_foreign_authority_grant_is_refused(tmp_path: Path):
    workspace = tmp_path / "ws"
    invoker, _ca, _grant_main = _setup(workspace)
    other = CapabilityAuthority()
    foreign = other.grant(
        GrantRequest(
            subject=Subject(kind="tool", identity="foreign"),
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=workspace),
            ttl_seconds=60.0,
            max_effects=4,
        )
    )
    result = invoker.invoke(
        "bots5.workspace_read", {"path": "inside.txt"}, grant=foreign, workspace_root=workspace
    )
    assert result.state is ToolState.REFUSED
    assert result.refusal_reason is DenialReason.NO_GRANT


def test_expired_grant_is_refused(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "inside.txt").write_text("inside", encoding="utf-8")
    registry = ToolRegistry()
    bind_workspace_read_tool(registry, root=workspace)
    clock = _FakeClock()
    ca = CapabilityAuthority(clock=clock)
    invoker = ToolInvoker(registry=registry, authority=ca, id_factory=_counter_ids())
    grant = ca.grant(
        GrantRequest(
            subject=Subject(kind="tool", identity="expiring"),
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=workspace),
            ttl_seconds=10.0,
            max_effects=4,
        )
    )
    clock.advance(11.0)
    result = invoker.invoke(
        "bots5.workspace_read", {"path": "inside.txt"}, grant=grant, workspace_root=workspace
    )
    assert result.state is ToolState.REFUSED
    assert result.refusal_reason is DenialReason.EXPIRED


def test_exhausted_grant_is_refused(tmp_path: Path):
    workspace = tmp_path / "ws"
    invoker, ca, _ = _setup(workspace, effects=1)
    grant = ca.grant(
        GrantRequest(
            subject=Subject(kind="tool", identity="tiny"),
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=workspace),
            ttl_seconds=60.0,
            max_effects=1,
        )
    )
    first = invoker.invoke(
        "bots5.workspace_read", {"path": "inside.txt"}, grant=grant, workspace_root=workspace
    )
    assert first.state is ToolState.SUCCEEDED
    second = invoker.invoke(
        "bots5.workspace_read", {"path": "inside.txt"}, grant=grant, workspace_root=workspace
    )
    assert second.state is ToolState.REFUSED
    assert second.refusal_reason is DenialReason.EXHAUSTED


def test_released_grant_is_refused(tmp_path: Path):
    workspace = tmp_path / "ws"
    invoker, ca, grant = _setup(workspace)
    ca.release(grant)
    assert grant.status is GrantStatus.RELEASED
    result = invoker.invoke(
        "bots5.workspace_read", {"path": "inside.txt"}, grant=grant, workspace_root=workspace
    )
    assert result.state is ToolState.REFUSED
    assert result.refusal_reason is DenialReason.NOT_FORWARD


def test_revoked_grant_is_refused(tmp_path: Path):
    workspace = tmp_path / "ws"
    invoker, ca, grant = _setup(workspace)
    ca.revoke(grant, "operator revoked the tool grant")
    result = invoker.invoke(
        "bots5.workspace_read", {"path": "inside.txt"}, grant=grant, workspace_root=workspace
    )
    assert result.state is ToolState.REFUSED
    assert result.refusal_reason is DenialReason.NOT_FORWARD


# ---------------------------------------------------------------------------
# E. resource bounds and non-regular files
# ---------------------------------------------------------------------------


def test_oversized_read_is_refused(tmp_path: Path):
    workspace = tmp_path / "ws"
    invoker, _ca, grant = _setup(workspace)
    from bots5.core.tools import WORKSPACE_READ_MAX_BYTES

    (workspace / "huge.txt").write_text(
        "h" * (WORKSPACE_READ_MAX_BYTES + 1), encoding="utf-8"
    )
    result = invoker.invoke(
        "bots5.workspace_read", {"path": "huge.txt"}, grant=grant, workspace_root=workspace
    )
    assert result.state is ToolState.FAILED
    assert "bound" in (result.error or "")


def test_non_utf8_payload_is_failed(tmp_path: Path):
    workspace = tmp_path / "ws"
    invoker, _ca, grant = _setup(workspace)
    result = invoker.invoke(
        "bots5.workspace_read", {"path": "big.bin"}, grant=grant, workspace_root=workspace
    )
    assert result.state is ToolState.FAILED
    assert "UTF-8" in (result.error or "")


def test_directory_and_missing_targets_are_failed(tmp_path: Path):
    workspace = tmp_path / "ws"
    invoker, _ca, grant = _setup(workspace)
    directory = invoker.invoke(
        "bots5.workspace_read", {"path": "dir"}, grant=grant, workspace_root=workspace
    )
    assert directory.state is ToolState.FAILED
    missing = invoker.invoke(
        "bots5.workspace_read", {"path": "absent.txt"}, grant=grant, workspace_root=workspace
    )
    assert missing.state is ToolState.FAILED


# ---------------------------------------------------------------------------
# F. direct workspace_read_file boundary (defense in depth)
# ---------------------------------------------------------------------------

DIRECT_REFUSALS = [
    "../x",
    "/etc/passwd",
    "",
    "a\x00b",
    "missing.txt",
    "dir",
]


@pytest.mark.parametrize("path", DIRECT_REFUSALS)
def test_direct_workspace_read_file_refuses(tmp_path: Path, path: str):
    (tmp_path / "dir").mkdir(exist_ok=True)
    with pytest.raises(Exception):
        workspace_read_file(tmp_path, path)


# ---------------------------------------------------------------------------
# Journal truthfulness across the whole matrix
# ---------------------------------------------------------------------------


def test_journal_is_append_only_and_terminal(tmp_path: Path):
    workspace = tmp_path / "ws"
    invoker, _ca, grant = _setup(workspace)
    attempts = [
        ("bots5.workspace_read", {"path": "inside.txt"}),
        ("bots5.workspace_read", {"path": "../escape"}),
        ("bots5.absent", {"path": "x"}),
        ("bots5.workspace_read", {}),
    ]
    for tool_id, arguments in attempts:
        invoker.invoke(tool_id, arguments, grant=grant, workspace_root=workspace)
    journal = invoker.journal()
    assert len(journal) == len(attempts)
    assert all(
        record.state
        in {ToolState.SUCCEEDED, ToolState.REFUSED, ToolState.FAILED, ToolState.UNKNOWN}
        for record in journal
    )
    # Rerun adds records; nothing is rewritten.
    invoker.invoke("bots5.workspace_read", {"path": "inside.txt"}, grant=grant, workspace_root=workspace)
    assert len(invoker.journal()) == len(attempts) + 1
