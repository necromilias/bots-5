"""M0.4 executed demonstration (deterministic fakes only — never a real endpoint).

Proofs:
  1. status/inspect/validate on a version-1 run are byte-identical to the pre-change output.
  2. status on a version-2 run appends attempt + synthesis_freshness markers (additively).
  3. inspect --attempt 1 / --attempt 2 read exactly the right attempts (3 refused, exit 1).
  4. regenerate without consent: preflight summary, exit 0, no provider construction, no write.
  5. regenerate --approve --actor: sibling attempt 2, exactly one provider request, exit 0.
  6. replaying the same approval: refused, exit 1, no second provider request, no att3.
  7. rerun-synthesis preflight-only: no consent, no spend, no write (exit 0).
  8. rerun-synthesis --approve --actor: new synthesis attempt 2, selection moves, exit 0.
  9. v1 regeneration attempt is refused (M-6: version 1 evidence is read-only), exit 1.
 10. staleness markers: STALE after selection move; integrity_warning after byte flip.
"""
from __future__ import annotations

import contextlib
import hashlib
import subprocess
import io
import json
import os
import sys
from pathlib import Path

REPO = Path("/home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1")
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

import bots5.cli as cli
from bots5.providers.base import CompletionRequest, CompletionResult
from bots5.providers.openrouter import OpenRouterProvider

FIX = REPO / ".m04-demo" / "fix"
V1RUNS = FIX / "v1tree" / ".bots5" / "runs"
V2RUNS = FIX / "v2tree" / ".bots5" / "runs"
V2DIR = V2RUNS / "demo-v2-run"
V1DIR = V1RUNS / "demo-v1-run"
JOB1 = FIX / "v1tree" / "job.json"
JOB2 = FIX / "v2tree" / "job.json"
BEFORE = REPO / ".m04-demo" / "before"

STEP = [0]

def banner(title: str) -> None:
    STEP[0] += 1
    print(f"\n=== proof {STEP[0]}: {title} " + "=" * max(0, 60 - len(title)))

def fingerprint(run_dir: Path) -> dict[str, str]:
    return {
        str(p.relative_to(run_dir)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(run_dir.rglob("*"))
        if p.is_file()
    }

def run_main(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()

class RecordingProvider(OpenRouterProvider):
    """Offline fake keeping the approved openrouter route kind; logs every request."""

    CALLS: list[str] = []
    INDEX = 0

    def __init__(self, api_key: str):
        super().__init__(api_key)

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        RecordingProvider.CALLS.append(request.model)
        RecordingProvider.INDEX += 1
        return CompletionResult(
            output_text=f"fake-output#{RecordingProvider.INDEX}:{request.model}",
            requested_model=request.model,
            finish_reason="stop",
            returned_model=request.model,
            request_id=f"req-{request.model}-{RecordingProvider.INDEX}",
            prompt_tokens=10,
            completion_tokens=20,
            total_tokens=30,
            known_cost_usd=None,  # unknown cost: must print cost=?
            duration_seconds=0.001,
        )

class ExplodingProvider:
    def __init__(self, *args, **kwargs):
        raise AssertionError("provider must not be constructed in a preflight-only path")

def install_tripwires() -> None:
    cli.OpenRouterProvider = ExplodingProvider  # type: ignore[assignment]
    cli.OpenAICompatibleProvider = ExplodingProvider  # type: ignore[assignment]
    os.environ.pop("OPENROUTER_API_KEY", None)

def install_recording() -> None:
    cli.OpenRouterProvider = RecordingProvider  # type: ignore[assignment]
    os.environ["OPENROUTER_API_KEY"] = "demo-key"  # _build_providers gate; no endpoint involved


# ---------------------------------------------------------------------------
banner("status/inspect/validate on the version-1 run are byte-identical to pre-change")
# The PRE-CHANGE CLI is loaded from git HEAD into the bots5 package namespace,
# so the old and new CLI run against the SAME fixture in the SAME process.
# (The inspect metadata line embeds run timestamps, so comparing against files
# captured from an earlier fixture build would be meaningless.)
import importlib.util

head_source = subprocess.run(
    ["git", "-C", str(REPO), "show", "HEAD:src/bots5/cli.py"],
    check=True, capture_output=True, text=True,
).stdout
head_path = REPO / ".m04-demo" / "cli_head.py"
head_path.write_text(head_source, encoding="utf-8")
spec = importlib.util.spec_from_file_location("bots5._cli_head", head_path)
head_cli = importlib.util.module_from_spec(spec)
sys.modules["bots5._cli_head"] = head_cli
spec.loader.exec_module(head_cli)

def run_head(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = head_cli.main(argv)
    return code, out.getvalue(), err.getvalue()

for label, argv in (
    ("status v1", ["status", "demo-v1-run", "--runs-dir", str(V1RUNS)]),
    ("inspect v1 w1", ["inspect", "demo-v1-run", "w1", "--runs-dir", str(V1RUNS)]),
    ("inspect v1 synth", ["inspect", "demo-v1-run", "synth", "--runs-dir", str(V1RUNS)]),
    ("validate", ["validate", str(JOB1)]),
):
    old_code, old_out, _ = run_head(argv)
    new_code, new_out, _ = run_main(argv)
    assert old_code == new_code == 0, (label, old_code, new_code)
    assert old_out == new_out, f"{label} differs from pre-change output"
    print(f"PASS {label}: exit={new_code}, {len(new_out)} bytes, byte-identical to git-HEAD CLI output")

# ---------------------------------------------------------------------------
banner("status on the version-2 run appends attempt + synthesis_freshness markers")
_old_code, old_v2, _e = run_head(["status", "demo-v2-run", "--runs-dir", str(V2RUNS)])
code, out, _err = run_main(["status", "demo-v2-run", "--runs-dir", str(V2RUNS)])
assert code == 0, (code, out)
assert out.startswith(old_v2), "v2 status must be purely additive over the pre-change shape"
appended = out[len(old_v2):]
assert appended == (
    "w1: attempt=1\n"
    "w2: attempt=1\n"
    "synth: attempt=1\n"
    "synthesis_freshness: FRESH\n"
), repr(appended)
print(out)
print("PASS v2 status exit=0, existing shape byte-identical, markers appended")

# ---------------------------------------------------------------------------
banner("inspect --attempt reads exactly the right attempts")
code, out1, _e = run_main(["inspect", "demo-v2-run", "w1", "--runs-dir", str(V2RUNS), "--attempt", "1"])
assert code == 0
att1_text = (V2DIR / "stages" / "w1.att1.md").read_text()
assert att1_text == "output:model-w1", repr(att1_text)
assert att1_text in out1, "--attempt 1 must read att1 bytes"
assert "w1.att2" not in out1
print("PASS inspect --attempt 1: reads stages/w1.att1.md exactly")
code, out3, err3 = run_main(["inspect", "demo-v2-run", "w1", "--runs-dir", str(V2RUNS), "--attempt", "3"])
assert code == 1, (code, out3)
assert err3.startswith("error:"), repr(err3)
print(f"PASS inspect --attempt 3: exit=1, typed refusal: {err3.strip()}")
# (inspect --attempt 2 follows proof 5, once attempt 2 exists)

# ---------------------------------------------------------------------------
banner("regenerate WITHOUT consent: preflight only, no provider, no write")
install_tripwires()
before_fp = fingerprint(V2DIR)
code, out, _err = run_main([
    "regenerate", "demo-v2-run", "w1",
    "--model", "model-w1-v2", "--job", str(JOB2), "--runs-dir", str(V2RUNS),
])
assert code == 0, (code, out)
for line in (
    "operation: worker_regeneration",
    "run_id: demo-v2-run",
    "stage_id: w1",
    "model: model-w1-v2",
    "attempt: 2",
    "scope: worker_regeneration:w1",
):
    assert line in out, (line, out)
assert "provider_route: " in out and "preflight_digest: " in out
assert "consent: none" in out
assert fingerprint(V2DIR) == before_fp, "preflight-only regenerate must write nothing"
assert RecordingProvider.CALLS == []
print(out)
print("PASS regenerate preflight-only: exit=0, zero run-dir writes, zero provider activity")

# ---------------------------------------------------------------------------
banner("rerun-synthesis WITHOUT consent: preflight only, no provider, no write")
code, out, _err = run_main([
    "rerun-synthesis", "demo-v2-run",
    "--job", str(JOB2), "--runs-dir", str(V2RUNS),
])
assert code == 0, (code, out)
for line in (
    "operation: synthesis_rerun",
    "stage_id: synth",
    "model: model-synth",
    "attempt: 2",
    "scope: synthesis_rerun",
    "consent: none",
):
    assert line in out, (line, out)
assert fingerprint(V2DIR) == before_fp, "preflight-only rerun-synthesis must write nothing"
assert RecordingProvider.CALLS == []
print(out)
print("PASS rerun-synthesis preflight-only: exit=0, zero run-dir writes, zero provider activity")

# ---------------------------------------------------------------------------
banner("regenerate WITH --approve --actor: sibling attempt 2, one provider request")
install_recording()
calls_before = len(RecordingProvider.CALLS)
code, out, _err = run_main([
    "regenerate", "demo-v2-run", "w1",
    "--model", "model-w1-v2", "--job", str(JOB2), "--runs-dir", str(V2RUNS),
    "--approve", "--actor", "demo-operator",
])
assert code == 0, (code, out)
att1_fp = {k: v for k, v in before_fp.items() if k.startswith("stages/w1.att1")}
after_fp = fingerprint(V2DIR)
for k, v in att1_fp.items():
    assert after_fp.get(k) == v, f"original attempt bytes must never change: {k}"
assert (V2DIR / "stages" / "w1.att2.json").is_file(), "sibling attempt 2 metadata must exist"
assert (V2DIR / "stages" / "w1.att2.md").is_file(), "sibling attempt 2 output must exist"
att2_text = (V2DIR / "stages" / "w1.att2.md").read_text()
assert att2_text == "fake-output#1:model-w1-v2", repr(att2_text)
assert len(RecordingProvider.CALLS) - calls_before == 1, RecordingProvider.CALLS
assert RecordingProvider.CALLS[-1] == "model-w1-v2"
assert "w1: state=succeeded attempt=2 cost=?" in out, out
approval_id = next(l.split(": ", 1)[1] for l in out.splitlines() if l.startswith("approval_id:"))
marker = V2DIR / "approvals" / f"{approval_id}.json"
assert marker.is_file(), marker
print(out)
print(f"PASS regenerate consented: exit=0, one provider request ({RecordingProvider.CALLS}), "
      f"att2 created, att1 bytes untouched, approval marker {marker.name} written")
# inspect --attempt 2 now reads the regenerated attempt
code, out2, _e = run_main(["inspect", "demo-v2-run", "w1", "--runs-dir", str(V2RUNS), "--attempt", "2"])
assert code == 0
assert att2_text in out2, "--attempt 2 must read the regenerated att2 bytes"
assert "output:model-w1" not in out2  # the att1 content must be absent
print("PASS inspect --attempt 2: reads stages/w1.att2.md exactly (the regenerated attempt)")

# ---------------------------------------------------------------------------
banner("replaying the same approval: refused, exit 1, no second provider request")
calls_before = len(RecordingProvider.CALLS)
code, out, err = run_main([
    "regenerate", "demo-v2-run", "w1",
    "--model", "model-w1-v2", "--job", str(JOB2), "--runs-dir", str(V2RUNS),
    "--approval", str(marker),
])
assert code == 1, (code, out, err)
assert err.startswith("error:"), repr(err)
assert len(RecordingProvider.CALLS) == calls_before, "a replayed approval must never dispatch"
assert not (V2DIR / "stages" / "w1.att3.json").is_file(), "no attempt 3 may be created"
marker_doc = json.loads(marker.read_text())
assert marker_doc["consumed_at"], "the marker proves the one-shot consumption"
print(f"refusal message: {err.strip()}")
print(f"provider requests during replay: {len(RecordingProvider.CALLS) - calls_before}")
print(f"consumed marker: approvals/{marker.name} (approval_id={marker_doc['approval_id']})")
print("PASS replayed approval: exit=1, typed refusal, zero provider requests, nothing created")

# ---------------------------------------------------------------------------
banner("rerun-synthesis WITH --approve --actor: synthesis attempt 2, selection moves")
calls_before = len(RecordingProvider.CALLS)
code, out, _err = run_main([
    "rerun-synthesis", "demo-v2-run",
    "--job", str(JOB2), "--runs-dir", str(V2RUNS),
    "--approve", "--actor", "demo-operator",
])
assert code == 0, (code, out)
assert (V2DIR / "stages" / "synth.att2.json").is_file()
assert (V2DIR / "stages" / "synth.att2.md").is_file()
assert len(RecordingProvider.CALLS) - calls_before == 1, RecordingProvider.CALLS
assert RecordingProvider.CALLS[-1] == "model-synth"
assert "synth: state=succeeded attempt=2 cost=?" in out, out
selection = json.loads((V2DIR / "selection.json").read_text())["selected_attempts"]
assert selection["synth"] == 2 and selection["w1"] == 1, selection  # regeneration never auto-selects
result_md = (V2DIR / "result.md").read_text()
assert result_md == (V2DIR / "stages" / "synth.att2.md").read_text(), "result.md mirrors the selected synthesis"
print(out)
print("PASS rerun-synthesis consented: exit=0, one request, synth att2 created+selected, result.md mirrors it")

# ---------------------------------------------------------------------------
banner("status after the operations: per-attempt markers + FRESH freshness")
code, out, _err = run_main(["status", "demo-v2-run", "--runs-dir", str(V2RUNS)])
assert code == 0
assert "w1: attempt=1" in out and "synth: attempt=2" in out
assert "synthesis_freshness: FRESH" in out
print(out)

# ---------------------------------------------------------------------------
banner("regenerate on a version-1 run is refused (M-6 read-only), exit 1")
install_tripwires()
v1_before = fingerprint(V1DIR)
code, out, err = run_main([
    "regenerate", "demo-v1-run", "w1",
    "--model", "model-w1-v2", "--job", str(JOB1), "--runs-dir", str(V1RUNS),
])
assert code == 1, (code, out)
assert err.startswith("error:"), repr(err)
assert fingerprint(V1DIR) == v1_before, "a refused operation must write nothing"
print(f"refusal message: {err.strip()}")
print("PASS v1 regenerate refused: exit=1, nothing written, no provider constructed")

# ---------------------------------------------------------------------------
banner("staleness markers: STALE after selection move; integrity_warning after byte flip")
from bots5.storage import write_selection  # the explicit selection command seam
write_selection(V2DIR, "demo-v2-run", {"w1": 2, "w2": 1, "synth": 2})
code, out, _err = run_main(["status", "demo-v2-run", "--runs-dir", str(V2RUNS)])
assert "synthesis_freshness: STALE" in out, out
assert "integrity_warning:" not in out, "selection move alone is a mechanical stale, no defect"
print("PASS selection move -> synthesis_freshness: STALE without integrity warning")
# Move the selection back so the recorded dependency attempt (1) equals the
# selected one again; only then does the digest predicate compare bytes.
write_selection(V2DIR, "demo-v2-run", {"w1": 1, "w2": 1, "synth": 2})
code, out, _err = run_main(["status", "demo-v2-run", "--runs-dir", str(V2RUNS)])
assert "synthesis_freshness: FRESH" in out, out
w1_att1 = V2DIR / "stages" / "w1.att1.md"
w1_att1.write_text(w1_att1.read_text() + "\ndrift\n", encoding="utf-8")
code, out, _err = run_main(["status", "demo-v2-run", "--runs-dir", str(V2RUNS)])
assert "synthesis_freshness: STALE" in out
warnings = [l for l in out.splitlines() if l.startswith("integrity_warning:")]
assert warnings, out
print("PASS output drift -> STALE with " + warnings[0])

print("\nALL DEMONSTRATION PROOFS PASSED")
