"""Adversarial case generator + exact Python mirror of the two SQL predicates.

Loads the REAL baseline builder from src/ and the candidate builder from scratch,
so the mirror vocabulary/map can never drift from the code under test.
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

ROOT = Path("/home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1")
SCRATCH = ROOT / "work/defect-evidence/sqlite-secret-guard-compat-20260930-01/scratch"
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(SCRATCH))

import bots5.core.secrets as baseline  # noqa: E402

FORBIDDEN = sorted(baseline._FORBIDDEN_SECRET_KEYS)
MAP = baseline._UNICODE_CASEFOLD_ASCII_MAP
NONALNUM = baseline._ASCII_NON_ALNUM_CODES
MAPPED_CHARS = [chr(c) for c, _ in MAP]


def sql_lower(s: str) -> str:
    return "".join(chr(ord(c) + 32) if "A" <= c <= "Z" else c for c in s)


def pred_sql(value, remove_separators: bool):
    """Exact mirror of secret_key_forbidden_sql_expression semantics.

    Baseline = remove_separators True; candidate = False.
    """
    if value is None:
        return None  # SQL NULL propagates; WHEN NULL is not-true
    s = value if isinstance(value, str) else str(value)
    if "\x00" in s:
        return True  # instr(CAST(col AS TEXT), char(0)) > 0
    t = sql_lower(s)
    for code, folded in MAP:
        t = t.replace(chr(code), folded)
    if remove_separators:
        for code in NONALNUM:
            t = t.replace(chr(code), "")
    if t in FORBIDDEN:
        return True
    for key in FORBIDDEN:
        chars = sorted(set(key))
        if all(len(t) - len(t.replace(c, "")) == key.count(c) for c in chars):
            residual = t
            for c in chars:
                residual = residual.replace(c, "")
            if not any(
                ("A" <= c <= "Z") or ("a" <= c <= "z") or ("0" <= c <= "9")
                for c in residual
            ):
                return True
    return False


def pred_python(value) -> bool:
    return baseline.is_forbidden_secret_key(value)


# --------------------------------------------------------------------------
# Case generators
# --------------------------------------------------------------------------
def targeted() -> list:
    cases: list = [None, "", " ", "!", "!!!", "0", "123", "\x00", "a\x00b",
                   "\x00token", "token\x00", "tok\x00en", "🙂", "é", "ß"]
    for key in FORBIDDEN:
        cases += [key, key.upper(), key.title(), key[::-1]]
        # every ASCII separator inserted at start/middle/end
        for code in NONALNUM:
            sep = chr(code)
            cases.append(sep + key)
            cases.append(key + sep)
            cases.append(key[: len(key) // 2] + sep + key[len(key) // 2 :])
        # digits
        cases += ["1" + key, key + "1", key + "0" * 5]
        # extra letters
        cases += ["x" + key, key + "x", key + key]
        # anonymised separators between every character
        cases.append("".join(c + "!" for c in key))
        cases.append("".join(c + "\x00" for c in key))
    # mapped characters: strings that casefold-stretch into forbidden keys
    stretch = {
        "password": [("\xdf", "pass", "word"), ("\u1e9e", "pass", "word"),
                     ("\u017f", "pa", "sword"), ("\u1e98", "pass", "ord")],
        "accesstoken": [("\xdf", "acces", "token"), ("\u1e9e", "acces", "token"),
                        ("\ufb05", "acces", "oken"), ("\ufb06", "acces", "oken"),
                        ("\u017f", "acce", "token")],
        "refreshtoken": [("\u017f", "refre", "htoken"), ("\xdf", "refre", "htoken")],
        "apikey": [("\u212a", "api", "ey"), ("\u1e99", "apike", "")],
        "token": [("\u212a", "to", "en"), ("\u0149", "toke", ""),
                  ("\u0130", "ap", "token")],
        "apitoken": [("\u0130", "ap", "token"), ("\u017f", "api", "token")],
        "clientsecret": [("\xdf", "client", "ecret")],
        "secret": [("\u017f", "", "ecret"), ("\u1e9a", "p", "ssword")],
    }
    for key, variants in stretch.items():
        for ch, pre, post in variants:
            cases.append(pre + ch + post)
            cases.append("_" + pre + ch + post + "_")
            cases.append(pre.upper() + ch + post.upper())
    # every mapped char alone / doubled / paired
    for ch in MAPPED_CHARS:
        cases += [ch, ch * 3, "x" + ch, ch + "x", "a" + ch + "b", ch + "\x00"]
    # all 19 mapped chars replacing a letter of every key where possible
    for key in FORBIDDEN:
        for ch in MAPPED_CHARS:
            cases.append(ch + key)
            cases.append(key + ch)
            cases.append(key[:1] + ch + key[2:])
    # anagrams (deterministic rotations / reversals / sorted) of every key
    for key in FORBIDDEN:
        letters = sorted(key)
        cases.append("".join(letters))
        for i in range(len(key)):
            cases.append(key[i:] + key[:i])
    # block of all mapped characters in one string
    cases.append("".join(MAPPED_CHARS))
    cases.append("".join(MAPPED_CHARS) + "token")
    cases.append("token" + "".join(MAPPED_CHARS))
    # long strings
    cases.append("a" * 100000)
    cases.append("token" + "!" * 50000)
    cases.append("!" * 100000 + "token")
    cases.append(("token" + "!") * 20000)
    return cases


def randoms(seed: int, n: int) -> list:
    rng = random.Random(seed)
    alphabet = (
        list("tokenpasswordclientsecretapikey")
        + list("ABCXYZ019")
        + list("!@#$%^&*()_+-=[]{};:'\",.<>/?\\|`~ \t\n\r")
        + ["\x00", "\u00df", "\u0130", "\u0149", "\u017f", "\u01f0", "\u1e96",
           "\u1e97", "\u1e98", "\u1e99", "\u1e9a", "\u1e9e", "\u212a", "\ufb00",
           "\ufb01", "\ufb02", "\ufb03", "\ufb04", "\ufb05", "\ufb06", "\u00e9",
           "\u0307", "\u0131", "\u212b", "\U0001f642"]
    )
    out = []
    for _ in range(n):
        length = rng.choice([0, 1, 2, 3, 4, 5, 6, 7, 8, 10, 16, 40, 200])
        out.append("".join(rng.choice(alphabet) for _ in range(length)))
    # random anagram + separators of each key
    for key in FORBIDDEN:
        for _ in range(40):
            letters = list(key) + [rng.choice("!@#\x00\u00df\u017f019") for _ in range(rng.randint(1, 4))]
            rng.shuffle(letters)
            out.append("".join(letters))
    return out


def exhaustive(max_len: int = 4) -> list:
    alphabet = ["t", "o", "k", "e", "n", "p", "a", "s", "1", "!", "X",
                "\u00df", "\u017f", "\u0130", "\u212a", "\x00", "\u00e9"]
    out = [""]
    frontier = [""]
    for _ in range(max_len):
        nxt = []
        for prefix in frontier:
            for ch in alphabet:
                nxt.append(prefix + ch)
        out.extend(nxt)
        frontier = nxt
    return out


def main() -> None:
    cases = targeted()
    cases += randoms(20260930, 6000)
    exhaustive_cases = exhaustive(4)
    all_cases = cases + exhaustive_cases
    # dedupe preserving order
    seen = set()
    unique = []
    for c in all_cases:
        k = ("N",) if c is None else ("S", c)
        if k in seen:
            continue
        seen.add(k)
        unique.append(c)
    OUT.joinpath("cases.json").write_text(json.dumps(unique))
    print("total cases", len(unique), "targeted", len(cases), "exhaustive", len(exhaustive_cases))


if __name__ == "__main__":
    main()
