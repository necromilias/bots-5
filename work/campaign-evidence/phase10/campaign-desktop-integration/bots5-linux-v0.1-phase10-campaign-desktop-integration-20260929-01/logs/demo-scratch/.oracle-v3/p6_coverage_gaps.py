#!/usr/bin/env python
"""Oracle v2 probe P6 — coverage the campaign record claims but that v4 does not test.

  P6a  window attach: MainWindow WITHOUT a factory creates no campaign dock and
       leaves the View menu unchanged; WITH a factory creates a hidden dock and
       a View-menu Campaign action   (T0.10 / first-oracle D-5 positive path)
  P6b  shutdown with an ACTIVE campaign (fence T0.8):
         b1  CampaignDockWidget.drain() with a hosted run -> durable terminal record
         b2  DesktopRuntime._close_campaigns() bounded stage -> cancel + drain
  P6c  raw durable attempt record of a cancelled run + the synthesis freshness
       report (what field carries the stage-level 'cancelled' label)
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import traceback
from decimal import Decimal
from pathlib import Path

from PySide6.QtWidgets import QApplication, QFileDialog, QDockWidget

app = QApplication.instance() or QApplication([])

from bots5.bootstrap.desktop import DesktopRuntime  # noqa: E402
from bots5.core.campaign import CampaignBridge, project_run  # noqa: E402
from bots5.desktop.campaign_dock import CampaignDockWidget  # noqa: E402
from bots5.providers.base import CompletionRequest, CompletionResult  # noqa: E402
from bots5.providers.openrouter import OpenRouterProvider  # noqa: E402
from tests.helpers import make_job_tree  # noqa: E402

PRICING = json.dumps({
    "entries": [{
        "provider": "openrouter",
        "input_usd_per_1m": "2.00", "output_usd_per_1m": "8.00",
        "rate_source": "https://example.invalid/pricing",
        "observed_at": "2026-09-30T00:00:00Z",
    }]
})


class Spy(OpenRouterProvider):
    def __init__(self, delay: float = 0.0) -> None:
        super().__init__("oracle-dummy-key-never-sent")
        self.calls: list[CompletionRequest] = []
        self.delay = delay

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.calls.append(request)
        if self.delay:
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


def view_menu_texts(window) -> list[str]:
    for action in window.actions():
        pass
    menubar = window.menuBar()
    for act in menubar.actions():
        if act.text().replace("&", "") == "View":
            menu = act.menu()
            return [a.text() for a in menu.actions()]
    return []


def make_slow_job_tree(tmp: Path, delay_label: str = "slow", workers: int = 1,
                       synthesis: bool = True) -> Path:
    job_path, data = make_job_tree(tmp, workers=workers, synthesis=synthesis,
                                   run_timeout=30.0)
    for spec in data["workers"]:
        spec["timeout_seconds"] = 60.0
    if data.get("synthesis"):
        data["synthesis"]["timeout_seconds"] = 60.0
    data["execution"]["run_timeout_seconds"] = 60.0
    job_path.write_text(json.dumps(data))
    return job_path


# --------------------------------------------------------------------- P6a
def p6a() -> None:
    section("P6a — window attach (T0.10) executed, not read")
    from bots5.desktop.window import MainWindow
    from tests.test_phase9_desktop_slice_e import _make_application

    tmp = Path(tempfile.mkdtemp(prefix="oracle-v2-p6a-"))
    application, store, authority = _make_application(tmp / "root")
    windows = []
    try:
        # (1) no factory
        w1 = MainWindow(application)
        windows.append(w1)
        docks1 = w1.findChildren(QDockWidget, "campaignDock")
        view1 = view_menu_texts(w1)
        print(f"  WITHOUT factory: campaignDock children = {len(docks1)}")
        print(f"  WITHOUT factory: has campaign_dock_action attr = {hasattr(w1, 'campaign_dock_action')}")
        print(f"  WITHOUT factory: View menu = {view1}")

        # (2) with factory
        calls = []

        def factory(runs):
            calls.append(runs)
            return CampaignBridge(runs)

        w2 = MainWindow(application, campaign_bridge_factory=factory)
        windows.append(w2)
        docks2 = w2.findChildren(QDockWidget, "campaignDock")
        view2 = view_menu_texts(w2)
        print(f"  WITH factory   : campaignDock children = {len(docks2)}")
        print(f"  WITH factory   : visible = {[d.isVisible() for d in docks2]}")
        print(f"  WITH factory   : has campaign_dock_action = {hasattr(w2, 'campaign_dock_action')}")
        print(f"  WITH factory   : View menu = {view2}")
        print(f"  WITH factory   : dock objectName/type = "
              f"{[(d.objectName(), type(d).__name__) for d in docks2]}")
        print(f"  factory invoked at construction: {len(calls)} time(s) "
              f"(factory is lazy - invoked on Load Job)")

        # toggle the View action
        if hasattr(w2, "campaign_dock_action"):
            w2.campaign_dock_action.setChecked(True)
            w2.campaign_dock_action.trigger()
            print(f"  after View toggle: dock visible = "
                  f"{[d.isVisible() for d in docks2]}")

        menu_delta = [t for t in view2 if t not in view1]
        menu_missing = [t for t in view1 if t not in view2]
        print(f"  View menu added by factory  : {menu_delta}")
        print(f"  View menu removed/changed   : {menu_missing}")
        ok = (len(docks1) == 0 and not hasattr(w1, "campaign_dock_action")
              and len(docks2) == 1 and menu_missing == []
              and any("Campaign" in t for t in menu_delta))
        print(f"  VERDICT: {'WINDOW ATTACH BEHAVIOUR CORRECT (but untested in v4)' if ok else 'FAIL'}")
    finally:
        for w in windows:
            try:
                w.close()
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------- P6b
def p6b() -> None:
    section("P6b — shutdown with an ACTIVE campaign (fence T0.8) executed")

    # b1: dock.drain() with a hosted run
    tmp = Path(tempfile.mkdtemp(prefix="oracle-v2-p6b1-"))
    job_path = make_slow_job_tree(tmp)
    spy = Spy(delay=0.8)
    calls = []

    def factory(runs):
        calls.append(runs)
        return CampaignBridge(runs, provider_factory=lambda job: {"openrouter": spy})

    QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (str(job_path), ""))
    dock = CampaignDockWidget(bridge_factory=factory)
    dock._pricing_input.setPlainText(PRICING)

    async def b1() -> str:
        dock._on_load_job()
        dock._on_validate()
        dock._on_approve()
        await asyncio.sleep(0.2)
        print(f"  b1 hosted & busy while draining : {dock._bridge.is_busy}")
        dock.mark_closed()
        await dock.drain()
        run_json = list((tmp / ".bots5" / "runs").glob("*/run.json"))
        return json.loads(run_json[0].read_text()).get("state") if run_json else "<none>"

    state1 = asyncio.run(b1())
    print(f"  b1 durable run state after dock.drain() : {state1}")
    b1_ok = state1 == "succeeded"
    print(f"  b1 VERDICT: {'drain reached a durable terminal record' if b1_ok else 'FAIL'}")

    # b2: DesktopRuntime._close_campaigns bounded stage, called unbound on a stub
    tmp2 = Path(tempfile.mkdtemp(prefix="oracle-v2-p6b2-"))
    job_path2 = make_slow_job_tree(tmp2, workers=2, synthesis=True)
    spy2 = Spy(delay=1.0)
    bridge2 = CampaignBridge(tmp2 / ".bots5" / "runs",
                             provider_factory=lambda job: {"openrouter": spy2})

    class StubRuntime:
        pass

    stub = StubRuntime()
    stub._campaign_bridges = {bridge2}

    async def b2() -> dict:
        bridge2.load_job(job_path2)
        bridge2.approve_and_start(
            bridge2.prepare_full_run("oracle", pricing_evidence=json.loads(PRICING)))
        await asyncio.sleep(0.3)
        busy_before = bridge2.is_busy
        import time
        t0 = time.monotonic()
        await DesktopRuntime._close_campaigns(stub)
        elapsed = time.monotonic() - t0
        run_json = list((tmp2 / ".bots5" / "runs").glob("*/run.json"))
        doc = json.loads(run_json[0].read_text()) if run_json else {}
        stages = {p.name: json.loads(p.read_text()).get("state")
                  for p in (tmp2 / ".bots5" / "runs").glob("*/stages/*.json")}
        errs = {p.name: json.loads(p.read_text()).get("error_type")
                for p in (tmp2 / ".bots5" / "runs").glob("*/stages/*.json")}
        return {"busy_before": busy_before, "elapsed": round(elapsed, 3),
                "state": doc.get("state"), "stages": stages, "errors": errs,
                "bridges_left": len(stub._campaign_bridges)}

    info = asyncio.run(b2())
    print(f"  b2 busy before close stage      : {info['busy_before']}")
    print(f"  b2 _close_campaigns elapsed     : {info['elapsed']}s (budget 30s)")
    print(f"  b2 durable run state            : {info['state']}")
    print(f"  b2 stage states                 : {json.dumps(info['stages'], sort_keys=True)}")
    print(f"  b2 stage error_type             : {json.dumps(info['errors'], sort_keys=True)}")
    print(f"  b2 bridge set emptied           : {info['bridges_left'] == 0}")
    b2_ok = info["state"] in ("cancelled", "succeeded", "failed") and info["bridges_left"] == 0
    print(f"  b2 VERDICT: {'bounded close stage drains to a durable terminal record' if b2_ok else 'FAIL'}")


# --------------------------------------------------------------------- P6c
def p6c() -> None:
    section("P6c — durable attempt record + freshness report of a cancelled run")
    tmp = Path(tempfile.mkdtemp(prefix="oracle-v2-p6c-"))
    job_path = make_slow_job_tree(tmp, workers=2, synthesis=True)
    spy = Spy(delay=3.0)
    bridge = CampaignBridge(tmp / ".bots5" / "runs",
                            provider_factory=lambda job: {"openrouter": spy})

    async def go() -> None:
        bridge.load_job(job_path)
        bridge.approve_and_start(
            bridge.prepare_full_run("oracle", pricing_evidence=json.loads(PRICING)))
        await asyncio.sleep(0.6)
        await bridge.cancel()
    asyncio.run(go())

    run_dir = next(x for x in (tmp / ".bots5" / "runs").iterdir() if x.is_dir())
    for p in sorted((run_dir / "stages").glob("*.json")):
        print(f"  --- {p.name} ---")
        print(f"      {json.dumps(json.loads(p.read_text()), sort_keys=True)}")
    proj = project_run(run_dir, hosted=False)
    print(f"  display_state                   : {proj.display_state}")
    print(f"  synthesis_freshness             : {proj.synthesis_freshness}")
    print(f"  synthesis_freshness_report      : {json.dumps(proj.synthesis_freshness_report, sort_keys=True)}")
    print(f"  integrity warnings              : {list(proj.integrity_warnings)}")
    for p in sorted((run_dir / "stages").glob("*.json")):
        d = json.loads(p.read_text())
        if p.name.startswith("synth"):
            print(f"  synth record keys                : {sorted(d)}")
            print(f"  synth consumed/dependency keys   : "
                  f"{ {k: d.get(k) for k in ('consumed_dependencies','dependency_digests','preflight_digest')} }")


def main() -> int:
    for fn in (p6a, p6b, p6c):
        try:
            fn()
        except Exception:  # noqa: BLE001
            print(f"  PROBE {fn.__name__} CRASHED:")
            traceback.print_exc()
    return 0


if __name__ == "__main__":
    sys.exit(main())
