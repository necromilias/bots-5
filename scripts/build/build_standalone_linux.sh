#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
PYTHON="${BOTS5_PACKAGING_PYTHON:-python3}"
ENV_DIR="${BOTS5_PACKAGING_ENV:-$ROOT/build/m7-venv}"
DIST_DIR="${BOTS5_STANDALONE_OUT:-$ROOT/dist/bots5-linux-standalone}"
MIGRATIONS="$ROOT/src/bots5/infrastructure/persistence/migrations"
CONSTRAINTS="$ROOT/scripts/build/standalone_linux_constraints.txt"

if [[ ! -x "$ENV_DIR/bin/python" ]]; then
    command -v "$PYTHON" >/dev/null || {
        echo "Packaging interpreter not found: $PYTHON" >&2
        exit 2
    }
    "$PYTHON" -m venv "$ENV_DIR"
fi
BUILD_PYTHON="$ENV_DIR/bin/python"
PYTHON_BIN_DIR="$(dirname -- "$BUILD_PYTHON")"
export PATH="$PYTHON_BIN_DIR:$PATH"
export NUITKA_CACHE_DIR="${BOTS5_NUITKA_CACHE_DIR:-$ROOT/build/m7-nuitka-cache}"
export PYTHONPATH="$ROOT/src"

"$BUILD_PYTHON" - <<'PY'
import _sqlite3
import ctypes
import ctypes.util
import sys

assert (3, 12) <= sys.version_info[:2] < (3, 15), sys.version
assert getattr(_sqlite3, "__file__", None), "packaging Python must expose shared _sqlite3"
shared_name = ctypes.util.find_library("sqlite3")
assert shared_name, "shared SQLite is required"
module = ctypes.CDLL(_sqlite3.__file__)
shared = ctypes.CDLL(shared_name)
assert ctypes.cast(module.sqlite3_vfs_find, ctypes.c_void_p).value == ctypes.cast(
    shared.sqlite3_vfs_find, ctypes.c_void_p
).value, "packaging Python must use the same shared SQLite instance"
PY
mapfile -t requirements < <("$BUILD_PYTHON" - "$ROOT/pyproject.toml" <<'PY'
import sys
import tomllib
from pathlib import Path
project = tomllib.loads(Path(sys.argv[1]).read_text())["project"]
print("\n".join(project["dependencies"] + project["optional-dependencies"]["dev"]))
PY
)
"$BUILD_PYTHON" -m pip install --disable-pip-version-check -c "$CONSTRAINTS" \
    "${requirements[@]}" 'Nuitka==4.1.1' 'patchelf==0.19.1.0'
"$BUILD_PYTHON" - <<'PY'
from importlib.metadata import version
assert version("Nuitka") == "4.1.1", version("Nuitka")
assert version("PySide6") == "6.11.2", version("PySide6")
PY

if [[ "$(realpath -m -- "$(dirname -- "$DIST_DIR")")" != "$(realpath -m -- "$ROOT/dist")" ]]; then
    echo "BOTS5_STANDALONE_OUT must be a direct child of $ROOT/dist" >&2
    exit 2
fi
mkdir -p "$ROOT/build" "$NUITKA_CACHE_DIR" "$(dirname -- "$DIST_DIR")"
# Entry-file siblings are on Nuitka's import path. A new directory prevents
# old generated wrappers such as bots5.py from shadowing the real package.
WORK_DIR="$(mktemp -d "$ROOT/build/m7-workspace.XXXXXX")"
ENTRY_DIR="$WORK_DIR/entrypoints"
SPEC_DIR="$WORK_DIR/specs"
NATIVE="$WORK_DIR/native/libbots5_rooted_sqlite_vfs.so"
mkdir -p "$ENTRY_DIR" "$SPEC_DIR"
mkdir -p "$DIST_DIR"
cp "$ROOT/tests/_standalone_restore_probe.py" "$ENTRY_DIR/m7_restore_probe.py"

cat > "$ENTRY_DIR/entry_cli.py" <<'PY'
from bots5.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
PY
cat > "$ENTRY_DIR/entry_desktop.py" <<'PY'
import json
import os
from pathlib import Path

from bots5.bootstrap.desktop import main

if __name__ == "__main__":
    if os.environ.get("BOTS5_PACKAGING_CHECK") == "restore-handoff":
        from m7_restore_probe import run
        raise SystemExit(run(main))
    if os.environ.get("BOTS5_PACKAGING_CHECK") == "1":
        import _sqlite3
        import sqlite3
        from PySide6.QtCore import QResource
        from PySide6.QtSvg import QSvgRenderer
        from PySide6.QtWidgets import QApplication
        from bots5.desktop.icons import action_icon
        from bots5.desktop.theme import apply_draft1_theme
        from bots5.infrastructure import rooted_sqlite_vfs
        from bots5.infrastructure.persistence import migration_runner
        from bots5.infrastructure.secrets import SecretServiceStore

        application = QApplication([])
        apply_draft1_theme(application)
        native = rooted_sqlite_vfs.native_library()
        # Backend construction proves the packaged strict backend is importable;
        # no get/set/delete credential operation is performed.
        backend = SecretServiceStore()._backend
        icons = ("up", "down", "send", "attach", "close")
        print(json.dumps({
            "sqlite_extension": _sqlite3.__file__,
            "sqlite_version": sqlite3.sqlite_version,
            "native_vfs": native._name,
            "shared_sqlite_matches": native.bots5_runtime_sqlite_matches() == 0,
            "migrations": str(Path(migration_runner.__file__).with_name("migrations")),
            "icons": {name: QResource(f":/bots/icons/{name}.svg").isValid()
                      and QSvgRenderer(f":/bots/icons/{name}.svg").isValid()
                      for name in icons},
            "action_icon_renders": not action_icon("send").pixmap(24, 24).isNull(),
            "stylesheet_bytes": len(application.styleSheet().encode()),
            "secret_service_backend": f"{type(backend).__module__}.{type(backend).__name__}",
        }))
        raise SystemExit(0)
    raise SystemExit(main())
PY

"$BUILD_PYTHON" - "$ENTRY_DIR" "$ROOT/src/bots5/__init__.py" <<'PY'
import importlib.util
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
spec = importlib.util.find_spec("bots5")
assert spec and spec.submodule_search_locations, "bots5 resolves to a shadow module"
assert Path(spec.origin).resolve() == Path(sys.argv[2]).resolve(), spec.origin
print(f"Package-origin preflight: {spec.origin}")
PY

"$BUILD_PYTHON" -m bots5.infrastructure.native.build_rooted_vfs --output "$NATIVE"

write_spec() {
    local title="$1" entry="$2"
    "$BUILD_PYTHON" - "$SPEC_DIR/$title.spec" "$title" "$ROOT" "$entry" \
        "$DIST_DIR" "$BUILD_PYTHON" "$NATIVE" "${MIGRATION_DATA_ARGS[@]}" <<'PY'
import shlex
import sys
from pathlib import Path

spec, title, root, entry, dist, python, native, *migration_args = sys.argv[1:]
extra_args = [
    "--quiet",
    "--jobs=8",
    "--include-package=bots5",
    f"--report={Path(spec).with_suffix('.nuitka.xml')}",
    "--noinclude-qt-translations",
    f"--include-data-files={native}=./libbots5_rooted_sqlite_vfs.so",
    *migration_args,
    "--include-distribution-metadata=keyring",
    "--include-distribution-metadata=SecretStorage",
    "--include-package=keyring.backends",
    "--include-package-data=keyring",
]
Path(spec).write_text(f"""[app]
title = {title}
project_dir = {root}
input_file = {entry}
exec_directory = {dist}
project_file =
icon =

[python]
python_path = {python}
packages = Nuitka==4.1.1

[qt]
qml_files =
excluded_qml_plugins =
modules =
plugins =

[android]

[nuitka]
mode = standalone
extra_args = {shlex.join(extra_args)}

[buildozer]
""", encoding="utf-8")
PY
}

MIGRATION_DATA_ARGS=()
while IFS= read -r source; do
    relative="${source#"$ROOT/src/"}"
    MIGRATION_DATA_ARGS+=("--include-data-files=$source=./$relative")
done < <(find "$MIGRATIONS" -type f \( -name '*.py' -o -name '*.mako' \) -print | sort)

apps=(bots5 bots5-desktop)
if [[ -n "${BOTS5_BUILD_ONLY:-}" ]]; then
    case "$BOTS5_BUILD_ONLY" in
        bots5|bots5-desktop) apps=("$BOTS5_BUILD_ONLY") ;;
        *) echo "BOTS5_BUILD_ONLY must be bots5 or bots5-desktop" >&2; exit 2 ;;
    esac
fi

for app in "${apps[@]}"; do
    case "$app" in
        bots5) entry="$ENTRY_DIR/entry_cli.py"; executable=entry_cli.bin ;;
        bots5-desktop) entry="$ENTRY_DIR/entry_desktop.py"; executable=entry_desktop.bin ;;
    esac
    rm -rf "$DIST_DIR/$app.dist"
    write_spec "$app" "$entry"
    echo "Building $app with Python $($BUILD_PYTHON --version 2>&1) and Nuitka 4.1.1"
    "$PYTHON_BIN_DIR/pyside6-deploy" "$entry" \
        --config-file "$SPEC_DIR/$app.spec" --mode standalone \
        --nuitka-version 4.1.1 --force --keep-deployment-files
    test -x "$DIST_DIR/$app.dist/$executable" || {
        echo "pyside6-deploy did not produce $DIST_DIR/$app.dist/$executable" >&2
        exit 1
    }
done

cat > "$DIST_DIR/bots5" <<'SH'
#!/usr/bin/env sh
set -eu
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
exec "$ROOT/bots5.dist/entry_cli.bin" "$@"
SH
cat > "$DIST_DIR/bots5-desktop" <<'SH'
#!/usr/bin/env sh
set -eu
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
exec "$ROOT/bots5-desktop.dist/entry_desktop.bin" "$@"
SH
chmod 755 "$DIST_DIR/bots5" "$DIST_DIR/bots5-desktop"

if [[ "${BOTS5_BUILD_ONLY:-}" != "bots5-desktop" ]]; then
    test -x "$DIST_DIR/bots5.dist/entry_cli.bin"
fi
if [[ "${BOTS5_BUILD_ONLY:-}" != "bots5" ]]; then
    test -x "$DIST_DIR/bots5-desktop.dist/entry_desktop.bin"
    test -f "$DIST_DIR/bots5-desktop.dist/libbots5_rooted_sqlite_vfs.so"
    test -f "$DIST_DIR/bots5-desktop.dist/bots5/infrastructure/persistence/migrations/versions/0020_provider_managed_context.py"
fi
echo "Standalone payload: $DIST_DIR"
echo "Retained build workspace and Nuitka reports: $WORK_DIR"
