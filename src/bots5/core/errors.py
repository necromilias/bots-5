from __future__ import annotations

from bots5.errors import Bots5Error


class CoreError(Bots5Error):
    """Base class for expected desktop-core failures."""


class AuthorityError(CoreError):
    """The authoritative data root is already owned by another process."""


class MigrationRecoveryStateError(CoreError):
    """A recognized pre-journal migration recovery state cannot be adopted."""


class StateError(CoreError):
    """The requested core operation cannot be applied to current state."""


class RevisionConflict(StateError):
    """The authoritative chat changed after a command read it."""


class SearchError(CoreError):
    """Base class for expected search and exact-navigation failures."""


class SearchInvalidQuery(SearchError):
    """The supplied search text or structured filters are invalid."""


class SearchUnavailable(SearchError):
    """Search is unavailable in the current SQLite runtime."""


class SearchRebuilding(SearchError):
    """The derived search index is currently rebuilding."""


class SearchStaleIndex(SearchError):
    """Authoritative source state is newer than the derived checkpoint."""


class SearchCursorStale(SearchError):
    """The search cursor does not match the current query or index generation."""


class SearchIndexInvalid(SearchError):
    """The derived search index is invalid or corrupt."""


class SearchResultGone(SearchError):
    """A selected result or exact navigation location no longer exists."""


class BackupError(CoreError):
    """Base class for Backup v1 generation and independent verification failures."""


class BackupArchiveInvalid(BackupError):
    """A backup container or manifest is malformed or contradicts itself."""


class BackupUnsupported(BackupError):
    """A backup declares a version or revision this build cannot verify."""


class BackupResourceLimit(BackupError):
    """A backup reached a bounded resource limit without claiming validity."""


class BackupResolutionCancelled(BackupError):
    """A backup operation was cancelled before or at a non-publication boundary."""


class BackupUnclassifiedState(BackupError):
    """B.O.T.S.-owned recovery state could not be classified and backup failed closed."""


class BackupDestinationExists(BackupError):
    """A create-new backup destination already exists."""


class BackupDestinationInvalid(BackupError):
    """A backup destination exists but cannot be used safely."""


class BackupPublicationFailed(BackupError):
    """A backup publication rename failed without establishing an outcome."""


class BackupUncertainPublication(BackupError):
    """A backup rename may have succeeded but publication durability is uncertain."""
