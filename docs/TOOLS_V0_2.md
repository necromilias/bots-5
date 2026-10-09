# Tools v0.2 — bounded local tool invocation

Status: implemented inside the `tools` workstream partition of the Linux v0.2
campaign (`bots5-linux-v0.2-control-plane-20261007-01`), baseline
`db82e0b34bf83ea9d0e306f90e8699d3e16e135b`.

## Purpose

`bots5.core.tools` is the reference implementation of the two contracts in
this workstream's scope:

- **tool declaration / invocation / result** (`ToolDefinition`,
  `ToolRegistry`, `ToolInvoker`, `ToolResult`);
- **workspace-bounded local effects** (the read-only
  `bots5.workspace_read` tool over an explicitly granted workspace root).

It is deliberately a **consumer** of the shared control plane, never a second
authority: every effect passes through a
`bots5.core.capabilities.CapabilityAuthority` grant, and the reference tool
physically confines reads to a granted root with descriptor-relative,
`O_NOFOLLOW` traversal.

## Threat model (parent campaign locked decision)

Accidental, defective, compromised-in-scope and out-of-authority behaviour —
**not** a hostile arbitrary-native-code sandbox.  The checks below fail closed
on ordinary mistakes (missing grant, path escape, oversized read, unknown
tool, malformed arguments); they are not a security boundary against a
malicious in-process attacker.

## Planes: visibility, permission, approval

Research claim `T11` (sealed `bots5-tools-code-plugins-research-20261006-01`)
separates three planes, and this implementation keeps them separate:

| Plane | Surface here | Confers execution? |
|---|---|---|
| Model-context visibility | `ToolDefinition` + `ToolRegistry.tool_ids()` / `application.list_tools()` | **No** — a listed definition grants nothing |
| Execution permission | `CapabilityAuthority.grant(...)` + `ToolInvoker.invoke(...)` | Only a scoped, budgeted, TTL-bounded grant does |
| Human approval | Out of this module (control plane / desktop surface) | Never implied by the first two |

## Closed declaration surface (fail-closed)

- `TOOL_SCHEMA_DIALECT = "bots5.tool-schema.v1"` — one closed dialect; there
  is no schema negotiation and no free-form pass-through.
- `ToolDefinition` validates its schema strictly: object type only, a closed
  property type set (`string`/`integer`/`number`/`boolean`), no unknown schema
  keys, bounded property count.  A definition that fails validation is refused
  at construction (`ToolSchemaError`), so an invalid definition can never
  reach model context.
- **Collision is an error, never last-wins**: `ToolRegistry.register()` raises
  `ToolCollision` on a duplicate `tool_id` unless the caller explicitly passes
  `replace=True`.
- `ToolDefinition.digest` is a sha256 of the canonical declaration and is
  pinned into every `ToolInvocation` (research `T07`: closed-versioned-digest
  pattern).

## Invocation path (`ToolInvoker.invoke`)

`declare → validate arguments → authorize (one effect span) → execute →
settle → journal`, with refusal **before** any filesystem effect:

1. **Unknown tool** → terminal `REFUSED` with `DenialReason.NO_GRANT`
   (nothing registered ⇒ no capability vocabulary exists for it).
2. **Malformed arguments** → terminal `REFUSED` (`INVALID_UNITS` for
   structural violations, `ToolArgumentError` carried in `error` for missing
   required fields).  Unknown argument names are refused, never ignored.
3. **Authorization** → a single
   `CapabilityAuthority.effect(grant, units=1, target=resolved_path)` span
   wraps the executor.  The `target` pins the **exact file**, so a
   `DirectoryScope` grant is checked per-file rather than whole-directory.
   Any `CapabilityDenied` (foreign/expired/exhausted/scope-mismatch/…) is
   returned as a terminal `REFUSED` result carrying the structured
   `DenialReason`; budget is consumed exactly once per successful dispatch and
   **not** consumed by a pre-effect refusal (the seam checks scope before
   decrementing).
4. **Executor failure** → terminal `FAILED` with the error text.
5. **Uncertain outcome** → terminal `UNKNOWN`, never rewritten to success or
   failure.  An executor signals this by raising `ToolUncertainOutcome`.
6. **Success** → terminal `SUCCEEDED` with the executor payload.

`ToolResult` construction enforces the state contract (a `REFUSED` result
must carry a reason, `FAILED` must carry an error, `SUCCEEDED`/`UNKNOWN` must
not), so a truthful journal is impossible to construct incorrectly.

## Reference read-only tool

`bots5.workspace_read` (registered by `bind_workspace_read_tool(registry,
root=...)`) reads exactly one UTF-8 text file inside the granted root:

- relative paths only — absolute paths and `..` are refused;
- component-by-component `openat`-style traversal with `O_NOFOLLOW` on every
  component (including the final leaf), so no symlink anywhere in the chain can
  redirect the read;
- non-regular files (directories, FIFOs, sockets, devices) are refused;
- a byte bound (`WORKSPACE_READ_MAX_BYTES`, 1 MiB) is enforced during the
  read, not just from `stat`, so a growing file cannot exceed it;
- the payload returns `content`, `bytes` and a `sha256` of exactly the bytes
  read.

The executor closes over the granted `root`, so the effect is physically
confined even if a caller supplies a misleading scope; the capability grant
remains the authorization source of truth.

## Application wiring

`BotsApplication(..., capability_authority=..., workspace_root=...)`:

- **Opt-in and deny-by-default**: without an explicit `CapabilityAuthority`
  the application registers **no** tools at all and `invoke_tool` raises
  `StateError` (matching the control plane's no-ambient-grant rule).  Existing
  callers (bootstrap, tests) pass neither argument, so their behaviour is
  unchanged.
- `list_tools()` (visibility plane) and `invoke_tool(...)`
  (permission plane) are ordinary application commands; `invoke_tool`
  publishes a `tool_invoked` core event with the invocation id, tool id,
  terminal state and refusal reason so the queue/receipts workstream can settle
  it later.

## Cross-workstream dependencies

- `bots5.core.capabilities` (the `control-capabilities` CapabilityAuthority
  seam) is consumed as a sealed API.  This worktree carries a copy so the
  consumer tests can run; the integrated candidate must contain that module
  exactly once.  Recorded as an unresolved dependency in the workstream
  handback.
- Durable persistence of `ToolResult`/`InvocationRecord` (append-only
  attempt-addressed storage) belongs to `execution-queue-receipts`; this
  module exposes the journal as data and publishes the event, it does not own
  a second storage engine.
- The desktop composer still shows the historical disabled "Tools" affordance
  (`desktop/window.py`); enabling a tool UI surface belongs to
  `desktop-product`, not this workstream.

## Not in scope (explicitly)

No third-party/MCP ecosystem, no shell/process execution, no network/egress
tool, no plugin-contributed tool registry, no model-facing tool-call wire
protocol (providers expose no tool-call type in this baseline).  Those are
either locked out of the campaign (MCP) or owned by other workstreams
(code-git, plugins, execution).
