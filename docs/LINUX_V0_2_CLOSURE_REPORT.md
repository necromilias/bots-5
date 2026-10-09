# Linux V0.2 closure record

## Disposition and landing

Mick accepted V0.2 and confirmed publication on 2026-10-09 Australia/Sydney. V0.2 is complete for current
Mick/Forge use. Technical validation, human acceptance, commit authorization, push authorization and tag
authorization were **separate** consequences, each separately granted.

The published V0.2.0 commit is `0fffe8941647fac68e4fbde4deb736e9f53beb33`, with Git tree
`483d26352ff87ee62e619f9f82f594cd3b7d2126`, parent `08fcd9c56898fa69f5d5a3c9700e52f66eed90e1`, and lightweight
tag `v0.2.0` pointing at that exact commit. `origin/main` was observed at this commit on 2026-10-09
Australia/Sydney. This is a dated landing observation, not a timeless branch identity.

Landing was a **fast-forward** from `08fcd9c` — no merge commit, no force, no history rewrite. The v0.1
release commit `7db9c2d357902a1f554b211af885af9c31eac4c6` remains intact in history under tag `v0.1.0`.

## Two V0.2 lines make up this release

Release `v0.2.0` contains **two separately-developed efforts**, both called V0.2, which this record for the
first time states together:

**Line A — provider configuration.** Schema v2 with required top-level `providers` (schema v1 unchanged),
validated `LocalOpenAIConfig` carried by `Job`, `run_job(job, providers, ...)` as the sole runner form, and one
built-in non-streaming `OpenAICompatibleProvider`. Landed as `17c195fd989cba3977d82f824367e1a3419c009e`
with the null-credential correction `38046d99bc98c6906f5175dae9494c62060109f5`. Its own chronology is recorded
in [V0.2 design campaign report](V0_2_DESIGN_CAMPAIGN_REPORT.md), whose "V0.2 implementation closure" and later
sections remain that line's authoritative account and are not restated here.

**Line B — control plane.** The capability/authority seam, queue/execution/receipts with durable persistence,
local tools and a bounded egress consumer, bounded code and process execution, the Git consequence ladder,
self-authored plugins with no ambient trust, and desktop integration. Landed as
`08fcd9c56898fa69f5d5a3c9700e52f66eed90e1`, with the release version commit `0fffe894…` on top.

Neither line was previously recorded alongside the other in this repository.

## Retained validation evidence

The final full-suite run on the released bytes recorded **exit code 0 with zero failures**. That run was
executed on the release-prepared tree identical to the published commit's content for every sealed path
(verified per file, see below). This is a retained result, not newly executed by this record.

Additional retained evidence from release preparation:

- newly generated archives stamp the release producer version across all three wire versions;
- an archive produced by `0.1.0` remains importable and keeps its **own** stamp rather than being rewritten;
- an archive stamped `0.3.0` also validates, confirming the producer field is treated as text rather than
  version-locked;
- a freshly built sdist and wheel declare `Version: 0.2.0` with no stale version retained.

Line A retains its own T4 and canary evidence as recorded in
[V0.2 design campaign report](V0_2_DESIGN_CAMPAIGN_REPORT.md).

## Release identity and its exact scope

The released candidate was sealed as **v21**:

| Field | Value |
| --- | --- |
| Seal | `3cc8264bd6d83769ca2c46f9b5c4be43736e94dc15bd401e154811ace0f21270` |
| Manifest | `2805c0ea5a07b438e7e80e6a81b4aba27dadb595f794e7978ab5973e785da0a0` |
| Files covered | **52** |

**The seal does not cover the whole release, and this record does not claim it does.** The sealed set is the
campaign's 49-file V0.2 delta plus three version-bearing files. The published tree contains **799** tracked
files; the remaining **747** were neither sealed nor validated by this campaign. They are pre-existing v0.1
content, and v0.1 retains its own closure records. No full-tree seal exists for V0.2.0.

## Retained limitations

**Trusted in-process plugins.** `PluginContext` is deliberately minimal — no store reference, no Qt object, no
connection, no module-level escape hatch, and a non-mintable grant view — but plugins execute **in the same
process** as the authority manager. `EntrypointKind.SUPERVISED_SUBPROCESS` is **declared but never
constructed or executed** anywhere in the source. Consequently the Git authority surface is hardened against
this version's stated threat model — accidental, defective, compromised-in-scope and out-of-authority
behaviour — but its tamper-resistance **depends on the authority manager not sharing a process with untrusted
code**. A hostile arbitrary-native-code sandbox was an explicit non-goal of this version. Implementing
supervised subprocess execution as a genuine out-of-process boundary is the control that would carry the
property forward.

**Other retained limits.** Repository identity binding could in principle be defeated by inode reuse; a
thousand recreation attempts produced no reuse on the development filesystem, which is unreached but unproven.
Unrecognised Git subcommands and flags fail closed, so unusual legitimate commands are refused until their
flags are registered. Rationale text on broad Git approvals is mandatory, visible and recorded verbatim, but is
**deliberately not scored** for quality, because structural word scoring was found to accept word salad while
rejecting honest low-diversity rationales; the controls are the registered approval record, the attributable
approver, the scope, the permitted consequence classes and the frozen repository binding.
`src/bots5.egg-info/` is a gitignored build artefact that remains at a stale version in the repository; it is
regenerated by any build and does not reach a distribution.

Reconsideration is required if untrusted code is ever admitted into the authority process, if broader
distribution or another host or toolchain is targeted, or if a future capability requires a retained limit to
be lifted. **Recording a trigger grants no authority.**

## Preserved history

Superseded candidate seals v1 through v20 are retained, including v10 and v16 as byte archives. Every
adversarial finding across every independent review round is retained, as are the campaign's own recorded
errors: a prematurely closed verification item that was reopened, defects found in code already probed as
fixed, two regressions introduced by repair work itself, and a launch-budget ceiling initially proposed above
what the work required and then corrected. The evidence is preserved as produced and is not rewritten here.

## Evidence pointers

Raw V0.2 campaign evidence remains local under the owning run tree
(`run/bots5-linux-v0.2-control-plane-20261007-01/`), including `CLOSEOUT.md`,
`admin/POST_RELEASE_RECONCILIATION.md`, `admin/release-prep/` and the retained seal records. Those paths are
**not** repository files and are not clickable here.

- [V0.2 design campaign report](V0_2_DESIGN_CAMPAIGN_REPORT.md) — the provider-configuration line
- [Phase 12 and Linux v0.1 closure record](LINUX_V0_1_PHASE12_CLOSURE_REPORT.md)
- Released commit: `0fffe8941647fac68e4fbde4deb736e9f53beb33`

## Scope

This record closes the V0.2.0 release work. It does not close process isolation, the trusted-in-process-plugin
limitation, any v0.1 retained limitation, or any deferred capability, and it grants no new implementation
authority.
