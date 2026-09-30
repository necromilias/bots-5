"""Focused tests for the deterministic CI v1 inventory/sharding support logic.

These tests themselves are part of the authoritative population, so they pin the
properties the CI gate depends on: deterministic assignment, exact coverage,
rejection of malformed input, and no dependence on Python hash randomisation.
"""

from __future__ import annotations

import json

import pytest

from scripts.ci import ci_support

SHA = "dee0b3b8446d7f84bcc4e1a5af2339c5ed78383f"
OTHER_SHA = "9762170099889ecd87d451341a15a29ce7aceae8"

# A deliberately awkward parameter ID set: parameters may contain spaces,
# backslashes, brackets and NUL escapes.  They must survive as whole strings.
AWKWARD_IDS = [
    r"tests/test_a.py::test_plain",
    r"tests/test_a.py::test_param[ordinary binary \xff-invalid_utf8]",
    r"tests/test_a.py::test_param[ordinary text with NUL\x00-contains_nul]",
    r"tests/test_a.py::test_param[with space and , comma:colon]",
    r"tests/test_b.py::test_nested[stage]",
    r"tests/test_b.py::test_nested[unstage]",
    r"tests/test_c.py::test_last",
]

COLLECT_TEXT = (
    "\n".join(AWKWARD_IDS)
    + "\n\n"
    + f"{len(AWKWARD_IDS)} tests collected in 0.12s\n"
)


def _collect_text(ids: list[str]) -> str:
    return "\n".join(ids) + f"\n\n{len(ids)} tests collected in 0.01s\n"


# ---------------------------------------------------------------------------
# parsing and validation
# ---------------------------------------------------------------------------


def test_parse_collection_output_extracts_only_node_ids():
    parsed = ci_support.parse_collection_output(COLLECT_TEXT)
    assert parsed == AWKWARD_IDS


def test_parse_collection_output_ignores_summary_and_warnings():
    text = (
        "tests/test_a.py::test_one\n"
        "warning: something :: odd\n"
        "\n"
        "1 test collected in 0.01s\n"
    )
    assert ci_support.parse_collection_output(text) == ["tests/test_a.py::test_one"]


def test_validate_inventory_rejects_empty_collection():
    with pytest.raises(ci_support.CiError, match="zero test node IDs"):
        ci_support.validate_inventory([])


def test_validate_inventory_rejects_duplicates():
    with pytest.raises(ci_support.CiError, match="duplicate"):
        ci_support.validate_inventory(["tests/a.py::x", "tests/a.py::x"])


def test_validate_inventory_rejects_malformed_ids():
    with pytest.raises(ci_support.CiError, match="malformed"):
        ci_support.validate_inventory(["tests/a.py::ok", "not a node id"])


def test_validate_inventory_preserves_collection_order():
    result = ci_support.validate_inventory(["tests/b.py::z", "tests/a.py::y"])
    assert result == ["tests/b.py::z", "tests/a.py::y"]


# ---------------------------------------------------------------------------
# shard assignment
# ---------------------------------------------------------------------------


def test_assign_shards_is_deterministic_and_complete():
    inventory = ci_support.validate_inventory(AWKWARD_IDS)
    first = ci_support.assign_shards(inventory, 3)
    second = ci_support.assign_shards(inventory, 3)
    assert first == second
    flattened = [node for shard in first for node in shard]
    assert sorted(flattened) == sorted(inventory)
    assert len(flattened) == len(set(flattened))


def test_assign_shards_single_shard_reproduces_serial_order():
    inventory = ci_support.validate_inventory(AWKWARD_IDS)
    assert ci_support.assign_shards(inventory, 1) == [inventory]


def test_assign_shards_spreads_each_file_across_shards():
    # Timing balance depends on round-robin interleaving, not contiguous chunks.
    inventory = ci_support.validate_inventory(AWKWARD_IDS)
    shards = ci_support.assign_shards(inventory, 3)
    assert shards[0] == [AWKWARD_IDS[0], AWKWARD_IDS[3], AWKWARD_IDS[6]]


def test_assign_shards_is_balanced_and_disjoint():
    inventory = ci_support.validate_inventory(AWKWARD_IDS)
    shards = ci_support.assign_shards(inventory, 3)
    sizes = sorted(len(shard) for shard in shards)
    assert sizes[-1] - sizes[0] <= 1
    union: set[str] = set()
    for shard in shards:
        assert not (union & set(shard))
        union |= set(shard)
    assert union == set(inventory)


@pytest.mark.parametrize("count", [0, -1, 17, True])
def test_assign_shards_rejects_invalid_counts(count):
    inventory = ci_support.validate_inventory(AWKWARD_IDS)
    with pytest.raises(ci_support.CiError):
        ci_support.assign_shards(inventory, count)


def test_assign_shards_refuses_empty_shards():
    inventory = ci_support.validate_inventory(AWKWARD_IDS)
    with pytest.raises(ci_support.CiError, match="empty shards"):
        ci_support.assign_shards(inventory, len(inventory) + 1)


# ---------------------------------------------------------------------------
# build-shards / manifest
# ---------------------------------------------------------------------------


def test_build_shards_is_deterministic_for_identical_collection_output():
    first = ci_support.build_shards(
        collect_text=_collect_text(AWKWARD_IDS),
        candidate_sha=SHA,
        shard_count=3,
        allowed_skips=[],
    )
    second = ci_support.build_shards(
        collect_text=_collect_text(list(AWKWARD_IDS)),
        candidate_sha=SHA,
        shard_count=3,
        allowed_skips=[],
    )
    assert first["canonical_record"] == second["canonical_record"]
    assert first["shard_bodies"] == second["shard_bodies"]


def test_build_shards_permutation_preserves_coverage_not_order():
    # A different collection order is a different canonical inventory (order is
    # part of the contract), but no test may be lost or duplicated.
    reversed_ids = list(reversed(AWKWARD_IDS))
    backward = ci_support.build_shards(
        collect_text=_collect_text(reversed_ids),
        candidate_sha=SHA,
        shard_count=3,
        allowed_skips=[],
    )
    assigned = [
        line
        for _name, body in backward["shard_bodies"]
        for line in body.splitlines()
        if line
    ]
    assert sorted(assigned) == sorted(AWKWARD_IDS)


def test_build_shards_accepts_and_records_reviewed_baseline():
    built = ci_support.build_shards(
        collect_text=_collect_text(AWKWARD_IDS),
        candidate_sha=SHA,
        shard_count=3,
        allowed_skips=[],
        baseline_ids=AWKWARD_IDS[:4],
    )
    record = built["canonical_record"]
    assert record["baseline_count"] == 4
    assert record["baseline_sha256"] == ci_support.text_sha256(
        "".join(f"{node}\n" for node in AWKWARD_IDS[:4])
    )


def test_build_shards_rejects_baseline_regression():
    with pytest.raises(ci_support.CiError, match="baseline tests are absent"):
        ci_support.build_shards(
            collect_text=_collect_text(AWKWARD_IDS),
            candidate_sha=SHA,
            shard_count=3,
            allowed_skips=[],
            baseline_ids=AWKWARD_IDS[:4] + ["tests/test_a.py::test_deleted"],
        )


def test_build_shards_assigns_every_test_exactly_once(tmp_path):
    built = ci_support.build_shards(
        collect_text=_collect_text(AWKWARD_IDS),
        candidate_sha=SHA,
        shard_count=3,
        allowed_skips=[],
    )
    ci_support.write_shards(tmp_path, built)
    assigned = []
    for name, _body in built["shard_bodies"]:
        assigned.extend(
            (tmp_path / f"{name}.txt").read_text(encoding="utf-8").splitlines()
        )
    assert sorted(assigned) == sorted(AWKWARD_IDS)
    assert len(assigned) == len(set(assigned))


def test_build_shards_manifest_hashes_match_written_files(tmp_path):
    built = ci_support.build_shards(
        collect_text=_collect_text(AWKWARD_IDS),
        candidate_sha=SHA,
        shard_count=3,
        allowed_skips=[],
    )
    ci_support.write_shards(tmp_path, built)
    manifest = json.loads((tmp_path / "shards.json").read_text(encoding="utf-8"))
    for record in manifest["shards"]:
        body = (tmp_path / f"{record['id']}.txt").read_text(encoding="utf-8")
        assert ci_support.text_sha256(body) == record["sha256"]
    canonical = (tmp_path / "canonical.txt").read_text(encoding="utf-8")
    assert ci_support.text_sha256(canonical) == manifest["canonical_sha256"]


def test_build_shards_rejects_stale_allowed_skip():
    with pytest.raises(ci_support.CiError, match="stale CI policy"):
        ci_support.build_shards(
            collect_text=COLLECT_TEXT,
            candidate_sha=SHA,
            shard_count=2,
            allowed_skips=["tests/test_a.py::test_removed"],
        )


def test_build_shards_rejects_bad_candidate_sha():
    with pytest.raises(ci_support.CiError, match="40-hex"):
        ci_support.build_shards(
            collect_text=COLLECT_TEXT,
            candidate_sha="not-a-sha",
            shard_count=2,
            allowed_skips=[],
        )


def test_build_shards_rejects_empty_collection_as_gate_failure():
    with pytest.raises(ci_support.CiError):
        ci_support.build_shards(
            collect_text="\nno tests ran\n",
            candidate_sha=SHA,
            shard_count=2,
            allowed_skips=[],
        )


def test_load_allowed_skips_ignores_comments_and_blanks():
    text = "# comment\n\n tests/test_a.py::test_plain \n"
    assert ci_support.load_allowed_skips(text) == ["tests/test_a.py::test_plain"]
