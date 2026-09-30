"""Design candidate v2: parser-safe, semantics-preserving forbidden-key predicate.

The 19 Unicode case-fold replacements are deliberately split across two nested
derived tables so that no single SQL expression nests deeply enough to trip the
fixed 100-entry LEMON parser stack used by older SQLite builds. Separator
removal is not materialised at all: the forbidden vocabulary is pure ASCII
letters, so removing non-alphanumeric characters cannot change occurrence counts
of those letters or whether any residual ASCII alphanumeric character remains.
"""
from __future__ import annotations

from bots5.core.secrets import (
    _FORBIDDEN_SECRET_KEYS,
    _UNICODE_CASEFOLD_ASCII_MAP,
)

_NORMALIZED = "_bots5_normalized_key"
_MAX_REPLACEMENTS_PER_STAGE = 10


def _replace_chain(source: str, replacements: list[tuple[int, str]]) -> str:
    expression = source
    for code, replacement in replacements:
        expression = f"replace({expression}, char({code}), {replacement})"
    return expression


def secret_key_forbidden_sql_expression(column: str) -> str:
    replacements = [(code, f"'{folded}'") for code, folded in _UNICODE_CASEFOLD_ASCII_MAP]
    stages = [
        replacements[index : index + _MAX_REPLACEMENTS_PER_STAGE]
        for index in range(0, len(replacements), _MAX_REPLACEMENTS_PER_STAGE)
    ]

    stage_sql = f"SELECT lower(CAST({column} AS TEXT)) AS _bots5_casefold_0"
    for index, stage in enumerate(stages, start=1):
        stage_sql = (
            f"SELECT {_replace_chain(f'_bots5_casefold_{index - 1}', stage)} "
            f"AS _bots5_casefold_{index} FROM ({stage_sql}) AS _bots5_stage_{index - 1}"
        )

    final_stage = f"_bots5_casefold_{len(stages)}"
    residuals = []
    for index, key in enumerate(sorted(_FORBIDDEN_SECRET_KEYS)):
        residuals.append(
            f"{_replace_chain(final_stage, [(ord(c), chr(39) + chr(39)) for c in sorted(set(key))])} "
            f"AS _bots5_residual_{index}"
        )
    projection = (
        f"SELECT {final_stage} AS {_NORMALIZED}, "
        + ", ".join(residuals)
        + f" FROM ({stage_sql}) AS _bots5_stage_{len(stages)}"
    )

    forbidden = ", ".join(f"'{key}'" for key in sorted(_FORBIDDEN_SECRET_KEYS))
    conditions = [f"{_NORMALIZED} IN ({forbidden})"]
    for index, key in enumerate(sorted(_FORBIDDEN_SECRET_KEYS)):
        counts = " AND ".join(
            f"length({_NORMALIZED}) - length(replace({_NORMALIZED}, '{character}', '')) = {key.count(character)}"
            for character in sorted(set(key))
        )
        conditions.append(
            f"({counts} AND _bots5_residual_{index} NOT GLOB '*[A-Za-z0-9]*')"
        )
    return (
        f"(instr(CAST({column} AS TEXT), char(0)) > 0 OR EXISTS ("
        f"SELECT 1 FROM ({projection}) AS _bots5_normalized WHERE "
        + " OR ".join(conditions)
        + "))"
    )
