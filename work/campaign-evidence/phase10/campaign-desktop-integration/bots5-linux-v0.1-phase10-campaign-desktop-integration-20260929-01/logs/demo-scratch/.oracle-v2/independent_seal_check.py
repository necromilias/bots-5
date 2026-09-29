#!/usr/bin/env python3
"""Independent (from-scratch) recomputation of candidate seal v4.
Deliberately does NOT import make_candidate_seal.py.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

REPO = Path("/home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1")
PACK = REPO / "work/campaign-evidence/phase10/campaign-desktop-integration/bots5-linux-v0.1-phase10-campaign-desktop-integration-20260929-01"
SEAL = PACK / "seals/implementation/PHASE10_CANDIDATE_SEAL_v4.json"
SIDECAR = PACK / "seals/implementation/PHASE10_CANDIDATE_SEAL_v4.sha256"
FENCE = PACK / "design/MUTATION_FENCE.json"
DSEAL = PACK / "seals/design/DESIGN_SEAL_v4.json"


def sha(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git(*a: str) -> str:
    return subprocess.run(["git", *a], cwd=REPO, capture_output=True, text=True, check=True).stdout


m = json.loads(SEAL.read_text())
fence = json.loads(FENCE.read_text())

fenced = sorted({e["path"] for e in fence["modify"]} | {e["path"] for e in fence["add"]})
seal_files = sorted(m["files"])

out = {}
out["seal_self_sha_expected_sidecar"] = SIDECAR.read_text().split()[0]
out["seal_self_sha_recomputed"] = sha(SEAL)
out["seal_self_sha_match"] = out["seal_self_sha_expected_sidecar"] == out["seal_self_sha_recomputed"]
out["seal_self_sha_matches_brief"] = out["seal_self_sha_recomputed"] == (
    "02fbcce47185dd83645febe14faab5808027f68593de3edfd806441626a29902"
)

# per-file digests
drift = []
for rel, d in m["files"].items():
    p = REPO / rel
    if not p.is_file():
        drift.append((rel, "MISSING"))
    elif sha(p) != d:
        drift.append((rel, sha(p)))
out["file_drift"] = drift or "none"

# candidate_id
out["candidate_id_recomputed"] = hashlib.sha256(
    json.dumps(m["files"], sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
out["candidate_id_match"] = out["candidate_id_recomputed"] == m["candidate_id"]
out["candidate_id_matches_brief"] = m["candidate_id"] == (
    "0b04683646f14ab66b71aa381aa506ffc82e5aa9fbb4e3a29ec9ca65346ffc17"
)

# fence path set vs seal files
out["fence_paths"] = len(fenced)
out["seal_file_count_declared"] = m["file_count"]
out["seal_file_count_actual"] = len(seal_files)
out["fence_set_equals_seal_set"] = fenced == seal_files
out["fence_symmetric_diff"] = sorted(set(fenced) ^ set(seal_files)) or "none"

# design seal + fence digests
out["design_seal_v4_sha256_recomputed"] = sha(DSEAL)
out["design_seal_v4_match"] = out["design_seal_v4_sha256_recomputed"] == m["design_seal_v4_sha256"]
out["mutation_fence_sha256_recomputed"] = sha(FENCE)
out["mutation_fence_sha_match"] = out["mutation_fence_sha256_recomputed"] == m["mutation_fence_sha256"]

# head
out["head"] = git("rev-parse", "HEAD").strip()
out["head_match"] = out["head"] == m["head_commit"]
out["head_matches_brief"] = out["head"] == "0756904481ae884bb9e864e8e1e11fc4a27a72ff"

# live fence status
tracked = [ln[3:] for ln in git("status", "--porcelain").splitlines() if ln and not ln.startswith("??")]
fset = set(fenced)
out["live_tracked_modified"] = sorted(tracked)
out["live_modified_in_fence"] = sorted(p for p in tracked if p in fset)
out["live_modified_out_of_fence"] = sorted(p for p in tracked if p not in fset)
out["seal_declared_in_fence"] = sorted(m["tracked_modified_in_fence"])
out["seal_declared_out_of_fence"] = m["tracked_modified_out_of_fence"]
out["seal_fence_violations_field"] = m["fence_violations"]
out["in_fence_lists_agree"] = out["live_modified_in_fence"] == out["seal_declared_in_fence"]

# staged
out["staged_files"] = git("diff", "--cached", "--name-only").splitlines() or "EMPTY"
out["stash"] = git("stash", "list").splitlines() or "EMPTY"

# zero diff guards
guards = [
    "pyproject.toml", "db/", "evidence/", "examples/", "src/bots5/providers/",
    "src/bots5/manifest.py", "src/bots5/paths.py", "src/bots5/core/application.py",
    "src/bots5/core/execution.py", "src/bots5/core/import_queue.py", "src/bots5/core/events.py",
    "src/bots5/desktop/session.py", "src/bots5/desktop/bridge.py", "src/bots5/desktop/widgets.py",
    "src/bots5/desktop/theme.py", "src/bots5/desktop/profile.py",
    "src/bots5/desktop/phase9.py", "src/bots5/desktop/phase9_dialogs.py",
    "src/bots5/desktop/phase9_imports.py", "src/bots5/desktop/phase9_queue_dock.py",
]
g = git("diff", "--name-only", "HEAD", "--", *guards).splitlines()
out["zero_diff_guard_changes"] = g or "NONE (all byte-identical to HEAD)"

# add files exist
adds = [e["path"] for e in fence["add"]]
out["add_files_present"] = {p: (REPO / p).is_file() for p in adds}

print(json.dumps(out, indent=2, sort_keys=True))
sys.exit(0)
