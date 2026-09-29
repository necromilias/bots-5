#!/usr/bin/env python
"""Oracle v2 probe P1 — D-1: is the campaign dock ACTUALLY OPERATIONAL?

Executes (does not read) the whole operator path against the sealed candidate v4:

  P1a  counting bridge factory invoked exactly once on Load Job, real CampaignBridge bound
  P1b  Validate (zero spend)
  P1c  prepare a full run with COMPLETE operator pricing evidence
  P1d  Approve against a deterministic in-process fake provider (NO NETWORK), run to terminal
  P1e  projection renders stage rows, live cost line with explicit unknown set, freshness class
  P1f  Regenerate control: does it still raise AttributeError?  (D-2)
  P1g  Approve AFTER Regenerate — does the desktop actually execute the regeneration?
  P1h  Make current -> a DIFFERENT attempt, freshness reads STALE
  P1i  selected-attempt output rendered from durable storage through the bridge
  P1j  New Job clears the view; every run-directory file byte-identical

Run:
  cd <repo> && QT_QPA_PLATFORM=offscreen PYTHONPATH=src .venv314/bin/python <this file>
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

from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QLineEdit

app = QApplication.instance() or QApplication([])

from bots5.core.campaign import CampaignBridge  # noqa: E402
from bots5.desktop.campaign_dock import CampaignDockWidget  # noqa: E402
from bots5.providers.base import CompletionRequest, CompletionResult  # noqa: E402
from bots5.providers.openrouter import OpenRouterProvider  # noqa: E402
from tests.helpers import FakeProvider, make_job_tree  # noqa: E402

REPORT: list[str] = []


def say(line: str = "") -> None:
    print(line, flush=True)
    REPORT.append(line)


class SpyProvider(OpenRouterProvider):
    """Deterministic, in-process, SUBCLASS of the approved route class.

    The engine validates the provider OBJECT against the frozen route kind
    (isinstance, subclasses keep their route identity), so the probe must look
    like a real openrouter provider. ``complete`` never opens a socket.

    model-w1 / model-w2 report a KNOWN cost; model-synth reports NO known cost
    so the projection's explicit unknown set is non-empty and observable.
    """

    def __init__(self) -> None:
        super().__init__("oracle-dummy-key-never-sent")
        self.calls: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.calls.append(request)
        if request.model == "model-synth":
            return CompletionResult(
                output_text="SYNTH-OUTPUT-FROM-DURABLE-STORE",
                requested_model=request.model,
                finish_reason="stop",
                returned_model=request.model,
                request_id="req-synth",
                prompt_tokens=11,
                completion_tokens=22,
                total_tokens=33,
                known_cost_usd=None,  # unknown -> must appear in the unknown set
                duration_seconds=0.001,
            )
        text = {"model-w1": "W1-DURABLE-OUTPUT", "model-w2": "W2-DURABLE-OUTPUT",
                "model-w1-alt": "W1-ATTEMPT-2-OUTPUT"}.get(request.model, f"output:{request.model}")
        return CompletionResult(
            output_text=text,
            requested_model=request.model,
            finish_reason="stop",
            returned_model=request.model,
            request_id=f"req-{request.model}",
            prompt_tokens=10,
            completion_tokens=20,
            total_tokens=30,
            known_cost_usd=Decimal("0.01"),
            duration_seconds=0.001,
        )


def sha_tree(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def cell(dock, row: int, col: int) -> str:
    item = dock._stages_table.item(row, col)
    return "" if item is None else item.text()


def dump_table(dock, header: str) -> None:
    say(f"  --- {header}: rows={dock._stages_table.rowCount()}")
    labels = ["stage", "state", "attempt", "model", "duration", "tokens", "cost", "outcome"]
    for r in range(dock._stages_table.rowCount()):
        vals = [cell(dock, r, c) for c in range(8)]
        combo = dock._stages_table.cellWidget(r, 2)
        extra = ""
        if combo is not None:
            extra = f"  [combo items={[combo.itemText(i) for i in range(combo.count())]} current={combo.currentText()}]"
        say("    " + " | ".join(f"{k}={v}" for k, v in zip(labels, vals)) + extra)


def main() -> int:
    global p1a_ok, p1b_ok, p1e_ok, p1f_ok, p1g_executed, p1h_ok, p1i_ok, p1j_ok
    p1a_ok = p1b_ok = p1e_ok = p1f_ok = p1g_executed = p1h_ok = p1i_ok = p1j_ok = False
    tmp = Path(tempfile.mkdtemp(prefix="oracle-v2-p1-"))
    job_path, _job = make_job_tree(tmp, workers=2, synthesis=True, run_timeout=10.0)
    runs_dir = tmp / ".bots5" / "runs"

    spy = SpyProvider()
    provider_factory_calls: list[str] = []
    bridge_factory_calls: list[str] = []

    def provider_factory(job):  # noqa: ANN001
        provider_factory_calls.append(str(job.name))
        return {"openrouter": spy}

    def bridge_factory(runs: Path) -> CampaignBridge:
        bridge_factory_calls.append(str(runs))
        return CampaignBridge(runs, provider_factory=provider_factory)

    PRICING = json.dumps({
        "entries": [{
            "provider": "openrouter",
            "input_usd_per_1m": "2.00",
            "output_usd_per_1m": "8.00",
            "rate_source": "https://example.invalid/pricing (operator-cited)",
            "observed_at": "2026-09-30T00:00:00Z",
        }]
    })

    orig_open = QFileDialog.getOpenFileName
    QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (str(job_path), ""))

    say("=" * 78)
    say("P1a — counting bridge factory + Load Job")
    say("=" * 78)
    dock = CampaignDockWidget(bridge_factory=bridge_factory)
    say(f"  dock type                      : {type(dock).__name__}")
    say(f"  dock._bridge BEFORE Load Job   : {dock._bridge!r}")
    say(f"  bridge_factory call count      : {len(bridge_factory_calls)}")
    say(f"  validate enabled before load   : {dock._validate_button.isEnabled()}")

    # pricing evidence must be present BEFORE load so preflight can bind it
    dock._pricing_input.setPlainText(PRICING)
    dock._on_load_job()

    say(f"  bridge_factory call count AFTER: {len(bridge_factory_calls)}  -> {bridge_factory_calls}")
    say(f"  dock._bridge                   : {type(dock._bridge).__name__ if dock._bridge is not None else None}")
    say(f"  isinstance CampaignBridge      : {isinstance(dock._bridge, CampaignBridge)}")
    say(f"  job identity label             : {dock._job_identity_label.text()}")
    say(f"  status                         : {dock._status_label.text()}")
    say(f"  validate enabled               : {dock._validate_button.isEnabled()}")
    say(f"  approve enabled (preflight)    : {dock._approve_button.isEnabled()}")
    say(f"  pricing status                 : {dock._pricing_status.text()}")
    say(f"  runs dir exists before run     : {runs_dir.exists()}")

    p1a_ok = (
        len(bridge_factory_calls) == 1
        and isinstance(dock._bridge, CampaignBridge)
        and dock._validate_button.isEnabled()
    )
    say(f"  P1a RESULT: {'PASS' if p1a_ok else 'FAIL'}")

    say()
    say("=" * 78)
    say("P1b — Validate (zero spend)")
    say("=" * 78)
    dock._on_validate()
    say(f"  status                         : {dock._status_label.text()}")
    say(f"  runs dir exists after validate : {runs_dir.exists()}")
    say(f"  provider constructions so far  : {len(provider_factory_calls)}")
    p1b_ok = "zero spend" in dock._status_label.text() and not runs_dir.exists()
    say(f"  P1b RESULT: {'PASS' if p1b_ok else 'FAIL'}")

    say()
    say("=" * 78)
    say("P1c/P1d — Approve a full run with complete pricing, drive to terminal (NO NETWORK)")
    say("=" * 78)
    say(f"  approve enabled                : {dock._approve_button.isEnabled()}")
    say(f"  prepared operation             : {getattr(dock._prepared_operation, 'operation', None)}")
    say(f"  approval.pricing_evidence set  : {getattr(getattr(dock._prepared_operation, 'approval', None), 'pricing_evidence', None) is not None}")
    say("  --- preflight summary shown to the operator ---")
    for ln in dock._preflight_text.toPlainText().splitlines():
        say(f"      {ln}")

    async def run_phase() -> None:
        dock._on_approve()
        say(f"  status after Approve           : {dock._status_label.text()}")
        say(f"  provider factory constructions : {len(provider_factory_calls)}")
        result = await dock._bridge.run_to_completion()
        say(f"  terminal result                : {type(result).__name__} state={getattr(getattr(result, 'state', None), 'value', getattr(result, 'state', None))}")
        await dock._refresh_projection_async()

    asyncio.run(run_phase())

    say()
    say("  P1e — projection render after terminal state")
    dump_table(dock, "stages table")
    say(f"  run identity label             : {dock._run_identity_label.text()}")
    say(f"  LIVE COST LINE                 : {dock._cost_label.text()}")
    say(f"  FRESHNESS LABEL                : {dock._freshness_label.text()}")
    say(f"  integrity warnings             : {dock._integrity_warnings_label.text()!r}")
    say(f"  cancel button enabled (terminal): {dock._cancel_button.isEnabled()}")
    say(f"  provider.complete() calls      : {len(spy.calls)}  models={[c.model for c in spy.calls]}")
    say(f"  projection.live_cost           : {json.dumps({k: str(v) for k, v in dock._projection.live_cost.items()}, sort_keys=True)}")
    say(f"  projection.display_state       : {dock._projection.display_state}")
    say(f"  projection.evidence_version    : {dock._projection.evidence_version}")

    rows_ok = dock._stages_table.rowCount() == 3
    cost_text = dock._cost_label.text()
    unknown_ok = "unknown" in cost_text and "synth" in cost_text
    fresh_ok = "FRESH" in dock._freshness_label.text() or "fresh" in dock._freshness_label.text()
    p1e_ok = rows_ok and unknown_ok and fresh_ok
    say(f"  P1e RESULT: {'PASS' if p1e_ok else 'FAIL'} "
        f"(rows={rows_ok}, unknown_set={unknown_ok}, freshness={fresh_ok})")

    # ------------------------------------------------------------------
    say()
    say("=" * 78)
    say("P1f/P1g — Regenerate control (D-2) and what Approve does afterwards")
    say("=" * 78)
    dock._stages_table.selectRow(0)
    say(f"  selected stage                 : {dock._selected_stage()}")
    say(f"  regenerate button enabled      : {dock._regenerate_button.isEnabled()}  (stage state = succeeded)")
    say(f"  rerun-synthesis button enabled : {dock._rerun_synthesis_button.isEnabled()}")

    # Does the engine allow regenerating a SUCCEEDED stage?
    eng_ok = True
    try:
        direct = dock._bridge.prepare_regeneration("w1", "model-probe", "oracle",
                                                   pricing_evidence=json.loads(PRICING))
        say(f"  engine prepare_regeneration on a SUCCEEDED stage: ACCEPTED "
            f"(operation={direct.operation}, attempt={direct.attempt_number})")
    except Exception as exc:  # noqa: BLE001
        eng_ok = False
        say(f"  engine prepare_regeneration on a SUCCEEDED stage: REFUSED {type(exc).__name__}: {exc}")

    captured: dict[str, object] = {}
    orig_exec = QDialog.exec

    def fake_exec(self):  # noqa: ANN001
        captured["dialog_class"] = type(self).__name__
        captured["dialog_is_qdialog"] = isinstance(self, QDialog)
        captured["has_accept"] = callable(getattr(self, "accept", None))
        captured["has_reject"] = callable(getattr(self, "reject", None))
        try:
            self.accept()          # the exact call that used to raise AttributeError
            captured["accept_call"] = "ok"
        except Exception as exc:   # noqa: BLE001
            captured["accept_call"] = repr(exc)
        edits = self.findChildren(QLineEdit)
        captured["line_edits"] = len(edits)
        if edits:
            edits[0].setText("model-w1-alt")
        return 1  # QDialog.Accepted

    QDialog.exec = fake_exec
    regen_exc = None
    try:
        dock._on_regenerate()
    except Exception as exc:  # noqa: BLE001
        regen_exc = exc
        say(f"  _on_regenerate RAISED: {type(exc).__name__}: {exc}")
    finally:
        QDialog.exec = orig_exec

    say(f"  dialog capture                 : {json.dumps({k: str(v) for k, v in captured.items()})}")
    say(f"  status after Regenerate prepare: {dock._status_label.text()}")
    say(f"  prepared operation             : {getattr(dock._prepared_operation, 'operation', None)}")
    say(f"  approve button enabled         : {dock._approve_button.isEnabled()}")
    say(f"  D-2 AttributeError             : {'NONE — control did not raise' if regen_exc is None else repr(regen_exc)}")
    p1f_ok = regen_exc is None and captured.get("dialog_class") == "QDialog"
    say(f"  P1f RESULT: {'PASS' if p1f_ok else 'FAIL'}")

    # Now press Approve with the regeneration prepared — the real operator act.
    pre_probe = sha_tree(runs_dir)
    async def approve_regen_phase() -> None:
        dock._on_approve()
        await asyncio.sleep(0.2)
        if dock._bridge is not None and dock._bridge._task is not None:
            await dock._bridge._task

    asyncio.run(approve_regen_phase())
    say()
    say("  P1g — operator presses Approve for the prepared REGENERATION")
    say(f"  status shown to operator       : {dock._status_label.text()!r}")
    att2 = list(runs_dir.glob("*/stages/w1.att2.json")) if runs_dir.exists() else []
    say(f"  attempt-2 file created         : {bool(att2)}  ({[str(p) for p in att2]})")
    say(f"  run tree changed by Approve    : {sha_tree(runs_dir) != pre_probe}")
    say(f"  provider calls                 : {len(spy.calls)}")
    p1g_executed = bool(att2)
    say(f"  P1g RESULT: {'regeneration EXECUTED by the desktop' if p1g_executed else 'REGENERATION NOT EXECUTED BY THE DESKTOP'}")

    # ------------------------------------------------------------------
    say()
    say("=" * 78)
    say("P1h — create a second attempt (via the bridge seam) then Make current")
    say("=" * 78)
    async def make_attempt_two() -> None:
        prepared = dock._bridge.prepare_regeneration(
            "w1", "model-w1-alt", "oracle", pricing_evidence=json.loads(PRICING)
        )
        dock._bridge.approve_and_regenerate(prepared)   # the entry point the dock never calls
        await dock._bridge.run_to_completion()
        await dock._refresh_projection_async()

    asyncio.run(make_attempt_two())
    dump_table(dock, "after regeneration of w1 -> attempt 2")
    say(f"  freshness after regen (selection still attempt 1): {dock._freshness_label.text()}")

    combo = dock._stages_table.cellWidget(0, 2)
    say(f"  attempt switcher widget on row 0: {type(combo).__name__ if combo is not None else None}")
    if combo is None:
        say("  P1h RESULT: FAIL — no attempt switcher rendered")
        p1h_ok = False
    else:
        say(f"  combo choices={[combo.itemText(i) for i in range(combo.count())]}")
        dock._stages_table.selectRow(0)
        combo.setCurrentIndex(combo.count() - 1)   # pick the OTHER attempt
        say(f"  selected stage after combo change: {dock._selected_stage()}")
        pre_sel = sha_tree(runs_dir)

        async def make_current_phase() -> None:
            dock._on_make_current()
            await asyncio.sleep(0.3)
            await dock._refresh_projection_async()

        asyncio.run(make_current_phase())
        say(f"  status                         : {dock._status_label.text()}")
        say(f"  FRESHNESS AFTER MAKE CURRENT   : {dock._freshness_label.text()}")
        say(f"  integrity warnings             : {dock._integrity_warnings_label.text()!r}")
        dump_table(dock, "after Make current -> attempt 2")
        sel = json.loads((list(runs_dir.glob('*'))[0] / "selection.json").read_text())
        say(f"  durable selection.json         : {json.dumps(sel, sort_keys=True)}")
        p1h_ok = "STALE" in dock._freshness_label.text()
        say(f"  P1h RESULT: {'PASS' if p1h_ok else 'FAIL'}")
        say(f"  (selection write changed run tree: {sha_tree(runs_dir) != pre_sel} — expected, it is the explicit selection command)")

    # ------------------------------------------------------------------
    say()
    say("=" * 78)
    say("P1i — selected-attempt output rendered from durable storage through the bridge")
    say("=" * 78)
    dock._stages_table.selectRow(0)
    say(f"  selected stage                 : {dock._selected_stage()}")
    say(f"  _result_text (first 200 chars) : {dock._result_text.toPlainText()[:200]!r}")
    run_dir = next(p for p in runs_dir.iterdir() if p.is_dir())
    att_files = sorted(p.name for p in (run_dir / "stages").glob("w1.att*.md"))
    say(f"  durable attempt artifacts      : {att_files}")
    direct_read = None
    try:
        direct_read = dock._bridge.read_stage_output("w1", dock._selected_stage()["attempt_number"])
    except Exception as exc:  # noqa: BLE001
        say(f"  bridge.read_stage_output FAILED: {exc}")
    say(f"  bridge.read_stage_output(...)  : {direct_read!r}")
    p1i_ok = bool(dock._result_text.toPlainText()) and direct_read is not None \
        and dock._result_text.toPlainText() == direct_read
    say(f"  P1i RESULT: {'PASS' if p1i_ok else 'FAIL'}")

    # ------------------------------------------------------------------
    say()
    say("=" * 78)
    say("P1j — New Job clears the view, run-directory bytes untouched")
    say("=" * 78)
    before = sha_tree(run_dir)
    say(f"  files in run dir               : {len(before)}")
    say(f"  UI before: job={dock._job_identity_label.text()!r} run={dock._run_identity_label.text()!r} "
        f"rows={dock._stages_table.rowCount()} cost={dock._cost_label.text()!r}")
    try:
        dock._on_clear_job()
        clear_exc = None
    except Exception as exc:  # noqa: BLE001
        clear_exc = exc
        say(f"  _on_clear_job RAISED: {type(exc).__name__}: {exc}")
    after = sha_tree(run_dir)
    say(f"  UI after : job={dock._job_identity_label.text()!r} run={dock._run_identity_label.text()!r} "
        f"rows={dock._stages_table.rowCount()} cost={dock._cost_label.text()!r} "
        f"freshness={dock._freshness_label.text()!r} status={dock._status_label.text()!r} "
        f"result={dock._result_text.toPlainText()!r}")
    say(f"  dock._bridge after New Job     : {dock._bridge!r}")
    say(f"  buttons: validate={dock._validate_button.isEnabled()} approve={dock._approve_button.isEnabled()} "
        f"clear={dock._clear_button.isEnabled()} make_current={dock._make_current_button.isEnabled()} "
        f"regen={dock._regenerate_button.isEnabled()}")
    say(f"  run-dir files byte-identical   : {before == after}  ({len(before)} files)")
    p1j_ok = (
        clear_exc is None
        and before == after
        and dock._bridge is None
        and dock._stages_table.rowCount() == 0
        and dock._job_identity_label.text() == "No job loaded"
        and dock._result_text.toPlainText() == ""
    )
    say(f"  P1j RESULT: {'PASS' if p1j_ok else 'FAIL'}")

    say()
    say("=" * 78)
    say("SUMMARY")
    say("=" * 78)
    say(f"  P1a factory/bind  : {'PASS' if p1a_ok else 'FAIL'}")
    say(f"  P1b validate      : {'PASS' if p1b_ok else 'FAIL'}")
    say(f"  P1e projection    : {'PASS' if p1e_ok else 'FAIL'}")
    say(f"  P1f regenerate D-2: {'PASS' if p1f_ok else 'FAIL'}")
    say(f"  P1g desktop executes regeneration: "
        f"{'YES' if p1g_executed else 'NO  <-- DEFECT'}")
    say(f"  P1h make current/STALE: {'PASS' if p1h_ok else 'FAIL'}")
    say(f"  P1i output render : {'PASS' if p1i_ok else 'FAIL'}")
    say(f"  P1j New Job       : {'PASS' if p1j_ok else 'FAIL'}")
    say(f"  network touched   : NO (in-process SpyProvider only)")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        code = 99
    sys.exit(code)
