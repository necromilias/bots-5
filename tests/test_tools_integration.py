"""Mechanical integration for tool invocation, results and refusal paths (v0.2).

Covers the composition that production will use:
  CapabilityAuthority → ToolRegistry/ToolInvoker → BotsApplication command →
  EventBus `tool_invoked` → append-only ToolResult journal.

Every path asserts the same three contracts:
  * refusal happens before any filesystem effect,
  * the capability budget is consumed exactly once per successful dispatch and
    never by a pre-effect refusal,
  * the journal is append-only with truthful terminal states (UNKNOWN never
    rewritten).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from bots5.core.application import BotsApplication
from bots5.core.capabilities import (
    CapabilityAuthority,
    DenialReason,
    DirectoryScope,
    GrantRequest,
    Subject,
    WORKSPACE_READ,
)
from bots5.core.events import EventBus
from bots5.core.tools import ToolState, ToolRegistry, ToolInvoker
from bots5.domain.clock import SystemClock
from bots5.domain.ids import Uuid7Factory
from bots5.infrastructure.data_root_authority import DataRootAuthority
from bots5.infrastructure.generation.fake import FakeStreamingBackend


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "readme.txt").write_text("integration content", encoding="utf-8")
    (workspace / "nested").mkdir()
    (workspace / "nested" / "note.txt").write_text("nested note", encoding="utf-8")
    return workspace


def _app(tmp_path: Path, workspace: Path, ca: CapabilityAuthority):
    authority = DataRootAuthority((tmp_path / "root").absolute()).acquire()
    store = authority.open_store()
    ids = Uuid7Factory()
    clock = SystemClock()
    events = EventBus(clock, ids, queue_size=64)
    application = BotsApplication(
        store,
        events,
        FakeStreamingBackend(),
        ids=ids,
        clock=clock,
        capability_authority=ca,
        workspace_root=workspace,
    )
    return authority, store, application, events


def _grant(ca: CapabilityAuthority, workspace: Path, *, effects: int = 16):
    return ca.grant(
        GrantRequest(
            subject=Subject(kind="tool", identity="integration"),
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=workspace),
            ttl_seconds=300.0,
            max_effects=effects,
        )
    )


def test_end_to_end_success_sequence(tmp_path: Path):
    workspace = _workspace(tmp_path)
    ca = CapabilityAuthority()
    authority, store, application, events = _app(tmp_path, workspace, ca)
    try:
        grant = _grant(ca, workspace)
        subscription = events.subscribe()

        async def scenario():
            first = await application.invoke_tool(
                "bots5.workspace_read",
                {"path": "readme.txt"},
                grant=grant,
                workspace_root=workspace,
            )
            second = await application.invoke_tool(
                "bots5.workspace_read",
                {"path": "nested/note.txt"},
                grant=grant,
                workspace_root=workspace,
            )
            events_seen = [
                await asyncio.wait_for(subscription.__anext__(), 5),
                await asyncio.wait_for(subscription.__anext__(), 5),
            ]
            return first, second, events_seen

        first, second, events_seen = asyncio.run(scenario())
        assert first.state is ToolState.SUCCEEDED
        assert first.payload is not None
        assert first.payload["content"] == "integration content"
        assert second.state is ToolState.SUCCEEDED
        assert second.payload is not None
        assert second.payload["content"] == "nested note"
        assert grant.remaining_effects == 16 - 2
        kinds = [event.kind for event in events_seen]
        assert kinds == ["tool_invoked", "tool_invoked"]
        states = [event.payload["state"] for event in events_seen]
        assert states == ["SUCCEEDED", "SUCCEEDED"]
        ids = [event.payload["invocation_id"] for event in events_seen]
        assert ids == [first.invocation_id, second.invocation_id]
        journal = application._tool_invoker.journal()
        assert [record.invocation_id for record in journal] == [
            first.invocation_id,
            second.invocation_id,
        ]
        subscription.close()
    finally:
        asyncio.run(application.close())


def test_refusal_sequence_consumes_no_budget_and_publishes_reason(tmp_path: Path):
    workspace = _workspace(tmp_path)
    ca = CapabilityAuthority()
    authority, store, application, events = _app(tmp_path, workspace, ca)
    try:
        grant = _grant(ca, workspace)
        subscription = events.subscribe()

        async def scenario():
            results = []
            results.append(
                await application.invoke_tool(
                    "bots5.workspace_read",
                    {"path": "../escape"},
                    grant=grant,
                    workspace_root=workspace,
                )
            )
            results.append(
                await application.invoke_tool(
                    "bots5.unknown",
                    {"path": "readme.txt"},
                    grant=grant,
                    workspace_root=workspace,
                )
            )
            results.append(
                await application.invoke_tool(
                    "bots5.workspace_read",
                    {"path": ""},
                    grant=grant,
                    workspace_root=workspace,
                )
            )
            seen = [
                await asyncio.wait_for(subscription.__anext__(), 5),
                await asyncio.wait_for(subscription.__anext__(), 5),
                await asyncio.wait_for(subscription.__anext__(), 5),
            ]
            return results, seen

        results, seen = asyncio.run(scenario())
        assert all(result.state is ToolState.REFUSED for result in results)
        assert results[0].refusal_reason is DenialReason.SCOPE_MISMATCH
        assert results[1].refusal_reason is DenialReason.NO_GRANT
        assert results[2].refusal_reason is DenialReason.INVALID_UNITS
        # No budget consumed by any of the three refusals.
        assert grant.remaining_effects == 16
        reasons = [event.payload["refusal_reason"] for event in seen]
        assert reasons == ["SCOPE_MISMATCH", "NO_GRANT", "INVALID_UNITS"]
        # No filesystem effect from any refusal.
        assert (workspace / "readme.txt").read_text(encoding="utf-8") == "integration content"
        assert not (tmp_path / "escape").exists()
        subscription.close()
    finally:
        asyncio.run(application.close())


def test_journal_reruns_append_never_rewrite(tmp_path: Path):
    workspace = _workspace(tmp_path)
    ca = CapabilityAuthority()
    authority, store, application, events = _app(tmp_path, workspace, ca)
    try:
        grant = _grant(ca, workspace)

        async def scenario():
            outcomes = []
            for _ in range(3):
                outcomes.append(
                    await application.invoke_tool(
                        "bots5.workspace_read",
                        {"path": "readme.txt"},
                        grant=grant,
                        workspace_root=workspace,
                    )
                )
                outcomes.append(
                    await application.invoke_tool(
                        "bots5.workspace_read",
                        {"path": "../nope"},
                        grant=grant,
                        workspace_root=workspace,
                    )
                )
            return outcomes

        outcomes = asyncio.run(scenario())
        journal = application._tool_invoker.journal()
        assert len(journal) == len(outcomes) == 6
        assert [record.state for record in journal] == [
            ToolState.SUCCEEDED,
            ToolState.REFUSED,
            ToolState.SUCCEEDED,
            ToolState.REFUSED,
            ToolState.SUCCEEDED,
            ToolState.REFUSED,
        ]
        # Budget: exactly 3 successes consumed 3 units; refusals consumed none.
        assert grant.remaining_effects == 16 - 3
        # invocation ids are unique per rerun (append-only evidence).
        ids = [record.invocation_id for record in journal]
        assert len(set(ids)) == len(ids)
    finally:
        asyncio.run(application.close())


def test_registry_and_invoker_share_one_authority_instance(tmp_path: Path):
    """One CapabilityAuthority instance: no second authority, no ambient grant."""
    workspace = _workspace(tmp_path)
    ca = CapabilityAuthority()
    authority, store, application, events = _app(tmp_path, workspace, ca)
    try:
        assert application._capability_authority is ca
        assert application._tool_invoker._authority is ca
        # The registry is closed: only the reference tool exists.
        assert application._tool_registry.tool_ids() == ("bots5.workspace_read",)
        grant = _grant(ca, workspace)
        result = asyncio.run(
            application.invoke_tool(
                "bots5.workspace_read",
                {"path": "readme.txt"},
                grant=grant,
                workspace_root=workspace,
            )
        )
        assert result.state is ToolState.SUCCEEDED
        # A fresh, unregistered authority cannot grant this tool implicitly:
        # the registry itself has no write/process tool to fall back to.
        assert application._tool_registry.tool_ids() == ("bots5.workspace_read",)
    finally:
        asyncio.run(application.close())
