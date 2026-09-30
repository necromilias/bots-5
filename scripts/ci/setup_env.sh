#!/usr/bin/env bash
#
# Construct the CI v1 test environment on a standard GitHub-hosted Ubuntu
# runner (or any Debian/Ubuntu host).  This is deliberately boring:
#
#   * uses the *system* Python (default /usr/bin/python3) for the authoritative
#     environment, because only the OS Python links the shared system
#     libsqlite3 that the native rooted VFS requires -- python-build-standalone
#     interpreters embed their own SQLite and cannot host the VFS;
#   * constructs the exact host filesystem properties the authoritative tests
#     assert (a real btrfs build/ directory, /dev/shm is already tmpfs);
#   * provides the second, embedded-SQLite interpreter that one test requires,
#     as a python-build-standalone CPython (the shape the canonical .venv has);
#     the actions/setup-python toolcache build on this image links the system
#     libsqlite3 and therefore does not have that property;
#   * installs the project editable plus its declared dev extras;
#   * builds the native rooted VFS from repository source;
#   * prints the real Python and SQLite versions so no job can imply a runtime
#     it did not actually use.
#
# No model-provider credentials are read, written or required.
#
# Environment knobs (all optional):
#   BOTS5_CI_PYTHON        system interpreter (default /usr/bin/python3)
#   BOTS5_CI_VENV          authoritative venv (default .venv-ci)
#   BOTS5_CI_BTRFS_BUILD   "1" to mount btrfs at build/
#   BOTS5_CI_STATIC_VENV   "1" to create the embedded-SQLite .venv
#   BOTS5_CI_STATIC_PYTHON explicit standalone interpreter for .venv (default:
#                          resolve a python-build-standalone CPython through uv)
#   BOTS5_CI_STANDALONE_MINOR  CPython minor for that interpreter (default: the
#                          CI venv's own minor)
set -euo pipefail

PYTHON_BIN="${BOTS5_CI_PYTHON:-/usr/bin/python3}"
VENV_DIR="${BOTS5_CI_VENV:-.venv-ci}"
CACHE_DIR="${BOTS5_CI_PIP_CACHE:-${HOME}/.cache/pip}"

missing=()
for pkg in \
  python3-venv python3-dev libsqlite3-dev gcc \
  libegl1 libgl1 libxkbcommon-x11-0 libdbus-1-3 libfontconfig1; do
  if ! dpkg -s "${pkg}" >/dev/null 2>&1; then
    missing+=("${pkg}")
  fi
done
if [ "${#missing[@]}" -gt 0 ]; then
  sudo apt-get update -qq
  sudo apt-get install -y -qq "${missing[@]}"
fi

if [ "${BOTS5_CI_BTRFS_BUILD:-0}" = "1" ]; then
  bash scripts/ci/mount_btrfs_build.sh build
fi

export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_CACHE_DIR="${CACHE_DIR}"

rm -rf "${VENV_DIR}"
"${PYTHON_BIN}" -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/python" -m pip install --quiet --upgrade pip
"${VENV_DIR}/bin/python" -m pip install --quiet -e '.[dev]'

"${VENV_DIR}/bin/python" -m bots5.infrastructure.native.build_rooted_vfs \
  --output build/native/libbots5_rooted_sqlite_vfs.so

if [ "${BOTS5_CI_STATIC_VENV:-0}" = "1" ]; then
  bash scripts/ci/make_static_venv.sh
  # tests/test_phase6_context_attachments.py:3337 spawns exactly this path.
  if [ ! -x "${BOTS5_CI_STATIC_VENV_DIR:-.venv}/bin/python" ]; then
    echo "::error::static interpreter missing at ${BOTS5_CI_STATIC_VENV_DIR:-.venv}/bin/python" >&2
    exit 1
  fi
fi

"${VENV_DIR}/bin/python" - <<'PY'
import sqlite3
import sys

import _sqlite3

print(f"ci_python={sys.version.split()[0]}")
print(f"ci_python_executable={sys.executable}")
print(f"ci_sqlite={sqlite3.sqlite_version}")
print(f"ci_sqlite_module={getattr(_sqlite3, '__file__', None)}")
PY
