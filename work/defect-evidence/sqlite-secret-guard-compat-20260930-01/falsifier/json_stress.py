"""json_tree-context stress: baseline vs candidate predicate over real json_tree keys."""
from __future__ import annotations

import json
import random
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[3] / "src"))
sys.path.insert(0, str(HERE.parent / "scratch"))
sys.path.insert(0, str(HERE))
import bots5.core.secrets as baseline  # noqa: E402
import candidate_secrets as candidate  # noqa: E402

EXPR_B = baseline.secret_key_forbidden_sql_expression("key")
EXPR_C = candidate.secret_key_forbidden_sql_expression("key")

KEY_ALPHABET = (
    list("tokenpasswordclientsecretapikey")
    + list("ABCXYZ019")
    + ["!", "_", ".", "-", " ", "\u00df", "\u017f", "\u0130", "\u212a",
       "\u1e98", "\u1e9a", "\ufb03", "\u00e9", "\U0001f642", "\\u0000"]
)


def rand_key(rng):
    style = rng.random()
    if style < 0.25:
        key = rng.choice(list(baseline._FORBIDDEN_SECRET_KEYS))
        if rng.random() < 0.5:
            key = "".join(
                c + (rng.choice(["!", "_", ".", " ", "\u00df", "\u017f"]) if rng.random() < 0.3 else "")
                for c in key
            )
        if rng.random() < 0.3:
            key = key.upper()
        return key
    if style < 0.4:
        letters = list(rng.choice(list(baseline._FORBIDDEN_SECRET_KEYS)))
        rng.shuffle(letters)
        return "".join(letters)
    return "".join(rng.choice(KEY_ALPHABET) for _ in range(rng.randint(0, 8)))


def rand_value(rng, depth=0):
    r = rng.random()
    if depth > 2 or r < 0.45:
        return rng.choice([1, 0, -5, 1.5, True, False, None, "x", "", "password"])
    if r < 0.72:
        return {rand_key(rng): rand_value(rng, depth + 1)
                for _ in range(rng.randint(0, 4))}
    return [rand_value(rng, depth + 1) for _ in range(rng.randint(0, 5))]


def python_keys_forbidden(doc):
    try:
        parsed = json.loads(doc)
    except Exception:
        return None

    def walk(v):
        if isinstance(v, dict):
            for k, sub in v.items():
                if isinstance(k, str) and baseline.normalize_secret_key(k) in baseline._FORBIDDEN_SECRET_KEYS:
                    return True
                if walk(sub):
                    return True
        elif isinstance(v, list):
            for sub in v:
                if walk(sub):
                    return True
        return False

    return walk(parsed)


def main() -> None:
    rng = random.Random(4242)
    docs = []
    # deterministic structural docs
    docs += [
        '{"a":[1,2,3]}',
        '{"a":[{"password":1}]}',
        '{"0":"x"}',
        '{"a":{"b":{"c":{"password":1}}}}',
        '{"arr":[[["secret"]]]}',
        '[{"token":1}]',
        '{"\u00df":1}',
    ]
    for _ in range(1500):
        docs.append(json.dumps(rand_value(rng)))

    con = sqlite3.connect(":memory:")
    diffs = []
    disagreements_py = []
    total_keys = 0
    for doc in docs:
        qb = con.execute(
            f"SELECT count(*) FROM json_tree(?) WHERE key IS NOT NULL AND ({EXPR_B})",
            (doc,)).fetchone()[0]
        qc = con.execute(
            f"SELECT count(*) FROM json_tree(?) WHERE key IS NOT NULL AND ({EXPR_C})",
            (doc,)).fetchone()[0]
        total_keys += con.execute(
            "SELECT count(*) FROM json_tree(?) WHERE key IS NOT NULL", (doc,)).fetchone()[0]
        if bool(qb) != bool(qc):
            diffs.append((doc, qb, qc))
        py = python_keys_forbidden(doc)
        if py is not None and bool(qb) != py:
            disagreements_py.append((doc, qb, qc, py))
    print("docs", len(docs), "keys", total_keys)
    print("json_tree baseline != candidate:", len(diffs))
    for d in diffs[:20]:
        print("  DIFF", repr(d[0])[:200], d[1], d[2])
    print("json_tree baseline != python ground truth:", len(disagreements_py))
    for d in disagreements_py[:15]:
        print("  PYDISAGREE", repr(d[0])[:200], "base", d[1], "cand", d[2], "py", d[3])
    (HERE / "json_tree_result.json").write_text(json.dumps({
        "docs": len(docs), "keys": total_keys, "diffs": len(diffs),
        "python_disagreements": len(disagreements_py),
        "samples": disagreements_py[:20]}, indent=1))
    print("wrote json_tree_result.json")


if __name__ == "__main__":
    main()
