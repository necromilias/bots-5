"""Deterministic inventory, sharding and reconciliation for CI v1 T4.

Design contract
---------------
* One exact candidate commit SHA binds every artefact.  The aggregate is given
  the workflow's *expected* SHA independently of the evidence and refuses to
  accept evidence for any other commit.
* The canonical inventory is whatever pytest collects for that SHA, in
  collection order; it is never hard-coded.  A reviewed baseline inventory acts
  as a population floor: a baseline test can only disappear through a deliberate,
  reviewable baseline update.
* Shard assignment is a pure function of (canonical inventory, shard count) and
  contiguous over collection order, so relative order is preserved and a
  one-shard run reproduces the historical serial order.
* The manifest (inventory, assignment hashes, allow-list) lives in the trusted
  prepare-job artefact directory and is read only from there; shard evidence
  lives in a separate directory so a shard cannot overwrite the manifest that
  judges it.
* The aggregate gate recomputes everything from raw evidence.  A shard that
  merely *claims* success is ignored; a missing, cancelled, duplicated,
  omitted, dirty-tree or foreign-SHA shard makes the aggregate fail closed.

This module has no third-party imports.  It is exercised by
``tests/test_ci_sharding.py`` and ``tests/test_ci_aggregate.py``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

SCHEMA_VERSION = 1
MAX_SHARDS = 16
NODE_ID_RE = re.compile(r"^[^\s:]+\.py::.+$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")

CANONICAL_FILE = "canonical.txt"
CANONICAL_JSON = "canonical.json"
SHARDS_JSON = "shards.json"
ALLOWED_SKIPS_FILE = "allowed-skips.txt"
AGGREGATE_JSON = "aggregate.json"

VALID_OUTCOMES = frozenset({"passed", "failed", "error", "skipped"})
FAILING_OUTCOMES = frozenset({"failed", "error"})


class CiError(Exception):
    """A CI input or reconciliation invariant was violated."""


# ---------------------------------------------------------------------------
# inventory
# ---------------------------------------------------------------------------


def parse_collection_output(text: str) -> list[str]:
    """Extract pytest node IDs from ``pytest --collect-only -q`` stdout.

    The trailing summary line and the blank separator are ignored.  Anything
    that is not a plausible node ID is ignored rather than guessed at; a run
    that yields too few IDs is caught by :func:`validate_inventory`.
    """
    node_ids: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if NODE_ID_RE.match(line):
            node_ids.append(line)
    return node_ids


def validate_inventory(node_ids: Sequence[str]) -> list[str]:
    """Return the canonical inventory **in collection order**, or raise.

    Order is deliberately preserved: the canonical inventory is the order in
    which pytest would execute the population serially, so a one-shard run
    reproduces the historical serial order exactly.
    """
    if not node_ids:
        raise CiError("canonical collection produced zero test node IDs")
    bad = [nid for nid in node_ids if not NODE_ID_RE.match(nid)]
    if bad:
        raise CiError(f"malformed node IDs in canonical inventory: {bad[:5]!r}")
    seen: set[str] = set()
    duplicates: list[str] = []
    for node_id in node_ids:
        if node_id in seen:
            duplicates.append(node_id)
        seen.add(node_id)
    if duplicates:
        raise CiError(
            "canonical inventory contains duplicate node IDs: "
            f"{sorted(set(duplicates))[:5]!r}"
        )
    return list(node_ids)


def assign_shards(ordered_ids: Sequence[str], shard_count: int) -> list[list[str]]:
    """Split an ordered inventory into ``shard_count`` deterministic shards.

    Assignment is round-robin over the canonical **collection order**, so:

    * the partition is a pure function of (inventory, shard count) — deterministic,
      disjoint, complete, sizes within one, independent of ``PYTHONHASHSEED``;
    * ``shard_count == 1`` reproduces the historical serial execution order
      byte-for-byte, because round-robin over one shard is identity;
    * for ``shard_count > 1`` each test file's tests are spread across all shards,
      which is what keeps per-shard wall clock near ``1/N`` of serial time rather
      than concentrating the heaviest file (``test_phase6_context_attachments``,
      ~47% of historical wall clock) into one shard.

    Empty shards are refused: a shard count larger than the population is an
    operator error, not something to paper over.
    """
    ids = list(ordered_ids)
    if type(shard_count) is not int or shard_count < 1:
        raise CiError(f"shard count must be an integer >= 1, got {shard_count!r}")
    if shard_count > MAX_SHARDS:
        raise CiError(f"shard count must be <= {MAX_SHARDS}, got {shard_count}")
    if len(ids) < shard_count:
        raise CiError(
            f"refusing to create empty shards: {len(ids)} tests < "
            f"{shard_count} shards"
        )
    shards: list[list[str]] = [[] for _ in range(shard_count)]
    for index, node_id in enumerate(ids):
        shards[index % shard_count].append(node_id)
    return shards


def shard_id(index: int) -> str:
    """Stable zero-padded shard identifier used in file and artifact names."""
    if index < 0:
        raise CiError(f"shard index must be >= 0, got {index}")
    return f"shard-{index:02d}"


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def text_sha256(text: str) -> str:
    return _sha256_bytes(text.encode("utf-8"))


def _write_lines(path: Path, lines: Iterable[str]) -> str:
    body = "".join(f"{line}\n" for line in lines)
    path.write_text(body, encoding="utf-8")
    return text_sha256(body)


def load_allowed_skips(text: str) -> list[str]:
    """Parse an allow-list file; blank lines and ``#`` comments are ignored."""
    entries: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not NODE_ID_RE.match(line):
            raise CiError(f"malformed allow-list entry: {line!r}")
        entries.append(line)
    return sorted(set(entries))


# ---------------------------------------------------------------------------
# build-shards
# ---------------------------------------------------------------------------


def build_shards(
    *,
    collect_text: str,
    candidate_sha: str,
    shard_count: int,
    allowed_skips: Sequence[str],
    baseline_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """Validate the collection and produce the canonical + shard manifest."""
    if not SHA_RE.match(candidate_sha):
        raise CiError(f"candidate SHA is not a 40-hex commit id: {candidate_sha!r}")
    inventory = validate_inventory(parse_collection_output(collect_text))
    assignments = assign_shards(inventory, shard_count)

    inventory_set = set(inventory)
    baseline = validate_inventory(baseline_ids) if baseline_ids else []
    if baseline:
        missing_baseline = sorted(set(baseline) - inventory_set)
        if missing_baseline:
            raise CiError(
                "reviewed T4 baseline tests are absent from the canonical "
                "inventory; removing a test requires a reviewed baseline "
                f"update: {missing_baseline[:5]!r}"
            )

    stale = sorted(set(allowed_skips) - inventory_set)
    if stale:
        raise CiError(
            "allow-list references tests absent from the canonical inventory "
            f"(stale CI policy): {stale!r}"
        )

    # The skip policy is part of the standard, not part of the candidate's
    # convenience: a skip may only be declared for a test in the reviewed
    # population floor.  Widening the allow-list therefore requires a reviewed
    # baseline update, exactly like removing a test does.
    if baseline:
        unpinned_skips = sorted(set(allowed_skips) - set(baseline))
        if unpinned_skips:
            raise CiError(
                "allow-list declares skips for tests outside the reviewed "
                "population floor; widening the skip policy requires a "
                f"reviewed baseline update: {unpinned_skips[:5]!r}"
            )

    shard_records: list[dict[str, Any]] = []
    bodies: list[tuple[str, str]] = []
    for index, assigned in enumerate(assignments):
        body = "".join(f"{node_id}\n" for node_id in assigned)
        bodies.append((shard_id(index), body))
        shard_records.append(
            {
                "id": shard_id(index),
                "index": index,
                "count": len(assigned),
                "sha256": text_sha256(body),
            }
        )

    canonical_body = "".join(f"{node_id}\n" for node_id in inventory)
    baseline_body = "".join(f"{node_id}\n" for node_id in baseline)
    allowed_body = "".join(f"{node_id}\n" for node_id in sorted(set(allowed_skips)))
    canonical_record = {
        "schema": SCHEMA_VERSION,
        "candidate_sha": candidate_sha,
        "count": len(inventory),
        "canonical_sha256": text_sha256(canonical_body),
        "baseline_count": len(baseline),
        "baseline_sha256": text_sha256(baseline_body) if baseline else None,
        "allowed_skips": sorted(set(allowed_skips)),
        "allowed_skips_sha256": text_sha256(allowed_body),
        "shard_count": shard_count,
        "producer": "scripts/ci/ci_support.py",
    }
    manifest = {
        "schema": SCHEMA_VERSION,
        "candidate_sha": candidate_sha,
        "shard_count": shard_count,
        "canonical_count": len(inventory),
        "canonical_sha256": canonical_record["canonical_sha256"],
        "baseline_count": len(baseline),
        "baseline_sha256": canonical_record["baseline_sha256"],
        "shards": shard_records,
    }
    return {
        "canonical_body": canonical_body,
        "canonical_record": canonical_record,
        "manifest": manifest,
        "shard_bodies": bodies,
    }


def write_shards(out_dir: Path, built: Mapping[str, Any]) -> None:
    """Materialise the built manifest into ``out_dir`` (files + JSON)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / CANONICAL_FILE).write_text(
        built["canonical_body"], encoding="utf-8"
    )
    _write_json(out_dir / CANONICAL_JSON, built["canonical_record"])
    _write_json(out_dir / SHARDS_JSON, built["manifest"])
    (out_dir / ALLOWED_SKIPS_FILE).write_text(
        "".join(f"{nid}\n" for nid in built["canonical_record"]["allowed_skips"]),
        encoding="utf-8",
    )
    for name, body in built["shard_bodies"]:
        (out_dir / f"{name}.txt").write_text(body, encoding="utf-8")


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CiError(f"missing evidence file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise CiError(f"malformed evidence file {path}: {exc}") from exc


# ---------------------------------------------------------------------------
# aggregate
# ---------------------------------------------------------------------------


@dataclass
class ShardEvidence:
    """Everything the aggregate knows about one shard, from raw files only."""

    index: int
    assigned_ids: list[str]
    report: Mapping[str, Any] | None
    exit_code: int | None
    report_path: Path | None
    exit_path: Path | None
    problems: list[str] = field(default_factory=list)
    collected_ids: list[str] = field(default_factory=list)
    outcomes: dict[str, str] = field(default_factory=dict)


def clean_tree_marker(candidate_sha: str, name: str) -> str:
    """The exact content of a clean tree-integrity record.

    A clean seal is deliberately *not* an empty file.  The evidence directory is
    writable by the tests it judges, so "no content" must not be the thing that
    means "clean": a candidate could otherwise plant an empty seal and force the
    sealing step to abort before it overwrites the file.
    """
    return f"clean {candidate_sha} {name}"


def _load_shard_evidence(
    evidence_dir: Path,
    index: int,
    assigned_ids: list[str],
    *,
    require_tree: bool = True,
    candidate_sha: str | None = None,
) -> ShardEvidence:
    name = shard_id(index)
    report_path = evidence_dir / f"{name}.report.json"
    exit_path = evidence_dir / f"{name}.exit"
    tree_path = evidence_dir / f"{name}.tree"
    evidence = ShardEvidence(
        index=index,
        assigned_ids=list(assigned_ids),
        report=None,
        exit_code=None,
        report_path=report_path if report_path.is_file() else None,
        exit_path=exit_path if exit_path.is_file() else None,
    )
    if report_path.is_file():
        evidence.report = _read_json(report_path)
    else:
        evidence.problems.append(f"missing shard report {report_path.name}")
    if exit_path.is_file():
        raw = exit_path.read_text(encoding="utf-8").strip()
        try:
            evidence.exit_code = int(raw)
        except ValueError:
            evidence.problems.append(f"malformed shard exit file {exit_path.name}")
    else:
        evidence.problems.append(f"missing shard exit file {exit_path.name}")
    if tree_path.is_file():
        record = tree_path.read_text(encoding="utf-8").strip()
        if record == clean_tree_marker(candidate_sha, name):
            pass
        elif not record:
            evidence.problems.append(
                f"{name} has an empty tree-integrity record, which is not a "
                "clean seal"
            )
        else:
            evidence.problems.append(
                f"{name} tests modified tracked repository files:\n{record}"
            )
    elif require_tree:
        evidence.problems.append(f"missing tree-integrity record {tree_path.name}")
    if evidence.report is not None:
        collected = evidence.report.get("collected_ids")
        outcomes = evidence.report.get("outcomes")
        if not isinstance(collected, list) or not all(
            isinstance(item, str) for item in collected
        ):
            evidence.problems.append(f"{name} report has no usable collected_ids")
        else:
            evidence.collected_ids = list(collected)
        if not isinstance(outcomes, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in outcomes.items()
        ):
            evidence.problems.append(f"{name} report has no usable outcomes")
        else:
            evidence.outcomes = dict(outcomes)
    return evidence


def reconcile(
    *,
    candidate_sha: str,
    shard_count: int,
    canonical_ids: Sequence[str],
    assignments: Sequence[Sequence[str]],
    evidences: Sequence[ShardEvidence],
    allowed_skips: Sequence[str],
    expected_sha: str | None = None,
    baseline_ids: Sequence[str] = (),
    event: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Recompute the truth of a sharded run from raw evidence.

    Raises :class:`CiError` only for structurally impossible inputs; every
    substantive failure is reported as a ``failure`` string and a non-PASS
    result so the aggregate job can still publish the evidence.
    """
    failures: list[str] = []
    if not SHA_RE.match(candidate_sha):
        failures.append(f"candidate SHA is not a 40-hex commit id: {candidate_sha!r}")
    if expected_sha is not None and candidate_sha != expected_sha:
        failures.append(
            f"evidence candidate SHA {candidate_sha!r} does not match the "
            f"workflow's expected SHA {expected_sha!r}"
        )
    if shard_count != len(assignments):
        failures.append(
            f"shard count {shard_count} disagrees with {len(assignments)} assignments"
        )
    if shard_count != len(evidences):
        failures.append(
            f"shard count {shard_count} disagrees with {len(evidences)} evidence sets"
        )

    canonical = list(canonical_ids)
    try:
        canonical_checked = validate_inventory(canonical)
    except CiError as exc:
        failures.append(f"canonical inventory invalid: {exc}")
        canonical_checked = sorted(set(canonical))
    if canonical_checked != canonical:
        failures.append("canonical inventory changed during validation")

    canonical_set = set(canonical_checked)

    # 0. the reviewed baseline population must still be present in full.
    if baseline_ids:
        missing_from_canonical = sorted(set(baseline_ids) - canonical_set)
        if missing_from_canonical:
            failures.append(
                "reviewed T4 baseline tests are absent from the canonical "
                f"inventory: {missing_from_canonical[:5]!r}"
            )

    # 1. assignments must partition the canonical inventory exactly.
    assigned_all: list[str] = []
    for index, assigned in enumerate(assignments):
        listed = list(assigned)
        assigned_all.extend(listed)
        if len(set(listed)) != len(listed):
            failures.append(f"{shard_id(index)} assignment contains duplicates")
        if any(node not in canonical_set for node in listed):
            failures.append(f"{shard_id(index)} assignment contains non-canonical tests")
    assigned_set = set(assigned_all)
    if len(assigned_all) != len(assigned_set):
        duplicated = sorted(
            {node for node in assigned_all if assigned_all.count(node) > 1}
        )
        failures.append(f"tests assigned to more than one shard: {duplicated[:5]!r}")
    omitted_from_assignment = sorted(canonical_set - assigned_set)
    if omitted_from_assignment:
        failures.append(
            f"canonical tests assigned to no shard: {omitted_from_assignment[:5]!r}"
        )
    extra_in_assignment = sorted(assigned_set - canonical_set)
    if extra_in_assignment:
        failures.append(
            f"assigned tests absent from canonical inventory: {extra_in_assignment[:5]!r}"
        )

    # 2. each shard: identity, exact collection, exact execution, clean exit.
    per_shard: list[dict[str, Any]] = []
    executed_all: list[str] = []
    skipped_observed: list[str] = []
    for index in range(shard_count):
        evidence = evidences[index] if index < len(evidences) else None
        assigned = assignments[index] if index < len(assignments) else []
        expected = list(assigned)
        expected_set = set(expected)
        record: dict[str, Any] = {
            "id": shard_id(index),
            "index": index,
            "assigned": len(expected),
            "collected": 0,
            "executed": 0,
            "passed": 0,
            "failed": 0,
            "error": 0,
            "skipped": 0,
            "exit_code": None,
            "report_present": False,
        }
        if evidence is None:
            failures.append(f"{shard_id(index)} has no evidence at all")
            per_shard.append(record)
            continue
        if evidence.problems:
            failures.extend(f"{shard_id(index)}: {problem}" for problem in evidence.problems)
        report = evidence.report
        record["report_present"] = report is not None
        if report is not None:
            if report.get("candidate_sha") != candidate_sha:
                failures.append(
                    f"{shard_id(index)} reports candidate "
                    f"{report.get('candidate_sha')!r} != {candidate_sha!r}"
                )
            if report.get("shard_index") != index:
                failures.append(
                    f"{shard_id(index)} report carries shard_index "
                    f"{report.get('shard_index')!r}"
                )
            assigned_hash = report.get("assigned_sha256")
            recomputed = text_sha256("".join(f"{node}\n" for node in expected))
            if assigned_hash != recomputed:
                failures.append(
                    f"{shard_id(index)} executed a different assignment "
                    f"({assigned_hash!r} != {recomputed!r})"
                )
        collected = set(evidence.collected_ids)
        outcomes = dict(evidence.outcomes)
        record["collected"] = len(evidence.collected_ids)
        record["executed"] = len(outcomes)
        executed_all.extend(outcomes.keys())
        if collected != expected_set:
            missing = sorted(expected_set - collected)
            extra = sorted(collected - expected_set)
            failures.append(
                f"{shard_id(index)} pytest collection mismatch "
                f"(missing={missing[:5]!r} extra={extra[:5]!r})"
            )
        if set(outcomes) != expected_set:
            missing = sorted(expected_set - set(outcomes))
            extra = sorted(set(outcomes) - expected_set)
            failures.append(
                f"{shard_id(index)} execution mismatch "
                f"(missing={missing[:5]!r} extra={extra[:5]!r})"
            )
        for node_id, outcome in sorted(outcomes.items()):
            if outcome not in VALID_OUTCOMES:
                failures.append(
                    f"{shard_id(index)} reports unknown outcome {outcome!r} for {node_id}"
                )
            elif outcome in FAILING_OUTCOMES:
                failures.append(f"{shard_id(index)} {outcome}: {node_id}")
                record[outcome] += 1
            elif outcome == "skipped":
                skipped_observed.append(node_id)
                record["skipped"] += 1
            else:
                record["passed"] += 1
        record["exit_code"] = evidence.exit_code
        if evidence.exit_code != 0:
            failures.append(
                f"{shard_id(index)} pytest exit code "
                f"{evidence.exit_code!r} is not 0"
            )
        per_shard.append(record)

    # 3. whole-population coverage.
    executed_set = set(executed_all)
    duplicated = sorted(
        {node for node in executed_all if executed_all.count(node) > 1}
    )
    omitted = sorted(canonical_set - executed_set)
    unexpected = sorted(executed_set - canonical_set)
    if duplicated:
        failures.append(f"tests executed by more than one shard: {duplicated[:5]!r}")
    if omitted:
        failures.append(f"canonical tests never executed: {omitted[:5]!r}")
    if unexpected:
        failures.append(f"executed tests outside canonical inventory: {unexpected[:5]!r}")

    # 4. skips must be truthful and declared.
    unexpected_skips = sorted(set(skipped_observed) - set(allowed_skips))
    if unexpected_skips:
        failures.append(f"unexpected skips: {unexpected_skips[:5]!r}")

    # The skip policy is part of the standard: a skip may only be declared for a
    # test in the reviewed population floor, so the allow-list cannot be widened
    # into a blanket excuse without a reviewed baseline update.
    if baseline_ids:
        unpinned_skips = sorted(set(allowed_skips) - set(baseline_ids))
        if unpinned_skips:
            failures.append(
                "skip allow-list covers tests outside the reviewed population "
                f"floor: {unpinned_skips[:5]!r}"
            )

    passed_total = sum(record["passed"] for record in per_shard)
    if passed_total == 0 and canonical_checked:
        failures.append(
            "no test passed: a run in which the entire population is skipped or "
            "errored cannot be a T4 pass"
        )

    result = "T4_PASS" if not failures else "T4_FAIL"
    return {
        "schema": SCHEMA_VERSION,
        "candidate_sha": candidate_sha,
        "expected_sha": expected_sha,
        "result": result,
        "shard_count": shard_count,
        "canonical_count": len(canonical_checked),
        "baseline_count": len(baseline_ids),
        "assigned_count": len(assigned_all),
        "executed_count": len(executed_set),
        "passed": passed_total,
        "failed": sum(record["failed"] for record in per_shard),
        "skipped": sum(record["skipped"] for record in per_shard),
        "allowed_skips": sorted(set(allowed_skips)),
        "omitted": omitted,
        "duplicated": duplicated,
        "unexpected_executed": unexpected,
        "unexpected_skips": unexpected_skips,
        "shards": per_shard,
        "event": dict(event) if event else {},
        "failures": failures,
    }


def resolve_evidence_dir(root: Path) -> Path:
    """Find the directory holding the manifest, tolerating one artifact level.

    ``actions/download-artifact`` may or may not preserve a wrapper directory
    depending on how artefacts were uploaded, so the aggregate locates
    ``canonical.json`` instead of assuming a fixed depth.  A missing or
    ambiguous manifest is itself a failed gate.
    """
    if (root / CANONICAL_JSON).is_file():
        return root
    candidates = sorted(root.glob(f"**/{CANONICAL_JSON}"))
    if len(candidates) == 1:
        return candidates[0].parent
    if not candidates:
        raise CiError(f"no {CANONICAL_JSON} found under {root}")
    raise CiError(f"ambiguous evidence root under {root}: {candidates!r}")


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise CiError(f"missing evidence file: {path}") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise CiError(f"unreadable evidence file {path}: {exc}") from exc


def load_and_reconcile(
    manifest_dir: Path,
    evidence_dir: Path | None = None,
    *,
    expected_sha: str | None = None,
    baseline_ids: Sequence[str] = (),
    require_tree: bool = True,
    event: Mapping[str, Any] | None = None,
    expected_allowed_skips_path: str | Path | None = None,
) -> dict[str, Any]:
    """Read the trusted manifest and untrusted shard evidence, then reconcile.

    ``manifest_dir`` holds the prepare job's outputs (canonical inventory,
    shard assignment hashes, allow-list).  ``evidence_dir`` holds only what
    shards produced.  They are separate arguments on purpose: a shard must not
    be able to overwrite the manifest that judges it.
    """
    manifest_dir = resolve_evidence_dir(manifest_dir)
    if evidence_dir is None:
        evidence_dir = manifest_dir
    canonical_record = _read_json(manifest_dir / CANONICAL_JSON)
    manifest = _read_json(manifest_dir / SHARDS_JSON)
    candidate_sha = canonical_record.get("candidate_sha")
    shard_count = canonical_record.get("shard_count")
    if not isinstance(candidate_sha, str):
        raise CiError("canonical.json is missing a string candidate_sha")
    if type(shard_count) is not int or not 1 <= shard_count <= MAX_SHARDS:
        raise CiError(f"canonical.json has an invalid shard_count: {shard_count!r}")

    canonical_text = _read_text(manifest_dir / CANONICAL_FILE)
    canonical_ids = validate_inventory(
        [line for line in canonical_text.splitlines() if line.strip()]
    )
    if text_sha256(canonical_text) != canonical_record.get("canonical_sha256"):
        raise CiError("canonical.txt does not match canonical.json hash")

    manifest_shards = manifest.get("shards")
    if not isinstance(manifest_shards, list) or len(manifest_shards) != shard_count:
        raise CiError("shards.json is inconsistent with canonical.json")
    if manifest.get("candidate_sha") != candidate_sha:
        raise CiError("shards.json candidate SHA disagrees with canonical.json")
    if manifest.get("shard_count") != shard_count:
        raise CiError("shards.json shard count disagrees with canonical.json")
    if manifest.get("canonical_sha256") != canonical_record.get("canonical_sha256"):
        raise CiError("shards.json canonical hash disagrees with canonical.json")
    if manifest.get("canonical_count") != len(canonical_ids):
        raise CiError("shards.json canonical count disagrees with canonical.txt")
    if canonical_record.get("count") != len(canonical_ids):
        raise CiError("canonical.json count disagrees with canonical.txt")

    assignments: list[list[str]] = []
    for index in range(shard_count):
        body = _read_text(manifest_dir / f"{shard_id(index)}.txt")
        recorded = manifest_shards[index]
        if not isinstance(recorded, Mapping):
            raise CiError(f"shards.json entry {index} is not an object")
        if recorded.get("sha256") != text_sha256(body):
            raise CiError(f"{shard_id(index)}.txt does not match the manifest hash")
        if recorded.get("index") != index:
            raise CiError(f"shards.json entry {index} carries the wrong index")
        if recorded.get("count") != len(
            [line for line in body.splitlines() if line.strip()]
        ):
            raise CiError(f"shards.json entry {index} carries the wrong count")
        assignments.append([line for line in body.splitlines() if line.strip()])

    # The allow-list is policy and therefore comes only from the trusted
    # manifest; it must be present and every entry must be a real test.
    allowed_path = manifest_dir / ALLOWED_SKIPS_FILE
    if not allowed_path.is_file():
        raise CiError(f"trusted manifest has no {ALLOWED_SKIPS_FILE}")
    allowed_text = _read_text(allowed_path)
    recorded_allowed_sha = canonical_record.get("allowed_skips_sha256")
    if not isinstance(recorded_allowed_sha, str):
        raise CiError("canonical.json does not record allowed_skips_sha256")
    if text_sha256(allowed_text) != recorded_allowed_sha:
        raise CiError(
            f"{ALLOWED_SKIPS_FILE} does not match the hash recorded in canonical.json"
        )
    allowed_skips = load_allowed_skips(allowed_text)
    if expected_allowed_skips_path is not None:
        # The reviewed skip policy lives outside the candidate.  When the caller
        # can supply it (a landed run), the manifest's policy must be the same
        # *set* of tests: the manifest copy is stored in canonical form (sorted,
        # deduplicated, comments stripped), while the reviewed file may carry a
        # comment header, so the comparison is semantic rather than byte-wise.
        # Widening the manifest allow-list cannot survive this check.
        expected_path = Path(expected_allowed_skips_path)
        if not expected_path.is_file():
            raise CiError(f"reviewed allow-list file not found: {expected_path}")
        reviewed_skips = load_allowed_skips(
            expected_path.read_text(encoding="utf-8")
        )
        if reviewed_skips != allowed_skips:
            raise CiError(
                "the manifest allow-list does not match the reviewed allow-list"
            )
        allowed_skips = reviewed_skips
    stale = sorted(set(allowed_skips) - set(canonical_ids))
    if stale:
        raise CiError(f"allow-list references unknown tests: {stale!r}")

    if baseline_ids:
        expected_baseline_sha = canonical_record.get("baseline_sha256")
        actual_baseline_sha = text_sha256(
            "".join(f"{node}\n" for node in baseline_ids)
        )
        if expected_baseline_sha != actual_baseline_sha:
            raise CiError(
                "the reviewed baseline inventory does not match the one "
                "recorded in the manifest"
            )

    evidences = [
        _load_shard_evidence(
            evidence_dir,
            index,
            assignments[index],
            require_tree=require_tree,
            candidate_sha=candidate_sha,
        )
        for index in range(shard_count)
    ]
    return reconcile(
        candidate_sha=candidate_sha,
        shard_count=shard_count,
        canonical_ids=canonical_ids,
        assignments=assignments,
        evidences=evidences,
        allowed_skips=allowed_skips,
        expected_sha=expected_sha,
        baseline_ids=baseline_ids,
        event=event,
    )


def render_summary(aggregate: Mapping[str, Any]) -> str:
    """Human-readable markdown summary for the GitHub job summary."""
    lines = [
        f"## T4 aggregate: {aggregate['result']}",
        "",
        f"- candidate SHA: `{aggregate['candidate_sha']}`",
        f"- expected SHA: `{aggregate.get('expected_sha')}`",
        f"- shards: {aggregate['shard_count']}",
        f"- canonical tests: {aggregate['canonical_count']}",
        f"- reviewed baseline tests: {aggregate.get('baseline_count')}",
        f"- executed tests: {aggregate['executed_count']}",
        f"- passed: {aggregate['passed']}  failed: {aggregate['failed']}  "
        f"skipped: {aggregate['skipped']}",
    ]
    if aggregate.get("event"):
        lines.append(f"- trigger: `{aggregate['event']}`")
    lines.extend(
        [
            "",
            "| shard | assigned | collected | executed | passed | failed | skipped | exit |",
            "|---|---|---|---|---|---|---|---|",
        ]
    )
    for record in aggregate["shards"]:
        lines.append(
            "| {id} | {assigned} | {collected} | {executed} | {passed} | "
            "{failed} | {skipped} | {exit_code} |".format(**record)
        )
    if aggregate["failures"]:
        lines.extend(["", "### Failures", ""])
        lines.extend(f"- {failure}" for failure in aggregate["failures"])
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# environment reporting
# ---------------------------------------------------------------------------


def collect_environment(*, candidate_sha: str, role: str) -> dict[str, Any]:
    """Record the *actual* runtime environment rather than inferring it."""
    import sqlite3

    report: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "role": role,
        "candidate_sha": candidate_sha,
        "runner_os": os.environ.get("RUNNER_OS", platform.system()),
        "runner_image_os": os.environ.get("ImageOS"),
        "runner_image_version": os.environ.get("ImageVersion"),
        "platform": platform.platform(),
        "uname": platform.uname()._asdict(),
        "python_version": sys.version,
        "python_executable": sys.executable,
        "python_implementation": platform.python_implementation(),
        "sqlite_module_version": sqlite3.sqlite_version,
        "sqlite_module_file": getattr(
            __import__("_sqlite3"), "__file__", None
        ),
        "sqlite_cli_version": _command_output(["sqlite3", "--version"]),
    }
    report["ldd_sqlite_module"] = _ldd(report["sqlite_module_file"])
    report["pip_freeze"] = _command_output(
        [sys.executable, "-m", "pip", "freeze"]
    )
    report["native_vfs"] = _native_vfs_report()
    return report


def _command_output(command: Sequence[str]) -> str | None:
    try:
        completed = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _ldd(path: str | None) -> str | None:
    if not path:
        return None
    output = _command_output(["ldd", path])
    if output is None:
        return None
    return "\n".join(
        sorted(line.strip() for line in output.splitlines() if "sqlite" in line.lower())
    )


def _native_vfs_report() -> dict[str, Any]:
    """Report whether the native rooted VFS loads and shares Python's SQLite."""
    try:
        from bots5.infrastructure.rooted_sqlite_vfs import _load_library
    except Exception as exc:  # pragma: no cover - environment dependent
        return {"loaded": False, "error": f"{type(exc).__name__}: {exc}"}
    try:
        library = _load_library()
    except Exception as exc:
        return {"loaded": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"loaded": True, "library": getattr(library, "_name", None)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _read_baseline(path: str | None) -> list[str]:
    if not path:
        return []
    baseline_path = Path(path)
    if not baseline_path.is_file():
        raise CiError(f"baseline inventory file not found: {baseline_path}")
    ids = [
        line.strip()
        for line in baseline_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not ids:
        raise CiError(f"baseline inventory file is empty: {baseline_path}")
    return validate_inventory(ids)


def _cmd_build_shards(args: argparse.Namespace) -> int:
    collect_text = Path(args.collect_output).read_text(encoding="utf-8")
    skipped_path = Path(args.allowed_skips)
    if not skipped_path.is_file():
        raise CiError(f"allow-list file not found: {skipped_path}")
    allowed = load_allowed_skips(skipped_path.read_text(encoding="utf-8"))
    built = build_shards(
        collect_text=collect_text,
        candidate_sha=args.candidate_sha,
        shard_count=args.shard_count,
        allowed_skips=allowed,
        baseline_ids=_read_baseline(args.baseline),
    )
    write_shards(Path(args.out), built)
    shards_output = ",".join(
        str(record["index"]) for record in built["manifest"]["shards"]
    )
    summary = {
        "candidate_sha": args.candidate_sha,
        "canonical_count": built["manifest"]["canonical_count"],
        "baseline_count": built["manifest"]["baseline_count"],
        "shard_count": args.shard_count,
        "shards": shards_output,
        "canonical_sha256": built["manifest"]["canonical_sha256"],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as handle:
            handle.write(f"canonical_count={built['manifest']['canonical_count']}\n")
            handle.write(f"shard_count={args.shard_count}\n")
            handle.write(f"shards=[{shards_output}]\n")
            handle.write(f"canonical_sha256={built['manifest']['canonical_sha256']}\n")
    return 0


def _cmd_aggregate(args: argparse.Namespace) -> int:
    # An authoritative dispatch must be judged by tooling that did not come from
    # the commit under test.  If the default-branch checkout was unavailable the
    # run is self-attested, which the bootstrap push path may be but a dispatch
    # may not: refuse rather than print a T4_PASS the candidate authored itself.
    if args.event_name == "workflow_dispatch" and args.trusted_tools != "true":
        raise CiError(
            "workflow_dispatch requires trusted CI tooling from the default "
            "branch; refusing to reconcile a self-attested run"
        )
    event = {
        "event_name": args.event_name,
        "ref": args.ref,
        "actor": args.actor,
        "trusted_tools": args.trusted_tools,
    }
    aggregate = load_and_reconcile(
        Path(args.manifest),
        Path(args.evidence),
        expected_sha=args.expected_sha,
        baseline_ids=_read_baseline(args.baseline),
        require_tree=not args.allow_missing_tree,
        event={key: value for key, value in event.items() if value},
        expected_allowed_skips_path=args.allowed_skips,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    _write_json(out, aggregate)
    summary = render_summary(aggregate)
    print(summary)
    if args.github_step_summary:
        with open(args.github_step_summary, "a", encoding="utf-8") as handle:
            handle.write(summary)
    return 0 if aggregate["result"] == "T4_PASS" else 1


def _cmd_env_report(args: argparse.Namespace) -> int:
    report = collect_environment(candidate_sha=args.candidate_sha, role=args.role)
    _write_json(Path(args.out), report)
    print(
        f"{args.role}: python {report['python_version'].split()[0]} "
        f"sqlite {report['sqlite_module_version']} os {report['platform']}"
    )
    return 0


def _cmd_check_sha(args: argparse.Namespace) -> int:
    repo = Path(args.repo)
    actual = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if not SHA_RE.match(args.expected):
        print(f"expected SHA is not 40-hex: {args.expected!r}", file=sys.stderr)
        return 2
    if actual != args.expected:
        print(
            f"candidate mismatch: checked-out HEAD {actual} != expected {args.expected}",
            file=sys.stderr,
        )
        return 1
    print(actual)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ci_support")
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build-shards", help="canonicalise and shard inventory")
    build.add_argument("--collect-output", required=True)
    build.add_argument("--candidate-sha", required=True)
    build.add_argument("--shard-count", type=int, required=True)
    build.add_argument("--allowed-skips", required=True)
    build.add_argument("--baseline", default="scripts/ci/t4_baseline_inventory.txt")
    build.add_argument("--out", required=True)
    build.add_argument("--github-output", default=None)
    build.set_defaults(func=_cmd_build_shards)

    aggregate = sub.add_parser("aggregate", help="reconcile shard evidence")
    aggregate.add_argument("--manifest", required=True)
    aggregate.add_argument("--evidence", required=True)
    aggregate.add_argument("--expected-sha", required=True)
    aggregate.add_argument(
        "--baseline", default="scripts/ci/t4_baseline_inventory.txt"
    )
    aggregate.add_argument("--event-name", default=None)
    aggregate.add_argument("--ref", default=None)
    aggregate.add_argument("--actor", default=None)
    aggregate.add_argument(
        "--allowed-skips",
        default=None,
        help=(
            "path to the reviewed allow-list; when given, the manifest's copy "
            "must be byte-identical to it"
        ),
    )
    aggregate.add_argument(
        "--trusted-tools",
        default=None,
        help="'true' when the verifier and baseline came from a trusted ref",
    )
    aggregate.add_argument("--allow-missing-tree", action="store_true")
    aggregate.add_argument("--out", required=True)
    aggregate.add_argument("--github-step-summary", default=None)
    aggregate.set_defaults(func=_cmd_aggregate)

    env = sub.add_parser("env-report", help="record actual runtime environment")
    env.add_argument("--out", required=True)
    env.add_argument("--candidate-sha", required=True)
    env.add_argument("--role", required=True)
    env.set_defaults(func=_cmd_env_report)

    check = sub.add_parser("check-sha", help="fail closed on candidate mismatch")
    check.add_argument("--expected", required=True)
    check.add_argument("--repo", default=".")
    check.set_defaults(func=_cmd_check_sha)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except CiError as exc:
        print(f"ci_support: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
