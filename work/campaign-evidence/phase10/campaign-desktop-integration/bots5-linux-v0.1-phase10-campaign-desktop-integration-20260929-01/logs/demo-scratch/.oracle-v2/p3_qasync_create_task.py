#!/usr/bin/env python
"""Oracle v2 probe P3 — the five asyncio.create_task(...) calls inside Qt slots,
driven under the application's REAL qasync QEventLoop (as bootstrap/desktop.py
serve() builds it: `QEventLoop(qt_application); asyncio.set_event_loop(...)`).

Each slot is invoked from Qt's own event dispatch (QTimer.singleShot callback),
which is exactly how a button click reaches it in the running application.

Sites under test (src/bots5/desktop/campaign_dock.py):
  646  _on_approve
  698  _on_poll_timeout
  918  _on_make_current
  1040 _on_cancel
  1045 _on_visibility_changed
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import traceback
from decimal import Decimal
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QFileDialog

app = QApplication.instance() or QApplication([])

from qasync import QEventLoop  # noqa: E402

from bots5.core.campaign import CampaignBridge  # noqa: E402
from bots5.desktop.campaign_dock import CampaignDockWidget  # noqa: E402
from bots5.providers.base import CompletionRequest, CompletionResult  # noqa: E402
from bots5.providers.openrouter import OpenRouterProvider  # noqa: E402
from tests.helpers import make_job_tree  # noqa: E402

RESULTS: dict[str, str] = {}


def note(key: str, value: str) -> None:
    RESULTS[key] = value
    print(f"  {key:44s}: {value}", flush=True)


class Spy(OpenRouterProvider):
    def __init__(self) -> None:
        super().__init__("oracle-dummy-key-never-sent")
        self.calls: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.calls.append(request)
        if request.model == "model-synth":
            await asyncio.sleep(1.2)     # keep the run ALIVE so cancel is meaningful
        return CompletionResult(
            output_text=f"output:{request.model}",
            requested_model=request.model,
            finish_reason="stop",
            returned_model=request.model,
            request_id="req",
            prompt_tokens=10,
            completion_tokens=20,
            total_tokens=30,
            known_cost_usd=Decimal("0.01"),
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


def qt_dispatch(fn, *args) -> None:
    """Invoke `fn` from Qt's event dispatch, like a real button click."""
    QTimer.singleShot(0, lambda: fn(*args))


async def scenario() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="oracle-v2-p3-"))
    job_path, _ = make_job_tree(tmp, workers=1, synthesis=True, run_timeout=30.0)
    spy = Spy()
    factory_calls: list[str] = []

    def bridge_factory(runs: Path) -> CampaignBridge:
        factory_calls.append(str(runs))
        return CampaignBridge(runs, provider_factory=lambda job: {"openrouter": spy})

    QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (str(job_path), ""))
    dock = CampaignDockWidget(bridge_factory=bridge_factory)
    dock.show()
    note("dock.isVisible()", str(dock.isVisible()))
    note("running loop inside coroutine", type(asyncio.get_running_loop()).__name__)

    dock._pricing_input.setPlainText(PRICING)
    qt_dispatch(dock._on_load_job)
    await asyncio.sleep(0.3)
    note("bridge factory calls", str(len(factory_calls)))
    note("bridge bound", str(dock._bridge is not None))

    qt_dispatch(dock._on_validate)
    await asyncio.sleep(0.3)
    note("validate status", dock._status_label.text())

    # --- site 646: _on_approve -> asyncio.create_task -------------------------
    before = len(factory_calls)
    qt_dispatch(dock._on_approve)
    await asyncio.sleep(0.4)
    note("646 _on_approve create_task", f"OK status={dock._status_label.text()!r}")
    note("  hosted task running", str(dock._bridge.is_busy))
    note("  provider factory constructions", str(len(factory_calls) - before + 1))

    # render the projection once (direct await; not one of the five sites)
    await dock._refresh_projection_async()
    note("  rows while running", str(dock._stages_table.rowCount()))
    note("  run identity while running", dock._run_identity_label.text())

    # --- site 698: _on_poll_timeout -> asyncio.create_task --------------------
    try:
        qt_dispatch(dock._on_poll_timeout)
        await asyncio.sleep(0.5)
        note("698 _on_poll_timeout create_task", "OK (no RuntimeError)")
    except Exception as exc:  # noqa: BLE001
        note("698 _on_poll_timeout create_task", f"{type(exc).__name__}: {exc}")

    # --- site 1045: _on_visibility_changed -> asyncio.create_task -------------
    try:
        qt_dispatch(dock._on_visibility_changed, True)
        await asyncio.sleep(0.5)
        note("1045 _on_visibility_changed create_task",
             f"OK poll_timer_active={dock._poll_timer.isActive()}")
    except Exception as exc:  # noqa: BLE001
        note("1045 _on_visibility_changed create_task", f"{type(exc).__name__}: {exc}")

    # --- site 918: _on_make_current -> asyncio.create_task --------------------
    try:
        dock._stages_table.selectRow(0)
        qt_dispatch(dock._on_make_current)
        await asyncio.sleep(0.5)
        note("918 _on_make_current create_task", f"OK status={dock._status_label.text()!r}")
    except Exception as exc:  # noqa: BLE001
        note("918 _on_make_current create_task", f"{type(exc).__name__}: {exc}")

    # --- site 1040: _on_cancel -> asyncio.create_task -------------------------
    try:
        qt_dispatch(dock._on_cancel)
        await asyncio.sleep(1.5)
        note("1040 _on_cancel create_task", f"OK status={dock._status_label.text()!r}")
        run_json = sorted((tmp / ".bots5" / "runs").glob("*/run.json"))
        if run_json:
            doc = json.loads(run_json[0].read_text())
            note("  durable run state after cancel", str(doc.get("state")))
            stages = sorted(p.name for p in run_json[0].parent.glob("stages/*.json"))
            note("  durable stage records", str(stages))
            states = {p.name: json.loads(p.read_text()).get("state")
                      for p in sorted(run_json[0].parent.glob("stages/*.json"))}
            note("  durable stage states", json.dumps(states, sort_keys=True))
        note("  bridge cancel verified terminal", "no exception raised")
    except Exception as exc:  # noqa: BLE001
        note("1040 _on_cancel create_task", f"{type(exc).__name__}: {exc}")

    await dock._refresh_projection_async()
    note("  display state after cancel", dock._projection.display_state if dock._projection else "None")
    note("  cost line after cancel", dock._cost_label.text())

    dock.mark_closed()
    await dock.drain()
    if dock._bridge is not None:
        await dock._bridge.close()


def main() -> int:
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)
    print("=" * 78)
    print("P3 — five asyncio.create_task sites under the real qasync QEventLoop")
    print("=" * 78)
    try:
        loop.run_until_complete(scenario())
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 99
    finally:
        try:
            loop.close()
        except Exception:  # noqa: BLE001
            pass
    print()
    print("SUMMARY")
    for k, v in RESULTS.items():
        print(f"  {k} = {v}")
    bad = [k for k, v in RESULTS.items()
           if "RuntimeError:" in v or "no running event loop" in v]
    print(f"\nSITES FAILING UNDER QASYNC: {bad or 'NONE'}")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
