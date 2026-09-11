"""Run isolated deliberately-bad authority/effect variants.

The live checkout is never rewritten.  Each variant receives a private copy of
``src`` and must make its release-critical oracle selection fail.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Mutant:
    name: str
    relative_path: str
    replacements: tuple[tuple[str, str], ...]
    tests: tuple[str, ...]
    expected_failure: str | None = None


MUTANTS = (
    Mutant(
        "F1_immediate_failed_closed",
        "bots5/infrastructure/data_root_authority.py",
        (
            (
                "        self._publish_requested_poison_locked()\n"
                "        self._condition.notify_all()\n",
                "        self._state = target\n"
                "        self._condition.notify_all()\n",
            ),
        ),
        (
            "tests/test_authority_effect_grants.py::test_f1_terminal_request_waits_for_unrelated_public_command_effect",
            "tests/test_authority_effect_grants.py::test_f1_failed_enumeration_close_sibling_also_waits_for_owner",
        ),
    ),
    Mutant(
        "F2_remove_direct_store_grant",
        "bots5/infrastructure/persistence/sqlite.py",
        (
            (
                "        try:\n"
                "            with self._authority.operation():\n"
                "                return method(self, *args, **kwargs)\n"
                "        except AuthorityError as exc:\n"
                "            raise StateError(\n"
                "                \"state store is not admitting work; fresh-authority recovery is required\"\n"
                "            ) from exc\n",
                "        return method(self, *args, **kwargs)\n",
            ),
        ),
        (
            "tests/test_authority_effect_grants.py::test_f2_direct_store_grant_survives_insert_commit_and_close",
        ),
    ),
    Mutant(
        "F2_admit_after_dbapi_creation",
        "bots5/infrastructure/rooted_sqlite_vfs.py",
        (
            (
                "        if self._authority is not None:\n"
                "            lease = self._authority._acquire_database_resource(self._resource_key)\n"
                "        generation: int | None = None\n",
                "        generation: int | None = None\n",
            ),
            (
                "            if lease is not None:\n"
                "                connection._bots5_bind(self, lease, generation)\n",
                "            if self._authority is not None:\n"
                "                lease = self._authority._acquire_database_resource(self._resource_key)\n"
                "            if lease is not None:\n"
                "                connection._bots5_bind(self, lease, generation)\n",
            ),
        ),
        (
            "tests/test_authority_effect_grants.py::test_f2_invalidation_rejects_before_creator_and_private_checkout",
        ),
    ),
    Mutant(
        "F3_global_ready_only_reentry",
        "bots5/infrastructure/data_root_authority.py",
        (
            (
                "                if grant.status is not _GrantStatus.FORWARD:\n"
                "                    raise AuthorityError(\"effect grant has been revoked\")\n",
                "                # MUTANT: ambient identity is treated as sufficient.\n",
            ),
            (
                "                if current.status is not _GrantStatus.FORWARD:\n"
                "                    raise AuthorityError(\"application effect grant has been revoked\")\n"
                "                grant = current\n",
                "                grant = current\n",
            ),
        ),
        (
            "tests/test_authority_effect_grants.py::test_f3_self_failed_grant_cannot_reenter_while_caught",
        ),
    ),
    Mutant(
        "EF1_remove_cleanup_failure_poison",
        "bots5/infrastructure/persistence/sqlite.py",
        (
            (
                "            except AttachmentCleanupUncertain as exc:\n"
                "                self._poison_attachment_lifecycle(\n"
                "                    \"capture cleanup durability is uncertain\"\n"
                "                )\n"
                "                raise StateError(\n",
                "            except AttachmentCleanupUncertain as exc:\n"
                "                raise StateError(\n",
            ),
        ),
        (
            "tests/test_phase6_context_attachments.py::test_ef1_capture_cleanup_contract_on_tmpfs",
        ),
    ),
    Mutant(
        "resource_close_twice_with_swallowed_error",
        "bots5/infrastructure/persistence/sqlite.py",
        (
            (
                "        except BaseException as exc:\n"
                "            self._poison_attachment_lifecycle(\n"
                "                f\"{operation} database connection close outcome uncertain\"\n"
                "            )\n",
                "        except BaseException as exc:\n"
                "            try:\n"
                "                connection.close()\n"
                "            except BaseException:\n"
                "                pass\n"
                "            self._poison_attachment_lifecycle(\n"
                "                f\"{operation} database connection close outcome uncertain\"\n"
                "            )\n",
            ),
        ),
        (
            "tests/test_phase6_context_attachments.py::test_n1_t2_close_post_real_is_poisoned_and_restart_reconciles",
        ),
    ),
    Mutant(
        "event_delivery_detached_from_grant",
        "bots5/core/events.py",
        (
            (
                "                        await subscription._deliver(event)\n",
                "                        asyncio.create_task(subscription._deliver(event))\n",
            ),
        ),
        (
            "tests/test_authority_effect_grants.py::test_real_full_queue_blocks_delivery_until_owner_settles",
        ),
    ),
    Mutant(
        "terminal_cleanup_opens_fresh_database",
        "bots5/infrastructure/persistence/sqlite.py",
        (
            (
                "        try:\n"
                "            self._attachment_manager.close()\n"
                "        finally:\n"
                "            self._engine.dispose()\n\n"
                "    def close(self) -> None:\n",
                "        try:\n"
                "            with self._engine.connect():\n"
                "                pass\n"
                "            self._attachment_manager.close()\n"
                "        finally:\n"
                "            self._engine.dispose()\n\n"
                "    def close(self) -> None:\n",
            ),
        ),
        (
            "tests/test_authority_effect_grants.py::test_invalidated_close_performs_no_new_database_creation",
        ),
    ),
    Mutant(
        "native_unknown_handoff_suppressed",
        "bots5/infrastructure/rooted_sqlite_vfs.py",
        (
            (
                "        if current != generation and self._authority is not None:\n",
                "        if False and current != generation and self._authority is not None:\n",
            ),
        ),
        (
            "tests/test_authority_effect_grants.py::test_thread_attributed_native_unknown_handoff_revokes_before_return",
        ),
    ),
    Mutant(
        "I21_required_payload_invalidation_removed",
        "bots5/infrastructure/attachments.py",
        (
            (
                "        except AttachmentIntegrityError:\n"
                "            # ``read_verified`` is the canonical-object boundary: its digest\n"
                "            # and size are durable authority-owned facts supplied by the\n"
                "            # store, not untrusted request validation.  Once those bytes are\n"
                "            # absent, unsafe, or different, the discovering grant may unwind\n"
                "            # but must never be reused for forward work.\n"
                "            self._authority.poison(\n"
                "                \"required attachment payload integrity failure\"\n"
                "            )\n"
                "            raise\n",
                "        except AttachmentIntegrityError:\n"
                "            raise\n",
            ),
        ),
        (
            "tests/test_authority_effect_grants.py::test_required_attachment_payload_integrity_revokes_future_writes",
        ),
    ),
    Mutant(
        "I21_E13_representation_invalidation_removed",
        "bots5/infrastructure/persistence/sqlite.py",
        (
            (
                "            self._authority.poison(\n"
                "                \"required attachment text representation integrity failure\"\n"
                "            )\n",
                "            # MUTANT: preserve every local error but bypass authority invalidation.\n",
            ),
        ),
        (
            "tests/test_authority_effect_grants.py::test_authoritative_representation_corruption_matrix_revokes_owner_and_future_work",
        ),
    ),
    Mutant(
        "I21_E13_NUL_text_eligibility_weakened",
        "bots5/infrastructure/attachments.py",
        (
            (
                "    if \"\\x00\" in text:\n",
                "    if False and \"\\x00\" in text:\n",
            ),
        ),
        (
            "tests/test_authority_effect_grants.py::test_authoritative_representation_corruption_matrix_revokes_owner_and_future_work[nul_claimed_text]",
        ),
    ),
    Mutant(
        "B1_convenience_cursor_bypass",
        "bots5/infrastructure/rooted_sqlite_vfs.py",
        (
            (
                "    def execute(self, *args, **kwargs):\n"
                "        return self._bots5_cursor_execute(\"execute\", *args, **kwargs)\n",
                "    def execute(self, *args, **kwargs):\n"
                "        self._bots5_before_effect()\n"
                "        try:\n"
                "            return super().execute(*args, **kwargs)\n"
                "        finally:\n"
                "            self._bots5_handoff(\"connection execute\")\n",
            ),
        ),
        (
            "tests/test_authority_effect_grants.py::test_b1_rooted_connection_convenience_methods_return_guarded_cursors",
        ),
        "assert isinstance(created, _RootedCursor)",
    ),
    Mutant(
        "B1_cursor_revalidation_bypass",
        "bots5/infrastructure/rooted_sqlite_vfs.py",
        (
            (
                "        connection._bots5_before_effect()\n",
                "        # MUTANT: retained cursors bypass their owning grant.\n",
            ),
        ),
        (
            "tests/test_authority_effect_grants.py::test_b1_post_revocation_convenience_cursor_rejects_forward_work",
            "tests/test_authority_effect_grants.py::test_b1_cursor_stepping_rejects_before_sqlite_after_revocation",
        ),
        "revoked cursor",
    ),
    Mutant(
        "B1_parent_release_before_child_drain",
        "bots5/infrastructure/rooted_sqlite_vfs.py",
        (
            (
                "            self._bots5_drain_cursors()\n",
                "            # MUTANT: release the parent while retained children remain live.\n"
                "            pass\n",
            ),
        ),
        (
            "tests/test_authority_effect_grants.py::test_b1_rooted_child_cursor_lifetime_matches_parent_lease[connection_first]",
        ),
        "assert cursor._bots5_closed is True",
    ),
    Mutant(
        "B1_child_cursor_tracking_removed",
        "bots5/infrastructure/rooted_sqlite_vfs.py",
        (
            (
                "        self._bots5_live_cursors[id(cursor)] = cursor\n",
                "        # MUTANT: the parent does not retain its child resource.\n",
            ),
        ),
        (
            "tests/test_authority_effect_grants.py::test_b1_rooted_child_cursor_lifetime_matches_parent_lease[connection_first]",
        ),
        "assert len(connection._bots5_live_cursors) == 1",
    ),
)


def apply_mutant(source_root: Path, mutant: Mutant) -> None:
    target = source_root / mutant.relative_path
    value = target.read_text(encoding="utf-8")
    for old, new in mutant.replacements:
        count = value.count(old)
        if count != 1:
            raise RuntimeError(
                f"{mutant.name}: expected one mutation site in {mutant.relative_path}, got {count}"
            )
        value = value.replace(old, new, 1)
    target.write_text(value, encoding="utf-8")


def compact_result(completed: subprocess.CompletedProcess[str]) -> str:
    lines = (completed.stdout + completed.stderr).splitlines()
    summaries = [
        line.strip()
        for line in lines
        if " failed" in line or " passed" in line or " error" in line.lower()
    ]
    return summaries[-1] if summaries else f"exit {completed.returncode}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "mutants",
        nargs="*",
        help="optional exact mutant names; omission runs the complete matrix",
    )
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        help="optional directory for complete per-mutant pytest transcripts",
    )
    args = parser.parse_args()
    requested = set(args.mutants)
    known = {mutant.name for mutant in MUTANTS}
    unknown = sorted(requested - known)
    if unknown:
        parser.error("unknown mutant(s): " + ", ".join(unknown))
    selected = tuple(
        mutant for mutant in MUTANTS if not requested or mutant.name in requested
    )

    failures = []
    with tempfile.TemporaryDirectory(prefix="bots5-authority-mutants-") as value:
        parent = Path(value)
        for mutant in selected:
            variant = parent / mutant.name
            source_root = variant / "src"
            shutil.copytree(REPO / "src", source_root)
            native_target = variant / "build" / "native"
            native_target.mkdir(parents=True)
            native_library = native_target / "libbots5_rooted_sqlite_vfs.so"
            environment = dict(os.environ)
            environment["PYTHONPATH"] = os.fspath(source_root)
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            native_build = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "bots5.infrastructure.native.build_rooted_vfs",
                    "--output",
                    os.fspath(native_library),
                ],
                cwd=variant,
                env=environment,
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            if native_build.returncode != 0 or not native_library.is_file():
                print(f"{mutant.name}: SETUP FAILED; native rebuild failed", flush=True)
                failures.append(mutant.name)
                continue
            native_sha256 = hashlib.sha256(native_library.read_bytes()).hexdigest()
            environment["BOTS5_ROOTED_VFS_LIBRARY"] = os.fspath(native_library)
            apply_mutant(source_root, mutant)
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-o",
                    "addopts=",
                    "-p",
                    "no:cacheprovider",
                    "-q",
                    *mutant.tests,
                ],
                cwd=REPO,
                env=environment,
                text=True,
                capture_output=True,
                timeout=180,
                check=False,
            )
            transcript = completed.stdout + completed.stderr
            if args.evidence_dir is not None:
                args.evidence_dir.mkdir(parents=True, exist_ok=True)
                (args.evidence_dir / f"{mutant.name}.log").write_text(
                    transcript,
                    encoding="utf-8",
                )
            killed = (
                completed.returncode == 1
                and " failed" in transcript
                and "ERROR" not in transcript
                and (
                    mutant.expected_failure is None
                    or mutant.expected_failure in transcript
                )
            )
            print(
                f"{mutant.name}: {'KILLED' if killed else 'SURVIVED'}; "
                f"native={native_sha256}; {compact_result(completed)}",
                flush=True,
            )
            if not killed:
                failures.append(mutant.name)
    if failures:
        print("SURVIVING MUTANTS: " + ", ".join(failures), flush=True)
        return 1
    print(f"ALL {len(selected)} SELECTED MUTANTS KILLED", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
