from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_cache_dir, user_config_dir, user_data_dir, user_state_dir


@dataclass(frozen=True, slots=True)
class AppPaths:
    data_root: Path
    config_root: Path
    state_root: Path
    cache_root: Path
    logs_root: Path
    database: Path
    authority_lock: Path

    def ensure(self) -> None:
        for path in (
            self.data_root,
            self.config_root,
            self.state_root,
            self.cache_root,
            self.logs_root,
        ):
            path.mkdir(parents=True, exist_ok=True)


def resolve_app_paths(data_root: Path | None = None) -> AppPaths:
    if data_root is not None:
        root = data_root.expanduser().resolve(strict=False)
        config = root / "config"
        state = root / "state"
        cache = root / "cache"
    else:
        root = Path(user_data_dir("bots5", "B.O.T.S."))
        config = Path(user_config_dir("bots5", "B.O.T.S."))
        state = Path(user_state_dir("bots5", "B.O.T.S."))
        cache = Path(user_cache_dir("bots5", "B.O.T.S."))
    return AppPaths(
        data_root=root,
        config_root=config,
        state_root=state,
        cache_root=cache,
        logs_root=state / "logs",
        database=root / "state.sqlite3",
        authority_lock=root / "authority.lock",
    )
