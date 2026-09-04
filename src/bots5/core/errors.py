from __future__ import annotations

from bots5.errors import Bots5Error


class CoreError(Bots5Error):
    """Base class for expected desktop-core failures."""


class AuthorityError(CoreError):
    """The authoritative data root is already owned by another process."""


class StateError(CoreError):
    """The requested core operation cannot be applied to current state."""


class RevisionConflict(StateError):
    """The authoritative chat changed after a command read it."""
