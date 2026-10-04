#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
EVIDENCE_DIR="${BOTS5_M7_EVIDENCE_DIR:-$ROOT/work/m7-validation/$(date -u +%Y%m%d-%H%M%S)}"
PACKAGING_PYTHON="${BOTS5_PACKAGING_ENV:-$ROOT/build/m7-venv}/bin/python"
DIST_DIR="${BOTS5_STANDALONE_OUT:-$ROOT/dist/bots5-linux-standalone}"
mkdir -p "$EVIDENCE_DIR/validation"

printf 'command: %q\n' "$ROOT/scripts/build/build_standalone_linux.sh" > "$EVIDENCE_DIR/validation/build.log"
set +e
BOTS5_PACKAGING_ENV="$(dirname -- "$(dirname -- "$PACKAGING_PYTHON")")" \
    "$ROOT/scripts/build/build_standalone_linux.sh" 2>&1 | tee -a "$EVIDENCE_DIR/validation/build.log"
build_status=${PIPESTATUS[0]}
set -e

"$PACKAGING_PYTHON" - "$EVIDENCE_DIR/validation/BUILD_RESULT.json" "$build_status" "$DIST_DIR" <<'PY'
import json
import os
import platform
import sys
from pathlib import Path

path, status, dist = Path(sys.argv[1]), int(sys.argv[2]), Path(sys.argv[3])
path.write_text(json.dumps({
    "command": "scripts/build/build_standalone_linux.sh",
    "exit_status": status,
    "artifact": str(dist.resolve()),
    "python": sys.version,
    "platform": platform.platform(),
    "environment_is_virtualenv": sys.prefix != sys.base_prefix,
}, indent=2) + "\n", encoding="utf-8")
PY
if [[ "$build_status" -ne 0 ]]; then
    exit "$build_status"
fi

export BOTS5_STANDALONE_ROOT="$DIST_DIR"
export BOTS5_M7_BUILD_RESULT="$EVIDENCE_DIR/validation/BUILD_RESULT.json"
export BOTS5_M7_EVIDENCE_DIR="$EVIDENCE_DIR"
export PYTHONPATH="$ROOT/src"
export QT_QPA_PLATFORM=offscreen

"$PACKAGING_PYTHON" - "$EVIDENCE_DIR/validation/TARGET_HOST.json" <<'PY'
import _sqlite3
import ctypes.util
import json
import platform
import sqlite3
import subprocess
import sys
from importlib.metadata import distributions
from pathlib import Path

command = ["busctl", "--user", "call", "org.freedesktop.DBus", "/org/freedesktop/DBus",
           "org.freedesktop.DBus", "NameHasOwner", "s", "org.freedesktop.secrets"]
try:
    probe = subprocess.run(command, text=True, capture_output=True, timeout=5)
    service = {"command": command, "exit_status": probe.returncode,
               "stdout": probe.stdout, "stderr": probe.stderr}
except (OSError, subprocess.TimeoutExpired) as error:
    service = {"command": command, "unverified": str(error)}
Path(sys.argv[1]).write_text(json.dumps({
    "os_release": Path("/etc/os-release").read_text(),
    "kernel": platform.release(), "architecture": platform.machine(),
    "glibc": platform.libc_ver(), "python": sys.version,
    "sqlite": sqlite3.sqlite_version, "sqlite_extension": _sqlite3.__file__,
    "shared_sqlite": ctypes.util.find_library("sqlite3"),
    "secret_service": service,
    "packages": {d.metadata["Name"]: d.version for d in distributions()},
}, indent=2) + "\n")
PY

set +e
(cd "$ROOT" && "$PACKAGING_PYTHON" -m pytest --collect-only tests/test_phase11_packaging.py -p no:cacheprovider) \
    2>&1 | tee "$EVIDENCE_DIR/validation/T0_COLLECTION.log"
collect_status=${PIPESTATUS[0]}
set -e
if [[ "$collect_status" -ne 0 ]]; then exit "$collect_status"; fi

set +e
(cd "$ROOT" && "$PACKAGING_PYTHON" -m pytest tests/test_phase11_packaging.py -p no:cacheprovider -vv -s) \
    2>&1 | tee "$EVIDENCE_DIR/validation/T0_PACKAGING.log"
t0_status=${PIPESTATUS[0]}
set -e
if [[ "$t0_status" -ne 0 ]]; then exit "$t0_status"; fi

run_targeted() {
    local level="$1"
    shift
    set +e
    (cd "$ROOT" && "$PACKAGING_PYTHON" -m pytest "$@" -p no:cacheprovider -vv) \
        2>&1 | tee "$EVIDENCE_DIR/validation/$level.log"
    local status=${PIPESTATUS[0]}
    set -e
    return "$status"
}
# Wave 5 dispatch impact: frozen/source bootstrap, restore UI/close, restore
# admission and cross-cutting lock/lifecycle. Unchanged VFS/migration source
# consumers retain wave 4 evidence; the new artifact's resource proof runs above.
run_targeted T0_RESTORE \
    tests/test_phase11_packaging_restore.py
run_targeted T1_RESTORE_CLOSE \
    tests/test_phase9_desktop_slice_e.py tests/test_phase10_desktop_lifecycle.py
run_targeted T2_PERSISTENCE \
    tests/test_phase9_restore.py
run_targeted T3_CONTRACTS \
    tests/test_phase1_paths_lock.py tests/test_phase10_cross_cutting.py

echo "M7 T0 and affected T1/T2/T3 validation passed. Evidence: $EVIDENCE_DIR/validation"
