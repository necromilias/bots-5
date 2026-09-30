"""Pytest plugin that records exact execution evidence for one CI shard.

Enabled only when ``BOTS5_CI_REPORT_PATH`` is set, so ordinary local runs are
unaffected.  The JSON it writes is the raw evidence the aggregate gate trusts:
for every assigned test it records the outcome actually observed by pytest,
plus the node IDs pytest actually collected for this invocation.  A test that
is merely *claimed* to have run never appears here.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class CiShardReporter:
    def __init__(self, config: Any) -> None:
        self._config = config
        self._report_path = os.environ.get("BOTS5_CI_REPORT_PATH")
        self._candidate_sha = os.environ.get("BOTS5_CI_CANDIDATE_SHA", "")
        self._shard_index = int(os.environ.get("BOTS5_CI_SHARD_INDEX", "-1"))
        self._assigned_sha256 = os.environ.get("BOTS5_CI_ASSIGNED_SHA256", "")
        self._active = bool(self._report_path)
        self._collected: list[str] = []
        self._failed: set[str] = set()
        self._passed: set[str] = set()
        self._skipped: dict[str, str] = {}
        self._seen: set[str] = set()

    # -- collection ---------------------------------------------------------
    def pytest_collection_modifyitems(self, session: Any, config: Any, items: Any) -> None:
        if self._active:
            self._collected = [item.nodeid for item in items]

    # -- per-phase outcomes -------------------------------------------------
    def pytest_runtest_logreport(self, report: Any) -> None:
        if not self._active:
            return
        node_id = report.nodeid
        self._seen.add(node_id)
        if hasattr(report, "wasxfail"):
            if report.skipped:
                self._skipped.setdefault(node_id, "xfail")
            else:
                self._passed.add(node_id)
            return
        if report.failed:
            self._failed.add(node_id)
        elif report.skipped:
            reason = "skipped"
            longrepr = getattr(report, "longrepr", None)
            if isinstance(longrepr, tuple) and len(longrepr) >= 3:
                reason = str(longrepr[2])
            elif longrepr is not None:
                reason = str(longrepr)
            self._skipped.setdefault(node_id, reason.splitlines()[0][:500])
        elif report.when == "call" and report.passed:
            self._passed.add(node_id)

    # -- write evidence -----------------------------------------------------
    def pytest_sessionfinish(self, session: Any, exitstatus: Any) -> None:
        if not self._active or not self._report_path:
            return
        outcomes: dict[str, str] = {}
        for node_id in self._collected:
            if node_id in self._failed:
                outcomes[node_id] = "failed"
            elif node_id in self._skipped:
                outcomes[node_id] = "skipped"
            elif node_id in self._passed:
                outcomes[node_id] = "passed"
            else:
                outcomes[node_id] = "error"
        uncollected_executed = sorted(self._seen - set(self._collected))
        payload = {
            "schema": 1,
            "candidate_sha": self._candidate_sha,
            "shard_index": self._shard_index,
            "assigned_sha256": self._assigned_sha256,
            "collected_count": len(self._collected),
            "collected_ids": list(self._collected),
            "outcomes": outcomes,
            "outcomes_count": len(outcomes),
            "skipped": {key: value for key, value in sorted(self._skipped.items())},
            "executed_not_collected": uncollected_executed,
            "pytest_exitstatus": int(exitstatus) if exitstatus is not None else None,
        }
        path = Path(self._report_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )


def pytest_configure(config: Any) -> None:
    config.pluginmanager.register(CiShardReporter(config), "bots5-ci-shard-reporter")
