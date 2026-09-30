"""Focused tests for the CI shard evidence plugin.

The plugin is the only source of truth about which node IDs a shard actually
executed, so its outcome mapping must be pinned: a collected test that never
produced a report outcome must be recorded as an error, never as a pass.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from scripts.ci import ci_pytest_report


class FakeReport:
    def __init__(self, nodeid: str, when: str, outcome: str, wasxfail: bool = False):
        self.nodeid = nodeid
        self.when = when
        self.outcome = outcome
        self.passed = outcome == "passed"
        self.failed = outcome == "failed"
        self.skipped = outcome == "skipped"
        self.longrepr = ("file", 1, "reason") if outcome == "skipped" else None
        if wasxfail:
            self.wasxfail = "expected failure"


def _reporter(tmp_path, monkeypatch, shard_index: int = 2):
    report_path = tmp_path / "shard-02.report.json"
    monkeypatch.setenv("BOTS5_CI_REPORT_PATH", str(report_path))
    monkeypatch.setenv("BOTS5_CI_CANDIDATE_SHA", "dee0b3b8446d7f84bcc4e1a5af2339c5ed78383f")
    monkeypatch.setenv("BOTS5_CI_SHARD_INDEX", str(shard_index))
    monkeypatch.setenv("BOTS5_CI_ASSIGNED_SHA256", "a" * 64)
    return ci_pytest_report.CiShardReporter(SimpleNamespace()), report_path


def _collect(reporter, node_ids):
    items = [SimpleNamespace(nodeid=node_id) for node_id in node_ids]
    reporter.pytest_collection_modifyitems(None, None, items)


def test_outcome_mapping_and_identity(tmp_path, monkeypatch):
    reporter, report_path = _reporter(tmp_path, monkeypatch)
    node_ids = [
        "tests/a.py::test_pass",
        "tests/a.py::test_fail",
        "tests/a.py::test_skip",
    ]
    _collect(reporter, node_ids)
    reporter.pytest_runtest_logreport(FakeReport(node_ids[0], "setup", "passed"))
    reporter.pytest_runtest_logreport(FakeReport(node_ids[0], "call", "passed"))
    reporter.pytest_runtest_logreport(FakeReport(node_ids[1], "call", "failed"))
    reporter.pytest_runtest_logreport(FakeReport(node_ids[2], "setup", "skipped"))
    reporter.pytest_sessionfinish(None, 1)

    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["candidate_sha"] == "dee0b3b8446d7f84bcc4e1a5af2339c5ed78383f"
    assert payload["shard_index"] == 2
    assert payload["assigned_sha256"] == "a" * 64
    assert payload["outcomes"] == {
        node_ids[0]: "passed",
        node_ids[1]: "failed",
        node_ids[2]: "skipped",
    }
    assert payload["pytest_exitstatus"] == 1


def test_collected_without_report_is_error_not_pass(tmp_path, monkeypatch):
    reporter, report_path = _reporter(tmp_path, monkeypatch)
    node_ids = ["tests/a.py::test_never_ran"]
    _collect(reporter, node_ids)
    reporter.pytest_sessionfinish(None, 0)

    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["outcomes"] == {"tests/a.py::test_never_ran": "error"}


def test_executed_but_not_collected_is_recorded(tmp_path, monkeypatch):
    reporter, report_path = _reporter(tmp_path, monkeypatch)
    _collect(reporter, ["tests/a.py::test_expected"])
    reporter.pytest_runtest_logreport(FakeReport("tests/a.py::test_expected", "call", "passed"))
    reporter.pytest_runtest_logreport(FakeReport("tests/a.py::test_unexpected", "call", "passed"))
    reporter.pytest_sessionfinish(None, 0)

    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["executed_not_collected"] == ["tests/a.py::test_unexpected"]


def test_plugin_is_inert_without_report_path(tmp_path, monkeypatch):
    monkeypatch.delenv("BOTS5_CI_REPORT_PATH", raising=False)
    reporter = ci_pytest_report.CiShardReporter(SimpleNamespace())
    _collect(reporter, ["tests/a.py::test_one"])
    reporter.pytest_runtest_logreport(FakeReport("tests/a.py::test_one", "call", "passed"))
    reporter.pytest_sessionfinish(None, 0)
    assert list(tmp_path.iterdir()) == []
