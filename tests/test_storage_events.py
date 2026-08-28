from __future__ import annotations

import json

from bots5.events import EventWriter
from bots5.storage import create_run_tree, new_run_id


def test_unique_run_ids():
    assert new_run_id("x") != new_run_id("x")


def test_event_jsonl_valid(tmp_path):
    dirs = create_run_tree(tmp_path / "runs", "run-1")
    writer = EventWriter(dirs.events, "run-1")
    writer.write("run_started")
    writer.write("stage_started", "stage1")
    lines = dirs.events.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    assert parsed[0]["event"] == "run_started"
    assert parsed[1]["stage_id"] == "stage1"
