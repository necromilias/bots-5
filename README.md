# B.O.T.S. 5

B.O.T.S. is a local-first AI harness and native Linux desktop project built around explicit authority,
inspectable state, durable evidence, and human-controlled consequential actions.

## Current status

The original campaign harness baseline is closed and preserved. V0.2 adds the built-in
`local_openai` provider while preserving the bounded manifest-driven execution model.

Linux v0.1 is implemented, validated, and landed through **Phase 9 closure (Slices A–E)** and
**Phase 10 — native desktop surface over the headless campaign engine** and
**Phase 11 — product finishing and standalone packaging**:

1. native walking skeleton;
2. conversation truth, immutable lineage/revisions, and deterministic fake generation;
3. real generation backend, streaming, cancellation, checkpointing, and local-model acceptance;
4. concurrency, multi-window workspace, shutdown, and crash reconciliation;
5. provider/model usability, secrets, catalogue/capability discovery, and settings;
6. deterministic context construction, content-addressed attachments, unified data-root authority,
   rooted SQLite/native durability handling, and fail-closed effect ownership.
7. search and exact navigation through authoritative branch/message identities;
8. core-owned inspection and provenance UX over durable request-time facts;
9. Phase 9 Slice A readable Transcript v0.1 export and strict one-chat Archive v1 export;
10. Phase 9 Slice B validated Archive v1 import, strict Archive v2 provenance-preserving interchange,
    durable import provenance, queue/recovery semantics, and branch-aware continuation;
11. Phase 9 Slice C Backup v1 capture with independent closed-manifest package verification;
12. Phase 9 Slice D whole-installation restore: staged, verified adoption through a durable journal,
    startup reconciliation of interrupted restores, indefinite operator-directed retention of the
    displaced installation, a non-UI `--restore-from` entry point, and typed destructive-failure
    semantics;
13. Phase 9 Slice E native desktop integration for the landed Phase 9 workflows — export, import,
    continuation resolution, backup creation/verification, and the whole-installation restore
    handoff — plus the Phase 9 technical-closure record.
14. Phase 10 native campaign desktop surface over the headless campaign engine: the Qt-free
    `CampaignBridge`/`CampaignProjection` seam, a native campaign dock, attempt-addressed evidence
    with `selection.json`, one-shot preflight approval binding, truthful cancellation, cumulative
    versus selected cost accounting, worker regeneration, synthesis rerun with mechanical staleness,
    and headless CLI parity (`inspect --attempt`, `regenerate`, `rerun-synthesis`).
15. Phase 11 product finishing, generation settings, provider-managed OpenRouter context/Archive v3,
    explicit provider-stream cleanup and validated standalone CLI/desktop packaging.

The landed Phase 6 implementation is commit
`20847c7a49e26679d0d3dfe99798a2c211bec436`. Its final reserved acceptance gates were
**616 passed** for the complete Phase 6 + authority/grant suite and **956 passed, 1 expected skip** for
the complete repository suite, with no failures and no provider contacted. See
`docs/LINUX_V0_1_PHASE6_CLOSURE_REPORT.md` for the final closure and landing record.

Phase 7 was accepted, committed, and pushed at
`6ccdaf01ce880cf5f00fca55209c2a99dd06c1cd`. Its supplied final closure evidence is
**1,065 passed, 1 expected provider skip, 0 failed**, with no provider or network
activity. Phase 8 inspection and provenance UX was accepted and landed at
`f72e0ea6e10694972f3b3455d973651629de448a`. Its final pre-commit T4 gate recorded
**1082 passed, 1 skipped, 3124 warnings in 3946.05s (1:05:46)**, exit `0`, with
no provider/network activity. Phase 9 Slice A — readable transcript export and strict Archive v1
export — was substantively accepted, committed, and pushed at
`35b206a404d4cd3e2dd05a5c07ffdc6dd0e1ba40`. Its final T4 gate recorded
**1133 passed, 1 skipped, and the sole accepted pre-existing Phase 8 roadmap-test failure**.
`docs/LINUX_V0_1_PHASE9_SLICE_A_CLOSURE_REPORT.md` is the authoritative Slice A landing record.

Phase 9 Slice B is substantively accepted and landed at
`9a84d38b6ad2d3968db58f471d53bf85820656b1`. Slice B adds additive migration
`0012_phase9_archive_import`, validated Archive v1 import, strict Archive v2 evolution for durable
provenance/continuation round trips, persistent import-queue/recovery state, truthful missing-attachment
handling/healing, and branch-aware imported continuation. Final current-byte validation recorded
**1038 passed, 1 skipped** on the accepted T2 preservation selection and **1238 passed, 1 skipped** on
T4 with the candidate unchanged. See `docs/LINUX_V0_1_PHASE9_SLICE_B_CLOSURE_REPORT.md`.

Phase 9 Slice C — Backup v1 and independent verification — and Phase 9 Slice D — whole-installation
staged restart restore — are likewise accepted and landed. Phase 9 is closed: Slice E — native
desktop integration and Phase 9 technical closure — landed at
`0756904481ae884bb9e864e8e1e11fc4a27a72ff`, and `docs/LINUX_V0_1_PHASE9_SLICE_E_CLOSURE_REPORT.md`
is its closure record. Phase 10 — campaign desktop integration — subsequently landed at
`9762170099889ecd87d451341a15a29ce7aceae8`; `docs/LINUX_V0_1_PHASE10_CLOSURE_REPORT.md` is its
closure and landing record. A SQLite ≤ 3.45.1 parser-compatibility repair landed at
`dee0b3b8446d7f84bcc4e1a5af2339c5ed78383f` (`docs/SQLITE_COMPATIBILITY_REPAIR.md`), and CI v1 landed
through `5bb783326b5a8c68bb1e2b2719aca6070e22f225` — the CI-v1 admission-retirement commit that made
the workflow dispatch-only. The authoritative CI v1 run exercised candidate
`58fca2c7b1b4111d982733980c303565bf91695e`, not `5bb7833` or any later `main` descendant
(`docs/CI_V1.md`).

**Phase 11 — product finishing and standalone packaging — is closed and landed** at
`59265916abeb2e9f6cbf953726f22a9f7c00f3b5`. See the [Phase 11 closure record](docs/LINUX_V0_1_PHASE11_CLOSURE_REPORT.md).
The validated standalone remains local; no public release or deployment occurred.
**Phase 12: Linux v0.1 torture run and separate closure adjudication** is next; this status
reconciliation does not authorize its design or execution.

The build-facing Linux v0.1 contract remains `docs/LINUX_V0_1_DESIGN.md`. The cumulative Phase 6
implementation history remains in `docs/LINUX_V0_1_PHASE6_IMPLEMENTATION_REPORT.md` and the later
phase implementation reports. Their earlier candidate-status statements are historical evidence;
the applicable phase closure reports record current landing authority. Phase 7 and Phase 8 do not
change the landed Phase 6 disposition. The Phase 9 Slice A and Slice B closure reports record their
historical interchange/export/import landing boundaries, the Slice E closure report records Phase 9
technical closure, and `docs/LINUX_V0_1_PHASE10_CLOSURE_REPORT.md` records the Phase 10 landing.

The landed Phase 11 shell follows the accepted later chrome/colour authority recorded in the
closure report. Historical Draft 1 and later visual candidate documents retain their own
provisional/draft status; this reconciliation does not promote them wholesale.

## Campaign harness

The original B.O.T.S. harness executes fixed, reviewable multi-model jobs:

`strict JSON manifest -> explicit text inputs -> bounded parallel workers -> optional synthesis -> durable run artifacts`

It deliberately does not give models autonomous authority. Models cannot spawn workers, invoke a shell,
discover repository context, mutate Git, perform RAG, or invent execution topology. The harness owns
validation, scheduling, persistence, limits, and state.

V0.1 compiles each model system message from a fixed harness-owned execution boundary plus a validated
six-section worker contract. Declared source and worker outputs remain untrusted data in user-message
blocks. See `docs/WORKER_CONTRACTS.md`.

V0.2 adds schema-v2 and the built-in non-streaming `local_openai` provider. Schema v1 remains
OpenRouter-only; schema v2 can route workers and synthesis to OpenRouter or an operator-supplied local
OpenAI-compatible HTTP/HTTPS endpoint.

## Linux desktop authority boundary

Linux v0.1 uses one authoritative native core shared by desktop windows. Persisted authoritative state
outranks in-memory presentation state. Runtime data-root effects are governed by one
`DataRootAuthority` and effect-grant protocol across public application commands, SQLite/store work,
EventBus delivery, attachments/GC, startup/migration/recovery, native VFS outcome handoff, and terminal
teardown.

Invalidation closes new admission immediately and revokes the discovering grant. Already-admitted
unrelated work may settle only within its existing ownership. Database resources, including rooted
child cursors, remain accounted for until consequential native state has settled or been classified.
Unknown or integrity-threatening outcomes fail closed rather than being rewritten as success.

See `docs/UNIFIED_AUTHORITY_EFFECT_INVENTORY.md` — the landed Phase 6 base inventory — together with
`docs/UNIFIED_AUTHORITY_EFFECT_INVENTORY_PHASE7_10_SUPPLEMENT.md`, which records the landed Phase 7–10
participation deltas. The Phase 6 record remains authoritative for its own closure scope and is not
superseded.

## Local Phase 3 compatibility route

For opt-in local Phase 3 desktop testing, supply the backend, endpoint, and model explicitly. This
`local_openai` route is an explicit legacy compatibility mode: it is Phase 6 disabled, makes no Phase 6
planning/accounting/provenance claim, and does not permit selecting attachments.

```bash
bots5-desktop --backend local_openai \
  --base-url http://127.0.0.1:8000/v1 \
  --model Qwen3-8B \
  --api-key-env LOCAL_QWEN_API_KEY
```

The fake backend remains the default. The opt-in acceptance probe is `tests/test_phase3_local_qwen.py`;
it skips unless a local endpoint and model are explicitly provided through its documented environment
variables. It never selects OpenRouter.

## Install

Requires Python `>=3.12,<3.15`.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

For real OpenRouter execution of the campaign harness:

```bash
export OPENROUTER_API_KEY='...'
```

The key is read only at runtime and is not written to job or run artifacts.

For a local-only schema-v2 campaign job, set `providers.local_openai.base_url`. Authentication is
optional; when `api_key_env` is present, only that named environment variable is read. A local-only job
does not require `OPENROUTER_API_KEY`.

## Validate and run campaign jobs

```bash
bots5 validate examples/example-job.json
bots5 validate examples/example-job-v2-local-openai.json
bots5 run examples/example-job.json
```

Validation performs no API calls and creates no run directory.

For normal paid-operation preflight, worker selection, completion review, evidence retention, and human
acceptance, see `docs/OPERATING_PROCEDURE_V1.md`. For local-provider operation, see
`docs/OPERATING_PROCEDURE_V2.md`.

With the default run location (`./.bots5/runs`):

```bash
bots5 status RUN_ID
bots5 inspect RUN_ID STAGE_ID
```

For custom run directories:

```bash
bots5 status RUN_ID --runs-dir PATH
bots5 inspect RUN_ID STAGE_ID --runs-dir PATH
```

These inspection commands are disk-only and make no API calls.

## Legacy campaign constraints

The following describe the campaign harness, not the native desktop product:

- strict JSON, closed objects, no coercion;
- mandatory ordered `TASK`, `ALLOWED`, `FORBIDDEN`, `EVIDENCE`, `OUTPUT`, and `STOP CONDITION` sections;
- bounded parallel worker execution;
- no autonomous delegation loop;
- no model shell/filesystem/Git/RAG/plugin authority;
- non-streaming V0.2 campaign-provider completions;
- no retries;
- exact-known/unknown provider cost semantics;
- local operator trust model, not a hostile sandbox.

The Linux desktop adds SQLite, native UI, streaming generation, attachments, wider lifecycle authority,
and recovery semantics under the accepted Linux design; do not infer desktop capability from the old V0
non-goal lists.

## Deferred beyond Linux v0.1

Unless a concrete blocker separately promotes bounded work, Android, Code/Git, persistent daemon/remote
clients, MCP/general tool frameworks, scheduling/automation, remote execution nodes, RAG/semantic search,
and operating-mode capability frameworks remain deferred.

## Build provenance

Key records include:

- `docs/BUILD_CAMPAIGN.md` — original build method retrospective;
- `docs/V0_CLOSURE_REPORT.md` — final V0 closure;
- `docs/V0_2_DESIGN_CAMPAIGN_REPORT.md` — V0.2 provider design and implementation closure;
- `docs/LINUX_V0_1_PHASE1_PHASE2_CLOSURE_REPORT.md` — early desktop closure;
- `docs/LINUX_V0_1_PHASE3_IMPLEMENTATION_REPORT.md` through
  `docs/LINUX_V0_1_PHASE6_IMPLEMENTATION_REPORT.md` — cumulative phase implementation evidence;
- `docs/LINUX_V0_1_PHASE6_CLOSURE_REPORT.md` — Phase 6 closure and landing record;
- `docs/LINUX_V0_1_PHASE7_IMPLEMENTATION_REPORT.md` — cumulative Phase 7 implementation evidence;
- `docs/LINUX_V0_1_PHASE8_IMPLEMENTATION_REPORT.md` — cumulative Phase 8 implementation archaeology;
- `docs/LINUX_V0_1_PHASE8_CLOSURE_REPORT.md` — authoritative Phase 8 closure and landing record;
- `docs/LINUX_V0_1_PHASE9_SLICE_A_CLOSURE_REPORT.md` — authoritative Phase 9 Slice A closure and landing record;
- `docs/LINUX_V0_1_PHASE9_SLICE_B_CLOSURE_REPORT.md` — authoritative Phase 9 Slice B closure and landing record;
- `docs/LINUX_V0_1_PHASE9_SLICE_E_CLOSURE_REPORT.md` — Phase 9 technical-closure record;
- `docs/LINUX_V0_1_PHASE10_CLOSURE_REPORT.md` — authoritative Phase 10 closure and landing record;
- [Phase 11 closure report](docs/LINUX_V0_1_PHASE11_CLOSURE_REPORT.md) — final accepted source/package and landing record;
- [Final Phase 11 authority/effect supplement](docs/UNIFIED_AUTHORITY_EFFECT_INVENTORY_PHASE11_FINAL_SUPPLEMENT.md) — additive post-M3 participation accounting;
- `docs/CI_V1.md` — CI v1 authoritative T4 reference and its stated limitations;
- `docs/SQLITE_COMPATIBILITY_REPAIR.md` — SQLite ≤ 3.45.1 parser-compatibility repair record;
- `docs/LINUX_V0_1_DESIGN.md` — accepted Linux v0.1 build-facing contract.

The additive production OpenRouter accounting and Archive v3 contract is specified in
[Provider-managed context and Archive v3](docs/PROVIDER_MANAGED_CONTEXT_ARCHIVE_V3.md).
