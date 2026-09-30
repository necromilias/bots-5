"""Independent implementation-falsification equivalence corpus.

Loads the PRISTINE baseline predicate from work/baseline-t0/src/bots5/core/secrets.py
under an isolated module name and compares it against the candidate on a corpus that
is generated here from scratch (fixed seed), not reused from the design falsifier.

Run from repo root:
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. .venv314/bin/python \
      work/defect-evidence/.../falsifier/impl/equivalence.py
"""
from __future__ import annotations

import importlib.util
import json
import random
import sqlite3
import sys
from pathlib import Path

REPO = Path("/home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1")
BASELINE = REPO / "work/baseline-t0/src/bots5/core/secrets.py"
OUT = REPO / "work/defect-evidence/sqlite-secret-guard-compat-20260930-01/falsifier/impl"

# --- baseline module, loaded in isolation (does not shadow the candidate) ---
spec = importlib.util.spec_from_file_location("baseline_secrets_implfals", BASELINE)
baseline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(baseline)

from bots5.core import secrets as candidate  # noqa: E402

assert candidate.__file__.startswith(str(REPO / "src")), candidate.__file__
assert Path(baseline.__file__).resolve() == BASELINE.resolve()

FORBIDDEN = sorted(candidate._FORBIDDEN_SECRET_KEYS)
assert sorted(baseline._FORBIDDEN_SECRET_KEYS) == FORBIDDEN
assert baseline._UNICODE_CASEFOLD_ASCII_MAP == candidate._UNICODE_CASEFOLD_ASCII_MAP

BASE_EXPR = baseline.secret_key_forbidden_sql_expression("v")
CAND_EXPR = candidate.secret_key_forbidden_sql_expression("v")
BASE_JSON_EXPR = baseline.secret_key_forbidden_sql_expression("jt.key")
CAND_JSON_EXPR = candidate.secret_key_forbidden_sql_expression("jt.key")


def build_corpus() -> list[str]:
    rng = random.Random(20260930)
    values: list[str] = []

    # 1. separator insertions for every forbidden key and benign words
    seps = [
        " ", "-", "_", ".", "/", "\\", "+", "=", "!", "@", "#", "$", "%", "^",
        "&", "*", "(", ")", "[", "]", "{", "}", "|", ";", ":", ",", "<", ">",
        "?", "~", "`", '"', "\t", "\n", "\r", "\x0b", "\x0c", "\x1f", "\x7f",
        "\u2022", "\u00a0", "\u2013", "\u2014", "\u3000", "\u200b", "\ufeff",
        "--", "__", ".-", "\\/",
    ]
    words = FORBIDDEN + [
        "apricotKey", "appleKey", "username", "key", "tokenizer", "secretive",
        "passwordless", "apikeyish", "not-a-secret", "caf\u00e9", "na\u00efve",
        "aipkey", "nekto", "opssward", "apikey1", "readme", "settings",
    ]
    for word in words:
        for sep in seps:
            for variant in (word, word.upper(), word.title()):
                values.append(variant)
                values.append(sep.join(variant))
                values.append(variant + sep)
                values.append(sep + variant)
                if len(variant) > 2:
                    mid = len(variant) // 2
                    values.append(variant[:mid] + sep + variant[mid:])
                    values.append(variant[:mid] + sep + sep + variant[mid:])

    # 2. every Unicode fold code point: alone, doubled, embedded, mixed with separators
    for code, folded in candidate._UNICODE_CASEFOLD_ASCII_MAP:
        ch = chr(code)
        for base in ("", "api", "key", "secret", "pass", "tok"):
            values.append(base + ch)
            values.append(ch + base)
            values.append(base + ch + "key" if base != "key" else base + ch)
        values.append(ch)
        values.append(ch * 2)
        values.append(ch * 5)
        values.append("ap" + ch + "ikey")
        values.append("api" + ch + "-key")
        values.append(ch.join("password"))
        values.append(ch.join("token"))
        values.append(ch.upper())
        values.append(folded + ch)
        values.append(ch + folded)

    # 3. digits / mixed case / anagrams
    for key in FORBIDDEN:
        chars = sorted(key)
        for _ in range(60):
            rng.shuffle(chars)
            values.append("".join(chars))
        values.append(key + "1")
        values.append("1" + key)
        values.append(key.upper() + "0")
        values.append("".join(c.upper() if i % 2 else c for i, c in enumerate(key)))
    values += ["", " ", "  ", "\t", "\n", "0", "00", "123", "abc", "xyz",
               "\u00e9", "\u4e2d\u6587", "\U0001F600", "\u0130\u0131"]

    # 4. NUL and near-NUL
    for word in ["password", "token", "apikey", "secret", ""]:
        values.append("\x00" + word)
        values.append(word + "\x00")
        values.append("\x00")
        values.append("\x00\x00")
        values.append("\x01" + word)
        values.append(word + "\x1f")
    values.append("pass\x00word")
    values.append("\x00password")

    # 5. very long strings
    values.append("a" * 100_000)
    values.append(("password-" * 10_000))
    values.append(("-" * 50_000) + "token")
    values.append(("s" * 5_000) + ("e" * 5_000) + ("c" * 5_000) + "ret")
    values.append(("\u00df" * 20_000) + "password")
    values.append(("".join(chr(c) for c in range(32, 127)) * 2_000))

    # 6. seeded random fuzz over a hostile alphabet
    alphabet = list("aAbBcCdDeEfFgGhHiIjJkKlLmMnNoOpPqQrRsStTuUvVwWxXyYzZ0123456789")
    alphabet += [" ", "-", "_", ".", "/", "\x00", "\u00df", "\u0130", "\u017f",
                 "\u212a", "\ufb03", "\u0149", "\u1e96", "\u1e9e", "\u2022",
                 "\u00e9", "\u4e2d", "\t", "\x7f", "\uff21"]
    for _ in range(40_000):
        length = rng.randint(0, 14)
        values.append("".join(rng.choice(alphabet) for _ in range(length)))

    # de-dup while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            unique.append(value)
    return unique


def evaluate_predicate(expr: str, values: list[str]) -> set[str]:
    """Return the set of inputs for which the predicate is true (bare sqlite3)."""
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE TABLE t (v TEXT)")
        connection.executemany("INSERT INTO t (v) VALUES (?)", ((v,) for v in values))
        rows = connection.execute(
            f"SELECT v FROM t WHERE {expr}"
        ).fetchall()
        return {row[0] for row in rows}
    finally:
        connection.close()


def json_documents(rng: random.Random) -> list[str]:
    docs: list[str] = []
    adversarial_keys = []
    for key in FORBIDDEN:
        adversarial_keys += [
            key, key.upper(), key.title(), "-".join(key), " ".join(key),
            ".".join(key), "\u2022".join(key), "a" + key, key + "z",
        ]
    benign_keys = ["apricotKey", "username", "key", "tokenizer", "caf\u00e9",
                   "apikeyish", "passwordless", "name", "id", "value"]
    pools = adversarial_keys + benign_keys
    for i in range(600):
        key = rng.choice(pools)
        shape = rng.randint(0, 5)
        if shape == 0:
            docs.append(json.dumps({key: "x"}))
        elif shape == 1:
            docs.append(json.dumps({"outer": {key: {"deep": key}}}))
        elif shape == 2:
            docs.append(json.dumps({"arr": [{key: 1}, {"plain": 2}, [key]]}))
        elif shape == 3:
            docs.append(json.dumps({key: [{"inner": key}]}))
        elif shape == 4:
            docs.append(json.dumps({"a": {"b": {"c": {key: [1, 2]}}}}))
        else:
            docs.append(json.dumps([{key: key}, {key: "x"}, {"nested": {"x": key}}]))
    docs.append(json.dumps({"deep": {"deeper": {"deepest": {"token": 1}}}}))
    docs.append(json.dumps({}))
    docs.append(json.dumps([]))
    docs.append(json.dumps({"null": None, "num": 1, "bool": True}))
    return docs


def evaluate_json_tree(expr: str, docs: list[str]) -> set[tuple[int, str]]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE TABLE docs (id INTEGER PRIMARY KEY, j TEXT)")
        connection.executemany(
            "INSERT INTO docs (j) VALUES (?)", ((d,) for d in docs)
        )
        rows = connection.execute(
            "SELECT d.id, jt.fullkey FROM docs AS d, json_tree(d.j) AS jt "
            f"WHERE jt.key IS NOT NULL AND ({expr})"
        ).fetchall()
        return {(int(r[0]), str(r[1])) for r in rows}
    finally:
        connection.close()


def main() -> int:
    failures: list[str] = []
    rng = random.Random(777)
    corpus = build_corpus()
    print("corpus size:", len(corpus))
    print("baseline expr bytes:", len(BASE_EXPR), "candidate expr bytes:", len(CAND_EXPR))

    base_true = evaluate_predicate(BASE_EXPR, corpus)
    cand_true = evaluate_predicate(CAND_EXPR, corpus)
    print("baseline true count:", len(base_true), "candidate true count:", len(cand_true))
    only_base = base_true - cand_true
    only_cand = cand_true - base_true
    if only_base or only_cand:
        failures.append(f"predicate divergence: only_base={len(only_base)} only_cand={len(only_cand)}")
        for v in sorted(only_base)[:20]:
            print("ONLY-BASELINE", repr(v))
        for v in sorted(only_cand)[:20]:
            print("ONLY-CANDIDATE", repr(v))
    else:
        print("PREDICATE: identical verdicts on all", len(corpus), "corpus values")

    docs = json_documents(rng)
    print("json documents:", len(docs))
    base_json = evaluate_json_tree(BASE_JSON_EXPR, docs)
    cand_json = evaluate_json_tree(CAND_JSON_EXPR, docs)
    print("json_tree matched: baseline", len(base_json), "candidate", len(cand_json))
    if base_json != cand_json:
        failures.append("json_tree divergence")
        for row in sorted(base_json - cand_json)[:20]:
            print("ONLY-BASELINE-JSON", row)
        for row in sorted(cand_json - base_json)[:20]:
            print("ONLY-CANDIDATE-JSON", row)
    else:
        print("JSON_TREE: identical key verdicts")

    # Cross-check the SQL predicate against Python is_forbidden_secret_key for a
    # sample, to make sure the corpus exercises the intended boundary.
    sample = [v for v in corpus if len(v) < 40][:5000]
    missed = [v for v in sample
              if candidate.is_forbidden_secret_key(v) and v not in cand_true]
    print("python-forbidden missed by candidate SQL (sample):", len(missed))
    if missed:
        failures.append("candidate SQL misses python-forbidden inputs")
        for v in missed[:20]:
            print("MISSED", repr(v))

    Path(OUT / "equivalence_result.json").write_text(json.dumps({
        "corpus_size": len(corpus),
        "baseline_true": len(base_true),
        "candidate_true": len(cand_true),
        "only_baseline": sorted(only_base),
        "only_candidate": sorted(only_cand),
        "json_docs": len(docs),
        "json_only_baseline": sorted([list(x) for x in base_json - cand_json]),
        "json_only_candidate": sorted([list(x) for x in cand_json - base_json]),
        "failures": failures,
    }, indent=2))
    print("FAILURES:", failures if failures else "none")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
