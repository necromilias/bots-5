from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DesktopSessionInfo:
    """Read-only runtime identity presented by the desktop shell."""

    backend_id: str
    model: str
    provider_id: str | None = None

    @property
    def display_label(self) -> str:
        if self.provider_id:
            return f"{self.model} · {self.provider_id}"
        return self.model
