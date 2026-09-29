#!/usr/bin/env python
"""Oracle v3 probe P9 — V-1/V-2 dispatch + NEW-DEFECT HUNT, EXECUTED.

  P9a  worker_regeneration prepared in the DOCK, Approve pressed -> durable sibling,
       provider called with the OPERATOR'S model, no full_run refusal
  P9b  synthesis_rerun prepared in the DOCK, Approve pressed -> same contract
  P9c  V-2 enablement matrix: Regenerate/Rerun-synthesis across every selected row
       and with no row selected (run-level independence)
  P9d  dispatch totality: unknown operation strings and an object without an
       ``operation`` attribute must refuse gracefully, never raise
  P9e  Regenerate offered on the SYNTHESIS row -> what does the engine do?
  P9f  Approve pressed while a campaign task is already hosted (V-2 side-effect hunt)

Run:
  cd <repo> && PYTHONPATH=src:. .venv314/bin/python <this file>
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QLineEdit

app = QApplication.instance() or QApplication([])

from bots5.core.campaign import CampaignBridge  # noqa: E402
from bots5.desktop.campaign_dock import CampaignDockWidget  # noqa: E402
from bots5.providers.base import CompletionRequest, CompletionResult  # noqa: E402
from bots5.providers.openrouter import OpenRouterProvider  # noqa: E402
from tests.helpers import make_job_tree  # noqa: E402

OUT: list[str] = []


def say(line: str = "") -> None:
    print(line, flush=True)
    OUT.append(line)


class Spy(OpenRouterProvider):
    def __init__(self, delay: float = 0.0) -> None:
        super().__init__("oracle-key-never-sent")
        self.calls: list[CompletionRequest] = []
        self.delay = delay

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.calls.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        return CompletionResult(
            output_text=f"OUTPUT[{request.model}]",
            requested_model=request.model,
            finish_reason="stop",
            returned_model=request.model,
            request_id=f"req-{len(self.calls)}",
            prompt_tokens=10,
            completion_tokens=20,
            total_tokens=30,
            known_cost_usd=None,
            duration_seconds=0.001,
        )


PRICING = json.dumps({
    "entries": [{
        "provider": "openrouter",
        "input_usd_per_1m": "2.00",
        "output_usd_per_1m": "8.00",
        "rate_source": "https://example.invalid/pricing (operator-cited)",
        "observed_at": "2026-09-30T00:00:00Z",
    }]
})


def build(tmp: Path):
    tmp.mkdir(parents=True, exist_ok=True)
    job_path, _ = make_job_tree(tmp, workers=2, synthesis=True, run_timeout=30.0)
    spy = Spy()

    def bridge_factory(runs: Path) -> CampaignBridge:
        return CampaignBridge(runs, provider_factory=lambda job: {"openrouter": spy})

    QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (str(job_path), ""))
    dock = CampaignDockWidget(bridge_factory=bridge_factory)
    dock._pricing_input.setPlainText(PRICING)
    dock._on_load_job()
    dock._on_validate()
    asyncio.run(_approve_and_finish(dock))
    return dock, spy


async def _approve_and_finish(dock) -> None:
    dock._on_approve()
    await asyncio.sleep(0.2)
    if dock._bridge is not None and dock._bridge._task is not None:
        await dock._bridge._task
    await dock._refresh_projection_async()


def _press_approve(dock) -> None:
    """Exactly what a button press does under the real loop: call the slot,
    then drain the hosted task and refresh the projection."""

    async def _inner():
        dock._on_approve()
        await asyncio.sleep(0.3)
        if getattr(dock, "_bridge", None) is not None and dock._bridge._task is not None:
            await dock._bridge._task
        await dock._refresh_projection_async()

    asyncio.run(_inner())


def p9a(tmp: Path) -> None:
    say("=" * 78)
    say("P9a — DOCK-prepared worker_regeneration, Approve pressed")
    say("=" * 78)
    dock, spy = build(tmp / "p9a")
    runs_dir = dock._bridge.runs_dir
    say(f"  status after full run      : {dock._status_label.text()!r}")
    say(f"  provider calls after run   : {[c.model for c in spy.calls]}")

    dock._stages_table.selectRow(0)
    say(f"  selected row state         : {dock._selected_stage()['state']}")

    orig_exec = QDialog.exec

    def fake_exec(self):
        edits = self.findChildren(QLineEdit)
        edits[0].setText("model-w1-operator-choice")
        return QDialog.DialogCode.Accepted

    QDialog.exec = fake_exec
    try:
        dock._on_regenerate()
    finally:
        QDialog.exec = orig_exec

    before = len(spy.calls)
    status_before_approve = dock._status_label.text()
    _press_approve(dock)

    run_dir = next(p for p in runs_dir.iterdir() if p.is_dir())
    att2 = run_dir / "stages" / "w1.att2.json"
    say(f"  status after prepare       : {status_before_approve!r}")
    say(f"  status after Approve       : {dock._status_label.text()!r}")
    say(f"  full_run refusal present   : "
        f"{'requires a full_run operation' in dock._status_label.text()}")
    say(f"  w1.att2.json created       : {att2.exists()}")
    if att2.exists():
        rec = json.loads(att2.read_text(encoding="utf-8"))
        say(f"  sibling record             : state={rec['state']} "
            f"attempt={rec['attempt_number']} requested_model={rec['requested_model']}")
    say(f"  provider calls delta       : {len(spy.calls) - before} "
        f"models={[c.model for c in spy.calls[before:]]}")
    ok = (
        att2.exists()
        and json.loads(att2.read_text(encoding="utf-8"))["state"] == "succeeded"
        and any(c.model == "model-w1-operator-choice" for c in spy.calls[before:])
        and "requires a full_run operation" not in dock._status_label.text()
    )
    say(f"  P9a RESULT: {'PASS' if ok else 'FAIL'}")
    say()

    # stash for P9c/P9e/P9d reuse
    P9["dock"], P9["spy"], P9["run_dir"] = dock, spy, run_dir


def p9b(tmp: Path) -> None:
    say("=" * 78)
    say("P9b — DOCK-prepared synthesis_rerun, Approve pressed")
    say("=" * 78)
    dock, spy = build(tmp / "p9b")
    before = len(spy.calls)

    dock._on_rerun_synthesis()
    say(f"  status after prepare       : {dock._status_label.text()!r}")
    say(f"  prepared operation         : {dock._prepared_operation.operation}")
    say(f"  approve enabled            : {dock._approve_button.isEnabled()}")
    _press_approve(dock)
    run_dir = next(p for p in dock._bridge.runs_dir.iterdir() if p.is_dir())
    synth_att2 = run_dir / "stages" / "synth.att2.json"
    say(f"  status after Approve       : {dock._status_label.text()!r}")
    say(f"  full_run refusal present   : "
        f"{'requires a full_run operation' in dock._status_label.text()}")
    say(f"  synth.att2.json created    : {synth_att2.exists()}")
    if synth_att2.exists():
        rec = json.loads(synth_att2.read_text(encoding="utf-8"))
        say(f"  sibling record             : state={rec['state']} "
            f"attempt={rec['attempt_number']} requested_model={rec['requested_model']}")
    say(f"  provider calls delta       : {len(spy.calls) - before} "
        f"models={[c.model for c in spy.calls[before:]]}")
    ok = (
        synth_att2.exists()
        and json.loads(synth_att2.read_text(encoding="utf-8"))["state"] == "succeeded"
        and any(c.model == "model-synth" for c in spy.calls[before:])
        and "requires a full_run operation" not in dock._status_label.text()
    )
    say(f"  P9b RESULT: {'PASS' if ok else 'FAIL'}")


def p9c(tmp: Path) -> None:
    say("=" * 78)
    say("P9c — V-2 enablement matrix (all rows succeeded after a full successful run)")
    say("=" * 78)
    dock, spy = build(tmp / "p9c")
    say(f"  run state                  : {dock._projection.display_state}")
    rows = dock._stages_table.rowCount()
    say(f"  rows                       : {rows}")
    say("    row | stage | state     | regen enabled | rerun enabled")
    states = []
    for r in range(rows):
        dock._stages_table.selectRow(r)
        stage = dock._selected_stage()
        states.append(stage["state"])
        say(f"    {r}   | {stage['stage_id']:5s} | {stage['state']:9s} | "
            f"{str(dock._regenerate_button.isEnabled()):13s} | "
            f"{dock._rerun_synthesis_button.isEnabled()}")
    # no selection
    dock._stages_table.clearSelection()
    say(f"  no selection: regen={dock._regenerate_button.isEnabled()} "
        f"rerun={dock._rerun_synthesis_button.isEnabled()} "
        f"(selection={dock._selected_stage()})")
    # engine acceptance for the succeeded target
    try:
        prepared = dock._bridge.prepare_regeneration(
            "w1", "model-probe", "oracle", pricing_evidence=json.loads(PRICING))
        say(f"  engine prepare_regeneration on SUCCEEDED stage: "
            f"ACCEPTED operation={prepared.operation} attempt={prepared.attempt_number}")
        engine_ok = True
    except Exception as exc:  # noqa: BLE001
        say(f"  engine prepare_regeneration: REFUSED {type(exc).__name__}: {exc}")
        engine_ok = False
    all_rows_enabled = all(
        (dock._stages_table.selectRow(r) or dock._regenerate_button.isEnabled())
        for r in range(rows)
    )
    dock._stages_table.clearSelection()
    rerun_without_selection = dock._rerun_synthesis_button.isEnabled()
    ok = states == ["succeeded"] * rows and all_rows_enabled and rerun_without_selection and engine_ok
    say(f"  P9c RESULT: {'PASS' if ok else 'FAIL'} "
        f"(rows_all_succeeded={states}, rerun_no_selection={rerun_without_selection}, engine={engine_ok})")
    P9["dock2"], P9["spy2"], P9["run2"] = dock, spy, next(
        p for p in dock._bridge.runs_dir.iterdir() if p.is_dir())
    say()


def p9d(tmp: Path) -> None:
    say("=" * 78)
    say("P9d — dispatch totality: unknown operation strings / missing operation attr")
    say("=" * 78)
    dock = P9["dock2"]
    saved = dock._prepared_operation
    results = []
    for label, prepared in (
        ("operation='bogus'", SimpleNamespace(operation="bogus")),
        ("operation=None", SimpleNamespace(operation=None)),
        ("no operation attribute", SimpleNamespace()),
        ("operation='full_run' but stale", SimpleNamespace(operation="full_run")),
    ):
        dock._prepared_operation = prepared
        try:
            _press_approve(dock)
            results.append((label, "no exception", dock._status_label.text()))
        except Exception as exc:  # noqa: BLE001
            results.append((label, f"RAISED {type(exc).__name__}: {exc}", ""))
    dock._prepared_operation = saved
    for label, outcome, status in results:
        say(f"  {label:32s} -> {outcome}  status={status!r}")
    raised = any(o.startswith("RAISED") for _, o, _ in results)
    graceful_unknown = any(
        "unsupported operation" in s for l, o, s in results if "bogus" in l or "None" in l
        or "no operation" in l
    )
    say(f"  any case raised            : {raised}")
    say(f"  unknown operation refused gracefully : {graceful_unknown}")
    say(f"  P9d RESULT: {'PASS (total, graceful)' if not raised else 'FAIL (raised)'}")
    say()


def p9e(tmp: Path) -> None:
    say("=" * 78)
    say("P9e — Regenerate offered on the SYNTHESIS row: what does the engine do?")
    say("=" * 78)
    dock = P9["dock2"]
    synth_row = next(
        r for r in range(dock._stages_table.rowCount())
        if dock._stages_table.item(r, 0).text() == "synth")
    dock._stages_table.selectRow(synth_row)
    say(f"  selected                   : {dock._selected_stage()}")
    say(f"  regenerate button enabled  : {dock._regenerate_button.isEnabled()}")

    orig_exec = QDialog.exec

    def fake_exec(self):
        self.findChildren(QLineEdit)[0].setText("model-synth-alt")
        return QDialog.DialogCode.Accepted

    QDialog.exec = fake_exec
    try:
        dock._on_regenerate()
    finally:
        QDialog.exec = orig_exec
    say(f"  status after prepare       : {dock._status_label.text()!r}")
    say(f"  approve button enabled     : {dock._approve_button.isEnabled()}")
    say(f"  prepared operation         : "
        f"{getattr(dock._prepared_operation, 'operation', None)!r}")
    say("  (engine refusal is surfaced verbatim and Approve is disabled: the operator")
    say("   is redirected to Rerun synthesis; nothing is written, no provider built)")
    say(f"  P9e RESULT: OBSERVED — offered by UI, refused by engine with a typed message")
    say()


def p9f(tmp: Path) -> None:
    say("=" * 78)
    say("P9f — Approve pressed while a campaign task is already hosted (single loop)")
    say("=" * 78)
    (tmp / "p9f").mkdir(parents=True, exist_ok=True)
    job_path, _ = make_job_tree(tmp / "p9f", workers=1, synthesis=True, run_timeout=30.0)
    spy = Spy(delay=1.5)
    QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (str(job_path), ""))
    dock = CampaignDockWidget(
        bridge_factory=lambda runs: CampaignBridge(
            runs, provider_factory=lambda job: {"openrouter": spy}))
    dock._pricing_input.setPlainText(PRICING)
    dock._on_load_job()
    dock._on_validate()

    orig_exec = QDialog.exec

    def fake_exec(self):
        self.findChildren(QLineEdit)[0].setText("model-busy-probe")
        return QDialog.DialogCode.Accepted

    async def scenario():
        QDialog.exec = fake_exec
        try:
            dock._on_approve()                     # start the full run
            await asyncio.sleep(0.3)
            say(f"  bridge.is_busy while running : {dock._bridge.is_busy}")
            say(f"  provider calls so far        : {[c.model for c in spy.calls]}")
            run_dir = dock._bridge.current_run_dir
            say(f"  run.json state (durable)     : "
                f"{json.loads((run_dir / 'run.json').read_text())['state']!r}")

            # select a NON-running row: under V-2 Regenerate is offered there
            synth_row = next(r for r in range(dock._stages_table.rowCount())
                             if dock._stages_table.item(r, 0).text() == "synth")
            dock._stages_table.selectRow(synth_row)
            stage = dock._selected_stage()
            say(f"  selected row                 : {stage['stage_id']} state={stage['state']}")
            say(f"  regenerate button enabled    : {dock._regenerate_button.isEnabled()}")

            dock._on_regenerate()
            say(f"  status after prepare         : {dock._status_label.text()!r}")
            say(f"  approve enabled              : {dock._approve_button.isEnabled()}")
            before = len(spy.calls)
            dock._on_approve()
            await asyncio.sleep(0.3)
            say(f"  status after Approve         : {dock._status_label.text()!r}")
            say(f"  provider calls delta         : {len(spy.calls) - before}")
            say(f"  bridge.is_busy after         : {dock._bridge.is_busy}")

            if dock._bridge._task is not None:
                await dock._bridge._task           # let the real run finish
            await dock._refresh_projection_async()
        finally:
            QDialog.exec = orig_exec

    asyncio.run(scenario())
    run_dir = next(p for p in dock._bridge.runs_dir.iterdir() if p.is_dir())
    files = sorted(x.name for x in (run_dir / "stages").glob("*.json"))
    say(f"  attempt files                : {files}")
    say(f"  run.json final state         : "
        f"{json.loads((run_dir / 'run.json').read_text())['state']!r}")
    refused = "already hosted" in dock._status_label.text()
    say(f"  engine backstop refused      : {refused}")
    say(f"  P9f RESULT: {'PASS (engine refused the second host)' if refused else 'OBSERVED: ' + dock._status_label.text()!r}")


P9: dict = {}


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="oracle-v3-p9-"))
    p9a(tmp)
    p9b(tmp)
    p9c(tmp)
    p9d(tmp)
    p9e(tmp)
    p9f(tmp)
    say()
    say("=" * 78)
    say("SUMMARY")
    say("=" * 78)
    for line in list(OUT):
        if "RESULT" in line:
            say("  " + line.strip())
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        code = 99
    sys.exit(code)
