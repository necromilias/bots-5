"""Structural audit of the candidate generator output vs the source policy.

Extracts the Unicode fold pairs, forbidden key list, per-key count constants and
residual character sets from the emitted candidate SQL and compares them to
src/bots5/core/secrets.py.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[3] / "src"))
sys.path.insert(0, str(HERE.parent / "scratch"))
import bots5.core.secrets as baseline  # noqa: E402
import candidate_secrets as candidate  # noqa: E402

S = candidate.secret_key_forbidden_sql_expression("key")

# Unicode folds: replace(..., char(N), 'xxx').  Textual order differs from source
# order because the outer (later) stage is printed first; only the code->fold
# mapping (a disjoint set of target code points) is semantically relevant.
folds = [(int(a), b) for a, b in re.findall(r"char\((\d+)\), '([a-z]+)'", S)]
expected_folds = [(c, f) for c, f in baseline._UNICODE_CASEFOLD_ASCII_MAP]
print("fold pairs emitted:", folds)
print("fold pairs as mapping == expected mapping:", sorted(folds) == sorted(expected_folds))
print("disjoint targets:", len({c for c, _ in folds}) == len(folds))
print("all folds are ASCII alnum:", all(f.isascii() and f.isalnum() for _, f in folds))

# ASCII separator removals must NOT be materialised as `char(N), ''`
seps = [int(a) for a in re.findall(r"char\((\d+)\), ''", S)]
print("empty-replacement chars emitted:", sorted(set(seps)))
print("expected residual-key chars only:", sorted({ord(c) for k in baseline._FORBIDDEN_SECRET_KEYS for c in k}))
print("empty-replacement chars == residual key chars:",
      sorted(set(seps)) == sorted({ord(c) for k in baseline._FORBIDDEN_SECRET_KEYS for c in k}))

# forbidden list
inlist = re.search(r"_bots5_normalized_key IN \(([^)]*)\)", S).group(1)
keys = re.findall(r"'([a-z0-9]+)'", inlist)
print("IN list:", keys)
print("IN list == sorted forbidden:", keys == sorted(baseline._FORBIDDEN_SECRET_KEYS))

# residual columns: for each key verify the chain removes exactly sorted(set(key))
blocks = re.findall(r"AS _bots5_residual_(\d+)", S)
print("residual columns:", blocks)

# counts: verify each key's count equation set against key.count
count_sets = re.findall(r"length\(_bots5_normalized_key\) - length\(replace\(_bots5_normalized_key, '(\w)', ''\)\) = (\d+)", S)
print("count equations emitted:", count_sets)
expected_counts = []
for key in sorted(baseline._FORBIDDEN_SECRET_KEYS):
    for ch in sorted(set(key)):
        expected_counts.append((ch, str(key.count(ch))))
print("expected count equations:", expected_counts)
print("counts match:", count_sets == expected_counts)

# stage split sanity: depth and stage count
aliases = re.findall(r"AS _bots5_casefold_(\d+)", S)
print("casefold aliases:", aliases)
