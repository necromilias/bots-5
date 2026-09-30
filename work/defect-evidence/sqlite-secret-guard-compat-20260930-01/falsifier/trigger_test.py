"""Trigger-level behaviour test for the forbidden-key guard.

Builds the real migration trigger body (`safe_json_object`) twice:
  * baseline predicate (only possible on SQLite >= 3.46)
  * candidate predicate
and compares accept/reject over a JSON document corpus that exercises json_tree
nested objects, arrays (integer keys), unicode escapes and NUL keys.

Also compiles the candidate trigger on isolated SQLite 3.45.1 (like the real
migration must) and replays the same DML corpus there.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRATCH = HERE.parent / "scratch"
sys.path.insert(0, str(SCRATCH))
sys.path.insert(0, str(HERE))

from ctypes_sqlite import Sqlite  # noqa: E402

sys.path.insert(0, str(HERE.parents[3] / "src"))
import bots5.core.secrets as _baseline  # noqa: E402
import candidate_secrets as _candidate  # noqa: E402

EXPR_B = _baseline.secret_key_forbidden_sql_expression("key")
EXPR_C = _candidate.secret_key_forbidden_sql_expression("key")

DOCS = [
    "not json",
    '"str"',
    "[1,2,3]",
    "{}",
    '{"normal":1,"value":2}',
    '{"apricotKey":1}',
    '{"apikey":1}',
    '{"api_key":1}',
    '{"api.key":1}',
    '{"APIKey":1}',
    '{"ApiKey":1}',
    '{"apikeyvalue":1}',
    '{"apitoken":1}',
    '{"accesstoken":1}',
    '{"authorization":1}',
    '{"clientsecret":1}',
    '{"password":1}',
    '{"refreshtoken":1}',
    '{"secret":1}',
    '{"secretvalue":1}',
    '{"token":1}',
    '{"tokenvalue":1}',
    '{"nekto":1}',
    '{"enotk":1}',
    '{"sseccret":1}',
    '{"passwordx":1}',
    '{"xpassword":1}',
    '{"password1":1}',
    '{"1password":1}',
    '{"pass word":1}',
    '{"pass\\tword":1}',
    '{"pa\\u00dfword":1}',
    '{"pa\\u1e9eword":1}',
    '{"pa\\u017fsword":1}',
    '{"to\\u212aen":1}',
    '{"api\\u0130ey":1}',
    '{"toke\\u0149":1}',
    '{"acces\\ufb05oken":1}',
    '{"pa\\u1e98ssord":1}',
    '{"password\\u0307":1}',
    '{"password\\u00e9":1}',
    '{"p\\u00e9ssword":1}',
    '{"password\\u0000x":1}',
    '{"\\u0000token":1}',
    '{"to\\u0000ken":1}',
    '{"a":{"password":1}}',
    '{"a":{"b":{"secret":1}}}',
    '{"a":["x",{"secret":1}]}',
    '{"a":[{"token":1}]}',
    '{"a":[[["clientsecret"]]]}',
    '{"arr":[1,2,3],"ok":true}',
    '{"arr":["apikey"]}',
    '{"outer":{"arr":[1,2,3]}}',
    '{"\\ud83d\\ude42":1}',
    '{"normal":"password"}',
    '{"normal":{"value":"apikey"}}',
    '{"secretValue":1}',
    '{"SECRETVALUE":1}',
    '{"secret_value":1}',
    '{"se.cret":1}',
    '{"s-e-c-r-e-t":1}',
    '{"s_e_c_r_e_t":1}',
    '{"a"*4096:1}',
    json.dumps({"x" * 100000: 1}),
    json.dumps({"token": "x" * 50000}),
    '{"password":1,"token":2}',
    "null",
    "123",
    "true",
]


def safe_json_object(expr: str, column: str) -> str:
    return (
        f"json_valid({column}) = 1 AND json_type({column}) = 'object' AND NOT EXISTS ("
        f"SELECT 1 FROM json_tree({column}) WHERE key IS NOT NULL AND {expr})"
    )


def python_ground_truth(doc: str, forbidden, normalize) -> bool:
    try:
        parsed = json.loads(doc)
    except Exception:
        return False
    if not isinstance(parsed, dict):
        return False

    def walk(value) -> bool:
        if isinstance(value, dict):
            for k, v in value.items():
                if isinstance(k, str) and normalize(k) in forbidden:
                    return False
                if not walk(v):
                    return False
        elif isinstance(value, list):
            for v in value:
                if not walk(v):
                    return False
        return True

    return walk(parsed)


def run_python_sqlite(expr: str, docs):
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE t (metadata_json TEXT)")
    con.execute(
        f"CREATE TRIGGER g BEFORE INSERT ON t WHEN NOT ({safe_json_object(expr, 'NEW.metadata_json')}) "
        f"BEGIN SELECT RAISE(ABORT, 'bad'); END"
    )
    results = []
    for doc in docs:
        try:
            con.execute("INSERT INTO t VALUES (?)", (doc,))
            results.append(True)
        except sqlite3.IntegrityError:
            results.append(False)
    con.close()
    return results


def run_ctypes_345(lib: Path, expr: str, docs):
    db = HERE / "trigger345_candidate.db"
    for suffix in ("", "-journal", "-wal"):
        p = Path(str(db) + suffix)
        if p.exists():
            p.unlink()
    sq = Sqlite(lib)
    sq.open(str(db))
    rc, msg = sq.execute("CREATE TABLE t (metadata_json TEXT);")
    assert rc == 0, msg
    rc, msg = sq.execute(
        f"CREATE TRIGGER g BEFORE INSERT ON t WHEN NOT ({safe_json_object(expr, 'NEW.metadata_json')}) "
        f"BEGIN SELECT RAISE(ABORT, 'bad'); END;"
    )
    if rc != 0:
        sq.close()
        return None, msg
    results = []
    errors = []
    for doc in docs:
        escaped = doc.replace("'", "''")
        rc, msg = sq.execute(f"INSERT INTO t VALUES ('{escaped}');")
        results.append(rc == 0)
        if rc != 0:
            errors.append(msg)
    sq.close()
    return results, errors


def main() -> None:
    import bots5.core.secrets as secrets

    docs = DOCS
    # also add generated docs around every forbidden key
    for key in sorted(secrets._FORBIDDEN_SECRET_KEYS):
        docs = docs + [
            json.dumps({key: 1}),
            json.dumps({key.upper(): 1}),
            json.dumps({"x" + key: 1}),
            json.dumps({"outer": {key: 1}}),
            json.dumps({"outer": [1, {key: 1}]}),
            json.dumps({"x": [1, 2, 3], key: 4}),
        ]

    base = run_python_sqlite(EXPR_B, docs)
    cand = run_python_sqlite(EXPR_C, docs)
    truth = [
        python_ground_truth(d, secrets._FORBIDDEN_SECRET_KEYS, secrets.normalize_secret_key)
        for d in docs
    ]

    diffs = [(i, docs[i], base[i], cand[i], truth[i]) for i in range(len(docs)) if base[i] != cand[i]]
    print("docs:", len(docs))
    print("baseline(3.50) vs candidate(3.50) trigger diffs:", len(diffs))
    for i, d, b, c, t in diffs[:30]:
        print("  DIFF", repr(d[:120]), "baseline", b, "candidate", c, "python", t)

    missed = [(i, docs[i]) for i in range(len(docs)) if base[i] and not truth[i]]
    overrej = [(i, docs[i]) for i in range(len(docs)) if truth[i] and not base[i]]
    print("baseline-vs-python MISSED secrets (SQL accepted, python forbidden):", len(missed))
    for i, d in missed[:10]:
        print("   MISS", repr(d[:120]))
    print("baseline-vs-python OVER-REJECTIONS (python benign, SQL rejected):", len(overrej))
    for i, d in overrej[:10]:
        print("   OVER", repr(d[:120]))

    # Python sqlite version used above
    print("python sqlite version", sqlite3.sqlite_version)

    # ---- isolated 3.45.1 candidate compile + replay ----
    lib = SCRATCH / "libsqlite3-3.45.1.so"
    res345, errs345 = run_ctypes_345(lib, EXPR_C, docs)
    if res345 is None:
        print("3.45.1 candidate trigger CREATE FAILED:", errs345)
    else:
        print("3.45.1 candidate trigger CREATE OK; replay rows", len(res345))
        d2 = [(i, docs[i], cand[i], res345[i]) for i in range(len(docs)) if cand[i] != res345[i]]
        print("candidate(3.50) vs candidate(3.45.1) diffs:", len(d2))
        for i, d, c, r in d2[:20]:
            print("   DIFF345", repr(d[:120]), c, r)
        # also check the isolated 3.45.1 parser rejects the baseline expression
        print("3.45.1 baseline trigger CREATE:", run_ctypes_345(lib, EXPR_B, docs[:2])[0] is not None)

    Path(HERE / "trigger_results.json").write_text(json.dumps({
        "docs": docs,
        "baseline": base,
        "candidate": cand,
        "python_truth": truth,
        "candidate_345": res345,
    }, indent=1))
    print("wrote trigger_results.json")


if __name__ == "__main__":
    main()
