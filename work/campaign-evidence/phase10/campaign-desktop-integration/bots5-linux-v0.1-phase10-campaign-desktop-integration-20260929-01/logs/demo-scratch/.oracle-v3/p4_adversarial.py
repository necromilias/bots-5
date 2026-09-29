#!/usr/bin/env python
"""Oracle v2 probe P4 — remaining adversarial probes.

  P4a  dock Approve for a prepared SYNTHESIS RERUN (same dispatch question as P1g)
  P4b  paid approval with INCOMPLETE pricing evidence refused before any run tree,
       attempt or provider call, writing nothing
  P4c  local-only operation is exempt (no pricing evidence needed)
  P4d  legacy plain run verb / run_job with no snapshot+approval still executes
  P4e  dual cost accounting: cumulative_spend vs derived selected_spend; the stored
       selected_spend copy is only a cache (tamper it -> derived wins + warning)
  P4f  cancellation truth: operator cancel -> durable CANCELLED + stage "cancelled",
       never "timed_out", never left cancelled_pending; hard-kill cancelled_pending
       reads interrupted/uncertain; a durable "running" run with no host is
       interrupted/uncertain (never succeeded/resumable)
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import traceback
from decimal import Decimal
from pathlib import Path

from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QLineEdit

app = QApplication.instance() or QApplication([])

from bots5.core.campaign import CampaignBridge  # noqa: E402
from bots5.desktop.campaign_dock import CampaignDockWidget  # noqa: E402
from bots5.errors import ApprovalInvalidatedError  # noqa: E402
from bots5.providers.base import CompletionRequest, CompletionResult  # noqa: E402
from bots5.providers.openrouter import OpenRouterProvider  # noqa: E402
from bots5.runner import run_job  # noqa: E402
from tests.helpers import make_job_tree  # noqa: E402

OK = "OK"
FAIL = "FAIL"


class Spy(OpenRouterProvider):
    def __init__(self, cost: Decimal = Decimal("0.01"), delay: float = 0.0) -> None:
        super().__init__("oracle-dummy-key-never-sent")
        self.calls: list[CompletionRequest] = []
        self.cost = cost
        self.delay = delay

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.calls.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        return CompletionResult(
            output_text=f"output:{request.model}",
            requested_model=request.model,
            finish_reason="stop",
            returned_model=request.model,
            request_id="req",
            prompt_tokens=10,
            completion_tokens=20,
            total_tokens=30,
            known_cost_usd=self.cost,
            duration_seconds=0.001,
        )


PRICING = json.dumps({
    "entries": [{
        "provider": "openrouter",
        "input_usd_per_1m": "2.00",
        "output_usd_per_1m": "8.00",
        "rate_source": "https://example.invalid/pricing",
        "observed_at": "2026-09-30T00:00:00Z",
    }]
})


def sha_all(root: Path) -> dict[str, str]:
    import hashlib
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def section(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


# --------------------------------------------------------------------------- P4a
def p4a() -> None:
    section("P4a — dock Approve after preparing a SYNTHESIS RERUN")
    tmp = Path(tempfile.mkdtemp(prefix="oracle-v2-p4a-"))
    job_path, _ = make_job_tree(tmp, workers=1, synthesis=True, run_timeout=30.0)
    spy = Spy()
    calls = []

    def factory(runs: Path) -> CampaignBridge:
        calls.append(runs)
        return CampaignBridge(runs, provider_factory=lambda job: {"openrouter": spy})

    QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (str(job_path), ""))
    dock = CampaignDockWidget(bridge_factory=factory)
    dock._pricing_input.setPlainText(PRICING)
    dock._on_load_job()
    dock._on_validate()

    async def go() -> None:
        dock._on_approve()
        await asyncio.sleep(0.2)
        if dock._bridge._task:
            await dock._bridge._task
        await dock._refresh_projection_async()
        # now prepare the synthesis rerun through the dock's own control
        dock._on_rerun_synthesis()
        print(f"  status after Rerun synthesis : {dock._status_label.text()!r}")
        print(f"  prepared operation           : {getattr(dock._prepared_operation, 'operation', None)}")
        print(f"  approve enabled              : {dock._approve_button.isEnabled()}")
        pre = sha_all(next(p for p in (calls[0]).iterdir() if p.is_dir()))
        n_calls = len(spy.calls)
        dock._on_approve()                      # the operator presses Approve
        await asyncio.sleep(0.4)
        if dock._bridge._task:
            await dock._bridge._task
        run_dir = next(p for p in (calls[0]).iterdir() if p.is_dir())
        atts = sorted(p.name for p in (run_dir / "stages").glob("synth.att*.json"))
        print(f"  status after Approve         : {dock._status_label.text()!r}")
        print(f"  synthesis attempt files      : {atts}")
        print(f"  run tree changed             : {sha_all(run_dir) != pre}")
        print(f"  provider calls delta         : {len(spy.calls) - n_calls}")
        print(f"  VERDICT: {'SYNTHESIS RERUN EXECUTED' if len(atts) > 1 else 'SYNTHESIS RERUN NOT EXECUTED BY THE DESKTOP <-- DEFECT'}")

    asyncio.run(go())


# --------------------------------------------------------------------------- P4b
def p4b() -> None:
    section("P4b — paid approval with INCOMPLETE pricing evidence")
    tmp = Path(tempfile.mkdtemp(prefix="oracle-v2-p4b-"))
    job_path, _ = make_job_tree(tmp, workers=2, synthesis=True, run_timeout=30.0)
    spy = Spy()
    prov_calls = []
    bridge = CampaignBridge(tmp / ".bots5" / "runs",
                            provider_factory=lambda job: (prov_calls.append(job) or {"openrouter": spy}))
    bridge.load_job(job_path)
    bridge.validate()
    runs = tmp / ".bots5" / "runs"
    before = sha_all(tmp) if runs.exists() else {}

    # (1) no pricing evidence at all
    prepared = bridge.prepare_full_run("oracle", pricing_evidence=None)
    print(f"  approval.pricing_evidence    : {prepared.approval.pricing_evidence}")
    try:
        bridge.approve_and_start(prepared)
        print(f"  {FAIL}: no-pricing approval ACCEPTED")
    except ApprovalInvalidatedError as exc:
        print(f"  {OK}: refused -> {type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001
        print(f"  refused with {type(exc).__name__}: {exc}")

    # (2) incomplete: rates present, rate_source missing
    bad = json.loads(PRICING)
    del bad["entries"][0]["rate_source"]
    prepared2 = bridge.prepare_full_run("oracle", pricing_evidence=bad)
    try:
        bridge.approve_and_start(prepared2)
        print(f"  {FAIL}: incomplete pricing ACCEPTED")
    except Exception as exc:  # noqa: BLE001
        print(f"  {OK}: refused -> {type(exc).__name__}: {exc}")

    # (3) incomplete: does not cover every paid route
    bad2 = {"entries": []}
    try:
        p3 = bridge.prepare_full_run("oracle", pricing_evidence=bad2)
        bridge.approve_and_start(p3)
        print(f"  {FAIL}: empty entries ACCEPTED")
    except Exception as exc:  # noqa: BLE001
        print(f"  {OK}: refused -> {type(exc).__name__}: {exc}")

    print(f"  runs dir exists               : {runs.exists()}")
    print(f"  run tree unchanged            : {sha_all(tmp) == before if runs.exists() else runs.exists() is False}")
    print(f"  attempt files anywhere        : {list(tmp.rglob('*.att*.json'))}")
    print(f"  provider factory calls        : {len(prov_calls)}")
    print(f"  provider.complete() calls     : {len(spy.calls)}")
    print(f"  approvals dir contents        : {[p.name for p in sorted((tmp / '.bots5').rglob('approvals/*'))] if (tmp / '.bots5').exists() else 'no .bots5 at all'}")
    verdict = (not runs.exists() and not prov_calls and not spy.calls)
    print(f"  VERDICT: {'REFUSED BEFORE ANY RUN TREE / PROVIDER CALL, NOTHING WRITTEN' if verdict else FAIL}")


# --------------------------------------------------------------------------- P4c
def p4c() -> None:
    section("P4c — local-only operation needs no pricing evidence")
    tmp = Path(tempfile.mkdtemp(prefix="oracle-v2-p4c-"))
    job_path, _ = make_job_tree(tmp, workers=1, synthesis=True, run_timeout=30.0)
    doc = json.loads(job_path.read_text())
    for w in doc["workers"]:
        w["provider"] = "local_openai"
    doc["synthesis"]["provider"] = "local_openai"
    doc["providers"] = {"local_openai": {"base_url": "http://127.0.0.1:9/v1"}}
    job_path.write_text(json.dumps(doc))

    spy = Spy()
    prov_calls = []

    def pf(job):  # noqa: ANN001
        prov_calls.append(job)
        return {"local_openai": spy}

    # the engine's own route check requires an OpenAICompatibleProvider instance
    from bots5.providers.openai_compatible import OpenAICompatibleProvider

    class LocalSpy(OpenAICompatibleProvider):
        def __init__(self) -> None:
            super().__init__("http://127.0.0.1:9/v1", api_key_env=None)
            self.calls = []

        async def complete(self, request):  # noqa: ANN001
            self.calls.append(request)
            return CompletionResult(
                output_text="local", requested_model=request.model, finish_reason="stop",
                returned_model=request.model, prompt_tokens=1, completion_tokens=1,
                total_tokens=2, known_cost_usd=None, duration_seconds=0.001)

    ls = LocalSpy()

    def pf2(job):  # noqa: ANN001
        prov_calls.append(job)
        return {"local_openai": ls}

    bridge = CampaignBridge(tmp / ".bots5" / "runs", provider_factory=pf2)
    bridge.load_job(job_path)
    bridge.validate()
    prepared = bridge.prepare_full_run("oracle", pricing_evidence=None)
    print(f"  approval.pricing_evidence    : {prepared.approval.pricing_evidence}")
    print(f"  provider routes              : {json.dumps(prepared.provider_routes, sort_keys=True)}")
    try:
        bridge.approve_and_start(prepared)

        async def wait() -> None:
            await asyncio.sleep(0.3)
            if bridge._task:
                await bridge._task
        asyncio.run(wait())
        run_json = list((tmp / ".bots5" / "runs").glob("*/run.json"))
        state = json.loads(run_json[0].read_text()).get("state") if run_json else None
        print(f"  run state                    : {state}")
        print(f"  provider factory calls       : {len(prov_calls)}")
        print(f"  complete() calls             : {len(ls.calls)}")
        preflight = list((tmp / ".bots5" / "runs").glob("*/preflight.json"))
        if preflight:
            doc = json.loads(preflight[0].read_text())
            print(f"  preflight pricing_evidence   : {doc.get('pricing_evidence')}")
        print(f"  VERDICT: {'LOCAL-ONLY EXEMPT AND IT RAN' if state == 'succeeded' else FAIL}")
    except Exception as exc:  # noqa: BLE001
        print(f"  {FAIL}: local-only approval refused: {type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- P4d
def p4d() -> None:
    section("P4d — legacy path: run_job with NO snapshot and NO approval")
    tmp = Path(tempfile.mkdtemp(prefix="oracle-v2-p4d-"))
    job_path, _ = make_job_tree(tmp, workers=1, synthesis=False, run_timeout=30.0)
    from bots5.manifest import load_job, validate_referenced_files
    job = load_job(job_path)
    validate_referenced_files(job)
    spy = Spy()
    result = asyncio.run(run_job(job, {"openrouter": spy}))
    run_json = list((tmp / ".bots5" / "runs").glob("*/run.json"))
    doc = json.loads(run_json[0].read_text())
    print(f"  run_job(no snapshot/approval) result : {type(result).__name__} "
          f"state={getattr(getattr(result, 'state', None), 'value', None)}")
    print(f"  run.json evidence_version present   : {'evidence_version' in doc}")
    print(f"  complete() calls                    : {len(spy.calls)}")
    stage_files = sorted(p.name for p in run_json[0].parent.glob("stages/*"))
    print(f"  artifacts                           : {stage_files}")
    stage = json.loads((run_json[0].parent / 'stages' / 'w1.json').read_text())
    print(f"  stage has attempt_number key        : {'attempt_number' in stage} value={stage.get('attempt_number')}")
    print(f"  stage has pricing keys              : {[k for k in stage if 'pricing' in k or 'rate' in k]}")
    pre = list(run_json[0].parent.glob("preflight.json"))
    print(f"  preflight.json written on legacy run: {bool(pre)}")
    print(f"  VERDICT: legacy run executed with no approval gate required: "
          f"{'YES' if len(spy.calls) == 1 and not pre else FAIL}")


# --------------------------------------------------------------------------- P4e
def p4e() -> None:
    section("P4e — dual cost accounting (cumulative vs derived selected; stored copy is a cache)")
    tmp = Path(tempfile.mkdtemp(prefix="oracle-v2-p4e-"))
    job_path, _ = make_job_tree(tmp, workers=1, synthesis=False, run_timeout=30.0)
    spy1 = Spy(cost=Decimal("0.01"))
    bridge = CampaignBridge(tmp / ".bots5" / "runs",
                            provider_factory=lambda job: {"openrouter": spy1})
    bridge.load_job(job_path)

    async def go() -> None:
        bridge.approve_and_start(bridge.prepare_full_run("oracle", pricing_evidence=json.loads(PRICING)))
        await asyncio.sleep(0.3)
        if bridge._task:
            await bridge._task
        # regenerate with a DIFFERENT (higher) cost so cumulative != selected
        p = bridge.prepare_regeneration("w1", "model-w1-alt", "oracle",
                                        pricing_evidence=json.loads(PRICING))
        spy2 = Spy(cost=Decimal("0.50"))
        bridge._provider_factory = lambda job: {"openrouter": spy2}
        bridge.approve_and_regenerate(p)
        await asyncio.sleep(0.3)
        if bridge._task:
            await bridge._task
        await bridge.projection_async()

        run_dir = next(x for x in (tmp / ".bots5" / "runs").iterdir() if x.is_dir())
        usage = json.loads((run_dir / "usage.json").read_text())
        print(f"  per_attempt                     : {json.dumps({k: str(v) for k, v in usage.get('per_attempt', {}).items()}, sort_keys=True)[:400]}")
        print(f"  cumulative_spend                : {json.dumps(usage.get('cumulative_spend'), sort_keys=True)}")
        print(f"  stored selected_spend (cache)   : {json.dumps(usage.get('selected_spend'), sort_keys=True)}")
        print(f"  aggregate                       : {json.dumps(usage.get('aggregate'), sort_keys=True)}")

        proj = await bridge.projection_async()
        print(f"  derived selected_spend          : {json.dumps({k: str(v) for k, v in proj.selected_spend.items()}, sort_keys=True)}")
        print(f"  projection cumulative_spend     : {json.dumps({k: str(v) for k, v in proj.cumulative_spend.items()}, sort_keys=True)}")
        print(f"  integrity warnings (cache)      : {list(proj.integrity_warnings)}")

        # consistency: cumulative >= selected
        cum = Decimal(str(proj.cumulative_spend.get("cost_usd") or 0))
        sel = Decimal(str(proj.selected_spend.get("cost_usd") or 0))
        print(f"  cumulative({cum}) >= selected({sel})          : {cum >= sel}")
        cache = usage.get("selected_spend")
        cache_matches = json.dumps(cache, sort_keys=True) == json.dumps(
            {k: (str(v) if isinstance(v, Decimal) else v) for k, v in proj.selected_spend.items()},
            sort_keys=True)
        print(f"  stored cache == derived (both attempts selected?) : see below")

        # switch selection to attempt 1 (the cheap one) -> derived changes, cache stale
        bridge.select_attempt("w1", 1)
        proj2 = await bridge.projection_async()
        usage2 = json.loads((run_dir / "usage.json").read_text())
        print(f"  after select attempt 1: derived selected_spend  : "
              f"{json.dumps({k: str(v) for k, v in proj2.selected_spend.items()}, sort_keys=True)}")
        print(f"  after select attempt 1: stored cache           : "
              f"{json.dumps(usage2.get('selected_spend'), sort_keys=True)}")
        print(f"  integrity warnings                              : {list(proj2.integrity_warnings)}")
        sel2 = Decimal(str(proj2.selected_spend.get("cost_usd") or 0))
        stored2 = Decimal(str((usage2.get('selected_spend') or {}).get('cost_usd') or 0))
        print(f"  derived({sel2}) != stored cache({stored2}) and derived wins : {sel2 != stored2}")
        print(f"  cumulative unchanged by selection               : "
              f"{json.dumps(usage2.get('cumulative_spend'), sort_keys=True) == json.dumps(usage.get('cumulative_spend'), sort_keys=True)}")
        good = (cum >= sel and sel2 != stored2
                and any("derived value is authoritative" in w for w in proj2.integrity_warnings))
        print(f"  VERDICT: {'DUAL ACCOUNTING CONSISTENT; STORED SELECTED IS ONLY A CACHE' if good else FAIL}")

    asyncio.run(go())


# --------------------------------------------------------------------------- P4f
def p4f() -> None:
    section("P4f — cancellation matrix and durable cancelled_pending truth")
    from bots5.core.campaign import project_run
    from bots5.runner import RunState  # noqa: F401

    # (i) operator cancellation of a live run
    tmp = Path(tempfile.mkdtemp(prefix="oracle-v2-p4f-"))
    job_path, _ = make_job_tree(tmp, workers=2, synthesis=True, run_timeout=30.0)
    spy = Spy(delay=1.5)
    bridge = CampaignBridge(tmp / ".bots5" / "runs",
                            provider_factory=lambda job: {"openrouter": spy})

    def cancelled() -> None:
        bridge.load_job(job_path)

        async def go() -> None:
            bridge.approve_and_start(
                bridge.prepare_full_run("oracle", pricing_evidence=json.loads(PRICING)))
            await asyncio.sleep(0.6)
            await bridge.cancel()
            await asyncio.sleep(0.3)
        asyncio.run(go())

    cancelled()
    run_dir = next(x for x in (tmp / ".bots5" / "runs").iterdir() if x.is_dir())
    run_doc = json.loads((run_dir / "run.json").read_text())
    print(f"  (i) run.json state                    : {run_doc.get('state')}")
    stage_states = {p.name: json.loads(p.read_text()).get("state")
                    for p in sorted((run_dir / "stages").glob("*.json"))}
    stage_errs = {p.name: json.loads(p.read_text()).get("error_type")
                  for p in sorted((run_dir / "stages").glob("*.json"))}
    print(f"  (i) stage states                      : {json.dumps(stage_states, sort_keys=True)}")
    print(f"  (i) stage error_type                  : {json.dumps(stage_errs, sort_keys=True)}")
    print(f"  (i) any stage labelled timed_out      : {any(v == 'run_timed_out' or v == 'timed_out' for v in stage_errs.values())}")
    print(f"  (i) any cancelled_pending left        : "
          f"{[p.name for p in run_dir.rglob('*') if p.is_file() and 'cancelled_pending' in p.read_text(errors='ignore')][:5] or 'none in any file'}")
    events = (run_dir / "events.jsonl").read_text().splitlines()
    kinds = [json.loads(e).get("event") for e in events if e.strip()]
    print(f"  (i) event kinds                       : {kinds}")
    proj = project_run(run_dir, hosted=False)
    print(f"  (i) display_state                     : {proj.display_state}")

    # (ii) hard-kill: durable cancelled_pending, no hosted task
    tmp2 = Path(tempfile.mkdtemp(prefix="oracle-v2-p4f2-"))
    job_path2, _ = make_job_tree(tmp2, workers=1, synthesis=False, run_timeout=30.0)
    spy2 = Spy()
    b2 = CampaignBridge(tmp2 / ".bots5" / "runs",
                        provider_factory=lambda job: {"openrouter": spy2})

    def hardkill() -> None:
        b2.load_job(job_path2)

        async def go() -> None:
            b2.approve_and_start(b2.prepare_full_run(
                "oracle", pricing_evidence=json.loads(PRICING)))
            await asyncio.sleep(0.05)
            # simulate power loss: kill the task WITHOUT terminalization
            if b2._task:
                b2._task.cancel()
                try:
                    await b2._task
                except BaseException:
                    pass
        try:
            asyncio.run(go())
        except BaseException as exc:  # noqa: BLE001
            print(f"  (ii) hard-kill simulation raised {type(exc).__name__}")

    hardkill()
    rd2 = next(x for x in (tmp2 / ".bots5" / "runs").iterdir() if x.is_dir())
    # force a genuinely stale/unfinished durable record: state running + cancelled_pending
    run2 = json.loads((rd2 / "run.json").read_text())
    run2["state"] = "running"
    (rd2 / "run.json").write_text(json.dumps(run2))
    for p in (rd2 / "stages").glob("*.json"):
        d = json.loads(p.read_text())
        d["state"] = "cancelled_pending"
        p.write_text(json.dumps(d))
    proj2 = project_run(rd2, hosted=False)
    print(f"  (ii) durable run state                : running (host gone)")
    print(f"  (ii) durable stage state              : cancelled_pending")
    print(f"  (ii) display_state                    : {proj2.display_state}")
    print(f"  (ii) integrity warnings               : {list(proj2.integrity_warnings)}")
    ok2 = proj2.display_state not in ("succeeded", "resumable", "failed")
    print(f"  (ii) never displayed as success/resumable : {ok2}")

    # (iii) durable running run, no host -> interrupted/uncertain
    proj2b = project_run(rd2, hosted=False)
    print(f"  (iii) durable running w/o host        : {proj2b.display_state}")
    print(f"  VERDICT: cancellation matrix truthful: "
          f"{'YES' if (run_doc.get('state') == 'cancelled' and ok2) else FAIL}")


def main() -> int:
    for fn in (p4a, p4b, p4c, p4d, p4e, p4f):
        try:
            fn()
        except Exception:  # noqa: BLE001
            print(f"  PROBE {fn.__name__} CRASHED:")
            traceback.print_exc()
    return 0


if __name__ == "__main__":
    sys.exit(main())
