import sys, faulthandler
faulthandler.dump_traceback_later(20, exit=True)
print("start", flush=True)
from pathlib import Path
import tempfile
print("importing bots5.storage", flush=True)
from bots5.storage import create_run_tree, persist_stage_attempt
from bots5.models import StageRecord, StageState
from bots5.core.campaign import project_run
print("imported", flush=True)
tmp = Path(tempfile.mkdtemp(prefix="t-"))
print("creating tree", flush=True)
dirs = create_run_tree(tmp/"runs", "t-run-20260101T000000Z-00000001")
print("tree ok", flush=True)
