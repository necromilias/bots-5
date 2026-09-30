# CI v1 — authoritative sharded T4

Status: landed and proven authoritative on `main`. This document records the durable CI v1 reference
and the exact limits of its evidence. It stands alone from committed Git identities and the GitHub
Actions run identity; it does not require any local untracked evidence tree.

## What CI v1 is

CI v1 is the authoritative T4 gate for this repository:

- workflow: `.github/workflows/t4.yml` (orchestration);
- correctness logic: `scripts/ci/` (`ci_support.py`, `ci_pytest_report.py`, `setup_env.sh`,
  `mount_btrfs_build.sh`, `make_static_venv.sh`), documented in `scripts/ci/README.md`;
- population floor: `scripts/ci/t4_baseline_inventory.txt`;
- permitted-skip allow-list: `scripts/ci/allowed_skips.txt`.

It takes one exact immutable candidate commit, runs the complete authoritative pytest population
concurrently across shards on standard GitHub-hosted Linux runners, mechanically proves that no test
was omitted or duplicated, preserves compact evidence, and returns one aggregate result bound to that
candidate SHA.

The workflow is **dispatch-only**: `workflow_dispatch` names the exact expected commit SHA and fails
closed if the checkout differs. An ordinary push cannot trigger it. The population floor and the
permitted-skip allow-list are policy and are read from the default branch (`tools/scripts/ci/*`), never
from the commit under test; a trusted run records `event.trusted_tools = "true"`. If trusted tooling is
unavailable, an authoritative dispatch fails closed rather than judging a candidate with the
candidate's own verifier, population floor, and skip policy.

## Landing identity

| Commit | Subject |
|---|---|
| `bbb9e6359adb0a2861fe30dd6d2c1a8c324e602d` | CI: add sharded authoritative T4 workflow and support machinery |
| `950c5377d23a8346233c53c157b65d84514f51a0` | CI: build the .venv fixture from a python-build-standalone interpreter |
| `58fca2c7b1b4111d982733980c303565bf91695e` | tests: stop the qasync helper depending on a leaked window |
| `5bb783326b5a8c68bb1e2b2719aca6070e22f225` | CI: retire the temporary bootstrap push trigger |

`58fca2c7b1b4111d982733980c303565bf91695e` is the candidate of the authoritative run below.
`5bb783326b5a8c68bb1e2b2719aca6070e22f225` is current `main`; it retired the temporary bootstrap push
admission and made the workflow dispatch-only. That change altered workflow admission only, not the T4
execution machinery, so **no further T4 run was performed against `5bb7833`**.

## Authoritative run

| Field | Value |
|---|---|
| Run id | `36682157600` |
| Event | `workflow_dispatch` |
| Ref | `refs/heads/main` |
| Candidate / expected SHA | `58fca2c7b1b4111d982733980c303565bf91695e` |
| Result | `T4_PASS` |
| `trusted_tools` | `"true"` |
| Canonical / baseline / executed | `1598` / `1598` / `1598` |
| Passed / failed / errors / skipped | `1597` / `0` / `0` / `1` |
| Shards | 6 (round-robin over canonical collection order) |

The single reviewed skip is
`tests/test_phase3_local_qwen.py::test_opt_in_local_qwen_acceptance_path`, the only entry in the
permitted-skip allow-list. The canonical inventory sha256 for this population is
`2158f273ba4fcdc042148572ef4bbf261790f59f42b5addcb841cf7737fea419`, identical to the reviewed baseline
inventory hash at the candidate.

**The authoritative run executed against `58fca2c7b1b4111d982733980c303565bf91695e`, not against
current `main` `5bb7833`.** The run identity and the candidate SHA must not be collapsed.

## Bootstrap history (retired)

While CI v1 was not yet on the default branch it could not be dispatched, so a temporary `push` trigger
on the `ci/bootstrap-v1` branch namespace was used to publish and validate the mechanism. Those runs had
no trusted checkout available and therefore used the candidate's own copies, recording
`event.trusted_tools = "false"` — self-attested, proving the mechanism rather than conferring
authority. That trigger was retired at `5bb7833`. The bootstrap branch `ci/bootstrap-v1` still exists
remotely at `58fca2c7b1b4111d982733980c303565bf91695e`; whether to retain or delete it is an open
policy decision, not a CI correctness requirement.

## Ubuntu 24.04 / SQLite 3.45.1 compatibility lane

The workflow includes a compatibility job on Ubuntu 24.04 (system Python 3.12.3, SQLite 3.45.1) that
asserts the SQLite version and runs the secret-key SQL conformance test, including a real
`CREATE TRIGGER` on the 3.45.1 parser. Its status differs by gate, and "non-gating" here must be read
against a specific one:

- **Aggregate T4 adjudication.** The lane is non-gating: it is excluded from the aggregate gate's
  `needs`, its evidence is not consumed by the aggregate reconciler, and a failure cannot change the
  aggregate `T4_PASS` / `T4_FAIL` result.
- **Overall GitHub Actions run.** The lane is still an ordinary job with no job-level
  `continue-on-error`, so a failure currently makes the overall workflow-run conclusion `failure`.
  That follows from how the run is assembled; it is not a support-policy statement.
- **Platform/support policy.** The lane provides compatibility evidence only. It does not establish
  Ubuntu 24.04, Ubuntu LTS, or SQLite 3.45.x as a supported platform, a minimum version, a fleet
  policy, or a release gate.

See `docs/SQLITE_COMPATIBILITY_REPAIR.md` for the repair it exercises.

## Known limitations of the CI evidence

- The compatibility lane is non-gating only with respect to the aggregate T4 adjudication: it is
  excluded from `aggregate.needs` and cannot change `T4_PASS` / `T4_FAIL`. It is still an ordinary
  GitHub Actions job, so a lane failure currently makes the overall workflow run conclude `failure`;
  and it does not establish a minimum SQLite version or an Ubuntu/LTS support guarantee.
- The evidence does **not** prove six distinct physical machines. Recorded runner hostnames cover fewer
  hosts than shards; no claim of six distinct hosts is made.
- There is **no external attestation**. The tree seal, reports, and aggregate are produced by the job
  that runs the candidate's code; the trust boundary is bounded against accidental or partial
  divergence, not against a hostile candidate forging its own reporter.
- Runner image versions were not uniform within the authoritative run, and the floating image label
  means job-to-image assignment is not reproducible run to run.
- Dependencies are not fully pinned (`pip install -e '.[dev]'` resolves latest, recorded per run via
  `pip freeze`).
- No formal long-term-support or fleet/platform support policy is established by CI v1.

## Open policy questions

These remain human decisions and are not settled by CI v1's existence:

- whether Arch and Ubuntu LTS become formal blocking support gates;
- whether the compatibility lane should become run-conclusion-non-blocking (for example through a
  job-level `continue-on-error`), which is a separate decision from its status in the aggregate T4
  adjudication;
- whether to retain or delete the `ci/bootstrap-v1` branch;
- whether to pin dependencies and/or address the residual (non-hostile-candidate) trust boundary.

## Related records

- `.github/workflows/t4.yml` — the workflow, including its own dispatch-only and least-privilege notes;
- `scripts/ci/README.md` — the support machinery and trusted-tooling policy;
- `docs/DEVELOPMENT.md` — how the gate fits the development workflow;
- `docs/SQLITE_COMPATIBILITY_REPAIR.md` — the compatibility repair exercised by the compatibility lane.

Detailed raw artifacts for run `36682157600` were captured during the CI v1 campaign in a local
untracked `work/ci-evidence/` directory. They are supplementary evidence only; the claim above is
fully stated from the committed identities and the run identity.
