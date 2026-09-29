# Design repair record — seal v3 → v4

Trigger: second fresh MiMo final design oracle against design seal v3
(`b08d85744065ab6d1b41e4f83912f4f194bc025725f6d95ab4c7b959874f1ded`).
Report: `reports/oracle/MIMO_FINAL_DESIGN_ORACLE_v3.md`
(sha256 `8c1435af0e4a7a138f779c058b0e4ff7688f45653bb7d6ccc2d6e49db54b8a6c`).

Oracle result: seal integrity **PASS** (23/23 entries, v1/v2 preserved, supersession
declared); verdict **PASS_WITH_LIMITATIONS**; **no blocking findings**; M-1…M-6 all
confirmed genuinely repaired; fence unchanged and sufficient; validation non-vacuous (22/22
reused selectors present, collect-only 115, exit 0). Four non-blocking residuals (N-1…N-4)
and two informational notes (N-5, N-6) were recorded. This record maps each to its repair.
**No repair required leaving the accepted design fence.**

The oracle and all earlier review evidence are preserved unchanged.

---

## N-1 — Sealed `MUTATION_FENCE.json` still echoed the false M-3 source claim

**Repair.** The fence note now reads: validation "reads the exposed `base_url` (and
`api_key_env` only where a provider exposes it, i.e. `OpenAICompatibleProvider`) without
changing provider semantics; `OpenRouterProvider` exposes only `base_url`" — consistent with
`PREFLIGHT_APPROVAL_STATE_MACHINE.md` §4.7.
**Status:** repaired; design evidence only.

## N-2 — Main design obligation-13 cell omitted the `--approval <path>` consent channel

**Repair.** `PHASE10_CAMPAIGN_DESKTOP_DESIGN.md` row 13 now reads "preflight-only unless
`--approve --actor <label>` **or `--approval <path>`** is given", matching
`CAMPAIGN_EVIDENCE_EVOLUTION.md` §6.4 and `MUTATION_FENCE.json`. The earlier v2→v3 ledger's
"the four artifacts now agree" claim is superseded by this record (the v2→v3 ledger is
preserved unedited as sealed prior evidence).
**Status:** repaired; documentation only.

## N-3 — Skipped / never-dispatched v2 synthesis read as UNVERIFIABLE with a false integrity warning

**Repair.** `REGENERATION_AND_STALE_SYNTHESIS.md` §4.2 gains a
**NOT_APPLICABLE** outcome for a synthesis attempt in a non-dispatched terminal state
(`StageState.SKIPPED`: dependency failed/incomplete, known-cost threshold exceeded, not
reached before timeout — `runner.py:347-361`, `:366-380`, `:385-399`, `:449`). Such an
attempt is displayed with its recorded `synthesis_skipped_reason` and raises **no**
integrity warning; only a **dispatched** v2 attempt with absent/malformed provenance is
UNVERIFIABLE. `DESKTOP_SURFACE_AND_LIFECYCLE.md` §4.1 gains the matching display row. Added
the T0.12 selector.
**Status:** repaired; inside fence (`storage.py`, `usage.py` readers).

## N-4 — Categorical "`cancelled_pending` is never durable" overstated; sibling lost its own cause

**Repair (two parts).**
(a) `DESKTOP_SURFACE_AND_LIFECYCLE.md` §5.2 now states the in-process guarantee precisely and
adds an explicit honest limit: a hard process kill (SIGKILL, power loss) inside the
outer-terminalization window can leave a durable `cancelled_pending`, which the reader
displays as **interrupted / uncertain** — never success, never retried, never rewritten.
(b) The generic-failure sweep now reclassifies a `cancelled_pending` sibling to
`error_type = "cancelled"` (its own cause) rather than `internal_error`; the run itself stays
`FAILED` + `internal_error`. This holds under either HSF-4 vocabulary branch, since the
stage-level label is a string either way. §4.1 gains the durable-`cancelled_pending` row and
T0.6's selectors were split accordingly.
**Status:** repaired; inside fence (`runner.py`).

## N-5 (informational) — exclusive-create could be misread as forbidding state transitions

**Repair.** `PREFLIGHT_APPROVAL_STATE_MACHINE.md` §3.1 point 3 now states explicitly that
exclusive-create governs **creating a new attempt** (and run tree, and approval marker), and
that the ordinary `queued` → `running` → terminal in-place updates of that same attempt are
not forbidden by it.
**Status:** repaired; documentation only.

## N-6 (informational) — openrouter `api_key_env_name` value origin unnamed

**Repair.** `PREFLIGHT_APPROVAL_STATE_MACHINE.md` §2.1 now names the origin: for
`openrouter` the value is the CLI convention constant `OPENROUTER_API_KEY`
(`cli.py:92-94`), because the manifest declares provider config only for `local_openai`
(`manifest.py:55`, `PROVIDER_CONFIG_KEYS = {"local_openai"}`); for `local_openai` it comes
from the job's provider config. `CAMPAIGN_EVIDENCE_EVOLUTION.md` revision note updated.
**Status:** repaired; documentation only.

---

## Repair outcome

- Findings repaired this wave: **N-1…N-6 = 6/6**.
- Fence expansion required: **none** (9 modify / 8 add unchanged).
- Seals v1, v2, v3 preserved unchanged; successor seal v4 covers the repaired design.
- Human forks HSF-1…HSF-5 remain open for Mick, with HSF-1 correctly framed.
- Next required step: third (final budgeted, MiMo max 3) fresh MiMo oracle against seal v4.
