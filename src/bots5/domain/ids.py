from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from uuid6 import uuid7


class IdFactory(Protocol):
    def new(self) -> str:
        """Return a new durable B.O.T.S. identifier."""


@dataclass(frozen=True, slots=True)
class Uuid7Factory:
    def new(self) -> str:
        return str(uuid7())
