from __future__ import annotations

from datetime import timezone
from uuid import UUID

from bots5.domain.clock import parse_utc, utc_iso
from bots5.domain.ids import Uuid7Factory


def test_durable_ids_are_uuidv7():
    value = UUID(Uuid7Factory().new())
    assert value.version == 7


def test_authoritative_time_is_aware_utc_and_round_trips():
    value = parse_utc("2026-09-03T00:00:00.123Z")
    assert value.tzinfo == timezone.utc
    assert utc_iso(value) == "2026-09-03T00:00:00.123Z"
