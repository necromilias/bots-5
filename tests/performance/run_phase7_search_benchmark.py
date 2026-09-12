#!/usr/bin/env python3
"""Opt-in, fixed-seed Phase 7 search benchmark.

This is evidence generation, not a pytest.  It deliberately uses the rooted
production store and VFS.  The corpus is synthetic and must not be presented
as an operator-workload measurement.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import random
import sqlite3
import subprocess
import tempfile
import threading
import time
from typing import Callable

from sqlalchemy import insert

from bots5.core.application import BotsApplication
from bots5.core.events import EventBus
from bots5.core.provider_configuration import ProviderConfiguration
from bots5.domain.models import MessageRole
from bots5.domain.search import SearchDocumentKind, SearchFilters, SearchIndexCondition
from bots5.infrastructure.data_root_authority import DataRootAuthority
from bots5.infrastructure.generation.fake import FakeStreamingBackend
from bots5.infrastructure.persistence.schema import chats
from bots5.infrastructure.persistence.search import SearchReceipt
from bots5.infrastructure.persistence.sqlite import SQLiteAppStateStore
from bots5.infrastructure.persistence.transition_guard import (
    arm_phase7_source_mutation,
    clear_phase7_source_mutation,
    require_phase7_consumed,
)


SEED = 0xB0750007
SMALL_CHAT_COUNT = 6
SMALL_TURNS_PER_CHAT = 3
SMALL_ATTACHMENT_COUNT = 4
BULK_CHAT_COUNT = 25_000
WARMUP_RUNS = 5
MEASURED_RUNS = 20
BASE_TIME = datetime(2026, 9, 11, 0, 0, 0, tzinfo=UTC)
REPO = Path(__file__).resolve().parents[2]
DEFAULT_EVIDENCE_DIR = REPO / "work/campaign-evidence/phase7/performance"
NATIVE_VFS = REPO / "build/native/libbots5_rooted_sqlite_vfs.so"


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _percentile(values: list[int], fraction: float) -> int:
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * fraction) - 1)
    return ordered[index]


def _latency_summary(values_ns: list[int]) -> dict[str, float | int]:
    if not values_ns:
        raise RuntimeError("latency sample is empty")
    return {
        "samples": len(values_ns),
        "p50_ms": round(_percentile(values_ns, 0.50) / 1_000_000, 3),
        "p95_ms": round(_percentile(values_ns, 0.95) / 1_000_000, 3),
        "max_ms": round(max(values_ns) / 1_000_000, 3),
    }


def _measure(call: Callable[[], object], *, require_results: bool = False) -> dict[str, float | int]:
    for _ in range(WARMUP_RUNS):
        value = call()
        if require_results and not getattr(value, "results", ()):
            raise RuntimeError("warmed search unexpectedly returned no results")
    samples: list[int] = []
    for _ in range(MEASURED_RUNS):
        started = time.perf_counter_ns()
        value = call()
        samples.append(time.perf_counter_ns() - started)
        if require_results and not getattr(value, "results", ()):
            raise RuntimeError("measured search unexpectedly returned no results")
    return _latency_summary(samples)


def _temporary_bytes(root: Path) -> int:
    total = 0
    for path in root.rglob("*"):
        try:
            relative = path.relative_to(root).as_posix()
            if not path.is_file():
                continue
            temporary = (
                relative.endswith(("-journal", "-wal", "-shm"))
                or relative.startswith("database/migration/")
                or relative.startswith("recovery/")
                or relative.startswith("attachments/captures/")
                or relative.startswith("attachments/staging/")
                or relative.startswith("attachments/gc/")
            )
            if temporary:
                total += path.stat().st_size
        except FileNotFoundError:
            continue
    return total


class _TemporarySampler:
    def __init__(self, root: Path):
        self.root = root
        self.peak = _temporary_bytes(root)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="phase7-temp-sampler", daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.peak = max(self.peak, _temporary_bytes(self.root))
            self._stop.wait(0.001)

    def __enter__(self) -> _TemporarySampler:
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._stop.set()
        self._thread.join()
        self.peak = max(self.peak, _temporary_bytes(self.root))


def _filesystem_description(path: Path) -> dict[str, object]:
    try:
        completed = subprocess.run(
            ["findmnt", "-no", "SOURCE,FSTYPE,OPTIONS", "--target", os.fspath(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        findmnt = completed.stdout.strip() if completed.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        findmnt = None
    stat = os.statvfs(path)
    return {
        "findmnt": findmnt,
        "block_size": stat.f_frsize,
        "total_bytes": stat.f_blocks * stat.f_frsize,
        "available_bytes_at_start": stat.f_bavail * stat.f_frsize,
    }


def _cpu_model() -> str | None:
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
            if line.casefold().startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or None


class _DeterministicIds:
    def __init__(self) -> None:
        self._next = 0

    def new(self) -> str:
        self._next += 1
        return f"benchmark-id-{self._next:010d}"


class _DeterministicClock:
    def __init__(self) -> None:
        self._next = 0

    def now(self) -> datetime:
        self._next += 1
        return BASE_TIME + timedelta(microseconds=self._next)

    @staticmethod
    def monotonic() -> float:
        return time.monotonic()


async def _finish(application: BotsApplication, attempt_id: str) -> None:
    task = application._generation_tasks.get(attempt_id)
    if task is not None:
        await task
    await asyncio.sleep(0)


async def _seed_corpus(
    store: SQLiteAppStateStore,
    source_dir: Path,
) -> tuple[list[int], list[object], list[str], BotsApplication]:
    rng = random.Random(SEED)
    vocabulary = (
        "aurora", "banyan", "cobalt", "delta", "ember", "fjord", "galaxy",
        "harbor", "indigo", "juniper", "keystone", "lantern", "meteor",
        "naive", "opal", "prairie", "quartz", "raven", "solstice", "tundra",
    )
    incremental_ns: list[int] = []
    original_accept = store._accept_search_receipt

    def timed_accept(receipt) -> None:
        started = time.perf_counter_ns()
        original_accept(receipt)
        incremental_ns.append(time.perf_counter_ns() - started)

    store._accept_search_receipt = timed_accept  # type: ignore[method-assign]
    ids = _DeterministicIds()
    clock = _DeterministicClock()
    application = BotsApplication(
        store,
        EventBus(clock, ids, queue_size=64),
        FakeStreamingBackend(),
        ids=ids,
        clock=clock,
        configuration=ProviderConfiguration(store, ids, clock),
    )
    attachments = []
    chat_ids: list[str] = []
    try:
        for chat_index in range(SMALL_CHAT_COUNT):
            chat = await application.create_chat(
                f"Atlas synthetic chat {chat_index:04d} archive navigation"
            )
            chat_ids.append(chat.id)
            if chat_index < SMALL_ATTACHMENT_COUNT:
                content = (
                    f"verifiedattachmenttoken atlas attachment {chat_index:04d} "
                    + " ".join(rng.choice(vocabulary) for _ in range(40))
                )
                source = source_dir / f"evidence_file_{chat_index:04d}.txt"
                source.write_text(content, encoding="utf-8")
                attachment = await application.attach_file(source)
                attachments.append(attachment)
                await application.stage_attachment(chat.id, attachment.id)
            for turn in range(SMALL_TURNS_PER_CHAT):
                selected = " ".join(rng.choice(vocabulary) for _ in range(24))
                attempt = await application.send_message(
                    chat.id,
                    f"atlas global user {chat_index:04d} turn {turn:02d} {selected}",
                )
                await _finish(application, attempt.id)
    finally:
        store._accept_search_receipt = original_accept  # type: ignore[method-assign]
    return incremental_ns, attachments, chat_ids, application


def _bulk_chat_fixture(
    authority: DataRootAuthority,
    store: SQLiteAppStateStore,
) -> dict[str, object]:
    """Add scale in one authoritative transaction, then deliver one receipt.

    This is deliberately test-fixture-only.  It holds normal application
    admission, uses the existing DataRootAuthority writer transition and
    rooted Engine, and lets the Phase 7 DML trigger atomically consume exactly
    one source revision for the whole executemany operation.
    """
    rows = []
    keys = []
    for index in range(BULK_CHAT_COUNT):
        chat_id = f"bulk-chat-{index:05d}"
        token_a = (SEED + index * 17) % 4096
        token_b = (SEED + index * 31) % 4096
        stamp = (
            BASE_TIME + timedelta(seconds=10_000 + index)
        ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        rows.append(
            {
                "id": chat_id,
                "title": (
                    f"Scalebeacon synthetic chat {index:05d} "
                    f"token{token_a:04d} token{token_b:04d}"
                ),
                "created_at": stamp,
                "updated_at": stamp,
                "head_message_id": None,
                "revision": 0,
                "archived_at": None,
            }
        )
        keys.append(f"chat:{chat_id}")

    before = store.search_status()
    business_started = time.perf_counter_ns()
    source_revision: int | None = None
    with store.command_admission():
        with authority.transition():
            with store._engine.begin() as connection:
                arm_phase7_source_mutation(connection, "phase7 benchmark bulk chats")
                try:
                    connection.execute(insert(chats), rows)
                    source_revision = require_phase7_consumed(connection)
                finally:
                    clear_phase7_source_mutation(connection)
            assert source_revision is not None
            store._record_committed_search_source_revision(source_revision)
    business_ns = time.perf_counter_ns() - business_started
    if source_revision != before.source_revision + 1:
        raise RuntimeError("bulk fixture did not consume exactly one source revision")

    stale = store.search_status()
    if (
        stale.condition is not SearchIndexCondition.STALE
        or stale.source_revision != source_revision
        or stale.checkpoint_revision != before.checkpoint_revision
    ):
        raise RuntimeError("bulk authoritative commit did not expose an exact stale gap")

    receipt_started = time.perf_counter_ns()
    with store.command_admission():
        store._accept_search_receipt(
            SearchReceipt(source_revision, frozenset(keys))
        )
    receipt_ns = time.perf_counter_ns() - receipt_started
    after = store.search_status()
    if (
        after.condition is not SearchIndexCondition.VALID
        or after.source_revision != source_revision
        or after.checkpoint_revision != source_revision
    ):
        raise RuntimeError("bulk derived receipt did not restore an exact valid checkpoint")
    return {
        "bulk_chat_count": BULK_CHAT_COUNT,
        "business_transaction_ms": round(business_ns / 1_000_000, 3),
        "derived_receipt_ms": round(receipt_ns / 1_000_000, 3),
        "source_revision_before": before.source_revision,
        "checkpoint_revision_before": before.checkpoint_revision,
        "source_revision_after_business_commit": stale.source_revision,
        "checkpoint_revision_after_business_commit": stale.checkpoint_revision,
        "source_revision_after_receipt": after.source_revision,
        "checkpoint_revision_after_receipt": after.checkpoint_revision,
    }


def _database_metrics(store: SQLiteAppStateStore, database: Path) -> dict[str, object]:
    with store.command_admission():
        with store._engine.connect() as connection:
            page_size = int(connection.exec_driver_sql("PRAGMA page_size").scalar_one())
            page_count = int(connection.exec_driver_sql("PRAGMA page_count").scalar_one())
            freelist_count = int(connection.exec_driver_sql("PRAGMA freelist_count").scalar_one())
            try:
                dbstat = {
                    str(row[0]): int(row[1])
                    for row in connection.exec_driver_sql(
                        "SELECT name, sum(pgsize) FROM dbstat GROUP BY name ORDER BY name"
                    ).fetchall()
                }
            except Exception:
                dbstat = {}
    derived_names = {
        name
        for name in dbstat
        if name.startswith("search_fts")
        or name.startswith("search_document_keys")
        or name == "search_index_state"
        or name.startswith("sqlite_autoindex_search_document_keys")
    }
    derived_bytes = sum(dbstat[name] for name in derived_names)
    allocated_bytes = page_size * page_count
    authoritative_plus_shared = max(0, allocated_bytes - derived_bytes)
    return {
        "file_bytes": database.stat().st_size,
        "page_size": page_size,
        "page_count": page_count,
        "freelist_pages": freelist_count,
        "allocated_bytes": allocated_bytes,
        "derived_search_page_bytes": derived_bytes if dbstat else None,
        "derived_to_total_ratio": (
            round(derived_bytes / allocated_bytes, 6) if dbstat and allocated_bytes else None
        ),
        "derived_to_authoritative_plus_shared_ratio": (
            round(derived_bytes / authoritative_plus_shared, 6)
            if dbstat and authoritative_plus_shared
            else None
        ),
        "derived_dbstat_objects": {
            name: dbstat[name] for name in sorted(derived_names)
        },
    }


def _corpus_metrics(store: SQLiteAppStateStore) -> dict[str, object]:
    with store.command_admission():
        with store._engine.connect() as connection:
            count = lambda table: int(
                connection.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar_one()
            )
            projections = store._all_search_projections(connection)
            searchable_bytes = sum(
                len(value.encode("utf-8"))
                for projection in projections
                for value in (projection.title, projection.body, projection.filename)
            )
            counts = {
                "chats": count("chats"),
                "messages": count("messages"),
                "message_revision_rows": int(
                    connection.exec_driver_sql(
                        "SELECT count(*) FROM messages WHERE revision >= 1"
                    ).scalar_one()
                ),
                "superseding_revision_rows": int(
                    connection.exec_driver_sql(
                        "SELECT count(*) FROM messages WHERE revision > 1"
                    ).scalar_one()
                ),
                "regeneration_siblings": int(
                    connection.exec_driver_sql(
                        "SELECT COALESCE(sum(lineage_count - 1), 0) FROM ("
                        "SELECT count(*) AS lineage_count FROM messages "
                        "WHERE role = 'assistant' "
                        "GROUP BY lineage_id HAVING count(*) > 1)"
                    ).scalar_one()
                ),
                "generation_attempts": count("generation_attempts"),
                "attachments": count("attachments"),
                "attachment_blobs": count("attachment_blobs"),
                "message_attachment_references": count("message_attachments"),
                "attempt_attachment_references": count("attempt_attachments"),
                "search_documents": count("search_document_keys"),
                "search_chat_documents": sum(p.document_kind == "chat" for p in projections),
                "search_message_documents": sum(p.document_kind == "message" for p in projections),
                "search_attachment_documents": sum(
                    p.document_kind == "attachment" for p in projections
                ),
            }
    return {"counts": counts, "searchable_utf8_bytes": searchable_bytes}


def _index_hash(store: SQLiteAppStateStore) -> str:
    digest = hashlib.sha256()
    with store.command_admission():
        with store._engine.connect() as connection:
            mapping_rows = connection.exec_driver_sql(
                "SELECT fts_rowid, document_kind, document_id "
                "FROM search_document_keys ORDER BY fts_rowid"
            ).fetchall()
            content_rows = connection.exec_driver_sql(
                "SELECT rowid, document_key, title, body, filename "
                "FROM search_fts ORDER BY rowid"
            ).fetchall()
    for group in (mapping_rows, content_rows):
        for row in group:
            digest.update(
                json.dumps(list(row), ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8"
                )
            )
            digest.update(b"\n")
    return digest.hexdigest()


def _render_markdown(evidence: dict[str, object]) -> str:
    corpus = evidence["corpus"]
    assert isinstance(corpus, dict)
    small = corpus["small"]
    large = corpus["large"]
    assert isinstance(small, dict) and isinstance(large, dict)
    counts = large["counts"]
    assert isinstance(counts, dict)
    queries = evidence["query_latency"]
    assert isinstance(queries, dict)
    rebuilds = evidence["rebuild"]
    assert isinstance(rebuilds, dict)
    lines = [
        "# Phase 7 synthetic search performance evidence",
        "",
        "> This is a fixed-seed synthetic corpus. It is not the operator's real workload.",
        "",
        f"- Seed: `{evidence['seed_hex']}`",
        f"- Large corpus: {counts['chats']} chats, {counts['messages']} messages, "
        f"{counts['attachments']} attachments, {counts['message_attachment_references']} message references",
        f"- Small-corpus searchable UTF-8 bytes: {small['searchable_utf8_bytes']}",
        f"- Large-corpus searchable UTF-8 bytes: {large['searchable_utf8_bytes']}",
        f"- Search documents: {counts['search_documents']}",
        f"- Incremental derived update: `{json.dumps(evidence['incremental_index_latency'], sort_keys=True)}`",
        f"- Bulk fixture: `{json.dumps(evidence['bulk_fixture'], sort_keys=True)}`",
        f"- Rebuild times: `{json.dumps(rebuilds['times_ms'])}` ms",
        f"- Rebuild content hashes: `{json.dumps(rebuilds['row_content_sha256'])}`",
        f"- Deterministic repeated rebuild: `{rebuilds['deterministic']}`",
        f"- Peak bounded temporary disk bytes during rebuilds: {rebuilds['peak_temporary_storage_bytes']}",
        "",
        "## Warmed query latency",
        "",
    ]
    for name, summary in queries.items():
        lines.append(f"- {name}: `{json.dumps(summary, sort_keys=True)}`")
    lines.extend(
        [
            "",
            "## Database/index size",
            "",
            f"```json\n{json.dumps(evidence['database_size'], indent=2, sort_keys=True)}\n```",
            "",
            "## Environment",
            "",
            f"```json\n{json.dumps(evidence['environment'], indent=2, sort_keys=True)}\n```",
            "",
        ]
    )
    return "\n".join(lines)


def run(evidence_dir: Path) -> dict[str, object]:
    benchmark_started = time.perf_counter_ns()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    if os.environ.get("PYTHONHASHSEED") != "0":
        raise RuntimeError("run with PYTHONHASHSEED=0 for deterministic evidence")
    filesystem = _filesystem_description(evidence_dir)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    tracked_patch = subprocess.run(
        ["git", "diff", "--binary"],
        cwd=REPO,
        check=True,
        capture_output=True,
        timeout=20,
    ).stdout
    launch_identity = {
        "head": head,
        "tracked_binary_patch_sha256": hashlib.sha256(tracked_patch).hexdigest(),
        "benchmark_script_sha256": _sha256(Path(__file__).resolve()),
        "search_mechanics_sha256": _sha256(
            REPO / "src/bots5/infrastructure/persistence/search.py"
        ),
        "sqlite_store_sha256": _sha256(
            REPO / "src/bots5/infrastructure/persistence/sqlite.py"
        ),
        "phase7_migration_sha256": _sha256(
            REPO
            / "src/bots5/infrastructure/persistence/migrations/versions/0010_phase7_search_navigation.py"
        ),
        "domain_search_sha256": _sha256(REPO / "src/bots5/domain/search.py"),
        "application_sha256": _sha256(REPO / "src/bots5/core/application.py"),
    }
    with tempfile.TemporaryDirectory(prefix="phase7-search-benchmark-", dir=evidence_dir) as raw:
        run_dir = Path(raw)
        root = run_dir / "data-root"
        source_dir = run_dir / "sources"
        source_dir.mkdir()
        authority = DataRootAuthority(root.absolute()).acquire()
        store = authority.open_store()
        database = root / "database/state.sqlite3"
        event_loop = asyncio.new_event_loop()
        application: BotsApplication | None = None
        try:
            initial_db = _database_metrics(store, database)
            initial_temp = _temporary_bytes(root)
            with _TemporarySampler(root) as small_sampler:
                (
                    incremental_ns,
                    attachments,
                    chat_ids,
                    application,
                ) = event_loop.run_until_complete(_seed_corpus(store, source_dir))
            status = store.search_status()
            if status.condition is not SearchIndexCondition.VALID:
                raise RuntimeError(f"incremental corpus left search {status.condition}")
            small_corpus = _corpus_metrics(store)
            post_small_db = _database_metrics(store, database)

            with _TemporarySampler(root) as bulk_sampler:
                bulk_fixture = _bulk_chat_fixture(authority, store)
            large_corpus = _corpus_metrics(store)
            post_bulk_db = _database_metrics(store, database)

            queries: dict[str, dict[str, float | int]] = {}
            queries["global"] = _measure(
                lambda: store.search("scalebeacon", limit=50), require_results=True
            )
            queries["in_chat"] = _measure(
                lambda: store.search(
                    "atlas", filters=SearchFilters(chat_id=chat_ids[0]), limit=50
                ),
                require_results=True,
            )
            queries["filtered_role_model"] = _measure(
                lambda: store.search(
                    "atlas",
                    filters=SearchFilters(
                        roles=(MessageRole.USER,), models=("fake-v0.1",)
                    ),
                    limit=50,
                ),
                require_results=True,
            )

            def two_pages():
                first = store.search("scalebeacon", limit=25)
                if first.next_cursor is None:
                    raise RuntimeError("pagination corpus unexpectedly fits on one page")
                second = store.search(
                    "scalebeacon", limit=25, cursor=first.next_cursor
                )
                if not second.results:
                    raise RuntimeError("second search page unexpectedly empty")
                return second

            queries["paginated_two_page"] = _measure(two_pages)
            queries["active_branch_only"] = _measure(
                lambda: store.search(
                    "atlas", filters=SearchFilters(active_branch_only=True), limit=50
                ),
                require_results=True,
            )
            queries["attachment_filename"] = _measure(
                lambda: store.search(
                    "evidence_file",
                    filters=SearchFilters(
                        document_kinds=(SearchDocumentKind.ATTACHMENT,)
                    ),
                    limit=50,
                ),
                require_results=True,
            )
            queries["attachment_verified_text"] = _measure(
                lambda: store.search(
                    "verifiedattachmenttoken",
                    filters=SearchFilters(
                        document_kinds=(SearchDocumentKind.ATTACHMENT,)
                    ),
                    limit=50,
                ),
                require_results=True,
            )

            rebuild_times: list[float] = []
            rebuild_hashes: list[str] = []
            peak_temp = max(initial_temp, small_sampler.peak, bulk_sampler.peak)
            rebuild_temp_peaks: list[int] = []
            for _ in range(3):
                with _TemporarySampler(root) as sampler:
                    started = time.perf_counter_ns()
                    rebuilt = store.rebuild_search_index()
                    elapsed = time.perf_counter_ns() - started
                if rebuilt.condition is not SearchIndexCondition.VALID:
                    raise RuntimeError(f"rebuild completed as {rebuilt.condition}")
                rebuild_times.append(round(elapsed / 1_000_000, 3))
                peak_temp = max(peak_temp, sampler.peak)
                rebuild_temp_peaks.append(sampler.peak)
                rebuild_hashes.append(_index_hash(store))
            if len(set(rebuild_hashes)) != 1:
                raise RuntimeError("repeated rebuild changed derived row/content hash")
            final_db = _database_metrics(store, database)
            final_status = asdict(store.search_status())
            final_status["condition"] = final_status["condition"].value
            attachment_ids = [item.id for item in attachments]
        finally:
            if application is not None:
                event_loop.run_until_complete(application.close())
            else:
                store.close()
            event_loop.close()

        result: dict[str, object] = {
            "evidence_kind": "fixed-seed synthetic; not operator workload",
            "seed": SEED,
            "seed_hex": hex(SEED),
            "exact_command": (
                "timeout --signal=TERM --kill-after=5s 300s env PYTHONHASHSEED=0 "
                "PYTHONPATH=src .venv314/bin/python "
                "tests/performance/run_phase7_search_benchmark.py --run "
                f"--evidence-dir {evidence_dir.relative_to(REPO).as_posix()}"
            ),
            "launch_candidate_identity": launch_identity,
            "benchmark_wall_seconds": round(
                (time.perf_counter_ns() - benchmark_started) / 1_000_000_000, 3
            ),
            "parameters": {
                "small_chat_count": SMALL_CHAT_COUNT,
                "small_turns_per_chat": SMALL_TURNS_PER_CHAT,
                "small_attachment_count": SMALL_ATTACHMENT_COUNT,
                "bulk_chat_count": BULK_CHAT_COUNT,
                "warmup_runs_per_query": WARMUP_RUNS,
                "measured_runs_per_query": MEASURED_RUNS,
            },
            "corpus": {"small": small_corpus, "large": large_corpus},
            "bulk_fixture": bulk_fixture,
            "attachment_ids_sha256": hashlib.sha256(
                "\n".join(attachment_ids).encode("utf-8")
            ).hexdigest(),
            "incremental_index_latency": _latency_summary(incremental_ns),
            "incremental_index_definition": (
                "receipt acceptance through derived transaction/checkpoint; excludes the "
                "authoritative business transaction and includes attachment verification "
                "when an affected attachment projection is recomputed"
            ),
            "query_latency": queries,
            "rebuild": {
                "times_ms": rebuild_times,
                "p50_ms": round(_percentile([int(v * 1_000_000) for v in rebuild_times], 0.5) / 1_000_000, 3),
                "max_ms": max(rebuild_times),
                "row_content_sha256": rebuild_hashes,
                "deterministic": True,
                "peak_temporary_storage_bytes": peak_temp,
                "temporary_storage_peaks_bytes": {
                    "initial": initial_temp,
                    "small_application_corpus": small_sampler.peak,
                    "bulk_business_and_receipt": bulk_sampler.peak,
                    "rebuilds": rebuild_temp_peaks,
                },
                "temporary_storage_definition": (
                    "peak sampled bytes in SQLite journal/WAL/SHM, migration/recovery, "
                    "and attachment capture/staging/GC areas during each synchronous rebuild"
                ),
            },
            "database_size": {
                "empty_migrated": initial_db,
                "post_small_application_corpus": post_small_db,
                "post_bulk_receipt": post_bulk_db,
                "post_three_rebuilds": final_db,
            },
            "final_search_status": final_status,
            "environment": {
                "python": platform.python_version(),
                "python_implementation": platform.python_implementation(),
                "sqlite": sqlite3.sqlite_version,
                "platform": platform.platform(),
                "cpu_model": _cpu_model(),
                "logical_cpu_count": os.cpu_count(),
                "pythonhashseed": os.environ.get("PYTHONHASHSEED"),
                "filesystem": filesystem,
                "native_rooted_vfs_path": os.fspath(NATIVE_VFS),
                "native_rooted_vfs_sha256": _sha256(NATIVE_VFS),
            },
        }
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run",
        action="store_true",
        help="explicitly run the benchmark; otherwise no corpus is created",
    )
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=DEFAULT_EVIDENCE_DIR,
        help="directory for JSON and Markdown evidence",
    )
    args = parser.parse_args()
    if not args.run:
        parser.error("benchmark is opt-in; pass --run")
    result = run(args.evidence_dir.resolve())
    json_path = args.evidence_dir / "phase7_search_benchmark.json"
    markdown_path = args.evidence_dir / "phase7_search_benchmark.md"
    encoded = json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    json_path.write_text(encoded, encoding="utf-8")
    markdown_path.write_text(_render_markdown(result), encoding="utf-8")
    print(encoded, end="")
    print(f"evidence_json={json_path}")
    print(f"evidence_markdown={markdown_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
