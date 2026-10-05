# Linux v0.1 Phase 11 closure and landing record

Status: **Phase 11 — product finishing and standalone packaging — closed, substantively accepted by Mick, and landed** at `59265916abeb2e9f6cbf953726f22a9f7c00f3b5` on `origin/main`. This is the durable repository-facing record of the final corrective state, dated 2026-10-05. It does not publish a release or authorize further work.

## Final subject and validation

| Subject | Final identity/result |
|---|---|
| Product source, 427-file candidate manifest | `sha256:c9ff10d64b71686306f2421090b8d0a5b2846c842d37f85d415b8ad01c9ab4ae` |
| Validated standalone, 366-file artifact manifest | `sha256:7d96ca245212773d1a9d6069373bd262d88526ace93552de3dd73c7a4ab681ea` |
| Source-to-artifact technical seal | `sha256:9031c2c224d20ece9d15fb7a359e291363f6e9b9823576d1920931a00e3fc722` |
| Final source T4 | **2189 passed / 1 permitted skip / 0 failures / 0 errors** |
| Frozen provider cleanup | **71/71 passed**, no failure, skip or unexecuted case |
| M7 package validation | **156/156 passed**, no failure, error or skip |
| Source landing commit | `59265916abeb2e9f6cbf953726f22a9f7c00f3b5` |

These results belong to the retained final candidate; this paperwork reconciliation did not rerun product validation. The permitted T4 skip is `tests/test_phase3_local_qwen.py::test_opt_in_local_qwen_acceptance_path`. All 2190 canonical T4 IDs were reconciled exactly once. Frozen cleanup exercised the actual compiled payload relocated outside the checkout, with isolated HOME/XDG and no development-runtime substitution. M7 covered both entrypoints, relocation, resources, rooted persistence, migrations, restore and close lifecycle.

The source identity remains the accepted product identity above. Subsequent documentation bytes have their own paperwork identity and do not redefine that product candidate.

## Completion, acceptance and landing are separate records

The technical report (`work/campaign-evidence/phase11/frozen-cleanup-compatibility-repair/20261005-01/REPORT.md`), reconciliation record (`work/campaign-evidence/phase11/frozen-cleanup-compatibility-repair/20261005-01/FINAL_RECONCILIATION_RECORD.json`) and technical seal (`work/campaign-evidence/phase11/frozen-cleanup-compatibility-repair/20261005-01/FINAL_SOURCE_TO_ARTIFACT_SEAL.json`) establish technical completion. Their `substantive_acceptance: false` fields are correct for that checkpoint: they precede human acceptance.

The subsequent acceptance and commit-authority record (`work/campaign-evidence/phase11/frozen-cleanup-compatibility-repair/20261005-01/commit/20261005-01/AUTHORITY.json`) records Mick's substantive acceptance of the exact final source, standalone artifact and technical seal, followed by the separate “Commit approved” grant. Acceptance is not inferred from green tests or the seal.

The commit receipt (`work/campaign-evidence/phase11/frozen-cleanup-compatibility-repair/20261005-01/commit/20261005-01/COMMIT_RECEIPT.json`) records one local commit on `phase11/ui-polish`, with parent `6a41939388944a450b0169e389901d54de72b02b`. The separate push report (`work/campaign-evidence/phase11/frozen-cleanup-compatibility-repair/20261005-01/push/20261005-01/REPORT.md`) and push receipt (`work/campaign-evidence/phase11/frozen-cleanup-compatibility-repair/20261005-01/push/20261005-01/PUSH_RECEIPT.json`) record the normal fast-forward to `origin/main`: local HEAD, cached origin/main and live remote main matched the landing commit with **0/0 divergence at that push boundary**. This is retained remote verification, not a new network observation made by this documentation job. The older local branch named `main` need not be moved to establish the landing.

## Corrective chronology

1. The October 4 technical-completion report (`work/campaign-evidence/phase11/final-reconciliation/20261004-01/reports/FINAL_REPORT.md`) and earlier acceptance (`work/campaign-evidence/phase11/final-acceptance/20261004-01/PHASE11_SUBSTANTIVE_ACCEPTANCE.json`) concern a historical predecessor candidate. They are not final corrective acceptance. The earlier acceptance explicitly carried an unresolved provider-stream cleanup observation.
2. The cleanup adjudication (`work/campaign-evidence/phase11/provider-stream-cleanup-adjudication/20261005-01/REPORT.md`) classified a real product lifetime defect. Explicit ownership was repaired across provider, backend, router and application/ExecutionManager. A post-DONE-tail regression encountered during repair was corrected: valid terminal completion closes owned resources without requesting irrelevant trailing payload. Genuine pre-DONE transport errors continue to fail. The accepted cleanup reconciliation (`work/campaign-evidence/phase11/provider-stream-cleanup-repair/20261005-01/post-acceptance/20261005-01/REPORT.md`) retains the intermediate source evidence and failed attempts.
3. The corrective frozen rebuild (`work/campaign-evidence/phase11/corrective-standalone-rebuild/20261005-01/REPORT.md`) exposed a compiler divergence. Its failed artifact and stopped frozen runs remain historical failure evidence, not final package validation.
4. The final compatibility correction constructs the same typed backend failure event inside `except ProviderError`, then yields it only **after leaving the exception handler**. This avoids the Nuitka “No active exception to reraise” close failure. Failure fields, sanitization, partial output, metadata, uncertainty and event order remain intact; cleanup ownership and the packaging strategy are unchanged.
5. The resulting final source and rebuilt standalone passed the final T4, all 71 frozen cleanup cases and all 156 M7 checks. Mick accepted those exact identities; the source was then committed and pushed. The standalone remained local.

Final ownership proof covers both providers, asyncio/qasync and DesktopRuntime: owned generators and provider/body iterators settle; response/client/transport close before store release; no owned cleanup child remains in the tested lifecycle matrix. Normal completion remains COMPLETE, explicit Stop ABORTED and owned timeout FAILED. Real provider failures retain partial output and remote-outcome uncertainty. This finite proof is not a global guarantee about every retained resource diagnostic.

## Retained limitations and dispositions

**P11-02 remains accepted/carried Classification B.** The human disposition (`work/campaign-evidence/phase11/p11-02-current-acceptance/20261005-01/HUMAN_ACCEPTANCE.json`) says: “Carry the historical `0013 → 0012` downgrade limitation. No forward product repair is authorised or required. Frozen migration `0013` remains unchanged.” The unsupported downgrade fails and leaves partial DDL; it does not produce genuine 0012. Supported recovery restores attributable prior source/verified backup and migrates forward. No successful historical reversal or repair of frozen 0013 is claimed.

The historical limitations record (`work/campaign-evidence/phase11/final-reconciliation/20261004-01/reports/LIMITATIONS_AND_HUMAN_GATES.json`), M7 milestone evidence (`work/campaign-evidence/phase11/standalone-packaging/20261004-01/wave5/reports/FINAL_REPORT.md`) and [implementation report](LINUX_V0_1_PHASE11_IMPLEMENTATION_REPORT.md) retain their checkpoint dispositions. The later provider cleanup repair supersedes the earlier unresolved-cleanup status for the corrected subject; it does not rewrite earlier reports or rebind earlier paid observations to corrected bytes.

The final technical report retains **807 pytest warnings**, including destructor ResourceWarnings surfaced through pytest, and pending restore-task diagnostics where observed. Allocation/ownership origins are unestablished. The later acceptance record accepts the exact final S/A/T candidate; it does not contain an individual harmlessness finding, a global leak-free guarantee or a newly proved waiver for every diagnostic. Preserve these observations with their original provenance. Green T4 does not establish that every warning was repaired. Historical diagnostic-budget nonconformance and excluded surplus compiler probe also remain retained; no broader approval is inferred.

Packaging proof is confined to the **Forge/Arch-family target**; there is no general Linux/older-glibc guarantee. Real Secret Service credential operations remain unverified; availability and packaged backend construction were proved. Python 3.14/Nuitka 4.1.1 support remains an experimental toolchain qualification. The existing source/package runtime split is recorded, not silently upgraded or treated as identical. M8/AppImage remains conditional/deferred; no Flatpak/Snap deliverable is claimed. Other historical implementation, UI/environment and recovery limitations retain their own dispositions.

The final chrome/colour authority and its requirement supersession are recorded in UI adjudication (`work/campaign-evidence/phase11/final-reconciliation/20261004-01/reports/UI_ADJUDICATION.json`). Accepted later owned frameless chrome and navy colour authority supersede the original frame/palette expectations. This does not promote the historical Draft 1 or later visual candidate documents wholesale into final design authority.

## Release state and handoff

The validated standalone remains local at `dist/bots5-linux-standalone-compatibility-20261005-01`. Campaign evidence remains local/untracked; the standalone is ignored and was not uploaded. **No public release or deployment occurred.** Source closure and landing do not imply artifact publication.

**Phase 12: Linux v0.1 torture run and separate closure adjudication.** It is next in the accepted sequence, not authorized by this paperwork pass. Detailed campaign design and execution require a later inspect/propose/approve boundary. Future tools/plugins/Code/Git and other deferred capabilities remain separately gated.

Read the [final Phase 11 authority/effect supplement](UNIFIED_AUTHORITY_EFFECT_INVENTORY_PHASE11_FINAL_SUPPLEMENT.md) with the earlier inventories. The build-facing contract remains [Linux v0.1 accepted design](LINUX_V0_1_DESIGN.md).

## Evidence identities

The local retained chain is bound by these SHA-256 values. Provenance paths above identify local retained evidence; the untracked evidence is not represented as committed GitHub content.

| Record | SHA-256 |
|---|---|
| Final technical report | `32c00687448fbfcb98f4009dfe36dfddf4e7db16536b3f9e57040861b58f56e6` |
| Final technical seal | `9031c2c224d20ece9d15fb7a359e291363f6e9b9823576d1920931a00e3fc722` |
| Final reconciliation | `152cc148fb9ecb3f1397cd96d75af00ae15468d32dce024475dbea81ab4c4174` |
| Acceptance/commit authority | `0f5913e201240826c075980d4025adf176ab839d684bf373e19a1bfdd20af439` |
| Commit receipt | `e8c8751deb4935252cbc2e4a37e08b6e34a4b98e5216567e21eaa35013766e93` |
| Push report | `fed5e80673841754a28298139c1f9a308476f585a0b3bec892fe93b44f3c02f2` |
| Push receipt | `1c4eac10da3f175a4d7df333099e6c7680cfe69dab38bc179c5f1d642c3fae79` |
