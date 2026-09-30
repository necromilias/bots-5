"""Systematic scan: which Unicode code points contribute ASCII alphanumeric text
under Python casefold but are absent from _UNICODE_CASEFOLD_ASCII_MAP?

This is a baseline-vs-Python question, not candidate-vs-baseline: any hit is a
pre-existing SQL guard gap that the candidate inherits unchanged.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
import bots5.core.secrets as baseline  # noqa: E402

mapped = {c for c, _ in baseline._UNICODE_CASEFOLD_ASCII_MAP}
hits = []
for cp in range(0x80, 0x110000):
    if 0xD800 <= cp <= 0xDFFF:
        continue
    ch = chr(cp)
    folded = ch.casefold()
    if any(("0" <= c <= "9") or ("a" <= c <= "z") for c in folded):
        hits.append((cp, ch, folded, cp in mapped))

print("code points (>=0x80) whose casefold contains an ASCII alnum:", len(hits))
for cp, ch, folded, is_mapped in hits:
    print(f"  U+{cp:04X} {ch!r} casefold={folded!r} mapped={is_mapped}")
unmapped = [h for h in hits if not h[3]]
print("NOT in the 19-entry map (baseline SQL gap):", len(unmapped))
for cp, ch, folded, _ in unmapped[:50]:
    print(f"   GAP U+{cp:04X} {ch!r} casefold={folded!r}")
