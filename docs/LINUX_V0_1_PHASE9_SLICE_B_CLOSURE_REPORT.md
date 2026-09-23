# B.O.T.S. Linux v0.1 Phase 9 Slice B closure report

Date: 2026-09-23 Australia/Melbourne

## Disposition

Linux v0.1 Phase 9 Slice B is technically complete, substantively accepted, committed, pushed, and landed.

Slice B implements validated chat-archive import and durable import provenance. It lands the additive
Phase 9 import schema, persistent import queue/recovery state, truthful missing-attachment handling and
healing, branch-aware continuation, and a strict Archive v2 evolution that can preserve import/continuation
provenance across later export/import hops.

Slice B remains chat interchange. It does not implement whole-installation Backup v1, independent backup
verification, staged restart restore, or Phase 9 desktop/Qt integration.

## Accepted product identity

- landed commit:
  `9a84d38b6ad2d3968db58f471d53bf85820656b1`;
- parent:
  `b31f41b8a789268182cbdba20727cab4648eeb4c`;
- tree:
  `21419b68da12ddbd4320df798dfde7a993953d9f`;
- subject:
  `Implement Phase 9 Slice B archive import provenance`;
- accepted `FINAL_CANDIDATE.json` SHA-256:
  `fadae89025e8cf5c3c76f239780d2e7176a121f84d492a18cedd7f49f6cf1eaf`;
- accepted `CONT_CANDIDATE_SEAL_01.json` SHA-256:
  `d4f1f574d70fa5493c1b6dc03bf2c4471493983b208b5e8bc552c9aa2f68cdbf`;
- accepted candidate path count: **36 tracked product/test paths**.

Campaign evidence under `work/**` remained administrative/local evidence and was excluded from the
product commit.

## Landed Slice B boundary

The shipped implementation includes:

- migration `0012_phase9_archive_import`, additive over `0011_phase8_inspector_state`;
- strict Archive v1 compatibility/intake without silently changing the v1 language;
- strict Archive v2 additions for import provenance, continuation history, history bindings, and truthful
  branch settings provenance;
- fresh local identities for imported object graphs with durable immediate-source provenance;
- persistent import queue/operation state and restart/re-preflight behavior;
- truthful broken external ordinary-attachment references and SHA-verified healing;
- authority-aware import settlement/recovery and source-revision/search interaction;
- receiving-installation continuation/provider/settings resolution without provider/credential mutation;
- branch-aware local continuation from imported history.

Archive v1 remains frozen. Archive v2 exists because the accepted durable-provenance requirement cannot be
losslessly represented by the frozen v1 grammar. Existing valid v1 archives remain supported import
sources.

Editing an imported user turn is an explicit local derivation choice. The imported source message remains
historical truth; the edit creates local branch-aware continuation rather than rewriting source history or
requiring a separate preliminary continuation-selection ritual.

## Final validation

Historical failed/blocked validation remains preserved and is not rewritten as green evidence.

The accepted current-byte broad preservation gate is:

- CONT-T2-02: **1038 passed, 1 skipped**, exit `0`.

The final complete repository gate on the sealed candidate is:

- T4: **1238 passed, 1 skipped**, exit `0`, approximately **1:20:42**;
- candidate identity remained unchanged.

The sole final skip was the opt-in live-Qwen provider probe.

Additional current-byte focused evidence included:

- complete five-file Slice B selection: **104 passed**;
- EF2 focused selection: **4 passed**;
- EF2 poison cluster: **24 passed, 537 deselected**;
- Phase 9 queue file: **92 passed**;
- Phase 8 repair selection: **13 passed**.

The earlier CONT-T2-01 terminal failure (**2 failed, 1035 passed, 1 skipped**) and earlier GLM/T3 failures
remain historical failure evidence. They are not represented as final validation.

## Independent review and oracle

Independent adversarial review returned PASS with its blockers resolved.

The final oracle returned PASS and independently recomputed the sealed candidate/evidence identities and
reproduced the required wire probes.

Both roles used `mimo-v2.6-flash` through OpenRouter under an explicitly authorized review-model
substitution. That reduced model diversity and remains a recorded evidence limitation; it was not hidden or
converted into stronger independence than was actually obtained.

Failed/replaced review-oracle probes remain retained historical evidence rather than product evidence.

## Landing

Mick separately granted:

1. substantive acceptance;
2. commit authority for the exact sealed 36-path candidate;
3. push authority for the resulting exact commit.

The product was committed at
`9a84d38b6ad2d3968db58f471d53bf85820656b1` and pushed as a normal fast-forward from
`b31f41b8a789268182cbdba20727cab4648eeb4c`.

After push:

- local `main`, local `origin/main`, and live remote `refs/heads/main` were verified equal at the
  landed commit;
- ahead/behind was `0/0`;
- the index was empty;
- tracked worktree state was clean;
- only untracked `work/**` campaign evidence remained.

## Current Phase 9 boundary

Phase 9 Slices A and B are closed and landed.

Slice C — Backup v1 and independent verification — is the next bounded Phase 9 sequence boundary.
Slice D remains staged restart restore/destructive failure semantics, and Slice E remains desktop
integration and Phase 9 closure.

This closure record grants no authority to begin Slice C, D, or E work.

Organisational Memory records the accepted Slice B product-semantic authority and later implementation-era
semantic amendments. The implementation repository remains authoritative for current runtime behavior.
