"""False-green falsification for the authoritative CI v1 aggregate gate.

Every test here builds real evidence files and then attacks one specific way a
partial or dishonest run could try to present as T4 PASS.  The aggregate must
recompute truth from raw evidence and fail closed in all of them.
"""

from __future__ import annotations

import json

import pytest

from scripts.ci import ci_support

SHA = "dee0b3b8446d7f84bcc4e1a5af2339c5ed78383f"
OTHER_SHA = "9762170099889ecd87d451341a15a29ce7aceae8"
ALLOWED_SKIP = "tests/test_c.py::test_six"

IDS = [
    "tests/test_a.py::test_one",
    "tests/test_a.py::test_two",
    "tests/test_b.py::test_three",
    "tests/test_b.py::test_four",
    "tests/test_b.py::test_five",
    ALLOWED_SKIP,
]


def _collect_text(ids=IDS) -> str:
    return "\n".join(ids) + f"\n\n{len(ids)} tests collected in 0.01s\n"


def _write_shard(
    directory,
    index: int,
    assigned: list[str],
    outcomes: dict[str, str],
    *,
    candidate_sha: str = SHA,
    collected: list[str] | None = None,
    exit_code: int | None = 0,
    assigned_sha256: str | None = None,
    extra: dict | None = None,
    tree: str | None = None,
) -> None:
    sid = ci_support.shard_id(index)
    body = "".join(f"{node}\n" for node in assigned)
    report = {
        "schema": 1,
        "candidate_sha": candidate_sha,
        "shard_index": index,
        "assigned_sha256": assigned_sha256
        if assigned_sha256 is not None
        else ci_support.text_sha256(body),
        "collected_ids": list(assigned if collected is None else collected),
        "outcomes": outcomes,
    }
    if extra:
        report.update(extra)
    (directory / f"{sid}.report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
    )
    if exit_code is not None:
        (directory / f"{sid}.exit").write_text(f"{exit_code}\n", encoding="utf-8")
    # A clean seal is an explicit marker, never an empty file.
    if tree is None:
        tree = ci_support.clean_tree_marker(candidate_sha, sid)
    (directory / f"{sid}.tree").write_text(tree, encoding="utf-8")


def _build_valid(
    tmp_path,
    *,
    shard_count: int = 3,
    outcomes=None,
    allowed=ALLOWED_SKIP,
    baseline_ids=(),
):
    built = ci_support.build_shards(
        collect_text=_collect_text(),
        candidate_sha=SHA,
        shard_count=shard_count,
        allowed_skips=[allowed],
        baseline_ids=baseline_ids,
    )
    ci_support.write_shards(tmp_path, built)
    per_shard: dict[int, list[str]] = {}
    for index, (name, body) in enumerate(built["shard_bodies"]):
        assigned = body.splitlines()
        per_shard[index] = assigned
        resolved = {}
        for node in assigned:
            if outcomes and node in outcomes:
                resolved[node] = outcomes[node]
            elif node == allowed:
                resolved[node] = "skipped"
            else:
                resolved[node] = "passed"
        _write_shard(tmp_path, index, assigned, resolved)
    return built, per_shard


def _report_path(directory, index: int):
    return directory / f"{ci_support.shard_id(index)}.report.json"


def _load_report(directory, index: int) -> dict:
    return json.loads(_report_path(directory, index).read_text(encoding="utf-8"))


def _save_report(directory, index: int, report: dict) -> None:
    _report_path(directory, index).write_text(
        json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# the honest baseline
# ---------------------------------------------------------------------------


def test_honest_complete_run_passes(tmp_path):
    _build_valid(tmp_path)
    aggregate = ci_support.load_and_reconcile(tmp_path)
    assert aggregate["result"] == "T4_PASS"
    assert aggregate["failures"] == []
    assert aggregate["canonical_count"] == len(IDS)
    assert aggregate["executed_count"] == len(IDS)
    assert aggregate["passed"] == len(IDS) - 1
    assert aggregate["skipped"] == 1
    assert aggregate["omitted"] == []


# ---------------------------------------------------------------------------
# attack 1: a canonical test is omitted from every shard
# ---------------------------------------------------------------------------


def test_omitted_test_fails_closed(tmp_path):
    _build_valid(tmp_path)
    report = _load_report(tmp_path, 0)
    victim = report["collected_ids"][0]
    report["collected_ids"] = [n for n in report["collected_ids"] if n != victim]
    report["outcomes"].pop(victim, None)
    _save_report(tmp_path, 0, report)
    aggregate = ci_support.load_and_reconcile(tmp_path)
    assert aggregate["result"] == "T4_FAIL"
    assert victim in aggregate["omitted"]


# ---------------------------------------------------------------------------
# attack 2: a test is executed by two shards
# ---------------------------------------------------------------------------


def test_duplicated_execution_fails_closed(tmp_path):
    _build_valid(tmp_path)
    report0 = _load_report(tmp_path, 0)
    report1 = _load_report(tmp_path, 1)
    duplicated = report0["collected_ids"][0]
    report1["collected_ids"].append(duplicated)
    report1["outcomes"][duplicated] = "passed"
    _save_report(tmp_path, 1, report1)
    aggregate = ci_support.load_and_reconcile(tmp_path)
    assert aggregate["result"] == "T4_FAIL"
    assert duplicated in aggregate["duplicated"]
    assert any("more than one shard" in failure for failure in aggregate["failures"])


def test_overlapping_assignments_fail_closed():
    inventory = ci_support.validate_inventory(IDS)
    first = inventory[:3]
    second = inventory[2:]  # overlaps inventory[2]
    evidence = []
    for index, assigned in enumerate((first, second)):
        evidence.append(
            ci_support.ShardEvidence(
                index=index,
                assigned_ids=list(assigned),
                report={"candidate_sha": SHA, "shard_index": index},
                exit_code=0,
                report_path=None,
                exit_path=None,
                collected_ids=list(assigned),
                outcomes={node: "passed" for node in assigned},
            )
        )
    aggregate = ci_support.reconcile(
        candidate_sha=SHA,
        shard_count=2,
        canonical_ids=inventory,
        assignments=[first, second],
        evidences=evidence,
        allowed_skips=[],
    )
    assert aggregate["result"] == "T4_FAIL"
    assert any("more than one shard" in failure for failure in aggregate["failures"])


# ---------------------------------------------------------------------------
# attack 3: a shard reports a failure
# ---------------------------------------------------------------------------


def test_shard_failure_fails_closed(tmp_path):
    _build_valid(tmp_path, outcomes={IDS[0]: "failed"})
    aggregate = ci_support.load_and_reconcile(tmp_path)
    assert aggregate["result"] == "T4_FAIL"
    assert aggregate["failed"] == 1
    assert any("failed:" in failure for failure in aggregate["failures"])


def test_nonzero_shard_exit_fails_closed(tmp_path):
    _build_valid(tmp_path)
    (tmp_path / f"{ci_support.shard_id(0)}.exit").write_text("1\n", encoding="utf-8")
    aggregate = ci_support.load_and_reconcile(tmp_path)
    assert aggregate["result"] == "T4_FAIL"
    assert any("exit code 1" in failure for failure in aggregate["failures"])


# ---------------------------------------------------------------------------
# attack 4/8: a shard produces no evidence (crash or cancellation)
# ---------------------------------------------------------------------------


def test_missing_shard_report_fails_closed(tmp_path):
    _build_valid(tmp_path)
    _report_path(tmp_path, 1).unlink()
    aggregate = ci_support.load_and_reconcile(tmp_path)
    assert aggregate["result"] == "T4_FAIL"
    assert any("missing shard report" in failure for failure in aggregate["failures"])


def test_missing_shard_exit_fails_closed(tmp_path):
    _build_valid(tmp_path)
    (tmp_path / f"{ci_support.shard_id(1)}.exit").unlink()
    aggregate = ci_support.load_and_reconcile(tmp_path)
    assert aggregate["result"] == "T4_FAIL"
    assert any("missing shard exit" in failure for failure in aggregate["failures"])


# ---------------------------------------------------------------------------
# attack 5: canonical collection fails or is absent
# ---------------------------------------------------------------------------


def test_absent_manifest_is_a_gate_failure(tmp_path):
    with pytest.raises(ci_support.CiError, match="no canonical.json"):
        ci_support.load_and_reconcile(tmp_path)


def test_tampered_manifest_count_is_a_gate_failure(tmp_path):
    _build_valid(tmp_path)
    manifest_path = tmp_path / "shards.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["canonical_count"] = manifest["canonical_count"] + 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ci_support.CiError, match="canonical count"):
        ci_support.load_and_reconcile(tmp_path)


def test_tampered_manifest_hash_is_a_gate_failure(tmp_path):
    _build_valid(tmp_path)
    manifest_path = tmp_path / "shards.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["canonical_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ci_support.CiError, match="canonical hash"):
        ci_support.load_and_reconcile(tmp_path)


def test_tampered_canonical_count_is_a_gate_failure(tmp_path):
    _build_valid(tmp_path)
    canonical_path = tmp_path / "canonical.json"
    record = json.loads(canonical_path.read_text(encoding="utf-8"))
    record["count"] = record["count"] + 1
    canonical_path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ci_support.CiError, match="count disagrees with canonical.txt"):
        ci_support.load_and_reconcile(tmp_path)


def test_empty_canonical_collection_is_rejected():
    with pytest.raises(ci_support.CiError):
        ci_support.build_shards(
            collect_text="\n0 tests collected\n",
            candidate_sha=SHA,
            shard_count=2,
            allowed_skips=[],
        )


# ---------------------------------------------------------------------------
# attack 6/7: wrong or mixed candidate SHA
# ---------------------------------------------------------------------------


def test_wrong_shard_sha_fails_closed(tmp_path):
    _build_valid(tmp_path)
    report = _load_report(tmp_path, 2)
    report["candidate_sha"] = OTHER_SHA
    _save_report(tmp_path, 2, report)
    aggregate = ci_support.load_and_reconcile(tmp_path)
    assert aggregate["result"] == "T4_FAIL"
    assert any(OTHER_SHA in failure for failure in aggregate["failures"])


def test_mixed_shard_shas_fail_closed(tmp_path):
    _build_valid(tmp_path)
    for index in (1, 2):
        report = _load_report(tmp_path, index)
        report["candidate_sha"] = OTHER_SHA
        _save_report(tmp_path, index, report)
    aggregate = ci_support.load_and_reconcile(tmp_path)
    assert aggregate["result"] == "T4_FAIL"
    assert sum("candidate" in failure for failure in aggregate["failures"]) == 2


def test_wrong_assignment_hash_fails_closed(tmp_path):
    _build_valid(tmp_path)
    report = _load_report(tmp_path, 0)
    report["assigned_sha256"] = "0" * 64
    _save_report(tmp_path, 0, report)
    aggregate = ci_support.load_and_reconcile(tmp_path)
    assert aggregate["result"] == "T4_FAIL"
    assert any("different assignment" in failure for failure in aggregate["failures"])


# ---------------------------------------------------------------------------
# attack 9: a manipulated claim of success with no matching evidence
# ---------------------------------------------------------------------------


def test_claimed_success_without_evidence_fails_closed(tmp_path):
    _build_valid(tmp_path)
    report = _load_report(tmp_path, 0)
    report["result"] = "T4_PASS"
    report["status"] = "green"
    report["collected_ids"] = []
    report["outcomes"] = {}
    _save_report(tmp_path, 0, report)
    aggregate = ci_support.load_and_reconcile(tmp_path)
    assert aggregate["result"] == "T4_FAIL"
    assert aggregate["result"] != report["result"]


# ---------------------------------------------------------------------------
# attack 10: an empty or partial run cannot present as T4 PASS
# ---------------------------------------------------------------------------


def test_partial_outcomes_fail_closed(tmp_path):
    _build_valid(tmp_path)
    report = _load_report(tmp_path, 0)
    report["outcomes"] = dict(list(report["outcomes"].items())[:-1])
    _save_report(tmp_path, 0, report)
    aggregate = ci_support.load_and_reconcile(tmp_path)
    assert aggregate["result"] == "T4_FAIL"
    assert aggregate["omitted"]


def test_unexpected_skip_fails_closed(tmp_path):
    _build_valid(tmp_path, outcomes={IDS[0]: "skipped"})
    aggregate = ci_support.load_and_reconcile(tmp_path)
    assert aggregate["result"] == "T4_FAIL"
    assert IDS[0] in aggregate["unexpected_skips"]


# ---------------------------------------------------------------------------
# evidence discovery tolerance
# ---------------------------------------------------------------------------


def test_nested_evidence_directory_is_discovered(tmp_path):
    nested = tmp_path / "t4-manifest" / "artifacts"
    nested.mkdir(parents=True)
    _build_valid(nested)
    aggregate = ci_support.load_and_reconcile(tmp_path)
    assert aggregate["result"] == "T4_PASS"


def test_render_summary_mentions_result_and_failures(tmp_path):
    _build_valid(tmp_path, outcomes={IDS[0]: "failed"})
    aggregate = ci_support.load_and_reconcile(tmp_path)
    summary = ci_support.render_summary(aggregate)
    assert "T4_FAIL" in summary
    assert "## T4 aggregate" in summary


# ---------------------------------------------------------------------------
# attack 11: the aggregate must use an external expected SHA, not the evidence
# ---------------------------------------------------------------------------


def test_foreign_expected_sha_fails_closed(tmp_path):
    _build_valid(tmp_path)
    aggregate = ci_support.load_and_reconcile(tmp_path, expected_sha=OTHER_SHA)
    assert aggregate["result"] == "T4_FAIL"
    assert any("expected SHA" in failure for failure in aggregate["failures"])


def test_matching_expected_sha_passes(tmp_path):
    _build_valid(tmp_path)
    aggregate = ci_support.load_and_reconcile(tmp_path, expected_sha=SHA)
    assert aggregate["result"] == "T4_PASS"


# ---------------------------------------------------------------------------
# attack 12: shard evidence must not be able to overwrite the trusted manifest
# ---------------------------------------------------------------------------


def _copy_shard_evidence(source, target, shard_count: int) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for index in range(shard_count):
        for suffix in (".report.json", ".exit", ".tree"):
            src = source / f"{ci_support.shard_id(index)}{suffix}"
            (target / src.name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")


def test_shard_evidence_cannot_overwrite_the_manifest(tmp_path):
    manifest = tmp_path / "manifest"
    shards = tmp_path / "shards"
    manifest.mkdir()
    built, _ = _build_valid(manifest)
    _copy_shard_evidence(manifest, shards, 3)

    # A malicious shard ships a self-consistent, shrunken manifest in its own
    # artefact directory.  The trusted manifest must still win.
    forged = dict(built["canonical_record"])
    forged["count"] = 1
    (shards / "canonical.json").write_text(json.dumps(forged), encoding="utf-8")
    (shards / "canonical.txt").write_text(f"{IDS[0]}\n", encoding="utf-8")

    aggregate = ci_support.load_and_reconcile(manifest, shards)
    assert aggregate["result"] == "T4_PASS"
    assert aggregate["canonical_count"] == len(IDS)


def test_evidence_dir_cannot_widen_the_skip_allow_list(tmp_path):
    manifest = tmp_path / "manifest"
    shards = tmp_path / "shards"
    manifest.mkdir()
    _build_valid(manifest)
    _copy_shard_evidence(manifest, shards, 3)

    report = _load_report(shards, 0)
    report["outcomes"][IDS[0]] = "skipped"
    _save_report(shards, 0, report)
    (shards / "allowed-skips.txt").write_text(
        f"{ALLOWED_SKIP}\n{IDS[0]}\n", encoding="utf-8"
    )

    aggregate = ci_support.load_and_reconcile(manifest, shards)
    assert aggregate["result"] == "T4_FAIL"
    assert IDS[0] in aggregate["unexpected_skips"]


def test_missing_manifest_allow_list_is_a_gate_failure(tmp_path):
    _build_valid(tmp_path)
    (tmp_path / "allowed-skips.txt").unlink()
    with pytest.raises(ci_support.CiError, match="trusted manifest has no allowed-skips"):
        ci_support.load_and_reconcile(tmp_path)


# ---------------------------------------------------------------------------
# attack 13: the reviewed baseline population must not silently shrink
# ---------------------------------------------------------------------------


def test_baseline_subset_passes(tmp_path):
    # A subset baseline is fine, but it must cover every declared skip: the skip
    # policy is part of the reviewed standard, not a free-floating list.
    subset = [IDS[1], IDS[2], IDS[3], ALLOWED_SKIP]
    _build_valid(tmp_path, baseline_ids=subset)
    aggregate = ci_support.load_and_reconcile(tmp_path, baseline_ids=subset)
    assert aggregate["result"] == "T4_PASS"
    assert aggregate["baseline_count"] == 4


def test_baseline_inventory_must_match_the_manifest(tmp_path):
    _build_valid(tmp_path, baseline_ids=IDS)
    with pytest.raises(ci_support.CiError, match="reviewed baseline inventory"):
        ci_support.load_and_reconcile(tmp_path, baseline_ids=IDS[:3])


def test_reconcile_reports_baseline_regression():
    inventory = ci_support.validate_inventory(IDS)
    assignments = [inventory]
    evidence = [
        ci_support.ShardEvidence(
            index=0,
            assigned_ids=list(inventory),
            report={"candidate_sha": SHA, "shard_index": 0},
            exit_code=0,
            report_path=None,
            exit_path=None,
            collected_ids=list(inventory),
            outcomes={node: "passed" for node in inventory},
        )
    ]
    aggregate = ci_support.reconcile(
        candidate_sha=SHA,
        shard_count=1,
        canonical_ids=inventory,
        assignments=assignments,
        evidences=evidence,
        allowed_skips=[],
        baseline_ids=inventory + ["tests/test_a.py::test_gone"],
    )
    assert aggregate["result"] == "T4_FAIL"
    assert any(
        "baseline tests are absent" in failure for failure in aggregate["failures"]
    )


# ---------------------------------------------------------------------------
# attack 14: the tested tree must not have been mutated by the tests
# ---------------------------------------------------------------------------


def test_dirty_tree_fails_closed(tmp_path):
    _build_valid(tmp_path)
    (tmp_path / "shard-00.tree").write_text(" M src/bots5/x.py\n", encoding="utf-8")
    aggregate = ci_support.load_and_reconcile(tmp_path)
    assert aggregate["result"] == "T4_FAIL"
    assert any("modified tracked" in failure for failure in aggregate["failures"])


def test_missing_tree_record_fails_closed(tmp_path):
    _build_valid(tmp_path)
    (tmp_path / "shard-01.tree").unlink()
    aggregate = ci_support.load_and_reconcile(tmp_path)
    assert aggregate["result"] == "T4_FAIL"
    assert any("missing tree-integrity" in failure for failure in aggregate["failures"])


# ---------------------------------------------------------------------------
# structural hardening
# ---------------------------------------------------------------------------


def test_missing_shard_assignment_is_a_gate_failure(tmp_path):
    _build_valid(tmp_path)
    (tmp_path / "shard-01.txt").unlink()
    with pytest.raises(ci_support.CiError, match="missing evidence file"):
        ci_support.load_and_reconcile(tmp_path)


def test_boolean_shard_count_is_rejected(tmp_path):
    _build_valid(tmp_path)
    record = json.loads((tmp_path / "canonical.json").read_text(encoding="utf-8"))
    record["shard_count"] = True
    (tmp_path / "canonical.json").write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ci_support.CiError, match="invalid shard_count"):
        ci_support.load_and_reconcile(tmp_path)


def test_manifest_shard_index_is_cross_checked(tmp_path):
    _build_valid(tmp_path)
    manifest = json.loads((tmp_path / "shards.json").read_text(encoding="utf-8"))
    manifest["shards"][1]["index"] = 9
    (tmp_path / "shards.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ci_support.CiError, match="wrong index"):
        ci_support.load_and_reconcile(tmp_path)


def test_event_metadata_is_recorded(tmp_path):
    _build_valid(tmp_path)
    aggregate = ci_support.load_and_reconcile(
        tmp_path,
        expected_sha=SHA,
        event={"event_name": "workflow_dispatch", "ref": "refs/heads/main"},
    )
    assert aggregate["event"]["event_name"] == "workflow_dispatch"
    assert "workflow_dispatch" in ci_support.render_summary(aggregate)


# ---------------------------------------------------------------------------
# attack 15: the population floor and skip policy must not be self-defeating
# ---------------------------------------------------------------------------


def _single_shard_evidence(inventory, outcomes):
    return [
        ci_support.ShardEvidence(
            index=0,
            assigned_ids=list(inventory),
            report={"candidate_sha": SHA, "shard_index": 0},
            exit_code=0,
            report_path=None,
            exit_path=None,
            collected_ids=list(inventory),
            outcomes=dict(outcomes),
        )
    ]


def test_assignment_omitting_a_canonical_test_fails_closed():
    inventory = ci_support.validate_inventory(IDS)
    victim = IDS[4]
    assigned = [node for node in inventory if node != victim]
    aggregate = ci_support.reconcile(
        candidate_sha=SHA,
        shard_count=1,
        canonical_ids=inventory,
        assignments=[assigned],
        evidences=_single_shard_evidence(
            assigned, {node: "passed" for node in assigned}
        ),
        allowed_skips=[],
    )
    assert aggregate["result"] == "T4_FAIL"
    assert victim in aggregate["omitted"]
    assert any("assigned to no shard" in failure for failure in aggregate["failures"])


def test_mass_skip_even_if_allow_listed_fails_closed():
    inventory = ci_support.validate_inventory(IDS)
    aggregate = ci_support.reconcile(
        candidate_sha=SHA,
        shard_count=1,
        canonical_ids=inventory,
        assignments=[inventory],
        evidences=_single_shard_evidence(
            inventory, {node: "skipped" for node in inventory}
        ),
        allowed_skips=list(inventory),
    )
    assert aggregate["result"] == "T4_FAIL"
    assert aggregate["passed"] == 0
    assert any("no test passed" in failure for failure in aggregate["failures"])


def test_read_baseline_missing_file_raises():
    with pytest.raises(ci_support.CiError, match="not found"):
        ci_support._read_baseline("definitely/not/a/real/baseline.txt")


def test_read_baseline_empty_file_raises(tmp_path):
    baseline = tmp_path / "baseline.txt"
    baseline.write_text("# only a comment\n\n", encoding="utf-8")
    with pytest.raises(ci_support.CiError, match="empty"):
        ci_support._read_baseline(str(baseline))


def test_aggregate_cli_requires_expected_sha(tmp_path):
    with pytest.raises(SystemExit):
        ci_support.main(
            [
                "aggregate",
                "--manifest",
                str(tmp_path),
                "--evidence",
                str(tmp_path),
                "--out",
                str(tmp_path / "aggregate.json"),
            ]
        )


# ---------------------------------------------------------------------------
# round 3: the skip policy is part of the standard, and the seal is content
# ---------------------------------------------------------------------------


def test_allow_list_outside_the_reviewed_baseline_fails_closed():
    inventory = ci_support.validate_inventory(IDS)
    aggregate = ci_support.reconcile(
        candidate_sha=SHA,
        shard_count=1,
        canonical_ids=inventory,
        assignments=[inventory],
        evidences=_single_shard_evidence(
            inventory, {node: "passed" for node in inventory}
        ),
        allowed_skips=[IDS[0]],
        baseline_ids=[node for node in inventory if node != IDS[0]],
    )
    assert aggregate["result"] == "T4_FAIL"
    assert any(
        "outside the reviewed population" in failure
        for failure in aggregate["failures"]
    )


def test_build_shards_rejects_allow_list_outside_the_baseline():
    with pytest.raises(ci_support.CiError, match="widening the skip policy"):
        ci_support.build_shards(
            collect_text=_collect_text(),
            candidate_sha=SHA,
            shard_count=1,
            allowed_skips=[IDS[0]],
            baseline_ids=[node for node in IDS if node != IDS[0]],
        )


def test_unknown_outcome_is_not_a_pass():
    inventory = ci_support.validate_inventory(IDS)
    outcomes = {node: "passed" for node in inventory}
    outcomes[IDS[0]] = "banana"
    aggregate = ci_support.reconcile(
        candidate_sha=SHA,
        shard_count=1,
        canonical_ids=inventory,
        assignments=[inventory],
        evidences=_single_shard_evidence(inventory, outcomes),
        allowed_skips=[],
    )
    assert aggregate["result"] == "T4_FAIL"
    assert aggregate["passed"] == len(inventory) - 1


def test_report_shard_index_mismatch_fails(tmp_path):
    _build_valid(tmp_path, shard_count=1)
    report = _load_report(tmp_path, 0)
    report["shard_index"] = 7
    _save_report(tmp_path, 0, report)
    aggregate = ci_support.load_and_reconcile(tmp_path, expected_sha=SHA)
    assert aggregate["result"] == "T4_FAIL"
    assert any("index" in failure for failure in aggregate["failures"])


def test_canonical_txt_hash_mismatch_fails(tmp_path):
    _build_valid(tmp_path, shard_count=1)
    document = json.loads(
        (tmp_path / "canonical.json").read_text(encoding="utf-8")
    )
    document["canonical_sha256"] = "0" * 64
    (tmp_path / "canonical.json").write_text(
        json.dumps(document, indent=2, sort_keys=True), encoding="utf-8"
    )
    with pytest.raises(ci_support.CiError, match="canonical.txt does not match"):
        ci_support.load_and_reconcile(tmp_path, expected_sha=SHA)


def test_authoritative_dispatch_requires_trusted_tools(tmp_path):
    # A matching baseline is supplied so that the only possible cause of failure
    # is the dispatch gate itself.
    baseline = tmp_path / "baseline.txt"
    baseline.write_text("".join(f"{node}\n" for node in IDS), encoding="utf-8")
    _build_valid(tmp_path, shard_count=1, baseline_ids=IDS)
    code = ci_support.main(
        [
            "aggregate",
            "--manifest",
            str(tmp_path),
            "--evidence",
            str(tmp_path),
            "--expected-sha",
            SHA,
            "--baseline",
            str(baseline),
            "--event-name",
            "workflow_dispatch",
            "--trusted-tools",
            "false",
            "--out",
            str(tmp_path / "aggregate.json"),
        ]
    )
    assert code != 0
    assert not (tmp_path / "aggregate.json").exists()


def test_self_attested_push_is_still_reconciled(tmp_path):
    baseline = tmp_path / "baseline.txt"
    baseline.write_text("".join(f"{node}\n" for node in IDS), encoding="utf-8")
    _build_valid(tmp_path, shard_count=1, baseline_ids=IDS)
    code = ci_support.main(
        [
            "aggregate",
            "--manifest",
            str(tmp_path),
            "--evidence",
            str(tmp_path),
            "--expected-sha",
            SHA,
            "--baseline",
            str(baseline),
            "--event-name",
            "push",
            "--trusted-tools",
            "false",
            "--out",
            str(tmp_path / "aggregate.json"),
        ]
    )
    assert code == 0
    document = json.loads(
        (tmp_path / "aggregate.json").read_text(encoding="utf-8")
    )
    assert document["result"] == "T4_PASS"
    assert document["event"]["trusted_tools"] == "false"


# ---------------------------------------------------------------------------
# round 4: the allow-list must be bound to the reviewed policy, not merely to
# the canonical inventory (which can equal the baseline, making a subset check
# vacuous)
# ---------------------------------------------------------------------------


def test_manifest_allow_list_hash_is_enforced(tmp_path):
    _build_valid(tmp_path, shard_count=1)
    widened = (tmp_path / "allowed-skips.txt").read_text(encoding="utf-8")
    (tmp_path / "allowed-skips.txt").write_text(
        widened + "".join(f"{node}\n" for node in IDS if node != ALLOWED_SKIP),
        encoding="utf-8",
    )
    with pytest.raises(ci_support.CiError, match="does not match the hash"):
        ci_support.load_and_reconcile(tmp_path, expected_sha=SHA)


def test_reviewed_allow_list_must_match_the_manifest(tmp_path):
    _build_valid(tmp_path, shard_count=1)
    reviewed = tmp_path / "reviewed-skips.txt"
    reviewed.write_text(
        "".join(f"{node}\n" for node in [ALLOWED_SKIP, IDS[1]]), encoding="utf-8"
    )
    with pytest.raises(ci_support.CiError, match="reviewed allow-list"):
        ci_support.load_and_reconcile(
            tmp_path, expected_sha=SHA, expected_allowed_skips_path=reviewed
        )


def test_reviewed_allow_list_matching_the_manifest_passes(tmp_path):
    # The real reviewed file carries a comment header; the manifest copy is
    # stored canonically.  The comparison must be over the parsed set, not over
    # raw bytes, or every honest landed run would fail closed.
    _build_valid(tmp_path, shard_count=1)
    reviewed = tmp_path / "reviewed-skips.txt"
    reviewed.write_text(
        "# reviewed skip policy\n\n" + ALLOWED_SKIP + "\n", encoding="utf-8"
    )
    aggregate = ci_support.load_and_reconcile(
        tmp_path, expected_sha=SHA, expected_allowed_skips_path=reviewed
    )
    assert aggregate["result"] == "T4_PASS"


def test_widened_manifest_allow_list_is_caught_by_the_reviewed_file(tmp_path):
    """A self-consistent manifest rewrite must still lose to the reviewed file."""
    _build_valid(tmp_path, shard_count=1, baseline_ids=[ALLOWED_SKIP])
    document = json.loads(
        (tmp_path / "canonical.json").read_text(encoding="utf-8")
    )
    shard = json.loads((tmp_path / "shards.json").read_text(encoding="utf-8"))
    widened = sorted(set([ALLOWED_SKIP, IDS[0], IDS[1]]))
    body = "".join(f"{node}\n" for node in widened)
    (tmp_path / "allowed-skips.txt").write_text(body, encoding="utf-8")
    document["allowed_skips"] = widened
    document["allowed_skips_sha256"] = ci_support.text_sha256(body)
    (tmp_path / "canonical.json").write_text(
        json.dumps(document, indent=2, sort_keys=True), encoding="utf-8"
    )
    report = _load_report(tmp_path, 0)
    for node in widened:
        report["outcomes"][node] = "skipped"
    _save_report(tmp_path, 0, report)
    (tmp_path / "shards.json").write_text(
        json.dumps(shard, indent=2, sort_keys=True), encoding="utf-8"
    )
    # Without the external policy the internally consistent rewrite is
    # indistinguishable from an honest manifest ...
    aggregate = ci_support.load_and_reconcile(
        tmp_path, expected_sha=SHA, baseline_ids=[ALLOWED_SKIP]
    )
    assert aggregate["result"] == "T4_FAIL"  # allow-list outside the baseline floor
    # ... and with it, the binding is enforced.
    reviewed = tmp_path / "reviewed-skips.txt"
    reviewed.write_text(ALLOWED_SKIP + "\n", encoding="utf-8")
    with pytest.raises(ci_support.CiError, match="reviewed allow-list"):
        ci_support.load_and_reconcile(
            tmp_path, expected_sha=SHA, expected_allowed_skips_path=reviewed
        )


def test_missing_allowed_skips_hash_key_is_rejected(tmp_path):
    _build_valid(tmp_path, shard_count=1)
    document = json.loads(
        (tmp_path / "canonical.json").read_text(encoding="utf-8")
    )
    del document["allowed_skips_sha256"]
    (tmp_path / "canonical.json").write_text(
        json.dumps(document, indent=2, sort_keys=True), encoding="utf-8"
    )
    with pytest.raises(ci_support.CiError, match="allowed_skips_sha256"):
        ci_support.load_and_reconcile(tmp_path, expected_sha=SHA)


def test_missing_reviewed_allow_list_fails(tmp_path):
    _build_valid(tmp_path, shard_count=1)
    with pytest.raises(ci_support.CiError, match="reviewed allow-list file not found"):
        ci_support.load_and_reconcile(
            tmp_path,
            expected_sha=SHA,
            expected_allowed_skips_path=tmp_path / "absent.txt",
        )


def test_error_outcome_is_reported_not_crashed():
    """An `error` outcome must produce a published T4_FAIL, not a traceback.

    The aggregate job is `if: always()` and promises to publish evidence even
    for a broken run, so every outcome the plugin can emit must be countable.
    """
    inventory = ci_support.validate_inventory(IDS)
    outcomes = {node: "passed" for node in inventory}
    outcomes[IDS[0]] = "error"
    aggregate = ci_support.reconcile(
        candidate_sha=SHA,
        shard_count=1,
        canonical_ids=inventory,
        assignments=[inventory],
        evidences=_single_shard_evidence(inventory, outcomes),
        allowed_skips=[],
    )
    assert aggregate["result"] == "T4_FAIL"
    assert aggregate["shards"][0]["error"] == 1
    assert any("error" in failure for failure in aggregate["failures"])


def test_planted_empty_seal_is_not_a_clean_run(tmp_path):
    """The evidence dir is candidate-writable, so an empty file must not mean clean."""
    _build_valid(tmp_path, shard_count=1)
    (tmp_path / "shard-00.tree").write_text("", encoding="utf-8")
    aggregate = ci_support.load_and_reconcile(tmp_path, expected_sha=SHA)
    assert aggregate["result"] == "T4_FAIL"
    assert any("empty tree-integrity record" in f for f in aggregate["failures"])


def test_planted_whitespace_seal_is_not_a_clean_run(tmp_path):
    _build_valid(tmp_path, shard_count=1)
    (tmp_path / "shard-00.tree").write_text("  \n\n", encoding="utf-8")
    aggregate = ci_support.load_and_reconcile(tmp_path, expected_sha=SHA)
    assert aggregate["result"] == "T4_FAIL"


def test_seal_for_another_commit_is_not_clean(tmp_path):
    _build_valid(tmp_path, shard_count=1)
    (tmp_path / "shard-00.tree").write_text(
        ci_support.clean_tree_marker(OTHER_SHA, "shard-00"), encoding="utf-8"
    )
    aggregate = ci_support.load_and_reconcile(tmp_path, expected_sha=SHA)
    assert aggregate["result"] == "T4_FAIL"
