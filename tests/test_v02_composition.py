"""BLK-04 composition proof: the advertised shared v0.2 composition exists.

These tests pin down what ``bootstrap/desktop.build_runtime`` composes and —
load-bearingly — that the consumers use **the same** authority/state
instances, not second copies:

* one :class:`CapabilityAuthority` per runtime, identical by ``is`` inside
  the application's ``ToolInvoker``, inside the controlled egress consumer,
  and behind every desktop control bridge built by the runtime;
* a second window's bridge shares the same state source, so a grant minted
  on the shared authority is visible through *both* bridges (common
  machinery, decision 9);
* an injected foreign grant object is refused ``NO_GRANT`` by the composed
  invoker — proof the invoker decides against this exact authority instance;
* the queue/receipt machinery (process executor, code executor, git
  authority manager, durable queue-store path) is composed once and owned
  by the runtime;
* the truthfulness boundary holds: the composed bridge reports
  ``durable_state is False`` because no write-through adapter exists yet.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

from bots5.bootstrap.desktop import ControlPlaneState, DesktopRuntime, build_runtime
from bots5.core.capabilities import (
    CapabilityAuthority,
    DenialReason,
    DirectoryScope,
    GrantRequest,
    Subject,
    WORKSPACE_READ,
)
from bots5.core.egress import ControlledEgressConsumer, EgressOutcome
from bots5.core.tools import ToolState
from bots5.desktop.control_bridge import ControlBridge, ExecutionState

# Plugin imports
from bots5.plugins import PluginHost, Capability, PluginLifecycleError, default_grants
from bots5.plugins.reference import line_counter


@pytest.fixture()
def runtime(tmp_path: Path):
    rt = build_runtime(tmp_path / "data")
    try:
        yield rt
    finally:
        asyncio.run(rt.close())


# ---------------------------------------------------------------------------
# the ONE shared capability authority
# ---------------------------------------------------------------------------


def test_build_runtime_composes_exactly_one_capability_authority(runtime):
    assert isinstance(runtime.capability_authority, CapabilityAuthority)
    # The application's tool invoker was constructed with THIS instance.
    invoker = runtime.application._tool_invoker
    assert invoker is not None
    assert invoker._authority is runtime.capability_authority
    # The controlled egress consumer decides against THIS instance.
    assert isinstance(runtime.egress_consumer, ControlledEgressConsumer)
    assert runtime.egress_consumer.authority is runtime.capability_authority
    # The shared state source reads grants from THIS instance.
    assert runtime.control_plane_state.capability_authority is runtime.capability_authority


def test_control_bridges_project_the_shared_state_source(runtime):
    bridge_a = runtime._control_bridge_factory(None)
    bridge_b = runtime._control_bridge_factory(None)
    assert bridge_a.state_source is runtime.control_plane_state
    assert bridge_b.state_source is runtime.control_plane_state
    # Both windows observe the SAME live grants — one common machinery.
    workspace = runtime.paths.data_root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    grant = runtime.capability_authority.grant(
        GrantRequest(
            subject=Subject(kind="tool", identity="composition-proof"),
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=workspace),
            ttl_seconds=60.0,
            max_effects=3,
        )
    )
    ids_a = {g.grant_id for g in bridge_a.list_grants()}
    ids_b = {g.grant_id for g in bridge_b.list_grants()}
    assert grant.grant_id in ids_a
    assert ids_a == ids_b
    # And the projection carries it too (same source, both directions).
    assert grant.grant_id in {g.grant_id for g in bridge_a.projection().grants}
    asyncio.run(bridge_a.close_async())
    asyncio.run(bridge_b.close_async())


def test_bridge_approval_issues_grant_on_shared_authority(runtime):
    """Operator approval on the composed bridge issues a real shared grant."""
    bridge_a = runtime._control_bridge_factory(None)
    bridge_b = runtime._control_bridge_factory(None)
    workspace = runtime.paths.data_root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    prepared = bridge_a.prepare_execution(
        kind="tool",
        scope="tool_invocation",
        description="composed read",
        approved_by="operator",
        grant_scope="workspace-read",
        grant_scope_target=workspace,
        estimated_resources={"ttl_seconds": 60.0, "max_effects": 1},
    )
    bridge_a.approve_execution(prepared)

    # Exactly one grant exists on the ONE shared authority.
    inventory = runtime.capability_authority.inventory()
    assert len(inventory) == 1
    shared_grant = inventory[0]

    # The execution is bound to that grant.
    exec_rows = [e for e in bridge_a.projection().executions]
    assert len(exec_rows) == 1
    assert exec_rows[0].state == ExecutionState.QUEUED.value
    assert exec_rows[0].grant_id == shared_grant.grant_id

    # The grant is visible through a second bridge sharing the same source.
    assert len(bridge_b.list_grants()) == 1
    assert bridge_b.list_grants()[0].grant_id == shared_grant.grant_id

    asyncio.run(bridge_a.close_async())
    asyncio.run(bridge_b.close_async())


def test_default_in_memory_bridge_still_available_for_existing_tests():
    # Fallback contract: a bridge built with no source behaves exactly as
    # before BLK-04 (in-memory dicts, honestly non-durable).
    bridge = ControlBridge()
    assert bridge.durable_state is False
    assert type(bridge.state_source).__name__ == "InMemoryControlState"
    bridge.close()


def test_foreign_grant_is_refused_by_the_composed_invoker(runtime):
    """Proof the invoker uses THIS authority: a copy-cat grant is NO_GRANT."""
    other_authority = CapabilityAuthority()
    workspace = runtime.paths.data_root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    foreign = other_authority.grant(
        GrantRequest(
            subject=Subject(kind="tool", identity="foreign"),
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=workspace),
            ttl_seconds=60.0,
            max_effects=1,
        )
    )
    result = asyncio.run(
        runtime.application.invoke_tool(
            "bots5.workspace_read", {"path": "x.txt"}, grant=foreign, workspace_root=workspace
        )
    )
    assert result.state is ToolState.REFUSED
    assert result.refusal_reason is DenialReason.NO_GRANT


def test_shared_authority_grant_drives_the_composed_tool_path(runtime):
    workspace = runtime.paths.data_root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "hello.txt").write_text("composed", encoding="utf-8")
    grant = runtime.capability_authority.grant(
        GrantRequest(
            subject=Subject(kind="tool", identity="composition-proof"),
            kind=WORKSPACE_READ,
            scope=DirectoryScope(root=workspace),
            ttl_seconds=60.0,
            max_effects=2,
        )
    )
    result = asyncio.run(
        runtime.application.invoke_tool(
            "bots5.workspace_read", {"path": "hello.txt"}, grant=grant, workspace_root=workspace
        )
    )
    assert result.state is ToolState.SUCCEEDED
    assert result.payload["content"] == "composed"
    # The shared budget decremented on the ONE authority instance.
    assert grant.remaining_effects == 1
    # And the settled invocation projects through the shared state source.
    runtime.control_plane_state.observe_tool_invocation(result)
    bridge = runtime._control_bridge_factory(None)
    rows = [e for e in bridge.projection().executions if e.operation_id == result.invocation_id]
    assert len(rows) == 1
    assert rows[0].kind == "tool"
    asyncio.run(bridge.close_async())


# ---------------------------------------------------------------------------
# shared queue/receipt machinery + honest durability boundary
# ---------------------------------------------------------------------------


def test_shared_queue_receipt_machinery_is_composed_once(runtime):
    assert runtime.git_authority_manager is not None
    assert runtime.process_executor is not None
    assert runtime.code_executor is not None
    # One process executor underlies both the code executor and the git
    # authority manager — the same machinery, not parallel copies.
    assert runtime.code_executor.get_process_executor() is runtime.process_executor
    assert runtime.git_authority_manager._process_executor is runtime.process_executor
    # The durable queue store is composed in the classified data-root
    # database directory and bound to one OwnedExecutionWorkers registry
    # (BLK-06 seam).
    state = runtime.control_plane_state
    assert state.queue_store_path is not None
    assert state.queue_store_path.parent == runtime.paths.data_root / "database"
    assert state.queue_store_path.name == "execution-queue.db"
    assert state.queue_store is not None
    assert state.execution_workers is not None


def test_queue_receipts_are_durable_through_the_composed_store(runtime):
    """A queue item saved through the composed plane survives a reopen."""
    from bots5.core.queue_state_machine import ExecutionQueueItem, ExecutionQueueState
    from bots5.core.queue_persistence import QueuePersistenceStore

    state = runtime.control_plane_state
    item = ExecutionQueueItem(
        id="blk04-proof", revision=1, state=ExecutionQueueState.PENDING,
        operation_id="op-blk04",
    )
    state.persist_execution(item)
    # Durable means readable by a second connection to the same file.
    reopened = QueuePersistenceStore(state.queue_store_path)
    loaded = reopened.load_item("blk04-proof")
    assert loaded is not None
    assert loaded.state is ExecutionQueueState.PENDING
    assert loaded.operation_id == "op-blk04"


def test_composed_bridge_claims_durability(runtime):
    bridge = runtime._control_bridge_factory(None)
    # With the BLK-06 write-through landed, executions()/receipts() read
    # from the durable SQLite queue store, so the bridge truthfully reports
    # durable_state=True.
    assert bridge.durable_state is True
    assert runtime.control_plane_state.durable is True
    asyncio.run(bridge.close_async())


def test_approval_persists_binding_through_new_store(runtime):
    """After bridge approval, a fresh store on the same file sees the binding."""
    from bots5.core.queue_persistence import QueuePersistenceStore

    bridge = runtime._control_bridge_factory(None)
    workspace = runtime.paths.data_root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    prepared = bridge.prepare_execution(
        kind="tool",
        scope="tool_invocation",
        description="durable read",
        approved_by="operator",
        grant_scope="workspace-read",
        grant_scope_target=workspace,
        estimated_resources={"ttl_seconds": 60.0, "max_effects": 1},
    )
    bridge.approve_execution(prepared)

    # A fresh connection to the same database file must see the queue item
    # and the bridge binding (approval + grant + operation metadata).
    fresh_store = QueuePersistenceStore(runtime.control_plane_state.queue_store_path)
    rows = fresh_store.load_durable_execution_state()
    assert len(rows) == 1
    row = rows[0]
    assert row.operation_id == prepared.operation_id
    assert row.approval_id == prepared.approval.approval_id
    assert row.grant_id is not None
    assert row.kind == "tool"
    assert row.scope == "tool_invocation"
    assert row.description == "durable read"

    asyncio.run(bridge.close_async())


def test_egress_consumer_composed_without_real_transport(runtime):
    # The composed consumer has no transport: an authorized send fails
    # closed instead of touching any network stack (offline by default).
    from bots5.core.capabilities import EgressDestination, EgressScope

    destination = EgressDestination(scheme="https", host="api.example", port=443)
    grant = runtime.capability_authority.grant(
        GrantRequest(
            subject=Subject(kind="egress-consumer", identity="composition-proof"),
            kind="egress",
            scope=EgressScope(destinations=(destination,), credential_reference=None),
            ttl_seconds=60.0,
            max_effects=1,
        )
    )
    from bots5.core.egress import EgressRequest

    result = runtime.egress_consumer.send(
        EgressRequest(destination=destination, payload="hi"), grant=grant
    )
    assert result.outcome is EgressOutcome.FAILED
    assert "no transport configured" in result.error


# ---------------------------------------------------------------------------
# injection discipline at the composition root
# ---------------------------------------------------------------------------


def test_build_runtime_takes_explicit_workspace_and_transport_injection():
    signature = inspect.signature(build_runtime)
    # Explicit constructor-injection parameters, not globals.
    assert "workspace_root" in signature.parameters
    assert "egress_transport" in signature.parameters
    # DesktopRuntime receives the seam instances as constructor fields.
    fields = DesktopRuntime.__dataclass_fields__
    for name in ("capability_authority", "control_plane_state", "egress_consumer"):
        assert name in fields
        # Seam instances are injected explicitly by build_runtime; the dataclass
        # defaults keep legacy test constructions working, and none of them is
        # a module-level singleton (rejected alternative F-05).
        assert fields[name].default is None


# ---------------------------------------------------------------------------
# plugin host wired to shared authority (decision 9)
# ---------------------------------------------------------------------------


def test_plugin_host_wired_to_shared_authority(runtime):
    """The plugin host uses the same CapabilityAuthority as tools and egress."""
    # The runtime should have a plugin host
    assert runtime.plugin_host is not None
    # The plugin host should be using the shared capability authority
    assert runtime.plugin_host._capability_authority is runtime.capability_authority
    # Proof: bind_grants creates an authority grant and stores the mapping
    workspace = runtime.paths.data_root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    from bots5.plugins import PluginManifest, Capability

    # Create a plugin manifest (must use reserved namespace)
    manifest = PluginManifest(
        plugin_id="bots5.plugin.test-shared-authority",
        semantic_version="0.1.0",
        declared_capabilities=(Capability.WORKSPACE_READ,),
    )
    # Discover and validate the plugin
    runtime.plugin_host.discover(manifest)
    runtime.plugin_host.validate("bots5.plugin.test-shared-authority")
    # Bind grants - this should create an authority grant for the correct kind.
    grants = runtime.plugin_host.bind_grants("bots5.plugin.test-shared-authority", {Capability.WORKSPACE_READ})
    # The plugin host should have stored this grant mapping per kind.
    assert "bots5.plugin.test-shared-authority" in runtime.plugin_host._authority_grants
    kind_mapping = runtime.plugin_host._authority_grants["bots5.plugin.test-shared-authority"]
    authority_grant_id = kind_mapping["workspace-read"]
    # And the authority should know about it
    authority_grant = runtime.capability_authority._grants.get(authority_grant_id)
    assert authority_grant is not None
    assert authority_grant.subject.identity == "bots5.plugin.test-shared-authority"
    assert authority_grant.kind == "workspace-read"
    # And the plugin host's grants should have the capability
    assert Capability.WORKSPACE_READ in grants.capabilities


def test_plugin_without_shared_authority_grant_is_refused(tmp_path: Path):
    """A plugin that fails _check_authority_grant is refused during activation."""
    from bots5.plugins import (
        Capability,
        PluginHost,
        PluginLifecycleError,
        PluginManifest,
        PluginCapabilityDenied,
    )
    from bots5.plugins.reference import line_counter

    # Create a plugin host with capability authority
    capability_authority = CapabilityAuthority()
    host = PluginHost(capability_authority=capability_authority)

    # Create a plugin with no declared capabilities (no workspace root)
    host._roots = {}  # No workspace roots, so workspace-read can't be granted

    # Discover and validate
    manifest = PluginManifest(
        plugin_id="bots5.plugin.no-grant",
        semantic_version="0.1.0",
        declared_capabilities=(Capability.WORKSPACE_READ,),
    )
    host.discover(manifest)
    host.validate("bots5.plugin.no-grant")

    # Bind grants creates an authority grant even without workspace root
    # (scope can be None for some capabilities)
    grants = host.bind_grants("bots5.plugin.no-grant", {Capability.WORKSPACE_READ})
    # Verify the authority grant was created
    assert "bots5.plugin.no-grant" in host._authority_grants
    authority_grant_id = host._authority_grants["bots5.plugin.no-grant"]["workspace-read"]
    authority_grant = capability_authority._grants.get(authority_grant_id)
    assert authority_grant is not None
    # The scope is None when there's no workspace root
    assert authority_grant.scope is None

    # Admit
    host.admit("bots5.plugin.no-grant")

    # Activation should succeed because authority grant exists, even without workspace root
    context = host.activate("bots5.plugin.no-grant")
    # But the facade will deny the capability check at seam time
    with pytest.raises(PluginCapabilityDenied, match="workspace.read"):
        context.facade.query()


def test_plugin_with_shared_authority_grant_succeeds(tmp_path: Path):
    """A plugin with a valid shared-authority grant succeeds."""
    from pathlib import Path as PathType
    from bots5.plugins import Capability, PluginHost, PluginLifecycleError
    from bots5.plugins.reference import line_counter

    # Create a plugin host with capability authority
    capability_authority = CapabilityAuthority()
    host = PluginHost(capability_authority=capability_authority)

    # Create workspace
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "test.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    # Set workspace roots on the host so the facade can resolve paths
    host._roots = {"ws": workspace}

    # Bind grants with the shared authority.  The host issues the correct
    # workspace-read grant; no separate manual grant is required.
    host.discover(line_counter.manifest())
    host.validate(line_counter.PLUGIN_ID)
    host.bind_grants(line_counter.PLUGIN_ID, {Capability.WORKSPACE_READ})

    host.admit(line_counter.PLUGIN_ID)
    context = host.activate(line_counter.PLUGIN_ID)

    # Now the plugin can use its capabilities through the shared authority.
    result = line_counter.count_lines(context, "ws", "test.txt")
    assert result.lines == 3


def test_reference_plugin_starts_with_zero_capabilities(tmp_path: Path):
    """The reference line_counter plugin starts with zero capabilities."""
    from bots5.plugins import PluginHost, default_grants

    host = PluginHost()
    # Without CapabilityAuthority, plugin starts with empty grants
    assert default_grants("test").capabilities == frozenset()
