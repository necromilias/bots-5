#!/usr/bin/env python3
"""Generate (or verify) the Phase 10 implementation candidate seal.

Usage:
    python3 make_candidate_seal.py generate <seal_id>
    python3 make_candidate_seal.py verify   <seal_path>

The seal covers exactly the files named by design/MUTATION_FENCE.json (9 modify,
8 add) plus the fence file itself, and records the git working-tree state so the
pre-commit boundary can be reconfirmed later.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PACK = Path(__file__).resolve().parents[2]
REPO = Path("/home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1")
FENCE = PACK / "design" / "MUTATION_FENCE.json"
OUT_DIR = PACK / "seals" / "implementation"

DESIGN_SEAL = PACK / "seals" / "design" / "DESIGN_SEAL_v4.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fenced_paths() -> list[str]:
    fence = json.loads(FENCE.read_text(encoding="utf-8"))
    paths = [entry["path"] for entry in fence["modify"]] + [
        entry["path"] for entry in fence["add"]
    ]
    return sorted(set(paths))


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout


def build(seal_id: str) -> dict:
    files = {}
    missing = []
    for rel in fenced_paths():
        path = REPO / rel
        if not path.is_file():
            missing.append(rel)
            continue
        files[rel] = sha256_file(path)
    if missing:
        raise SystemExit(f"fenced files missing: {missing}")

    tracked = [
        line[3:]
        for line in git("status", "--porcelain").splitlines()
        if line and not line.startswith("??")
    ]
    modified_in_fence = sorted(p for p in tracked if p in set(fenced_paths()))
    modified_out_of_fence = sorted(p for p in tracked if p not in set(fenced_paths()))

    manifest = {
        "seal_id": seal_id,
        "kind": "phase10_implementation_candidate",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "parcel": "bots5-linux-v0.1-phase10-campaign-desktop-integration-20260929-01",
        "parcel_v2_manifest_sha256": "45fd91f3f383620e4da7575c7a5ba92abf9298ed47c72cf7eb30a315c854391b",
        "design_seal_v4_sha256": sha256_file(DESIGN_SEAL),
        "mutation_fence_sha256": sha256_file(FENCE),
        "head_commit": git("rev-parse", "HEAD").strip(),
        "files": files,
        "file_count": len(files),
        "tracked_modified_in_fence": modified_in_fence,
        "tracked_modified_out_of_fence": modified_out_of_fence,
        "fence_violations": modified_out_of_fence,
    }
    manifest["candidate_id"] = hashlib.sha256(
        json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return manifest


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    action, target = argv[1], argv[2]
    if action == "generate":
        manifest = build(target)
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        out = OUT_DIR / f"{target}.json"
        out.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        digest = sha256_file(out)
        (OUT_DIR / f"{target}.sha256").write_text(
            f"{digest}  {out.name}\n", encoding="utf-8"
        )
        print(f"wrote {out}")
        print(f"candidate_id: {manifest['candidate_id']}")
        print(f"sha256: {digest}")
        print(f"fence violations: {manifest['fence_violations']}")
        return 0
    if action == "verify":
        seal_path = Path(target)
        expected = (seal_path.parent / (seal_path.stem + ".sha256")).read_text(
            encoding="utf-8"
        ).split()[0]
        actual = sha256_file(seal_path)
        manifest = json.loads(seal_path.read_text(encoding="utf-8"))
        drift = []
        for rel, digest in manifest["files"].items():
            path = REPO / rel
            if not path.is_file() or sha256_file(path) != digest:
                drift.append(rel)
        print(f"seal sha256 match: {expected == actual}")
        print(f"file drift: {drift or 'none'}")
        return 0 if (expected == actual and not drift) else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
