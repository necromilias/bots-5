# Plugins — self-authored, capability-bound, no ambient trust

Status: implemented in this worktree; not integrated into a sealed campaign
candidate.

## Purpose

Provide the self-authored plugin framework as a **capability-bound
consumer**. Self-authored plugins receive **no ambient trust**: they start
with zero capabilities and operate only through explicit grants.

Third-party plugin ecosystems and MCP are **out of scope**.

## Package layout

| Module | Responsibility |
| --- | --- |
| `plugins/capabilities.py` | Closed capability enum, requests, grants, `GrantSet` |
| `plugins/manifest.py` | `PluginManifest`, fail-closed validation, API negotiation |
| `plugins/facade.py` | Narrow host facade (never the wide `AppStateStore`) |
| `plugins/lifecycle.py` | Lifecycle state machine, `PluginHost`, `PluginHandle` |
| `plugins/errors.py` | Typed failures, including `PluginCapabilityDenied` |
| `plugins/reference/` | The one intentionally boring reference plugin |

## The central invariant

> A capability is a **request**, never a grant. Granting lives in the
> authority layer. A manifest cannot widen a grant.

`CapabilityRequest` is pure data and confers no authority. Only
`PluginHost.bind_grants` mints grants, and it does so only for capabilities
an authority-bearing caller names explicitly.

Self-authorship is never a grant source. The in-tree reference plugin is
denied `workspace.read` until that capability is explicitly bound.

## Capability enum (closed)

| Capability | Grants |
| --- | --- |
| `workspace.read` | Read-only reads inside an explicitly granted root |
| `named_effect.append` | Append one bounded, receipted named effect |
| `event.subscribe` | Subscribe to host events (bounded, explicit close) |
| `extension.state` | Extension-scoped key/value state |

The enum is closed on purpose: an open enum would let a compromised
extension define its own authority vocabulary and therefore its own blast
radius. An unknown capability name fails closed at validation.

## Lifecycle

```
ABSENT → DISCOVERED → VALIDATED → GRANT_REQUESTED → ADMITTED → ACTIVE
                                        ↓
                                     DRAINING → DISABLED → REVOKED → PURGED
```

* `VALIDATED → ADMITTED` is legal: a plugin with **zero** grants is a
  legitimate default-untrusted plugin.
* `REVOKED → VALIDATED` is legal but clears grants, so reactivation must
  re-pass grant binding.
* `PURGED` is the only truly terminal state.
* Drain closes event subscriptions explicitly; correctness never depends on
  garbage collection or destruction timing.
* Disable never deletes data; purge is a separate deliberate act.

## What a plugin cannot reach

Structurally, not by convention:

* the wide `AppStateStore` Protocol (~70 methods) — the facade holds it
  privately and never exposes it;
* direct Qt access;
* global mutable state;
* a core SQLite connection.

`PluginContext` exposes exactly `plugin_id`, `facade`, `grants` and
`capabilities`.

## Authority substrate

Plugin effects take or join the **existing** effect-admission grant
(`command_admission` / `event_admission` / `issued_event_effect`) at the
callee boundary. This package deliberately does **not** invent a second
authority system, per the campaign's locked decision to build the narrowest
seam demonstrated by real consumers.

## Denial semantics

A missing capability produces `PluginCapabilityDenied` naming the missing
capability. Denials are never silent empty results and never fall back to a
permissive default.

## Tests

* `tests/test_plugins_manifest_api.py` — manifest/API exact behaviour
* `tests/test_plugins_lifecycle.py` — lifecycle transitions, drain, terminal
* `tests/test_plugins_capability_denial.py` — denial semantics and the
  ambient-authority leak hunt

## Known limitations

* `event.subscribe` is declared but no event-bus integration is wired yet;
  the host binds subscription closeables through `PluginHandle`.
* Extension storage uses the in-memory `EXTENSION_STATE` seam only; the
  durable core-owned namespaced option is a follow-on depending on the
  control-capabilities workstream.
* No plugin migration/backup-boundary integration: plugin state is currently
  outside the durable backup boundary.
