#!/usr/bin/env bash
#
# Provide the second interpreter that one authoritative test requires.
#
# tests/test_phase6_context_attachments.py:3337 spawns REPO/.venv/bin/python and
# asserts that it *fails closed* when asked to register the rooted VFS, because
# that interpreter embeds a private SQLite.  This is a real property of the
# canonical development environment (a python-build-standalone / uv-managed
# interpreter) and CI v1 reproduces it rather than skipping the test.
#
# The interpreter is a *python-build-standalone* CPython (the shape uv installs
# and the shape the canonical development `.venv` has): only such a build embeds
# its own SQLite, which is the property the test asserts.  Its site-packages is
# linked to the CI environment's site-packages through a .pth file (same CPython
# minor version), so no second full dependency download is needed.  The project
# itself is imported through PYTHONPATH=src by the test, exactly as the test does
# today.
#
# Why not actions/setup-python: on the ubuntu-26.04 image that toolcache build
# links the *system* libsqlite3 (it reports the image's SQLite version), so the
# rooted VFS registers successfully there and the test premise does not hold.
# Callers may still name an explicit interpreter with BOTS5_CI_STATIC_PYTHON; the
# default resolves one from uv.
#
# Fail-closed: a missing standalone interpreter or an interpreter whose SQLite
# is *not* embedded aborts the build here, loudly.
set -euo pipefail

VENV_DIR="${BOTS5_CI_STATIC_VENV_DIR:-.venv}"
PYTHON_BIN="${BOTS5_CI_STATIC_PYTHON:-}"
CI_VENV_DIR="${BOTS5_CI_VENV:-.venv-ci}"

# Guard against the historical variable-name collision: the enable flag is
# BOTS5_CI_STATIC_VENV=1 (boolean), the path is BOTS5_CI_STATIC_VENV_DIR.  A
# bare "1" here means a caller conflated them and would build ./1, which is not
# the interpreter tests/test_phase6_context_attachments.py:3337 spawns.
case "${VENV_DIR}" in
  1|0|true|false|yes|no|on|off)
    echo "::error::BOTS5_CI_STATIC_VENV_DIR looks like a boolean ('${VENV_DIR}'); the enable flag is BOTS5_CI_STATIC_VENV and the path is BOTS5_CI_STATIC_VENV_DIR" >&2
    exit 1
    ;;
esac

if [ ! -x "${CI_VENV_DIR}/bin/python" ]; then
  echo "::error::${CI_VENV_DIR} must exist before building ${VENV_DIR}" >&2
  exit 1
fi

# Resolve a python-build-standalone interpreter unless the caller named one.
# "python" is the historical default and cannot be trusted to be standalone, so
# it is treated exactly like "unset".
STANDALONE_MINOR="${BOTS5_CI_STANDALONE_MINOR:-}"
if [ -z "${STANDALONE_MINOR}" ]; then
  STANDALONE_MINOR="$("${CI_VENV_DIR}/bin/python" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
fi

if [ -z "${PYTHON_BIN}" ] || [ "${PYTHON_BIN}" = "python" ]; then
  UV_BIN="$(command -v uv 2>/dev/null || true)"
  if [ -z "${UV_BIN}" ] && [ -x "${CI_VENV_DIR}/bin/uv" ]; then
    UV_BIN="${CI_VENV_DIR}/bin/uv"
  fi
  if [ -z "${UV_BIN}" ]; then
    echo "installing uv to obtain a python-build-standalone CPython ${STANDALONE_MINOR}" >&2
    "${CI_VENV_DIR}/bin/python" -m pip install --quiet uv
    UV_BIN="${CI_VENV_DIR}/bin/uv"
  fi
  if [ ! -x "${UV_BIN}" ]; then
    echo "::error::uv is unavailable, so no python-build-standalone interpreter can be resolved; set BOTS5_CI_STATIC_PYTHON to one explicitly" >&2
    exit 1
  fi
  rm -rf "${VENV_DIR}"
  # uv creates the venv from a managed (python-build-standalone) CPython and
  # downloads it on demand; only-managed keeps it from reusing a system build.
  "${UV_BIN}" venv --python-preference only-managed --python "${STANDALONE_MINOR}" "${VENV_DIR}"
  PYTHON_BIN="${VENV_DIR}/bin/python"
else
  rm -rf "${VENV_DIR}"
  "${PYTHON_BIN}" -m venv "${VENV_DIR}" 2>/dev/null \
    || "${PYTHON_BIN}" -m venv --without-pip "${VENV_DIR}"
fi

PY_VERSION="$("${VENV_DIR}/bin/python" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
CI_SITE="$("${CI_VENV_DIR}/bin/python" -c 'import site; print(site.getsitepackages()[0])')"
TARGET_SITE="${VENV_DIR}/lib/python${PY_VERSION}/site-packages"
if [ ! -d "${TARGET_SITE}" ]; then
  TARGET_SITE="$("${VENV_DIR}/bin/python" -c 'import site; print(site.getsitepackages()[0])')"
fi
printf '%s\n' "${CI_SITE}" > "${TARGET_SITE}/_bots5_ci_shared_deps.pth"

# Prove the interpreter is *actually* the one the authoritative test needs: it
# must fail closed when asked to register the rooted VFS, with an error naming
# SQLite and either the shared-instance or rooted-VFS condition.  This mirrors
# tests/test_phase6_context_attachments.py:3337 exactly rather than asserting a
# stricter proxy for it.
PYTHONPATH=src "${VENV_DIR}/bin/python" - <<'PY'
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import _sqlite3
import sqlalchemy  # noqa: F401

from bots5.infrastructure.data_root_authority import DataRootAuthority

print(f"static_venv_python={sys.version.split()[0]}")
print(f"static_venv_sqlite={sqlite3.sqlite_version}")
print(f"static_venv_sqlite_module={getattr(_sqlite3, '__file__', None)}")

root = Path(tempfile.mkdtemp(prefix="bots5-static-check-")) / "root"
try:
    authority = DataRootAuthority(os.fspath(root)).acquire()
    authority.open_store()
except Exception as exc:
    message = f"{type(exc).__name__}: {exc}"
    print(f"static_venv_expected_failure={message}")
    if "SQLite" not in message or not ("instance" in message or "rooted" in message):
        raise SystemExit(f"unexpected failure mode for embedded-SQLite interpreter: {message}")
else:
    authority.close()
    raise SystemExit(
        "interpreter registered the rooted VFS; it does not embed a private "
        "SQLite, so the static-VFS test would not be honest"
    )
PY

test -x "${VENV_DIR}/bin/python" || {
  echo "::error::static interpreter missing at ${VENV_DIR}/bin/python" >&2
  exit 1
}
if [ "${VENV_DIR}" != ".venv" ]; then
  echo "::warning::static interpreter is at ${VENV_DIR}, but tests/test_phase6_context_attachments.py:3337 spawns .venv/bin/python" >&2
fi
echo "static_venv_ready=${VENV_DIR}/bin/python"
