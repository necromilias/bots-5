#!/usr/bin/env bash
#
# Mount a real btrfs filesystem at the repository's build/ directory.
#
# Two authoritative Phase 6 tests assert `findmnt -T build -no FSTYPE` is
# exactly "btrfs" (tests/test_phase6_context_attachments.py:532 and :3404).
# GitHub-hosted runners use ext4/overlay, so CI v1 constructs a genuine btrfs
# loopback filesystem for the authoritative lane rather than weakening or
# skipping those tests.
#
# Fail-closed: if the filesystem cannot be created or mounted, this script
# exits non-zero.  It never substitutes a different filesystem or a skip.
set -euo pipefail

BUILD_DIR="${1:-build}"
IMAGE="${BOTS5_CI_BTRFS_IMAGE:-/tmp/t4-build-btrfs.img}"
SIZE="${BOTS5_CI_BTRFS_SIZE:-8G}"

mkdir -p "${BUILD_DIR}"

if findmnt -T "${BUILD_DIR}" -no FSTYPE 2>/dev/null | grep -qx btrfs; then
  echo "build directory is already btrfs: ${BUILD_DIR}"
  exit 0
fi

if ! command -v mkfs.btrfs >/dev/null 2>&1; then
  sudo apt-get update -qq
  sudo apt-get install -y -qq btrfs-progs
fi

if [ ! -f "${IMAGE}" ]; then
  truncate -s "${SIZE}" "${IMAGE}"
  mkfs.btrfs -q -f "${IMAGE}"
fi

sudo mount -o loop "${IMAGE}" "${BUILD_DIR}"
sudo chown "$(id -u):$(id -g)" "${BUILD_DIR}"

actual="$(findmnt -T "${BUILD_DIR}" -no FSTYPE)"
echo "build filesystem: ${actual}"
if [ "${actual}" != "btrfs" ]; then
  echo "::error::failed to construct a btrfs build filesystem (got ${actual})" >&2
  exit 1
fi
