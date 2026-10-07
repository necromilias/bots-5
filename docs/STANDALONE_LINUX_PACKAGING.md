# Standalone Linux packaging

The Phase 11 canonical package is a Nuitka standalone directory built through
`pyside6-deploy`. This path packages both declared entry points from
`pyproject.toml`: `bots5` and `bots5-desktop`.

## Build

On the current supported build target, install the system compiler/linker
toolchain and a Python interpreter within the project's `>=3.12,<3.15` range
whose `_sqlite3` extension shares the host SQLite library, then run:

```sh
scripts/build/build_standalone_linux.sh
```

The script creates an isolated virtual environment at `build/m7-venv` when one
does not exist, installs the project's declared runtime/test dependencies
using `scripts/build/standalone_linux_constraints.txt` plus `Nuitka==4.2`
and `patchelf`, freezes CPython UTF-8 mode via `PYTHONUTF8=1` during the
build, builds the rooted SQLite VFS, and writes the
standalone directory to `dist/bots5-linux-standalone/`. Set
`BOTS5_PACKAGING_PYTHON`, `BOTS5_PACKAGING_ENV`, or `BOTS5_STANDALONE_OUT` to
select the interpreter, isolated environment, or output directory.
The output override must remain a direct child of this checkout's `dist/`.
The build rejects interpreters with embedded private SQLite. It uses a new
entrypoint workspace for every build so stale generated modules cannot
shadow the application package; build workspaces and Nuitka reports are retained.
Nuitka captures the build interpreter's UTF-8 mode in the generated executable.
The build asserts this mode rather than relying on the operator's runtime locale.
The generated entrypoints also decode argv filesystem bytes as UTF-8 before
argparse or Qt consumes them: frozen startup can otherwise leave locale-decoded
surrogate escapes in argv under the `C` locale. The upstream directory repair,
frozen UTF-8 mode, and argv normalization are qualified together.

The output contains launchers `bots5` and `bots5-desktop`, with each Nuitka
standalone payload in its corresponding `*.dist/` directory. The desktop
payload includes the filesystem Alembic migration tree and native rooted VFS
library. The VFS locator resolves this library relative to the standalone
executable, so launch does not depend on the checkout, current directory, or
`BOTS5_ROOTED_VFS_LIBRARY`.

## Validate

Run the M7 selector and directly affected subsystem tests with:

```sh
scripts/ci/validate_standalone_build.sh
```

This builds the payload, collects all six M7 IDs, and runs T0 packaging tests.
It additionally runs source/frozen restore dispatch integration, affected T1
restore UI/close lifecycle, T2 restore/bootstrap, and T3 data-root lock and
cross-cutting close contracts. The broader unchanged VFS/migration source
coverage remains retained in the prior M7 evidence; resource loading is proved
again against each rebuilt artifact.
It does not run T4. It also runs `tests/test_standalone_unicode_paths.py` against
relocated ASCII, accented and CJK/home-like payload paths, renamed executable
files, `C.UTF-8` and `C` locales, and frozen restore re-entry under Unicode
ancestors. These checks require successful resource loading and desktop
initialization, not just a help response. The T0 desktop checks use an offscreen Qt platform,
a disposable external `--data-root` and default XDG startup, clean XDG paths,
and an environment without `PYTHONPATH`, `VIRTUAL_ENV`, provider variables, or
credentials. Validation records are written under
`work/m7-validation/<UTC timestamp>/` unless `BOTS5_M7_EVIDENCE_DIR` selects
a campaign evidence directory. The packaged resource probe validates actual
native VFS/shared-SQLite loading, migration paths, SVG resources, and stylesheet
application. Standalone tests are selected by this runner and skip in generic
pytest when no `BOTS5_STANDALONE_ROOT` artifact is supplied. They must all run
without skips in the M7 validation checkpoint.

`tests/test_phase11_packaging_restore.py` verifies the existing source
`python -m bots5.bootstrap.desktop` child route and Nuitka executable re-entry.
Frozen re-entry uses `/proc/self/exe`, the actual Linux executable image;
Nuitka's `sys.executable` may instead identify a nonexistent bundle `python`.
Its frozen checks use a relocated, read-only payload and disposable backup/data
roots. A finite diagnostic compiled from `tests/_standalone_restore_probe.py`
is selected only by `BOTS5_PACKAGING_CHECK=restore-handoff` with a test-generated
`BOTS5_PACKAGING_RESTORE_CONFIG`. It drives the real restore dialog/close/child
path, observes fresh authority acquisition after close and pre-store child
admission, prohibits parent restore/child store opening, and checks exact
success/refusal/fail-closed results. It never substitutes a mock child. Normal
launch and production restore dispatch do not depend on diagnostic variables.
The accepted parent remains alive to display the waited child's result after
its live desktop session/store/authority are closed.

The build and runtime evidence applies to the actual host that produced it.
The first validated target is the current Forge/Arch Linux host. It does not
claim compatibility with Ubuntu/Debian, older glibc environments, or Linux
distributions generally. AppImage remains a separate, conditional milestone;
Flatpak and Snap are outside this packaging path.
