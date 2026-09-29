"""Four processes replay the SAME approval id into the same run dir.

run_job's pre-tree check `approval_consumed()` is a plain stat() (TOCTOU window);
the authoritative gate should be consume_approval()'s O_CREAT|O_EXCL marker.
Verify at most one process dispatches provider work and every other fails closed.
"""
import asyncio, sys, shutil, json, multiprocessing as mp
from pathlib import Path
sys.path.insert(0, "src"); sys.path.insert(0, ".")


def worker(root_str, q):
    from bots5.manifest import load_job
    from bots5.runner import run_job, build_preflight_snapshot
    from bots5.models import ApprovalRecord
    from bots5.errors import ApprovalInvalidatedError, Bots5Error, StorageError
    from tests.test_phase10_desktop_preflight import FakeRoutedProvider
    from datetime import datetime, timezone
    root = Path(root_str)
    job = load_job(root / "job.json")
    snap = build_preflight_snapshot(job)
    run_id = "race-run"
    approval = ApprovalRecord(
        approval_id="approval-shared",
        approved_at=datetime.now(timezone.utc).isoformat(),
        approved_by="op",
        preflight_digest=snap.preflight_digest,
        scope="full_run",
        target={"run_id": run_id, "stage_id": None, "attempt_number": None},
    )
    p = FakeRoutedProvider()
    try:
        res = asyncio.run(run_job(job, {"openrouter": p}, run_id=run_id,
                                 snapshot=snap, approval=approval))
        q.put(("DISPATCHED", len(p.calls), str(res.state)))
    except (ApprovalInvalidatedError, StorageError, Bots5Error) as e:
        q.put(("REFUSED", type(e).__name__, "calls=%d" % len(p.calls)))
    except BaseException as e:
        q.put(("OTHER", type(e).__name__, str(e)[:160]))


if __name__ == "__main__":
    base = Path(__file__).resolve().parent
    root = base / "race"; shutil.rmtree(root, ignore_errors=True); root.mkdir(parents=True)
    from tests.helpers import make_job_tree
    make_job_tree(root)
    ctx = mp.get_context("spawn")
    qs, ps = [], []
    for _ in range(4):
        q = ctx.SimpleQueue()
        s = ctx.Process(target=worker, args=(str(root), q))
        s.start(); qs.append(q); ps.append(s)
    for s in ps:
        s.join(90)
    results = [q.get() for q in qs]
    for r in results:
        print(r)
    print("DISPATCHED_COUNT:", sum(1 for r in results if r[0] == "DISPATCHED"))
    rd = root / ".bots5" / "runs" / "race-run"
    if (rd / "run.json").is_file():
        print("run.json state:", json.loads((rd / "run.json").read_text())["state"])
    else:
        print("NO RUN JSON")
