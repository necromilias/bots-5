from __future__ import annotations

from .data_root_authority import DataRootAuthority


# Kept as an import alias for internal Phase 1-5 module compatibility.  It is
# not a second authority type and does not accept the old lock-file pathname.
AuthorityLock = DataRootAuthority
