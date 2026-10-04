from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DesktopSessionInfo:
    """Read-only runtime identity presented by the desktop shell."""

    backend_id: str
    model: str
    provider_id: str | None = None
    generation_mode: str = "LEGACY_CORE_COMPATIBILITY"
    phase6_enabled: bool = False
    developer_provider_test_mode: bool = False

    @property
    def display_label(self) -> str:
        if self.developer_provider_test_mode:
            return f"{self.model} · INEXACT / PROVIDER TEST MODE (not Phase 6 compliant)"
        if self.generation_mode == "LEGACY_PHASE3_LOCAL_OPENAI":
            return f"{self.model} · local_openai · Phase 3 legacy (Phase 6 disabled)"
        if self.provider_id:
            return f"{self.model} · {self.provider_id}"
        return self.model
