# Phase 12 and Linux v0.1 closure record

## Disposition and landing

Mick reported Phase 12 closed and accepted as **PASS_WITH_LIMITATIONS** on 2026-10-06 UTC
(2026-10-07 Australia/Sydney). Linux v0.1 is complete for current Mick/Forge use.
Technical validation, human acceptance, commit authorization, and push authorization remain separate.

The accepted Phase 12 repair commit is
`ba72d16aa3540218cb70e377fa9f452252b7c360`, with Git tree
`7ce9c9077f487296fc54f9a2954a52b030fb5b08`. Remote `main` was observed at this exact commit
on 2026-10-07 Australia/Sydney. This is a dated landing observation, not a timeless branch identity.

## Retained validation evidence

The landed commit message records the final T4 result: **2241 canonical tests, 2240 passed,
0 failed, 0 errors, and 1 permitted skip**. The reported candidate SHA-256 prefix is
`68973dfa…`; this abbreviated value is a provenance pointer, not a complete digest or a seal.

The same record reports a standalone rebuilt from repaired source: manifest SHA-256 prefix
`ccfc1848…`, **363 files**, and **19/19 frozen-product obligations passed**. The earlier accepted
Phase 11 standalone remains untouched at **366/366 files**. These are distinct artifacts;
Phase 11 validation does not substitute for validation of the repaired Phase 12 artifact.

These counts are retained campaign evidence, not checks rerun during this documentation closeout.
The full Phase 12 campaign evidence remains local under
`work/campaign-evidence/phase12/linux-v0.1-torture/20261006-01/`, including
`closeout/WAVE2_CLOSEOUT.md`. These are non-clickable provenance paths because the evidence
is not published in the implementation repository. This record does not claim independent
byte-level verification of the local campaign seal or standalone manifests.

## Landed repairs

- **RP-F-01:** HTTP 408/429 remote-outcome classification now delegates to
  `definitive_rejection`, preserving canonical unknown-outcome semantics. The retained
  HTTP 400–599 classifier sweep records zero disagreements.
- **RP-F-02:** `CompletionRequest.timeout_seconds` now reaches the four request-context
  transport sites through `transport_timeout()`. Discovery remains excluded because its
  path has no caller deadline.
- **RP-F-05:** provider-response uncertainty classification now delegates to
  `provider_side_outcome_unknown`. The retained 12-class sweep records zero disagreements.
  Two existing test expectations changed because they encoded the repaired behavior.

The landed tests include backup/restore crash coverage and six runtime/provider torture cases over
real loopback TCP, with two-sided reachability witnesses and a healthy control. The commit records
malformed streams, pre-DONE transport failure, post-DONE garbage, cancellation/timeout settlement,
and resource ownership. This is the reported bounded coverage, not universal failure immunity.

## Retained limitation and distribution boundary

**DEP-01:** packaged `bots5` and `bots5-desktop` abort at startup when any component of the
full executable path contains non-ASCII characters, including ancestor directories. Source execution
is unaffected. Use an ASCII-only installation path. The retained classification is frozen/toolchain;
the documented startup abort has no data-loss or security consequence.

Mick accepted DEP-01 for current Mick/Forge use. It is not acceptable as an undocumented general
Linux release property. Broader install-path compatibility requires separate toolchain qualification.
The packaging evidence is host-specific and does not qualify Linux distributions generally.

The rebuilt standalone remains local. This record neither publishes binaries nor claims a
GitHub Release, AppImage, installer, deployment, or completed release-tag operation.

## Scope and preserved history

This additive closure record extends the Phase 11 closure report. Earlier phase reports, the
historical CI v1 result, and prior accepted limitations retain their original scope. Phase 12
completion does not promote deferred Code/Git, tools/plugins, Android, daemon/remote clients,
automation, or other post-v0.1 work into authorized implementation.

Primary public provenance is the exact landed Phase 12
[commit and repair record](https://github.com/necromilias/bots-5/commit/ba72d16aa3540218cb70e377fa9f452252b7c360).
The [README](../README.md), [roadmap](ROADMAP.md), [Linux design](LINUX_V0_1_DESIGN.md),
[Phase 11 closure](LINUX_V0_1_PHASE11_CLOSURE_REPORT.md), and
[standalone packaging guide](STANDALONE_LINUX_PACKAGING.md) provide the related scope records.
