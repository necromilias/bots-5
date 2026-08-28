from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Sequence

from .errors import Bots5Error
from .manifest import load_job, validate_referenced_files
from .models import RunResult
from .paths import locate_run_dir
from .providers.openrouter import OpenRouterProvider
from .runner import run_job
from .storage import load_run_view, load_stage_view


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bots5")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="validate a job without API calls")
    validate.add_argument("job", type=Path)

    run = sub.add_parser("run", help="execute a validated job")
    run.add_argument("job", type=Path)

    status = sub.add_parser("status", help="show a persisted run")
    status.add_argument("run_id")
    status.add_argument("--runs-dir", type=Path, default=None)

    inspect = sub.add_parser("inspect", help="show a persisted stage")
    inspect.add_argument("run_id")
    inspect.add_argument("stage_id")
    inspect.add_argument("--runs-dir", type=Path, default=None)

    return parser


def _cost_text(stage: dict) -> str:
    return stage.get("cost_usd") if stage.get("cost_known") else "?"


def _print_result(result: RunResult) -> None:
    print(f"run_id: {result.run_id}")
    print(f"state: {result.state.value}")
    for stage in result.stages:
        cost = "?" if stage.known_cost_usd is None else str(stage.known_cost_usd)
        print(f"{stage.id}: {stage.state.value} cost={cost}")


def _cmd_validate(path: Path) -> int:
    job = load_job(path)
    validate_referenced_files(job)
    print(f"OK: {job.name}")
    return 0


def _cmd_run(path: Path) -> int:
    job = load_job(path)
    validate_referenced_files(job)
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise Bots5Error("OPENROUTER_API_KEY is not set")
    provider = OpenRouterProvider(api_key)
    result = asyncio.run(run_job(job, provider))
    _print_result(result)
    return result.exit_code


def _cmd_status(run_id: str, runs_dir: Path | None) -> int:
    run_dir = locate_run_dir(run_id, runs_dir)
    run, stages, usage = load_run_view(run_dir)
    print(f"run_id: {run['run_id']}")
    print(f"state: {run['state']}")
    for stage in stages:
        failure = stage.get("failure")
        failure_text = ""
        if failure:
            failure_text = f" failure={failure.get('type')}: {failure.get('message')}"
        usage_s = stage.get("usage") or {}
        print(
            f"{stage['stage_id']}: model={stage['requested_model']} state={stage['state']} "
            f"duration={stage.get('duration_seconds')} "
            f"tokens={usage_s.get('total_tokens')} cost={_cost_text(stage)} "
            f"output={stage.get('output_path')}{failure_text}"
        )
    agg = usage.get("aggregate", {})
    print(
        "aggregate_cost: "
        f"{agg.get('cost_usd_known_sum')} "
        f"status={agg.get('cost_status')} "
        f"complete={agg.get('cost_complete')}"
    )
    return 0


def _cmd_inspect(run_id: str, stage_id: str, runs_dir: Path | None) -> int:
    run_dir = locate_run_dir(run_id, runs_dir)
    meta, output = load_stage_view(run_dir, stage_id)
    print(f"stage_id: {meta['stage_id']}")
    print(f"model: {meta['requested_model']}")
    print(f"state: {meta['state']}")
    print(f"metadata: {meta}")
    if output is not None:
        print("--- output ---")
        print(output, end="" if output.endswith("\n") else "\n")
    failure = meta.get("failure")
    if failure:
        print("--- failure ---")
        print(f"{failure.get('type')}: {failure.get('message')}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate":
            return _cmd_validate(args.job)
        if args.command == "run":
            return _cmd_run(args.job)
        if args.command == "status":
            return _cmd_status(args.run_id, args.runs_dir)
        if args.command == "inspect":
            return _cmd_inspect(args.run_id, args.stage_id, args.runs_dir)
    except Bots5Error as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
