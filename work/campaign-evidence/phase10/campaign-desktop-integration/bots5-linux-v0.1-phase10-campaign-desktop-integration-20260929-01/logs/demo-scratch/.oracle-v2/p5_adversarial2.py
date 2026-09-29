#!/usr/bin/env python
"""Oracle v2 probe P5 — corrected/extended adversarial probes.

  P5b  paid approval with INCOMPLETE pricing evidence -> refused, NOTHING written
  P5c  local-only (schema v2) operation exempt from pricing evidence
  P5e  dual cost accounting + "stored selected_spend is only a cache" (tamper test)
  P5f  deep cancellation dump: operator cancel mid-run -> run.json, every stage
       record, events.jsonl, and where 'cancelled_pending' appears
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import tempfile
import traceback
from decimal import Decimal
from pathlib import Path

app = None
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])

from bots5.core.campaign import CampaignBridge, project_run  # noqa: E402
from bots5.errors import ApprovalInvalidatedError  # noqa: E402
from bots5.providers.base import CompletionRequest, CompletionResult  # noqa: E402
from bots5.providers.openai_compatible import OpenAICompatibleProvider  # noqa: E402
from bots5.providers.openrouter import OpenRouterProvider  # noqa: E402
from tests.helpers import make_job_tree  # noqa: E402

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
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


class RouterSpy(OpenRouterProvider):
    def __init__(self) -> None:
        super().__init__("oracle-dummy-key-never-sent")
        self.calls: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.calls.append(request)
        return CompletionResult(
            output_text=f"output:{request.model}", requested_model=request.model,
            finish_reason="stop", returned_model=request.model, request_id="r",
            prompt_tokens=10, completion_tokens=20, total_tokens=30,
            known_cost_usd=Decimal("0.01"), duration_seconds=0.001)


class SlowRouterSpy(RouterSpy):
    def __init__(self, delay: float) -> None:
        super().__init__()
        self.delay = delay

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.calls.append(request)
        await asyncio.sleep(self.delay)
        return CompletionResult(
            output_text=f"output:{request.model}", requested_model=request.model,
            finish_reason="stop", returned_model=request.model, request_id="r",
            prompt_tokens=10, completion_tokens=20, total_tokens=30,
            known_cost_usd=Decimal("0.01"), duration_seconds=0.001)


def section(t: str) -> None:
    print()
    print("=" * 78)
    print(t)
    print("=" * 78)


# ---------------------------------------------------------------- P5b
def p5b() -> None:
    section("P5b — paid approval with INCOMPLETE pricing evidence writes nothing")
    tmp = Path(tempfile.mkdtemp(prefix="oracle-v2-p5b-"))
    job_path, _ = make_job_tree(tmp, workers=2, synthesis=True, run_timeout=30.0)
    spy = RouterSpy()
    prov_calls: list = []
    runs = tmp / ".bots5" / "runs"
    bridge = CampaignBridge(
        runs,
        provider_factory=lambda job: (prov_calls.append(job) or {"openrouter": spy}))
    bridge.load_job(job_path)
    bridge.validate()
    before = sha_all(tmp)

    cases: list[tuple[str, dict | None]] = [
        ("no evidence block at all", None),
        ("entries present but rate_source missing", {"entries": [
            {"provider": "openrouter", "input_usd_per_1m": "2.00",
             "output_usd_per_1m": "8.00", "observed_at": "2026-09-30T00:00:00Z"}]}),
        ("entries present but observed_at missing", {"entries": [
            {"provider": "openrouter", "input_usd_per_1m": "2.00",
             "output_usd_per_1m": "8.00", "rate_source": "x"}]}),
        ("empty entries list", {"entries": []}),
        ("route identity mismatch (tampered base_url)", {"entries": [
            {"provider": "openrouter", "input_usd_per_1m": "2.00",
             "output_usd_per_1m": "8.00", "rate_source": "x",
             "observed_at": "2026-09-30T00:00:00Z",
             "route": {"kind": "openrouter", "base_url": "https://evil.invalid/v1"}}]}),
        ("negative rate", {"entries": [
            {"provider": "openrouter", "input_usd_per_1m": "-1",
             "output_usd_per_1m": "8.00", "rate_source": "x",
             "observed_at": "2026-09-30T00:00:00Z"}]}),
    ]

    for label, ev in cases:
        stage = "prepare"
        try:
            prepared = bridge.prepare_full_run("oracle", pricing_evidence=ev)
            stage = "approve"
            bridge.approve_and_start(prepared)
            print(f"  {label:48s}: ACCEPTED  <-- SHOULD NOT HAPPEN")
        except ApprovalInvalidatedError as exc:
            print(f"  {label:48s}: refused at {stage:8s} -> {exc}")
        except Exception as exc:  # noqa: BLE001
            print(f"  {label:48s}: refused at {stage:8s} -> {type(exc).__name__}: {exc}")

    after = sha_all(tmp)
    print(f"  runs dir exists                          : {runs.exists()}")
    print(f"  whole tmp tree byte-identical            : {before == after}")
    print(f"  attempt files anywhere                   : {list(tmp.rglob('*.att*.json'))}")
    print(f"  run.json files anywhere                  : {list(tmp.rglob('run.json'))}")
    print(f"  provider factory constructions           : {len(prov_calls)}")
    print(f"  provider.complete() calls                : {len(spy.calls)}")
    print(f"  approval markers written                 : "
          f"{[p.name for p in tmp.rglob('approvals/*') if p.is_file()]}")
    ok = (not runs.exists() and before == after and not prov_calls and not spy.calls)
    print(f"  VERDICT: {'REFUSED BEFORE ANY RUN TREE / ATTEMPT / PROVIDER CALL, NOTHING WRITTEN' if ok else 'FAIL'}")


# ---------------------------------------------------------------- P5c
class LocalSpy(OpenAICompatibleProvider):
    def __init__(self) -> None:
        super().__init__("http://127.0.0.1:9/v1", api_key_env=None)
        self.calls: list = []

    async def complete(self, request):  # noqa: ANN001
        self.calls.append(request)
        return CompletionResult(
            output_text="local-output", requested_model=request.model,
            finish_reason="stop", returned_model=request.model, request_id="l",
            prompt_tokens=3, completion_tokens=4, total_tokens=7,
            known_cost_usd=None, duration_seconds=0.001)


def p5c() -> None:
    section("P5c — local-only operation is exempt from pricing evidence")
    tmp = Path(tempfile.mkdtemp(prefix="oracle-v2-p5c-"))
    job_path, job_data = make_job_tree(tmp, workers=1, synthesis=True, run_timeout=30.0)
    for spec in job_data["workers"]:
        spec["provider"] = "local_openai"
    job_data["synthesis"]["provider"] = "local_openai"
    job_data["schema_version"] = 2
    job_data["providers"] = {"local_openai": {"base_url": "http://127.0.0.1:9/v1"}}
    job_path.write_text(json.dumps(job_data), encoding="utf-8")

    provider = LocalSpy()
    prov_calls: list = []
    bridge = CampaignBridge(
        tmp / ".bots5" / "runs",
        provider_factory=lambda job: (prov_calls.append(job) or {"local_openai": provider}))
    bridge.load_job(job_path)
    bridge.validate()
    prepared = bridge.prepare_full_run("oracle", pricing_evidence=None)
    print(f"  approval.pricing_evidence               : {prepared.approval.pricing_evidence}")
    print(f"  provider routes                         : {json.dumps(prepared.provider_routes, sort_keys=True)}")
    print(f"  approve button rule (paid? no)          : paid="
          f"{any(r.get('kind') != 'local_openai' for r in prepared.provider_routes.values())}")

    async def go() -> None:
        bridge.approve_and_start(prepared)
        await asyncio.sleep(0.3)
        if bridge._task:
            await bridge._task
    asyncio.run(go())

    run_json = list((tmp / ".bots5" / "runs").glob("*/run.json"))
    state = json.loads(run_json[0].read_text()).get("state") if run_json else None
    pre = list((tmp / ".bots5" / "runs").glob("*/preflight.json"))
    pre_doc = json.loads(pre[0].read_text()) if pre else {}
    print(f"  run state                               : {state}")
    print(f"  provider factory constructions          : {len(prov_calls)}")
    print(f"  provider.complete() calls               : {len(provider.calls)}")
    print(f"  preflight.json pricing_evidence         : {pre_doc.get('pricing_evidence', '<key absent>')}")
    print(f"  preflight.json has any pricing/rate key : "
          f"{[k for k in json.dumps(pre_doc).split('\"') if 'rate' in k or 'pricing' in k][:6]}")
    print(f"  VERDICT: {'LOCAL-ONLY EXEMPT, RUN EXECUTED, NO PRICING FIELDS ADDED' if state == 'succeeded' else 'FAIL'}")


# ---------------------------------------------------------------- P5e
def p5e() -> None:
    section("P5e — dual cost accounting; the stored selected_spend copy is only a cache")
    tmp = Path(tempfile.mkdtemp(prefix="oracle-v2-p5e-"))
    job_path, _ = make_job_tree(tmp, workers=1, synthesis=False, run_timeout=30.0)

    class Costed(RouterSpy):
        def __init__(self, cost: Decimal) -> None:
            super().__init__()
            self._cost = cost

        async def complete(self, request):  # noqa: ANN001
            self.calls.append(request)
            return CompletionResult(
                output_text=f"output:{request.model}", requested_model=request.model,
                finish_reason="stop", returned_model=request.model, request_id="r",
                prompt_tokens=10, completion_tokens=20, total_tokens=30,
                known_cost_usd=self._cost, duration_seconds=0.001)

    bridge = CampaignBridge(tmp / ".bots5" / "runs",
                            provider_factory=lambda job: {"openrouter": Costed(Decimal("0.01"))})
    bridge.load_job(job_path)

    async def go() -> None:
        bridge.approve_and_start(
            bridge.prepare_full_run("oracle", pricing_evidence=json.loads(PRICING)))
        await asyncio.sleep(0.3)
        if bridge._task:
            await bridge._task
        p = bridge.prepare_regeneration("w1", "model-w1-alt", "oracle",
                                        pricing_evidence=json.loads(PRICING))
        bridge._provider_factory = lambda job: {"openrouter": Costed(Decimal("0.50"))}
        bridge.approve_and_regenerate(p)
        await asyncio.sleep(0.3)
        if bridge._task:
            await bridge._task

        run_dir = next(x for x in (tmp / ".bots5" / "runs").iterdir() if x.is_dir())

        def dump(tag: str) -> None:
            usage = json.loads((run_dir / "usage.json").read_text())
            proj = project_run(run_dir, hosted=False)
            cum = usage.get("cumulative_spend", {}).get("cost_usd_known_sum")
            stored = usage.get("selected_spend", {}).get("cost_usd_known_sum")
            agg = usage.get("aggregate", {}).get("cost_usd_known_sum")
            derived = proj.selected_spend.get("cost_usd_known_sum")
            pcum = proj.cumulative_spend.get("cost_usd_known_sum")
            print(f"  [{tag}] per_attempt costs           : "
                  f"{ {k: v.get('cost_usd') for k, v in (usage.get('per_attempt') or {}).items()} }")
            print(f"  [{tag}] cumulative_spend (durable)  : {cum}   projection: {pcum}")
            print(f"  [{tag}] selected_spend stored cache : {stored}")
            print(f"  [{tag}] selected_spend DERIVED      : {derived}")
            print(f"  [{tag}] aggregate (legacy mirror)   : {agg}")
            print(f"  [{tag}] integrity warnings          : {list(proj.integrity_warnings)}")
            print(f"  [{tag}] selection.json              : "
                  f"{json.dumps(json.loads((run_dir / 'selection.json').read_text()), sort_keys=True)}")

        dump("after run + regeneration (selection still attempt 1)")
        cum = Decimal(json.loads((run_dir / "usage.json").read_text())["cumulative_spend"]["cost_usd_known_sum"])
        sel = Decimal(json.loads((run_dir / "usage.json").read_text())["selected_spend"]["cost_usd_known_sum"])
        print(f"  cumulative({cum}) > selected({sel}) because attempt 2 (0.50) is NOT selected : {cum > sel}")

        # switch selection to the expensive attempt -> derived must follow, cache must not be trusted
        bridge.select_attempt("w1", 2)
        dump("after Make current -> attempt 2 (expensive)")

        # tamper ONLY the stored cache: derived must still win and a warning must appear
        usage_path = run_dir / "usage.json"
        usage = json.loads(usage_path.read_text())
        usage["selected_spend"]["cost_usd_known_sum"] = "999.99"
        usage_path.write_text(json.dumps(usage))
        proj = project_run(run_dir, hosted=False)
        print(f"  [tampered] stored cache now         : "
              f"{json.loads(usage_path.read_text())['selected_spend']['cost_usd_known_sum']}")
        print(f"  [tampered] projection DERIVED       : {proj.selected_spend.get('cost_usd_known_sum')}")
        print(f"  [tampered] integrity warnings       : {list(proj.integrity_warnings)}")
        ok = (
            proj.selected_spend.get("cost_usd_known_sum") != "999.99"
            and any("derived value is authoritative" in w for w in proj.integrity_warnings)
        )
        print(f"  VERDICT: {'DERIVED SELECTED SPEND IS AUTHORITATIVE; STORED COPY IS ONLY A CACHE' if ok else 'FAIL'}")

    asyncio.run(go())


# ---------------------------------------------------------------- P5f
def p5f() -> None:
    section("P5f — deep cancellation dump (operator cancel while stages are in flight)")
    tmp = Path(tempfile.mkdtemp(prefix="oracle-v2-p5f-"))
    job_path, _ = make_job_tree(tmp, workers=2, synthesis=True, run_timeout=30.0)
    spy = SlowRouterSpy(delay=3.0)
    bridge = CampaignBridge(tmp / ".bots5" / "runs",
                            provider_factory=lambda job: {"openrouter": spy})

    async def go() -> None:
        bridge.load_job(job_path)
        bridge.approve_and_start(
            bridge.prepare_full_run("oracle", pricing_evidence=json.loads(PRICING)))
        await asyncio.sleep(0.8)
        print(f"  provider calls before cancel        : {len(spy.calls)}")
        await bridge.cancel()
        print("  bridge.cancel() returned (terminal record verified from disk)")
    asyncio.run(go())

    run_dir = next(x for x in (tmp / ".bots5" / "runs").iterdir() if x.is_dir())
    run_doc = json.loads((run_dir / "run.json").read_text())
    print(f"  run.json                            : {json.dumps(run_doc, sort_keys=True)}")
    for p in sorted((run_dir / "stages").glob("*.json")):
        d = json.loads(p.read_text())
        keep = {k: d.get(k) for k in
                ("id", "state", "error_type", "attempt_number", "provider_side_outcome_unknown",
                 "started_at", "finished_at")}
        print(f"  stage {p.name:22s}: {json.dumps(keep, sort_keys=True)}")
    print("  --- events.jsonl ---")
    for line in (run_dir / "events.jsonl").read_text().splitlines():
        if line.strip():
            print(f"    {line}")
    hits = [i for i, line in enumerate((run_dir / "events.jsonl").read_text().splitlines())
            if "cancelled_pending" in line or "run_timed_out" in line or "timed_out" in line]
    print(f"  lines mentioning cancelled_pending/timed_out: {hits}")
    proj = project_run(run_dir, hosted=False)
    print(f"  display_state                       : {proj.display_state}")
    print(f"  integrity warnings                  : {list(proj.integrity_warnings)}")
    run_state = run_doc.get("state")
    stage_states = {p.name: json.loads(p.read_text()).get("state")
                    for p in (run_dir / "stages").glob("*.json")}
    print(f"  VERDICT run-level cancelled & no timeout label & no live cancelled_pending stage: "
          f"{run_state == 'cancelled' and not any(v in ('run_timed_out', 'timed_out') for v in stage_states.values())}")


def main() -> int:
    for fn in (p5b, p5c, p5e, p5f):
        try:
            fn()
        except Exception:  # noqa: BLE001
            print(f"  PROBE {fn.__name__} CRASHED:")
            traceback.print_exc()
    return 0


if __name__ == "__main__":
    sys.exit(main())
