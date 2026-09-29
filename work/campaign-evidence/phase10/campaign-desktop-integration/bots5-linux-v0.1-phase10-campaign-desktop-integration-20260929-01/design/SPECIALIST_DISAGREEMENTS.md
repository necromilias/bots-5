# Specialist disagreements (preserved)

The supervisor integrates one design but does not erase disagreement. Each entry records the
positions, the evidence, and the supervisor's adjudication with reasoning.

## D-1 — Approval binding substrate: fork or mechanically settled?

- **MiniMax H-6**: content-digest/snapshot vs re-validation-at-execution vs human-only
  binding; classified HUMAN_SEMANTIC_FORK.
- **Step §9.2**: classified as a fork (engine-side snapshot vs desktop digest vs operator
  content pin).
- **Gemini §4**: treats the immutable snapshot as the architecture, not a fork.
- **Command §3.2/§5.2**: assumes content-digest binding as the mechanism.

**Adjudication: mechanically settled, not a fork.** CONTRACT §"Preflight/approval
integrity" mandates an immutable *or* mechanically reverified execution snapshot before the
first provider request and instructs classifying a fork only if that cannot be achieved
"inside existing accepted semantics without a product choice". An additive frozen
`PreflightSnapshot` + digest assertion + zero-reread dispatch achieves it with no product
choice. MiniMax's and Step's fork framing reflects implementation-mechanism uncertainty, not
an unresolved product semantic. The design fixes: engine-side snapshot, digest assertion,
disk re-verification, zero-reread dispatch, typed refusal that creates no run directory.

## D-2 — Attempt path layout: fork or engineering choice?

- **MiniMax H-2 / blocked condition C-3**: three layouts (flat discriminated names; nested
  `stages/<id>/<att>.*`; separate `attempts/` root), classified HUMAN_SEMANTIC_FORK.
- **GLM §5**: classified HUMAN_FORK, noting flat is "the only containment-preserving
  candidate".
- **Gemini §1.2 / Risk 4**: flat `<id>.att<N>` preserves the containment predicate exactly;
  nested layouts would require weakening it.

**Adjudication: engineering choice, not a product fork; flat adopted.** The product
requirement is "preserve the original attempt, create a sibling". Every additive layout
satisfies it. Choosing the one that requires **no change to a security guard**
(`storage.py:196-198`) and no `paths.py` change is engineering, and the nested alternative
would knowingly weaken a path-traversal predicate for no product gain. Recorded dissent:
MiniMax's table labels `<id>__<attempt>` flat as "breaks containment" — that is incorrect;
flat names keep `parent == stages/`. MiniMax's own §5.3 correctly lists keeping outputs in
`stages/` as option (a). The disagreement was about the label, not the mechanism.

## D-3 — Attempt resolution fallback

- **Gemini §2.2**: if `selection.json` is absent, scan and pick the highest completed
  attempt number.
- **Supervisor refinement**: no scanning. If `selection.json` is absent, every stage's
  selected attempt is 1; a missing selected-attempt file is a hard `ValidationError`; a v2
  directory lacking `selection.json` but containing `.att2+` files is malformed and fails
  closed.

**Reasoning:** `SAFE_ID_RE` (`src/bots5/paths.py:10`) allows `.` and `-`, so a declared
stage id can itself look like an attempt suffix (e.g. `w1.att2`). Max-scanning filenames
makes selection ambiguous and can silently select the wrong artifact. Selection-first
resolution is deterministic, reconstructable, and fail-closed. This is a strengthening of
Gemini's proposal, not a reversal of its intent.

## D-4 — Run discovery

- **Command §6.1/§7.4**: lists "no run-directory enumeration" as a critical defect and
  recommends adding a run-directory discovery API.
- **Qwen [X]5 / MiniMax H-3 / Step fork 5**: classify discovery as an open product question.
- **GLM §5**: classifies it HUMAN_FORK with a minimal "explicit path/id" branch.

**Adjudication: presented as HSF-3, recommendation 3a (explicit path/id).** Command's
"defect" framing overstates it: obligation 1 is to load a job, and obligation 6 is durable
result inspection; neither mandates browsing. Enumeration is a UX enhancement, not a Phase 10
compliance requirement. Presented to Mick rather than silently added to the fence.

## D-5 — Does regeneration auto-select the new attempt?

- **Command §1.4 / Gemini matrix row 16**: after regeneration, selection moves to attempt 2
  (auto-select).
- **Supervisor design**: regeneration does not change selection; a separate explicit
  "Make current" action writes `selection.json`.

**Adjudication: explicit selection, recorded as a non-blocking UX question.** Obligation 10
ties staleness to a *changed selected attempt*; making the change an explicit recorded act is
safer and keeps selection honest (a failed regeneration must never become "current").
Auto-select on successful completion is a plausible UX variant; it does not change evidence
semantics and can be revisited without a fork. Not escalated as a product fork.

## D-6 — Cancellation: engine-side vs desktop-owned

- **MiniMax H-1 / Step §9.6 / Qwen [X]1**: fork between engine mutation (Path A) and
  desktop-lifecycle ownership (Path B).
- **Supervisor**: engine-side terminalization is **fixed** (Path A); only the terminal
  *vocabulary* is a fork (HSF-4).

**Reasoning:** the terminal record must be written by the process that owns the run, before
the event loop tears down; Path B would place campaign lifecycle authority in the UI and
still fails for non-close cancellation (e.g. cancellation initiated elsewhere in the event
loop). "Desktop must not become a second engine" favors Path A. Recorded dissent preserved.

## D-7 — Terminal cost reporting and live progress

- **Command §2.3 option D / Gemini §5.2 option D**: cost may be shown as unknown.
- **MiniMax Rule D-5 / Qwen §5.2 / Gemini §6.3**: no token-dollar projection; known subtotal
  + explicit unknown set only.
- **Supervisor**: same as MiniMax/Qwen/Gemini. No disagreement in substance; recorded because
  Command's option table could be read as permitting a dollar estimate under option A. Under
  HSF-1 branch A the estimate is a *preflight upper bound*, never live accrual.

## D-8 — Mutable-vs-frozen projection state

- **Qwen §8 / MiniMax §6.1**: bounded polling of filesystem evidence.
- **Gemini §7.2**: same, with a Qt signal bridge.
- **Supervisor**: adopted; no event bus, no SQLite shadow state.

No substantive disagreement; recorded for completeness.

## D-9 — Whether old-run compatibility fixtures can reuse `evidence/**`

- **Step §10 / GLM §1**: golden-fixture reader tests derived from retained historical run
  directories.
- **Constraint**: `evidence/**` is tracked historical evidence and must not be mutated or
  repurposed as mutable test input.

**Adjudication:** tests must either read `evidence/**` read-only as fixtures or synthesise a
byte-exact v1 fixture; under no circumstance may a test rewrite a tracked evidence tree.
Recorded so implementation does not accidentally mutate historical evidence.
