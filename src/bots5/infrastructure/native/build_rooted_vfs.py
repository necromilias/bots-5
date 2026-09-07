"""Build the narrow rooted SQLite VFS against the active Python SQLite library."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = Path(__file__).with_name("rooted_sqlite_vfs.c")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "cc",
        "-std=c11",
        "-O2",
        "-fPIC",
        "-Wall",
        "-Wextra",
        "-Werror",
        "-shared",
        str(source),
        "-Wl,-z,relro,-z,now",
        "-lsqlite3",
        "-ldl",
        "-lpthread",
        "-o",
        str(args.output),
    ]
    subprocess.run(command, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
