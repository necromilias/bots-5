# CI support machinery (`scripts/ci`)

Small, standard-library-only helpers that make the authoritative sharded T4 gate
inspectable and unit-testable outside GitHub. `.github/workflows/t4.yml` orchestrates;
the correctness logic lives here.

| File | Purpose |
|---|---|
| `ci_support.py` | parse/validate the canonical pytest inventory, deterministic shard assignment, environment reporting, and the aggregate reconciliation gate. CLI: `build-shards`, `aggregate`, `env-report`, `check-sha`. |
| `ci_pytest_report.py` | Pytest plugin (enabled by `BOTS5_CI_REPORT_PATH`) that records, per shard, the node IDs actually collected and the outcome actually observed. A collected test with no observed outcome is recorded as `error`, never `passed`. |
| `setup_env.sh` | Builds the authoritative `.venv-ci` from the system Python, optionally mounts btrfs at `build/`, optionally builds the embedded-SQLite `.venv`, and compiles the native rooted VFS. |
| `mount_btrfs_build.sh` | Creates and mounts a real btrfs loopback at `build/`; two Phase 6 tests assert this and CI must not fake it. |
| `make_static_venv.sh` | Builds `REPO/.venv` (path overridable with `BOTS5_CI_STATIC_VENV_DIR`) from a standalone interpreter with an embedded private SQLite; one Phase 6 test asserts that interpreter fails closed. |
| `allowed_skips.txt` | The only skips the aggregate gate permits; any other skip fails T4. Read only from the trusted manifest directory, or from the default branch in a trusted run. |
| `t4_baseline_inventory.txt` | Reviewed node-ID population floor: the full reviewed population (product and CI tests). The gate fails if any is missing; adding tests is allowed, and removing one requires a deliberate baseline update. Read from the default branch in a trusted run. |

## Trusted tooling

`build-shards`, the baseline and the allow-list are policy, so the workflow does not take them
from the commit under test when it can avoid it: `prepare`, `shards` and `aggregate` each
check out the default branch into `tools/` and prefer `tools/scripts/ci/*`. During bootstrap
(the default branch does not yet carry CI v1) that checkout is absent, the candidate's copies
are used, and the aggregate records `event.trusted_tools = "false"` — self-attested, proving
the mechanism rather than conferring authority.

## Local use

```bash
# Canonical inventory and shard manifest (no GitHub needed)
PYTHONPATH=src:. .venv314/bin/python -m pytest -o addopts= -q --collect-only -p no:cacheprovider > /tmp/collect.txt
PYTHONPATH=src:. .venv314/bin/python -m scripts.ci.ci_support build-shards \
  --collect-output /tmp/collect.txt --candidate-sha <40-hex> --shard-count 6 \
  --allowed-skips scripts/ci/allowed_skips.txt \
  --baseline scripts/ci/t4_baseline_inventory.txt --out /tmp/manifest

# Reconcile: the manifest directory is trusted, the evidence directory is not.
python3 -m scripts.ci.ci_support aggregate \
  --manifest /tmp/manifest --evidence /tmp/shards \
  --expected-sha <40-hex> --baseline scripts/ci/t4_baseline_inventory.txt \
  --out /tmp/aggregate.json
```

The aggregate exits `0` only when `result == "T4_PASS"`. It never trusts a claim of
success: it recomputes identity, assignment, collection, execution, exit codes, tree
integrity and skip policy from the raw evidence files. The accepted SHA comes from the
caller (`--expected-sha`), never from the evidence; the inventory, assignment hashes and
allow-list are read only from `--manifest`, so shard evidence cannot rewrite the standard
that judges it.

Focused tests: `tests/test_ci_sharding.py`, `tests/test_ci_aggregate.py`,
`tests/test_ci_report_plugin.py`.
