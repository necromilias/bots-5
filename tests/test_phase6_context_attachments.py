"""Accepted-contract Phase 6 authority, attachment, and migration tests.

The production DataRootAuthority is the only store/migration entry point used
here. Path compatibility stores, public Engines, storage paths, and bearer
reservations are deliberately absent from the Phase 6 contract.
"""

from __future__ import annotations

import asyncio
import errno
import fcntl
import gc
import hashlib
import inspect
import json
import os
import sqlite3
import socket
import subprocess
import sys
import threading
import time
import tomllib
import tempfile
import weakref
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DatabaseError as SqlAlchemyDatabaseError
from uuid6 import uuid7

from bots5.core.application import BotsApplication, GenerationMode
from bots5.core.context import ContextBuildError, ContextBuilder, ContextSource
from bots5.core.errors import AuthorityError, StateError
from bots5.core.events import EventBus
from bots5.core.generation import GenerationCompleted, GenerationDelta, GenerationDispatched
from bots5.core.provider_configuration import ProviderConfiguration
from bots5.domain.clock import SystemClock
from bots5.domain.ids import Uuid7Factory
from bots5.domain.models import Chat
from bots5.infrastructure import data_root_authority as authority_module
from bots5.infrastructure.app_paths import resolve_app_paths
from bots5.infrastructure.attachments import (
    AttachmentIntegrityError,
    _AttachmentFS,
    digest_from_text,
)
from bots5.infrastructure.data_root_authority import AuthorityState, DataRootAuthority
from bots5.infrastructure.generation.fake import FakeStreamingBackend
from bots5.infrastructure.persistence.migration_runner import upgrade_database
from bots5.infrastructure.persistence.phase6_schema import (
    REQUIRED_INDEXES,
    REQUIRED_TABLES,
    REQUIRED_TRIGGERS,
    _normalise_sql,
)
from bots5.infrastructure.persistence.sqlite import SQLiteAppStateStore
from bots5.infrastructure.persistence.transition_guard import (
    arm_phase6_attachment_insert,
    arm_phase6_blob_transition,
    clear_phase6,
    require_phase6_consumed,
)
from tests._authority_test_support import upgrade_to


REPO = Path(__file__).resolve().parents[1]
HEAD = "0009_phase6_context_attachments"
PRIOR_REVISIONS = (
    "0001_desktop_state",
    "0002_conversation_lineage",
    "0003_integrity_boundaries",
    "0004_integrity_guard_function",
    "0005_generation_outcomes",
    "0006_phase4_workspace",
    "0007_phase5_provider_model_configuration",
    "0008_catalogue_refresh_outcomes",
)
ALL_REVISIONS = PRIOR_REVISIONS + (HEAD,)
PRIOR_MIGRATION_SHA256 = {
    "0001_desktop_state.py": "15b7a409d35e3313db288f201e67d2e23c0a89e6f90058ad367dc879034e2da1",
    "0002_conversation_lineage.py": "7ddd512e48e75518c2730b0881a49e0f639fcf6d378e6e6a68b937da2f157c74",
    "0003_integrity_boundaries.py": "2058cbaf6ed630d354d126edb58307b54ec9f2518b4d50810d4ced3025af4cd4",
    "0004_integrity_guard_function.py": "3d7187d735fd3caa6fbabcc622b47dde5209403bab886b971bf63a007437b253",
    "0005_generation_outcomes.py": "2afd16e2f7d21f27cb27aea87d7cbb135c37892900de5745eb8ae1c0dacd41c6",
    "0006_phase4_workspace.py": "b670b4d26f11e22c94bf30283ab840512d261b07fba7ae7bd92ce27ff1a821a4",
    "0007_phase5_provider_model_configuration.py": "46bc12a9cd10b4c6a262b5bc5ca0a02ecc5d60201931a45dffe7fef3cf69eea6",
    "0008_catalogue_refresh_outcomes.py": "de23ea29c3c9749adb7ef01ce5d8a4ce34425798972524d743d2b5152aac47b9",
}
JOURNAL_EDGES = (
    "before-journal",
    "after-journal-create",
    "after-journal-write",
    "after-journal-file-fsync",
    "before-journal-rename",
    "after-journal-rename",
    "after-journal-directory-fsync",
)
EXISTING_PHASES = (
    "PREPARING",
    "SOURCE_QUIESCED",
    "BACKUP_VERIFIED",
    "MIGRATING_COPY",
    "CANDIDATE_VALIDATED",
    "SWITCH_INTENT",
    "PROMOTED",
    "COMMITTED",
)
ABSENT_PHASES = (
    "ABSENT",
    "MIGRATING_COPY",
    "CANDIDATE_VALIDATED",
    "SWITCH_INTENT",
    "PROMOTED",
    "COMMITTED",
)


class ExactAdapter:
    adapter_id = "test.exact"
    adapter_version = "1"
    semantics = "one UTF-8 code point per accounting unit"

    def encode(self, messages, envelope):
        del envelope
        return "\n".join(
            f"{item['role']}:{item['content']}" for item in messages
        ).encode()

    def measure(self, wire_representation):
        return len(wire_representation.decode())


class RecordingBackend(FakeStreamingBackend):
    def __init__(self):
        super().__init__()
        self.requests = []

    async def stream(self, request):
        self.requests.append(request)
        async for item in super().stream(request):
            yield item


class LegacyRecordingBackend:
    backend_id = "openai_compatible_http"
    provider_id = "local_openai"
    base_url = "http://127.0.0.1:9000/v1"
    api_key_env = None

    def __init__(self):
        self.requests = []

    async def stream(self, request):
        self.requests.append(request)
        yield GenerationDispatched(request.attempt_id)
        yield GenerationDelta(request.attempt_id, "legacy response")
        yield GenerationCompleted(request.attempt_id)


def _new_authority(root: Path) -> DataRootAuthority:
    return DataRootAuthority(root.absolute()).acquire()


def _open_store(root: Path):
    authority = _new_authority(root)
    try:
        return authority, authority.open_store()
    except BaseException:
        authority.close()
        raise


@contextmanager
def _private_engine_connection(store, *, transaction: bool = False):
    """Give intentional test-only raw Engine work an explicit effect grant."""
    with store.command_admission():
        scope = store._engine.begin() if transaction else store._engine.connect()
        with scope as connection:
            yield connection


def _arm_selected_connection_close(monkeypatch, *, post_real: bool):
    """Inject one close result only on the selected explicit transaction."""
    selected: dict[str, object] = {"connection": None, "close_calls": 0}
    original_exec_driver_sql = Connection.exec_driver_sql
    original_close = Connection.close

    def select_transaction(self, statement, *args, **kwargs):
        result = original_exec_driver_sql(self, statement, *args, **kwargs)
        if (
            selected["connection"] is None
            and str(statement).strip().upper() == "BEGIN IMMEDIATE"
        ):
            selected["connection"] = self
        return result

    def fail_selected_close(self, *args, **kwargs):
        if self is selected["connection"]:
            selected["close_calls"] += 1
            if selected["close_calls"] == 1:
                if post_real:
                    original_close(self, *args, **kwargs)
                raise OSError("injected attachment connection close uncertainty")
        return original_close(self, *args, **kwargs)

    monkeypatch.setattr(Connection, "exec_driver_sql", select_transaction)
    monkeypatch.setattr(Connection, "close", fail_selected_close)
    return selected


def _raw_attachment_lifecycle(root: Path):
    database = root / "database" / "state.sqlite3"
    with sqlite3.connect(database) as connection:
        blobs = connection.execute(
            "SELECT state, operation_id, stage_name, gc_id FROM attachment_blobs"
        ).fetchall()
        attachments = connection.execute(
            "SELECT id, blob_digest FROM attachments ORDER BY id"
        ).fetchall()
    areas = {
        area: tuple(sorted(item.name for item in (root / "attachments" / area).iterdir()))
        for area in ("captures", "staging", "objects", "gc")
    }
    return blobs, attachments, areas


def _tree_fingerprint(root: Path):
    """Capture every observable regular-file/tree entry without store APIs."""
    entries = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            entries.append(("symlink", relative, path.readlink().as_posix()))
        elif path.is_dir():
            entries.append(("directory", relative))
        elif path.is_file():
            entries.append(("file", relative, hashlib.sha256(path.read_bytes()).hexdigest()))
        else:
            entries.append(("other", relative))
    return tuple(entries)


def _run_pre_release_t2_child(
    root: Path, source_path: Path
) -> subprocess.CompletedProcess[str]:
    source = r'''
import os
import sys
from sqlalchemy.engine import Connection
from bots5.core.errors import StateError
from bots5.infrastructure.data_root_authority import AuthorityState, DataRootAuthority

root, source_path = sys.argv[1:]
authority = DataRootAuthority(root).acquire()
store = authority.open_store()
selected = {"connection": None, "close_calls": 0}
original_exec_driver_sql = Connection.exec_driver_sql
original_close = Connection.close

def select_transaction(self, statement, *args, **kwargs):
    result = original_exec_driver_sql(self, statement, *args, **kwargs)
    if selected["connection"] is None and str(statement).strip().upper() == "BEGIN IMMEDIATE":
        selected["connection"] = self
    return result

def fail_selected_close(self, *args, **kwargs):
    if self is selected["connection"]:
        selected["close_calls"] += 1
        if selected["close_calls"] == 1:
            raise OSError("injected pre-release attachment connection close uncertainty")
    return original_close(self, *args, **kwargs)

Connection.exec_driver_sql = select_transaction
Connection.close = fail_selected_close
try:
    store.ingest_attachment(source_path)
except StateError as exc:
    assert "connection close is uncertain" in str(exc)
    assert isinstance(exc.__cause__, OSError)
else:
    os._exit(10)
assert selected["connection"] is not None
assert selected["close_calls"] == 1
assert store._poisoned is True
# The unresolved checked-out DB resource is itself a drain obligation.  New
# admission is closed immediately, but terminal publication cannot overtake
# that resource; process exit is the only honest release for this pre-real
# close injection.
assert authority.state is AuthorityState.READY
assert authority.poison_pending is True
assert authority._checked_out_connections == 1
try:
    store.assert_admitting()
except StateError as exc:
    assert "not admitting work" in str(exc)
else:
    os._exit(11)
os._exit(0)
'''
    return subprocess.run(
        [sys.executable, "-c", source, os.fspath(root), os.fspath(source_path)],
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": os.fspath(REPO / "src")},
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _assert_recovered_t2(root: Path, expected_digest: str) -> None:
    authority, store = _open_store(root)
    try:
        assert authority.state is AuthorityState.READY
        blobs, attachments, areas = _raw_attachment_lifecycle(root)
        assert blobs == [("ready", None, None, None)]
        assert attachments == []
        assert areas == {
            "captures": (),
            "staging": (),
            "objects": (expected_digest,),
            "gc": (),
        }
    finally:
        if authority.state is not AuthorityState.CLOSED:
            authority.close()


def _await_state(authority: DataRootAuthority, expected: AuthorityState) -> None:
    deadline = time.monotonic() + 5
    while authority.state is not expected and time.monotonic() < deadline:
        time.sleep(0.001)
    assert authority.state is expected


def _await_condition_waiters(authority: DataRootAuthority, expected: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with authority._condition:
            if len(authority._condition._waiters) >= expected:
                return
        time.sleep(0.001)
    with authority._condition:
        assert len(authority._condition._waiters) >= expected


def _configured_application(root: Path, backend=None):
    authority, store = _open_store(root)
    ids = Uuid7Factory()
    clock = SystemClock()
    application = BotsApplication(
        store,
        EventBus(clock, ids, queue_size=64),
        backend or FakeStreamingBackend(),
        ids=ids,
        clock=clock,
        configuration=ProviderConfiguration(store, ids, clock),
    )
    return application, store, authority


def _assert_ef1_capture_cleanup_contract(base: Path) -> None:
    clean_root = base / "clean-root"
    clean_source = base / "clean-source.txt"
    clean_source.write_bytes(b"EF1 clean cleanup control")
    clean_authority, clean_store = _open_store(clean_root)
    clean_captures_fd = clean_store._attachment_manager._captures_fd
    original_fsync = os.fsync
    clean_sync_calls = 0

    def fail_first_capture_sync(fd):
        nonlocal clean_sync_calls
        if fd == clean_captures_fd:
            clean_sync_calls += 1
            if clean_sync_calls == 1:
                raise OSError("injected capture publication barrier failure")
        return original_fsync(fd)

    os.fsync = fail_first_capture_sync
    try:
        with pytest.raises(OSError, match="publication barrier failure"):
            clean_store.ingest_attachment(clean_source)
    finally:
        os.fsync = original_fsync
    try:
        assert clean_sync_calls == 2
        assert clean_authority.state is AuthorityState.READY
        assert clean_store._poisoned is False
        clean_store.assert_admitting()
        assert _raw_attachment_lifecycle(clean_root) == (
            [],
            [],
            {"captures": (), "staging": (), "objects": (), "gc": ()},
        )
    finally:
        if clean_authority.state is not AuthorityState.CLOSED:
            clean_authority.close()

    failed_root = base / "failed-root"
    failed_source = base / "failed-source.txt"
    failed_source.write_bytes(b"EF1 failed cleanup durability")
    failed_authority, failed_store = _open_store(failed_root)
    ids = Uuid7Factory()
    clock = SystemClock()
    events = EventBus(clock, ids, queue_size=16)
    application = BotsApplication(
        failed_store,
        events,
        FakeStreamingBackend(),
        ids=ids,
        clock=clock,
    )
    failed_captures_fd = failed_store._attachment_manager._captures_fd
    failed_sync_calls = 0

    def fail_publication_and_cleanup_sync(fd):
        nonlocal failed_sync_calls
        if fd == failed_captures_fd:
            failed_sync_calls += 1
            if failed_sync_calls <= 2:
                raise OSError(
                    f"injected capture namespace barrier failure {failed_sync_calls}"
                )
        return original_fsync(fd)

    os.fsync = fail_publication_and_cleanup_sync
    try:
        with pytest.raises(
            StateError, match="capture cleanup durability is uncertain"
        ) as raised:
            failed_store.ingest_attachment(failed_source)
    finally:
        os.fsync = original_fsync
    assert isinstance(raised.value.__cause__, AttachmentIntegrityError)
    assert isinstance(raised.value.__cause__.__cause__, OSError)
    assert failed_sync_calls == 2
    assert failed_authority.state is AuthorityState.POISONED
    assert failed_store._poisoned is True
    assert _raw_attachment_lifecycle(failed_root) == (
        [],
        [],
        {"captures": (), "staging": (), "objects": (), "gc": ()},
    )
    before_tree = _tree_fingerprint(failed_root)
    before_events = events._sequence
    with pytest.raises(StateError, match="not admitting work"):
        failed_store.assert_admitting()
    with pytest.raises(StateError, match="not admitting work"):
        failed_store.create_chat(
            Chat(
                str(uuid7()),
                "EF1 unrelated store write",
                datetime.now(UTC),
                datetime.now(UTC),
            )
        )
    with pytest.raises(StateError, match="not admitting work"):
        asyncio.run(application.create_chat("EF1 unrelated application write"))
    with pytest.raises(AuthorityError, match="operation rejected"):
        failed_authority.database_durability_fence()
    assert failed_authority.state is AuthorityState.POISONED
    assert events._sequence == before_events
    assert _tree_fingerprint(failed_root) == before_tree
    assert _raw_attachment_lifecycle(failed_root)[0:2] == ([], [])

    failed_authority.close()
    assert failed_authority.state is AuthorityState.CLOSED
    recovered_authority, recovered = _open_store(failed_root)
    try:
        assert recovered_authority.state is AuthorityState.READY
        assert _raw_attachment_lifecycle(failed_root) == (
            [],
            [],
            {"captures": (), "staging": (), "objects": (), "gc": ()},
        )
        recovered.assert_admitting()
        recovered.create_chat(
            Chat(
                str(uuid7()),
                "EF1 admitted only after recovery",
                datetime.now(UTC),
                datetime.now(UTC),
            )
        )
        with sqlite3.connect(
            failed_root / "database" / "state.sqlite3"
        ) as connection:
            assert connection.execute("SELECT count(*) FROM chats").fetchone() == (1,)
    finally:
        if recovered_authority.state is not AuthorityState.CLOSED:
            recovered_authority.close()


def test_ef1_capture_cleanup_contract_on_tmpfs(tmp_path: Path):
    filesystem = subprocess.run(
        ["findmnt", "-T", os.fspath(tmp_path), "-no", "FSTYPE"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert filesystem == "tmpfs"
    _assert_ef1_capture_cleanup_contract(tmp_path)


def test_ef1_capture_cleanup_contract_on_btrfs():
    build = REPO / "build"
    build.mkdir(exist_ok=True)
    filesystem = subprocess.run(
        ["findmnt", "-T", os.fspath(build), "-no", "FSTYPE"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert filesystem == "btrfs"
    with tempfile.TemporaryDirectory(prefix="phase6-ef1-btrfs-", dir=build) as value:
        _assert_ef1_capture_cleanup_contract(Path(value))


def test_ef2_authority_only_poison_closes_application_admission_and_recovers(
    tmp_path: Path,
):
    root = tmp_path / "root"
    source = tmp_path / "source.txt"
    source.write_bytes(b"EF2 authority-only poison")
    child = r'''
import asyncio
import json
import os
import sqlite3
import sys
from datetime import UTC, datetime
from uuid6 import uuid7
from bots5.core.application import BotsApplication
from bots5.core.errors import AuthorityError, StateError
from bots5.core.events import EventBus
from bots5.domain.clock import SystemClock
from bots5.domain.ids import Uuid7Factory
from bots5.domain.models import Chat
from bots5.infrastructure.data_root_authority import AuthorityState, DataRootAuthority
from bots5.infrastructure.generation.fake import FakeStreamingBackend

async def scenario():
    root, source = sys.argv[1:]
    authority = DataRootAuthority(root).acquire()
    store = authority.open_store()
    attachment = store.ingest_attachment(source)
    ids = Uuid7Factory()
    clock = SystemClock()
    events = EventBus(clock, ids, queue_size=16)
    application = BotsApplication(
        store, events, FakeStreamingBackend(), ids=ids, clock=clock
    )
    chat = await application.create_chat('EF2 durable chat')
    await application.stage_attachment(chat.id, attachment.id)
    selection = application._pending_attachment_ids[chat.id]
    event_count = events._sequence
    cancel_state = set(application._cancel_requested)
    original_release = authority._release_scoped_fd
    selected = {'calls': 0, 'claim': None}

    def ambiguous_release(claim):
        if claim.label == 'scoped:attachment:artifact-proof' and not selected['calls']:
            selected['calls'] += 1
            selected['claim'] = claim
            original_release(claim)
            claim.status = 'UNKNOWN'
            raise AuthorityError('injected post-real descriptor close ambiguity')
        original_release(claim)

    authority._release_scoped_fd = ambiguous_release
    try:
        store.read_attachment_bytes(attachment.id)
    except StateError as exc:
        assert 'descriptor close outcome is uncertain' in str(exc)
    else:
        os._exit(20)
    assert selected['calls'] == 1
    assert selected['claim'].fd is None
    assert selected['claim'].status == 'UNKNOWN'
    assert ('scoped:attachment:artifact-proof', 'UNKNOWN') in authority.claim_inventory
    assert authority.state is AuthorityState.POISONED
    assert store._poisoned is False

    for operation in (
        lambda: application.unstage_attachment(chat.id, attachment.id),
        lambda: application.stage_attachment(chat.id, attachment.id),
        lambda: application.cancel_generation('missing-attempt'),
        lambda: application.create_chat('EF2 blocked chat'),
    ):
        try:
            await operation()
        except StateError as exc:
            assert 'not admitting work' in str(exc)
        else:
            os._exit(21)
    try:
        store.assert_admitting()
    except StateError as exc:
        assert 'not admitting work' in str(exc)
    else:
        os._exit(22)
    assert application._pending_attachment_ids[chat.id] == selection
    assert application._cancel_requested == cancel_state
    assert events._sequence == event_count
    try:
        store.create_chat(
            Chat(str(uuid7()), 'EF2 lower-gate write', datetime.now(UTC), datetime.now(UTC))
        )
    except StateError as exc:
        assert 'not admitting work' in str(exc)
    else:
        os._exit(23)
    with sqlite3.connect(os.path.join(root, 'database', 'state.sqlite3')) as connection:
        assert connection.execute('SELECT count(*) FROM chats').fetchone() == (1,)
        assert connection.execute('SELECT state FROM attachment_blobs').fetchall() == [('ready',)]
        assert connection.execute('SELECT count(*) FROM attachments').fetchone() == (1,)
    assert selected['calls'] == 1
    try:
        authority.close()
    except AuthorityError:
        pass
    else:
        os._exit(24)
    assert authority.state is AuthorityState.FAILED_CLOSED
    assert selected['calls'] == 1
    payload = {
        'attachment_id': attachment.id,
        'digest': attachment.blob_digest,
        'chat_id': chat.id,
        'unknown_claim': selected['claim'].label,
    }
    sys.stdout.write(json.dumps(payload))
    sys.stdout.flush()
    os._exit(0)

asyncio.run(scenario())
'''
    completed = subprocess.run(
        [sys.executable, "-c", child, os.fspath(root), os.fspath(source)],
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": os.fspath(REPO / "src")},
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    evidence = json.loads(completed.stdout)
    assert evidence["unknown_claim"] == "scoped:attachment:artifact-proof"
    with sqlite3.connect(root / "database" / "state.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM chats").fetchone() == (1,)
        assert connection.execute("SELECT state FROM attachment_blobs").fetchall() == [
            ("ready",)
        ]
        assert connection.execute("SELECT count(*) FROM attachments").fetchone() == (1,)
    assert tuple((root / "attachments" / "captures").iterdir()) == ()
    assert tuple((root / "attachments" / "staging").iterdir()) == ()
    assert tuple(path.name for path in (root / "attachments" / "objects").iterdir()) == (
        evidence["digest"],
    )

    authority, store = _open_store(root)
    try:
        assert authority.state is AuthorityState.READY
        assert all(status != "UNKNOWN" for _, status in authority.claim_inventory)
        assert store.read_attachment_bytes(evidence["attachment_id"]) == source.read_bytes()
        store.assert_admitting()
        ids = Uuid7Factory()
        clock = SystemClock()
        events = EventBus(clock, ids, queue_size=16)
        application = BotsApplication(
            store,
            events,
            FakeStreamingBackend(),
            ids=ids,
            clock=clock,
        )
        assert asyncio.run(
            application.stage_attachment(
                evidence["chat_id"], evidence["attachment_id"]
            )
        ) == (evidence["attachment_id"],)
        assert asyncio.run(
            application.unstage_attachment(
                evidence["chat_id"], evidence["attachment_id"]
            )
        ) == ()
    finally:
        if authority.state is not AuthorityState.CLOSED:
            authority.close()


def _run_ef2_concurrent_admission_child(
    root_parent: Path,
    *,
    ordering: str,
    repetitions: int = 1,
) -> list[dict[str, object]]:
    child = r'''
import asyncio
import json
import os
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from uuid6 import uuid7
from bots5.core.application import BotsApplication
from bots5.core.errors import AuthorityError, StateError
from bots5.core.events import EventBus
from bots5.domain.clock import SystemClock
from bots5.domain.ids import Uuid7Factory
from bots5.domain.models import Chat
from bots5.infrastructure.data_root_authority import AuthorityState, DataRootAuthority
from bots5.infrastructure.generation.fake import FakeStreamingBackend

async def one(parent, index, ordering):
    base = parent / str(index)
    base.mkdir()
    root = base / 'root'
    first_source = base / 'first.txt'
    second_source = base / 'second.txt'
    first_source.write_bytes(b'EF2 concurrent first')
    second_source.write_bytes(b'EF2 concurrent second')
    authority = DataRootAuthority(root.absolute()).acquire()
    store = authority.open_store()
    first = store.ingest_attachment(first_source)
    second = store.ingest_attachment(second_source)
    ids = Uuid7Factory()
    clock = SystemClock()
    events = EventBus(clock, ids, queue_size=16)
    application = BotsApplication(
        store, events, FakeStreamingBackend(), ids=ids, clock=clock
    )
    chat = await application.create_chat('EF2 concurrent')
    await application.stage_attachment(chat.id, first.id)
    pending_before = application._pending_attachment_ids[chat.id]
    cancel_before = set(application._cancel_requested)
    event_before = events._sequence
    subscriptions_before = len(events._subscriptions)
    original_release = authority._release_scoped_fd
    selected = {'calls': 0, 'claim': None}

    def ambiguous_release(claim):
        if claim.label == 'scoped:attachment:artifact-proof' and not selected['calls']:
            selected['calls'] += 1
            selected['claim'] = claim
            original_release(claim)
            claim.status = 'UNKNOWN'
            raise AuthorityError('injected post-real descriptor close ambiguity')
        original_release(claim)

    authority._release_scoped_fd = ambiguous_release
    poison_errors = []

    def cause_ambiguous_close():
        try:
            store.read_attachment_bytes(first.id)
        except StateError as exc:
            poison_errors.append(str(exc))
        else:
            raise AssertionError('ambiguous descriptor close unexpectedly succeeded')

    if ordering == 'command-wins':
        subscription = application.subscribe()
        await events.publish('ef2_backpressure_barrier')
        event_before = events._sequence
        admitted = threading.Event()
        release_command = threading.Event()
        poison_finished = threading.Event()
        original_assert = store.assert_admitting
        admission_calls = 0
        mid = {}

        def coordinated_assert():
            nonlocal admission_calls
            original_assert()
            admission_calls += 1
            if admission_calls == 2:
                mid['at_admission'] = {
                    'authority_state': authority.state.value,
                    'poison_pending': authority.poison_pending,
                    'lease_owned': authority._has_application_admission(),
                    'pending': application._pending_attachment_ids[chat.id],
                    'event_sequence': events._sequence,
                }
                admitted.set()
                if not release_command.wait(15):
                    raise AssertionError('command release barrier timed out')

        store.assert_admitting = coordinated_assert

        async def release_event_backpressure():
            while events._sequence == event_before:
                await asyncio.sleep(0)
            mid['during_event_backpressure'] = {
                'authority_state': authority.state.value,
                'poison_pending': authority.poison_pending,
                'pending': application._pending_attachment_ids.get(chat.id, ()),
                'event_sequence': events._sequence,
            }
            await subscription.__anext__()
            subscription.close()

        def poisoner():
            if not admitted.wait(15):
                poison_errors.append('command admission barrier timed out')
                release_command.set()
                return
            cause_ambiguous_close()
            mid['after_close_before_release'] = {
                'authority_state': authority.state.value,
                'poison_pending': authority.poison_pending,
                'pending': application._pending_attachment_ids[chat.id],
                'event_sequence': events._sequence,
            }
            poison_finished.set()
            release_command.set()

        thread = threading.Thread(target=poisoner, name='ef2-poisoner')
        thread.start()
        backpressure = asyncio.create_task(release_event_backpressure())
        command_exception = None
        try:
            command_result = await application.unstage_attachment(chat.id, first.id)
        except Exception as exc:
            command_result = None
            command_exception = f'{type(exc).__name__}: {exc}'
        thread.join(15)
        await asyncio.wait_for(backpressure, 15)
        assert not thread.is_alive()
        assert poison_finished.is_set()
        subsequent_rejected = False
        try:
            await application.stage_attachment(chat.id, second.id)
        except StateError as exc:
            subsequent_rejected = 'not admitting work' in str(exc)
        result = {
            'ordering': ordering,
            'admission_calls': admission_calls,
            'mid': mid,
            'command_result': command_result,
            'command_exception': command_exception,
            'subsequent_rejected': subsequent_rejected,
        }
    else:
        cause_ambiguous_close()
        assert authority.state is AuthorityState.POISONED
        rejected = {}
        operations = {
            'unstage': lambda: application.unstage_attachment(chat.id, first.id),
            'stage_sibling': lambda: application.stage_attachment(chat.id, second.id),
            'cancel_sibling': lambda: application.cancel_generation('missing-attempt'),
        }
        for name, operation in operations.items():
            try:
                await operation()
            except StateError as exc:
                rejected[name] = 'not admitting work' in str(exc)
            else:
                rejected[name] = False
        try:
            application.subscribe()
        except StateError as exc:
            rejected['subscribe_sibling'] = 'not admitting work' in str(exc)
        else:
            rejected['subscribe_sibling'] = False
        result = {
            'ordering': ordering,
            'rejected': rejected,
            'command_result': None,
            'command_exception': 'StateError',
            'mid': {},
        }

    database_rejected = False
    try:
        store.create_chat(
            Chat(
                str(uuid7()),
                'EF2 direct database probe',
                datetime.now(UTC),
                datetime.now(UTC),
            )
        )
    except StateError:
        database_rejected = True
    claim = selected['claim']
    result.update({
        'artifact_close_calls': selected['calls'],
        'claim_status': None if claim is None else claim.status,
        'claim_fd': None if claim is None else claim.fd,
        'poison_errors': poison_errors,
        'authority_state': authority.state.value,
        'poison_pending': authority.poison_pending,
        'store_poisoned': store._poisoned,
        'pending_before': pending_before,
        'pending_after': application._pending_attachment_ids.get(chat.id, ()),
        'cancel_unchanged': application._cancel_requested == cancel_before,
        'event_sequence_before': event_before,
        'event_sequence_after': events._sequence,
        'subscriptions_unchanged': len(events._subscriptions) == subscriptions_before,
        'database_rejected': database_rejected,
    })
    return result

async def main():
    parent = Path(sys.argv[1])
    ordering = sys.argv[2]
    repetitions = int(sys.argv[3])
    results = []
    for index in range(repetitions):
        results.append(await one(parent, index, ordering))
    sys.stdout.write(json.dumps(results))
    sys.stdout.flush()
    os._exit(0)

asyncio.run(main())
'''
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            child,
            os.fspath(root_parent),
            ordering,
            str(repetitions),
        ],
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": os.fspath(REPO / "src")},
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    return json.loads(completed.stdout)


def _assert_ef2_command_wins(result: dict[str, object]) -> None:
    assert result["ordering"] == "command-wins"
    assert result["admission_calls"] == 2
    assert result["mid"] == {
        "at_admission": {
            "authority_state": "READY",
            "poison_pending": False,
            "lease_owned": True,
            "pending": result["pending_before"],
            "event_sequence": result["event_sequence_before"],
        },
        "after_close_before_release": {
            "authority_state": "READY",
            "poison_pending": True,
            "pending": result["pending_before"],
            "event_sequence": result["event_sequence_before"],
        },
        "during_event_backpressure": {
            "authority_state": "READY",
            "poison_pending": True,
            "pending": [],
            "event_sequence": result["event_sequence_before"] + 1,
        },
    }
    assert result["command_result"] == []
    assert result["command_exception"] is None
    assert result["pending_after"] == []
    assert result["event_sequence_after"] == result["event_sequence_before"] + 1
    assert result["artifact_close_calls"] == 1
    assert result["claim_status"] == "UNKNOWN"
    assert result["claim_fd"] is None
    assert result["authority_state"] == "POISONED"
    assert result["poison_pending"] is True
    assert result["store_poisoned"] is False
    assert result["subsequent_rejected"] is True
    assert result["database_rejected"] is True
    assert result["poison_errors"] == [
        "attachment descriptor close outcome is uncertain"
    ]


def test_ef2_race_a_command_wins_serialization(tmp_path: Path):
    result, = _run_ef2_concurrent_admission_child(
        tmp_path, ordering="command-wins"
    )
    _assert_ef2_command_wins(result)


def test_ef2_race_b_poison_wins_serialization_and_centrality(tmp_path: Path):
    result, = _run_ef2_concurrent_admission_child(
        tmp_path, ordering="poison-wins"
    )
    assert result["authority_state"] == "POISONED"
    assert result["poison_pending"] is True
    assert result["artifact_close_calls"] == 1
    assert result["claim_status"] == "UNKNOWN"
    assert result["claim_fd"] is None
    assert result["rejected"] == {
        "unstage": True,
        "stage_sibling": True,
        "cancel_sibling": True,
        "subscribe_sibling": True,
    }
    assert result["pending_after"] == result["pending_before"]
    assert result["cancel_unchanged"] is True
    assert result["event_sequence_after"] == result["event_sequence_before"]
    assert result["subscriptions_unchanged"] is True
    assert result["database_rejected"] is True
    assert result["poison_errors"] == [
        "attachment descriptor close outcome is uncertain"
    ]


def test_ef2_race_c_original_sol_barriers_are_inverted(tmp_path: Path):
    result, = _run_ef2_concurrent_admission_child(
        tmp_path, ordering="command-wins"
    )
    before_release = result["mid"]["after_close_before_release"]
    assert before_release["authority_state"] == "READY"
    assert before_release["poison_pending"] is True
    assert before_release["pending"] == result["pending_before"]
    assert before_release["event_sequence"] == result["event_sequence_before"]
    _assert_ef2_command_wins(result)


def test_ef2_race_d_repeated_deterministic_serialization(tmp_path: Path):
    results = _run_ef2_concurrent_admission_child(
        tmp_path, ordering="command-wins", repetitions=6
    )
    assert len(results) == 6
    for result in results:
        _assert_ef2_command_wins(result)


def test_ef2_poison_pending_gates_direct_fence_but_admitted_owner_finishes(
    tmp_path: Path,
):
    authority, store = _open_store(tmp_path / "root")
    admitted = threading.Event()
    release = threading.Event()
    owner_result = []

    def admitted_owner():
        try:
            with store.command_admission():
                admitted.set()
                assert release.wait(15)
                authority.database_durability_fence()
                owner_result.append("fenced")
        except BaseException as exc:
            owner_result.append(f"{type(exc).__name__}: {exc}")

    holder = threading.Thread(target=admitted_owner, name="ef2-fence-owner")
    holder.start()
    try:
        assert admitted.wait(15)
        authority.poison("injected competing fence poison")
        assert authority.state is AuthorityState.READY
        assert authority.poison_pending is True
        with pytest.raises(AuthorityError, match="data-root operation rejected"):
            authority.database_durability_fence()
        release.set()
        holder.join(15)
        assert not holder.is_alive()
        assert owner_result == ["fenced"]
        assert authority._active_operations == 0
        assert authority.state is AuthorityState.POISONED
        assert authority.poison_pending is True
    finally:
        release.set()
        holder.join(15)
        if authority.state not in {
            AuthorityState.CLOSED,
            AuthorityState.FAILED_CLOSED,
        }:
            authority.close()


def test_ef2_background_generation_effects_share_serialization_without_provider_lease(
    tmp_path: Path,
):
    class ControlledBackend:
        def __init__(self):
            self.release_event = asyncio.Event()

        async def stream(self, request):
            await self.release_event.wait()
            yield GenerationDelta(request.attempt_id, "EF2 delta")
            yield GenerationCompleted(request.attempt_id)

    async def command_wins():
        root = tmp_path / "command-wins"
        authority, store = _open_store(root)
        ids = Uuid7Factory()
        clock = SystemClock()
        events = EventBus(clock, ids, queue_size=16)
        backend = ControlledBackend()
        application = BotsApplication(store, events, backend, ids=ids, clock=clock)
        release_publish = threading.Event()
        effect_entered = threading.Event()
        poison_evidence = {}
        original_publish = application._publish_after_persistence

        async def coordinated_publish(kind, **payload):
            if kind == "message_delta":
                poison_evidence["lease_owned_at_publication"] = (
                    authority._has_application_admission()
                )
                effect_entered.set()
                assert release_publish.wait(15)
            await original_publish(kind, **payload)
            if kind == "message_delta":
                poison_evidence["state_after_publication"] = authority.state.value
                poison_evidence["pending_after_publication"] = authority.poison_pending

        application._publish_after_persistence = coordinated_publish
        try:
            chat = await application.create_chat("EF2 generation command wins")
            attempt = await application.send_message(chat.id, "start")
            task = application._generation_tasks[attempt.id]
            assistant_id = application._pending_generations[attempt.id][0].id
            event_before = events._sequence
            assert authority._active_operations == 0

            def poisoner():
                assert effect_entered.wait(15)
                authority.poison("injected generation-effect poison")
                poison_evidence["state_before_release"] = authority.state.value
                poison_evidence["pending_before_release"] = authority.poison_pending
                release_publish.set()

            thread = threading.Thread(target=poisoner, name="ef2-generation-poisoner")
            thread.start()
            backend.release_event.set()
            task_result, = await asyncio.gather(task, return_exceptions=True)
            thread.join(15)
            assert not thread.is_alive()
            assert isinstance(task_result, StateError)
            assert events._sequence == event_before + 1
            assert poison_evidence == {
                "lease_owned_at_publication": True,
                "state_before_release": "READY",
                "pending_before_release": True,
                "state_after_publication": "READY",
                "pending_after_publication": True,
            }
            assert authority.state is AuthorityState.POISONED
            assert authority.poison_pending is True
        finally:
            release_publish.set()
            if authority.state not in {
                AuthorityState.CLOSED,
                AuthorityState.FAILED_CLOSED,
            }:
                authority.close()
        with sqlite3.connect(root / "database" / "state.sqlite3") as connection:
            assert connection.execute(
                "SELECT content, state FROM messages WHERE id = ?", (assistant_id,)
            ).fetchone() == ("EF2 delta", "streaming")

    async def poison_wins():
        root = tmp_path / "poison-wins"
        authority, store = _open_store(root)
        ids = Uuid7Factory()
        clock = SystemClock()
        events = EventBus(clock, ids, queue_size=16)
        backend = ControlledBackend()
        application = BotsApplication(store, events, backend, ids=ids, clock=clock)
        try:
            chat = await application.create_chat("EF2 generation poison wins")
            attempt = await application.send_message(chat.id, "start")
            task = application._generation_tasks[attempt.id]
            assistant_id = application._pending_generations[attempt.id][0].id
            event_before = events._sequence
            assert authority._active_operations == 0
            authority.poison("injected pre-effect poison")
            assert authority.state is AuthorityState.POISONED
            backend.release_event.set()
            task_result, = await asyncio.gather(task, return_exceptions=True)
            assert isinstance(task_result, StateError)
            assert events._sequence == event_before
        finally:
            if authority.state not in {
                AuthorityState.CLOSED,
                AuthorityState.FAILED_CLOSED,
            }:
                authority.close()
        with sqlite3.connect(root / "database" / "state.sqlite3") as connection:
            assert connection.execute(
                "SELECT content, state FROM messages WHERE id = ?", (assistant_id,)
            ).fetchone() == ("", "streaming")

    asyncio.run(command_wins())
    asyncio.run(poison_wins())


def test_ef2_admission_reentrancy_exception_cleanup_and_terminal_close(
    tmp_path: Path,
):
    authority, store = _open_store(tmp_path / "normal")
    try:
        with store.command_admission():
            assert authority._active_operations == 1
            store.assert_admitting()
            assert authority._active_operations == 1
        assert authority._active_operations == 0
        with pytest.raises(RuntimeError, match="injected command failure"):
            with store.command_admission():
                raise RuntimeError("injected command failure")
        assert authority._active_operations == 0
        assert authority.state is AuthorityState.READY
        store.assert_admitting()

        admitted = threading.Event()
        release = threading.Event()

        def hold_admission():
            with store.command_admission():
                admitted.set()
                assert release.wait(15)

        holder = threading.Thread(target=hold_admission)
        holder.start()
        assert admitted.wait(15)
        closer = threading.Thread(target=authority.close)
        closer.start()
        deadline = time.monotonic() + 15
        while authority.state is not AuthorityState.CLOSING:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert closer.is_alive()
        release.set()
        holder.join(15)
        closer.join(15)
        assert not holder.is_alive()
        assert not closer.is_alive()
        assert authority._active_operations == 0
        assert authority.state is AuthorityState.CLOSED
    finally:
        if authority.state not in {
            AuthorityState.CLOSED,
            AuthorityState.FAILED_CLOSED,
        }:
            authority.close()

    poisoned, poisoned_store = _open_store(tmp_path / "poison")
    try:
        with pytest.raises(RuntimeError, match="injected poisoned command"):
            with poisoned_store.command_admission():
                poisoned.poison("injected admitted poison")
                assert poisoned.poison_pending is True
                assert poisoned.state is AuthorityState.POISONED
                raise RuntimeError("injected poisoned command")
        assert poisoned._active_operations == 0
        assert poisoned.state is AuthorityState.POISONED
        with pytest.raises(StateError, match="not admitting work"):
            poisoned_store.assert_admitting()
    finally:
        if poisoned.state not in {
            AuthorityState.CLOSED,
            AuthorityState.FAILED_CLOSED,
        }:
            poisoned.close()


def test_n1_close_classifier_is_one_attempt_and_monotonic():
    class Authority:
        def __init__(self):
            self.state = AuthorityState.READY
            self.reasons = []

        def poison(self, reason):
            self.reasons.append(reason)
            self.state = AuthorityState.POISONED

    class Connection:
        def __init__(self):
            self.close_calls = 0
            self.released = False

        def close(self):
            self.close_calls += 1
            raise OSError("injected close failure")

    store = object.__new__(SQLiteAppStateStore)
    authority = Authority()
    connection = Connection()
    store._poisoned = False
    store._authority = authority
    with pytest.raises(StateError, match="connection close is uncertain") as raised:
        store._close_attachment_connection(connection, "test close")
    assert connection.close_calls == 1
    assert store._poisoned is True
    assert authority.state is AuthorityState.POISONED
    assert authority.reasons == [
        "attachment lifecycle failure: test close database connection close outcome uncertain"
    ]
    assert isinstance(raised.value.__cause__, OSError)
    connection.released = True
    assert store._poisoned is True
    assert authority.state is AuthorityState.POISONED


def test_n1_t2_close_post_real_is_poisoned_and_restart_reconciles(tmp_path: Path, monkeypatch):
    root = tmp_path / "root"
    source = tmp_path / "payload.txt"
    source.write_bytes(b"T2 post-real close")
    authority, store = _open_store(root)
    selected = _arm_selected_connection_close(monkeypatch, post_real=True)
    try:
        with pytest.raises(StateError, match="connection close is uncertain") as raised:
            store.ingest_attachment(source)
        assert isinstance(raised.value.__cause__, OSError)
        assert selected["connection"] is not None
        assert selected["close_calls"] == 1
        assert store._poisoned is True
        assert authority.state is AuthorityState.POISONED
        assert authority._checked_out_connections == 0
        before_rejection = _tree_fingerprint(root)
        before = _raw_attachment_lifecycle(root)
        with pytest.raises(StateError, match="not admitting work"):
            store.list_attachments()
        with pytest.raises(StateError, match="not admitting work"):
            store.assert_admitting()
        with pytest.raises(StateError, match="not admitting work"):
            store.create_chat(
                Chat(
                    id=str(uuid7()),
                    title="rejected after T2 close uncertainty",
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
        assert _tree_fingerprint(root) == before_rejection
        assert _raw_attachment_lifecycle(root) == before
    finally:
        if authority.state is not AuthorityState.CLOSED:
            authority.close()
    blobs, attachments, areas = _raw_attachment_lifecycle(root)
    assert len(blobs) == 1
    state, operation_id, stage_name, gc_id = blobs[0]
    assert state == "staging"
    assert operation_id and stage_name == operation_id and gc_id is None
    assert attachments == []
    assert areas["captures"] == (operation_id,)
    assert areas["staging"] == ()
    assert areas["objects"] == ()
    _assert_recovered_t2(root, hashlib.sha256(source.read_bytes()).hexdigest())


def test_n1_t2_close_pre_real_requires_process_exit_and_restart_reconciles(tmp_path: Path):
    root = tmp_path / "root"
    source = tmp_path / "payload.txt"
    source.write_bytes(b"T2 pre-real close")
    _initialise_root(root)
    completed = _run_pre_release_t2_child(root, source)
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    blobs, attachments, areas = _raw_attachment_lifecycle(root)
    assert len(blobs) == 1
    state, operation_id, stage_name, gc_id = blobs[0]
    assert state == "staging"
    assert operation_id and stage_name == operation_id and gc_id is None
    assert attachments == []
    assert areas["captures"] == (operation_id,)
    assert areas["staging"] == ()
    assert areas["objects"] == ()
    _assert_recovered_t2(root, hashlib.sha256(source.read_bytes()).hexdigest())


@pytest.mark.parametrize("boundary", ("T1", "T4", "T7"))
def test_n1_live_k1_close_siblings_poison_before_unwind(
    tmp_path: Path, monkeypatch, boundary: str
):
    root = tmp_path / boundary
    source = tmp_path / f"{boundary}.txt"
    source.write_bytes(f"{boundary} K1 close".encode())
    authority, store = _open_store(root)
    original = store.ingest_attachment(source)
    selected = None
    post_close_calls = {"database-fence": 0, "deleting-recovery": 0}
    try:
        if boundary == "T1":
            selected = _arm_selected_connection_close(monkeypatch, post_real=True)
            with pytest.raises(StateError, match="connection close is uncertain"):
                store.ingest_attachment(source)
        elif boundary == "T4":
            selected = _arm_selected_connection_close(monkeypatch, post_real=True)
            with pytest.raises(StateError, match="connection close is uncertain"):
                store.delete_attachment(original.id)
        else:
            store.delete_attachment(original.id)
            selected = _arm_selected_connection_close(monkeypatch, post_real=True)

            original_fence = authority.database_durability_fence
            original_recovery = store._recover_deleting_blob

            def fence_after_close_failure():
                post_close_calls["database-fence"] += 1
                return original_fence()

            def recovery_after_close_failure(*args, **kwargs):
                post_close_calls["deleting-recovery"] += 1
                return original_recovery(*args, **kwargs)

            monkeypatch.setattr(authority, "database_durability_fence", fence_after_close_failure)
            monkeypatch.setattr(store, "_recover_deleting_blob", recovery_after_close_failure)
            with pytest.raises(StateError, match="connection close is uncertain"):
                store.gc_attachments()
        assert selected["connection"] is not None
        assert selected["close_calls"] == 1
        if boundary == "T7":
            assert post_close_calls["database-fence"] == 0
            assert post_close_calls["deleting-recovery"] == 0
        assert store._poisoned is True
        assert authority.state is AuthorityState.POISONED
        assert authority._checked_out_connections == 0
        with pytest.raises(StateError, match="not admitting work"):
            store.assert_admitting()
    finally:
        if authority.state is not AuthorityState.CLOSED:
            authority.close()

    blobs, attachments, areas = _raw_attachment_lifecycle(root)
    if boundary == "T1":
        assert len(blobs) == 1 and blobs[0][0] == "ready"
        assert len(attachments) == 2
        assert areas["captures"] == ()
        assert areas["staging"] == ()
        assert areas["objects"] == (original.blob_digest,)
    elif boundary == "T4":
        assert blobs == [("ready", None, None, None)]
        assert attachments == []
        assert areas["objects"] == (original.blob_digest,)
    else:
        assert len(blobs) == 1
        assert blobs[0][0] == "deleting" and blobs[0][3]
        assert attachments == []
        assert areas["objects"] == (original.blob_digest,)
        assert areas["gc"] == ()

    restarted_authority, restarted = _open_store(root)
    try:
        assert restarted_authority.state is AuthorityState.READY
    finally:
        if restarted_authority.state is not AuthorityState.CLOSED:
            restarted_authority.close()
    _, recovered_attachments, recovered_areas = _raw_attachment_lifecycle(root)
    if boundary == "T1":
        assert len(recovered_attachments) == 2
        assert recovered_areas["objects"] == (original.blob_digest,)
    elif boundary == "T4":
        assert recovered_attachments == []
        assert recovered_areas["objects"] == (original.blob_digest,)
    else:
        assert recovered_attachments == []
        assert recovered_areas["objects"] == ()


@pytest.mark.parametrize("operation", ("send", "edit", "regenerate"))
def test_n1_t5_t6_application_callers_stop_before_dispatch_on_close_failure(
    tmp_path: Path, monkeypatch, operation: str
):
    async def scenario():
        backend = RecordingBackend()
        application, store, authority = _configured_application(
            tmp_path / operation, backend
        )
        source = tmp_path / f"{operation}.txt"
        source.write_text(f"selected {operation}", encoding="utf-8")
        try:
            chat = await application.create_chat()
            initial = None
            if operation in {"edit", "regenerate"}:
                initial_attachment = await application.attach_file(source)
                await application.stage_attachment(chat.id, initial_attachment.id)
                initial = await application.send_message(chat.id, "initial")
                await _finish(application, initial.id)
                attachment = initial_attachment
            else:
                attachment = await application.attach_file(source)
            await application.stage_attachment(chat.id, attachment.id)
            event_sequence = application._events._sequence
            request_count = len(backend.requests)
            selected = _arm_selected_connection_close(monkeypatch, post_real=True)
            if operation == "send":
                with pytest.raises(StateError, match="connection close is uncertain"):
                    await application.send_message(chat.id, "send with attachment")
            elif operation == "edit":
                with pytest.raises(StateError, match="connection close is uncertain"):
                    await application.edit_message(
                        chat.id, initial.user_message_id, "edited with attachment"
                    )
            else:
                with pytest.raises(StateError, match="connection close is uncertain"):
                    await application.regenerate_message(
                        chat.id, initial.assistant_message_id
                    )
            assert selected["connection"] is not None
            assert selected["close_calls"] == 1
            assert store._poisoned is True
            assert authority.state is AuthorityState.POISONED
            assert len(backend.requests) == request_count
            assert application._events._sequence == event_sequence
            assert application._pending_attachment_ids[chat.id] == (attachment.id,)
            assert application._pending_generations == {}
            pending_before_rejection = dict(application._pending_attachment_ids)
            event_sequence_before_rejection = application._events._sequence
            with pytest.raises(StateError, match="not admitting work"):
                await application.unstage_attachment(chat.id, attachment.id)
            assert application._pending_attachment_ids == pending_before_rejection
            assert application._events._sequence == event_sequence_before_rejection
            with pytest.raises(StateError, match="not admitting work"):
                await application.list_chats()
            assert application._pending_attachment_ids == pending_before_rejection
            assert application._events._sequence == event_sequence_before_rejection
        finally:
            try:
                await application.close()
            except BaseException:
                pass
            if authority.state is not AuthorityState.CLOSED:
                authority.close()

        database = tmp_path / operation / "database" / "state.sqlite3"
        with sqlite3.connect(database) as connection:
            message_count = connection.execute(
                "SELECT count(*) FROM messages WHERE chat_id = ?", (chat.id,)
            ).fetchone()[0]
            attempt_count = connection.execute(
                "SELECT count(*) FROM generation_attempts WHERE chat_id = ?", (chat.id,)
            ).fetchone()[0]
            plan_count = connection.execute(
                "SELECT count(*) FROM context_plans"
            ).fetchone()[0]
            message_ref_count = connection.execute(
                "SELECT count(*) FROM message_attachments"
            ).fetchone()[0]
            attempt_ref_count = connection.execute(
                "SELECT count(*) FROM attempt_attachments"
            ).fetchone()[0]
        expected_messages = 2 if operation == "send" else 3 if operation == "regenerate" else 4
        expected_attempts = 1 if operation == "send" else 2
        expected_plans = expected_attempts
        expected_message_refs = 2 if operation == "edit" else 1
        expected_attempt_refs = 1 if operation == "send" else 2
        assert message_count == expected_messages
        assert attempt_count == expected_attempts
        assert plan_count == expected_plans
        assert message_ref_count == expected_message_refs
        assert attempt_ref_count == expected_attempt_refs

        restarted_authority, restarted = _open_store(tmp_path / operation)
        try:
            assert restarted_authority.state is AuthorityState.READY
            assert restarted.get_attachment(attachment.id) is not None
        finally:
            if restarted_authority.state is not AuthorityState.CLOSED:
                restarted_authority.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("boundary", ("T1", "T2", "T4", "T5", "T6", "T7"))
def test_n1_live_k0_close_failure_poison_survives_clean_business_rollback(
    tmp_path: Path, monkeypatch, boundary: str
):
    async def scenario():
        root = tmp_path / boundary
        source = tmp_path / f"{boundary}.txt"
        source.write_bytes(f"{boundary} K0 close".encode())
        backend = RecordingBackend()
        application = store = authority = None
        baseline_requests = 0
        baseline_events = 0
        if boundary in {"T5", "T6"}:
            application, store, authority = _configured_application(root, backend)
            chat = await application.create_chat()
            attachment = await application.attach_file(source)
            if boundary == "T6":
                await application.stage_attachment(chat.id, attachment.id)
                initial = await application.send_message(chat.id, "initial")
                await _finish(application, initial.id)
            await application.stage_attachment(chat.id, attachment.id)
            if boundary == "T6":
                baseline_requests = len(backend.requests)
            baseline_events = application._events._sequence
        else:
            authority, store = _open_store(root)
            attachment = None
            if boundary != "T2":
                attachment = store.ingest_attachment(source)
            if boundary == "T7":
                store.delete_attachment(attachment.id)

        marker = {
            "T1": "INSERT INTO ATTACHMENTS",
            "T2": "INSERT INTO ATTACHMENT_BLOBS",
            "T4": "DELETE FROM ATTACHMENTS",
            "T5": "INSERT INTO MESSAGES",
            "T6": "INSERT INTO MESSAGES",
            "T7": "UPDATE ATTACHMENT_BLOBS",
        }[boundary]
        selected = _arm_selected_connection_close(monkeypatch, post_real=True)
        post_close_callbacks: list[str] = []
        if boundary == "T7":
            original_fence = authority.database_durability_fence
            original_recovery = store._recover_deleting_blob

            def fence_after_close_failure():
                post_close_callbacks.append("database-fence")
                return original_fence()

            def recovery_after_close_failure(*args, **kwargs):
                post_close_callbacks.append("deleting-recovery")
                return original_recovery(*args, **kwargs)

            monkeypatch.setattr(authority, "database_durability_fence", fence_after_close_failure)
            monkeypatch.setattr(store, "_recover_deleting_blob", recovery_after_close_failure)
        original_execute = Connection.execute
        failed = False

        def fail_statement(self, statement, *args, **kwargs):
            nonlocal failed
            if not failed and marker in str(statement).upper():
                failed = True
                raise OSError(f"injected {boundary} K0 statement failure")
            return original_execute(self, statement, *args, **kwargs)

        monkeypatch.setattr(Connection, "execute", fail_statement)
        try:
            if boundary in {"T1", "T2"}:
                with pytest.raises(StateError, match="connection close is uncertain"):
                    store.ingest_attachment(source)
            elif boundary == "T4":
                with pytest.raises(StateError, match="connection close is uncertain"):
                    store.delete_attachment(attachment.id)
            elif boundary == "T7":
                with pytest.raises(StateError, match="connection close is uncertain"):
                    store.gc_attachments()
            elif boundary == "T5":
                with pytest.raises(StateError, match="connection close is uncertain"):
                    await application.send_message(chat.id, "K0 send")
            else:
                with pytest.raises(StateError, match="connection close is uncertain"):
                    await application.regenerate_message(
                        chat.id, initial.assistant_message_id
                    )
            assert failed is True
            assert selected["connection"] is not None
            assert selected["close_calls"] == 1
            assert store._poisoned is True
            assert authority.state is AuthorityState.POISONED
            assert authority._checked_out_connections == 0
            if boundary == "T7":
                assert post_close_callbacks == []
            with pytest.raises(StateError, match="not admitting work"):
                store.assert_admitting()
            if boundary in {"T5", "T6"}:
                assert len(backend.requests) == baseline_requests
                assert application._events._sequence == baseline_events
        finally:
            if application is not None:
                try:
                    await application.close()
                except BaseException:
                    pass
            if authority.state is not AuthorityState.CLOSED:
                authority.close()

        blobs, attachments, areas = _raw_attachment_lifecycle(root)
        if boundary == "T2":
            assert blobs == [] and attachments == []
            assert areas == {"captures": (), "staging": (), "objects": (), "gc": ()}
        elif boundary == "T7":
            assert blobs == [("ready", None, None, None)]
            assert attachments == [] and areas["objects"] == (attachment.blob_digest,)
            assert areas["gc"] == ()
        else:
            assert len(blobs) == 1 and blobs[0][0] == "ready"
            if boundary == "T1":
                assert areas["captures"] == ()
                assert areas["staging"] == ()
            assert areas["objects"] == (attachment.blob_digest,)
            assert len(attachments) == 1

        if boundary in {"T5", "T6"}:
            with sqlite3.connect(root / "database" / "state.sqlite3") as connection:
                counts = tuple(
                    connection.execute(query).fetchone()[0]
                    for query in (
                        "SELECT count(*) FROM messages WHERE chat_id = '%s'" % chat.id,
                        "SELECT count(*) FROM generation_attempts WHERE chat_id = '%s'" % chat.id,
                        "SELECT count(*) FROM context_plans",
                        "SELECT count(*) FROM message_attachments",
                        "SELECT count(*) FROM attempt_attachments",
                    )
                )
            assert counts == ((0, 0, 0, 0, 0) if boundary == "T5" else (2, 1, 1, 1, 1))

        restarted_authority, restarted = _open_store(root)
        try:
            assert restarted_authority.state is AuthorityState.READY
            if attachment is not None and boundary != "T7":
                assert restarted.get_attachment(attachment.id) is not None
            if boundary == "T7":
                assert restarted.list_attachments() == ()
        finally:
            if restarted_authority.state is not AuthorityState.CLOSED:
                restarted_authority.close()

    asyncio.run(scenario())


async def _finish(application: BotsApplication, attempt_id: str) -> None:
    task = application._generation_tasks.get(attempt_id)
    if task is not None:
        await task
    await asyncio.sleep(0)


def test_generation_mode_truth_table_rejects_implicit_real_local_and_core_http(
    tmp_path: Path,
):
    authority, store = _open_store(tmp_path / "root")
    ids = Uuid7Factory()
    clock = SystemClock()
    backend = LegacyRecordingBackend()
    try:
        with pytest.raises(StateError, match="real provider or HTTP backend"):
            BotsApplication(
                store,
                EventBus(clock, ids),
                backend,
                backend_id=backend.backend_id,
                model="local-model",
                provider_id=backend.provider_id,
                base_url=backend.base_url,
            )
        with pytest.raises(StateError, match="real provider or HTTP backend"):
            BotsApplication(
                store,
                EventBus(clock, ids),
                backend,
                generation_mode=GenerationMode.LEGACY_CORE_COMPATIBILITY,
                backend_id=backend.backend_id,
            )
        application = BotsApplication(
            store,
            EventBus(clock, ids),
            backend,
            generation_mode=GenerationMode.LEGACY_PHASE3_LOCAL_OPENAI,
            backend_id=backend.backend_id,
            model="local-model",
            provider_id=backend.provider_id,
            base_url=backend.base_url,
        )
        assert application.generation_mode is GenerationMode.LEGACY_PHASE3_LOCAL_OPENAI
        assert application.phase6_enabled is False
    finally:
        authority.close()


def test_explicit_local_legacy_plain_send_has_no_phase6_plan_evidence(tmp_path: Path):
    async def scenario():
        authority, store = _open_store(tmp_path / "root")
        backend = LegacyRecordingBackend()
        ids = Uuid7Factory()
        clock = SystemClock()
        application = BotsApplication(
            store,
            EventBus(clock, ids),
            backend,
            generation_mode=GenerationMode.LEGACY_PHASE3_LOCAL_OPENAI,
            backend_id=backend.backend_id,
            model="local-model",
            provider_id=backend.provider_id,
            base_url=backend.base_url,
        )
        try:
            chat = await application.create_chat()
            attempt = await application.send_message(chat.id, "plain legacy text")
            await _finish(application, attempt.id)
            assert len(backend.requests) == 1
            snapshot = json.loads(attempt.request_snapshot)
            assert "context_plan" not in snapshot
            with _private_engine_connection(store) as connection:
                assert connection.exec_driver_sql(
                    "SELECT count(*) FROM context_plans WHERE attempt_id = ?",
                    (attempt.id,),
                ).scalar_one() == 0
        finally:
            await application.close()
            if authority.state is not AuthorityState.CLOSED:
                authority.close()

    asyncio.run(scenario())


def test_local_legacy_attachment_rejection_precedes_persistence_and_dispatch(
    tmp_path: Path,
):
    async def scenario():
        authority, store = _open_store(tmp_path / "root")
        backend = LegacyRecordingBackend()
        ids = Uuid7Factory()
        clock = SystemClock()
        application = BotsApplication(
            store,
            EventBus(clock, ids),
            backend,
            generation_mode=GenerationMode.LEGACY_PHASE3_LOCAL_OPENAI,
            backend_id=backend.backend_id,
            model="local-model",
            provider_id=backend.provider_id,
            base_url=backend.base_url,
        )
        source = tmp_path / "selected.txt"
        source.write_text("selected", encoding="utf-8")
        try:
            chat = await application.create_chat()
            attachment = await application.attach_file(source)
            with pytest.raises(StateError, match="attachments are unavailable"):
                await application.stage_attachment(chat.id, attachment.id)
            # Simulate stale/manual state carried from a prior Phase 6 chat;
            # the defensive preparation check must still precede persistence.
            application._pending_attachment_ids[chat.id] = (attachment.id,)
            with pytest.raises(StateError, match="attachments are unavailable"):
                await application.send_message(chat.id, "must not dispatch")
            assert backend.requests == []
            assert await application.list_message_history(chat.id) == ()
            assert await application.list_generation_attempts(chat.id) == ()
        finally:
            await application.close()
            if authority.state is not AuthorityState.CLOSED:
                authority.close()

    asyncio.run(scenario())


def test_configured_phase6_missing_selection_fails_closed_without_legacy_fallback(
    tmp_path: Path, monkeypatch
):
    async def scenario():
        authority, store = _open_store(tmp_path / "root")
        backend = LegacyRecordingBackend()
        ids = Uuid7Factory()
        clock = SystemClock()
        configuration = ProviderConfiguration(store, ids, clock)
        def no_adapter(**kwargs):
            raise StateError("no exact Phase 6 accounting adapter is registered")

        monkeypatch.setattr(configuration, "prepare_generation", no_adapter)
        application = BotsApplication(
            store,
            EventBus(clock, ids),
            backend,
            generation_mode=GenerationMode.CONFIGURED,
            configuration=configuration,
            backend_id=backend.backend_id,
            model="local-model",
            provider_id=backend.provider_id,
            base_url=backend.base_url,
        )
        try:
            chat = await application.create_chat()
            with pytest.raises(StateError):
                await application.send_message(chat.id, "must fail before dispatch")
            assert backend.requests == []
            assert await application.list_message_history(chat.id) == ()
        finally:
            await application.close()
            if authority.state is not AuthorityState.CLOSED:
                authority.close()

    asyncio.run(scenario())


TOPOLOGY_EDGE_LABELS = (
    "configured-root",
    "database",
    "attachments",
    "attachments/objects",
    "attachments/staging",
    "attachments/captures",
    "attachments/gc",
    "database/migration",
    "database/temp",
    "recovery",
)


def test_n1_classified_commit_callsite_inventory_is_complete():
    source = inspect.getsource(SQLiteAppStateStore)
    for label in (
        "T1 deduplicated attachment identity",
        "T2 initial staging intent",
        "T3 final publication",
        "T4 reusable attachment deletion",
        "T5 generation start with attachments",
        "T6 regeneration with attachments",
        "T7 GC authorization",
        "T8 startup staging recovery",
        "T9 GC final intent deletion",
    ):
        assert label in source
    assert source.count("_commit_attachment_transaction(") >= 10


def test_n1_uncertain_commit_poison_is_fail_closed_before_return():
    class Authority:
        def __init__(self):
            self.reasons = []

        def poison(self, reason):
            self.reasons.append(reason)

        @contextmanager
        def application_operation(self, *, independent=False):
            del independent
            if self.reasons:
                raise AuthorityError("test authority is not admitting work")
            yield

    class Connection:
        def __init__(self):
            self.rollbacks = 0

        def commit(self):
            raise OSError("injected uncertain commit")

        def rollback(self):
            self.rollbacks += 1

    store = object.__new__(SQLiteAppStateStore)
    store._closed = False
    store._poisoned = False
    store._authority = Authority()
    connection = Connection()
    with pytest.raises(StateError, match="outcome is uncertain"):
        store._commit_attachment_transaction(connection, "T1 test")
    assert store._poisoned is True
    assert connection.rollbacks == 1
    assert store._authority.reasons == ["uncertain attachment transaction commit: T1 test"]
    with pytest.raises(StateError, match="not admitting work"):
        store.assert_admitting()


@pytest.mark.parametrize(
    ("boundary", "statement_marker"),
    (("T1", "INSERT INTO attachments"), ("T2", "INSERT INTO attachment_blobs")),
)
def test_n1_true_k0_statement_abort_discards_only_capture_and_allows_retry(
    tmp_path: Path, monkeypatch, boundary: str, statement_marker: str
):
    root = tmp_path / boundary
    source = tmp_path / f"{boundary}.txt"
    source.write_bytes(f"{boundary} K0".encode())
    authority, store = _open_store(root)
    failed = False
    original_execute = Connection.execute

    def fail_statement(self, statement, *args, **kwargs):
        nonlocal failed
        if not failed and statement_marker.upper() in str(statement).upper():
            failed = True
            raise OSError(f"injected {boundary} statement failure")
        return original_execute(self, statement, *args, **kwargs)

    try:
        if boundary == "T1":
            store.ingest_attachment(source)
        monkeypatch.setattr(Connection, "execute", fail_statement)
        with pytest.raises(OSError, match="statement failure"):
            store.ingest_attachment(source)
        assert failed is True
        assert store._poisoned is False
        assert store._attachment_manager.inventory("captures") == ()
        assert store._attachment_manager.inventory("staging") == ()
        assert store.ingest_attachment(source)
        assert store._attachment_manager.inventory("captures") == ()
        assert store._attachment_manager.inventory("staging") == ()
    finally:
        if authority.state is not AuthorityState.CLOSED:
            authority.close()


def test_n1_true_k0_begin_abort_discards_capture_and_allows_retry(
    tmp_path: Path, monkeypatch
):
    source = tmp_path / "begin.txt"
    source.write_bytes(b"BEGIN K0")
    authority, store = _open_store(tmp_path / "root")
    failed = False
    original_exec_driver_sql = Connection.exec_driver_sql

    def fail_begin(self, statement, *args, **kwargs):
        nonlocal failed
        if not failed and str(statement).strip().upper() == "BEGIN IMMEDIATE":
            failed = True
            raise OSError("injected BEGIN failure")
        return original_exec_driver_sql(self, statement, *args, **kwargs)

    monkeypatch.setattr(Connection, "exec_driver_sql", fail_begin)
    try:
        with pytest.raises(OSError, match="BEGIN failure"):
            store.ingest_attachment(source)
        assert failed is True
        assert store._poisoned is False
        assert store._attachment_manager.inventory("captures") == ()
        assert store.ingest_attachment(source)
    finally:
        if authority.state is not AuthorityState.CLOSED:
            authority.close()


def test_n1_checkout_failure_discards_capture_and_allows_retry(
    tmp_path: Path, monkeypatch
):
    root = tmp_path / "root"
    source = tmp_path / "checkout.txt"
    source.write_bytes(b"checkout K0")
    authority, store = _open_store(root)
    original_connect = store._engine.connect
    failed = False

    def fail_checkout(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("injected checkout failure")
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(store._engine, "connect", fail_checkout)
    try:
        with pytest.raises(OSError, match="checkout failure"):
            store.ingest_attachment(source)
        assert failed is True
        assert store._poisoned is False
        assert authority.state is AuthorityState.READY
        assert store._attachment_manager.inventory("captures") == ()
        assert store.list_attachments() == ()
        chat = Chat(
            id=str(uuid7()),
            title="checkout retry",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        store.create_chat(chat)
        assert store.ingest_attachment(source).blob_digest
        assert store._attachment_manager.inventory("captures") == ()
    finally:
        if authority.state is not AuthorityState.CLOSED:
            authority.close()


def test_n1_checkout_failure_cleanup_failure_poison_and_restart_reconciles(
    tmp_path: Path, monkeypatch
):
    root = tmp_path / "root"
    source = tmp_path / "checkout-cleanup.txt"
    source.write_bytes(b"checkout cleanup K0")
    authority, store = _open_store(root)
    original_connect = store._engine.connect
    original_discard = type(store._attachment_manager).discard_capture
    failed = False

    def fail_checkout(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("injected checkout failure")
        return original_connect(*args, **kwargs)

    def fail_discard(manager, operation_id):
        if manager is store._attachment_manager:
            raise OSError("injected checkout capture cleanup failure")
        return original_discard(manager, operation_id)

    monkeypatch.setattr(store._engine, "connect", fail_checkout)
    monkeypatch.setattr(type(store._attachment_manager), "discard_capture", fail_discard)
    try:
        with pytest.raises(StateError, match="capture cleanup failed"):
            store.ingest_attachment(source)
        assert store._poisoned is True
        assert authority.state is AuthorityState.POISONED
        assert tuple((root / "attachments" / "captures").iterdir())
        with pytest.raises(StateError, match="not admitting work"):
            store.list_attachments()
        with pytest.raises(StateError, match="not admitting work"):
            store.create_chat(
                Chat(
                    id=str(uuid7()),
                    title="blocked",
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
    finally:
        if authority.state is not AuthorityState.CLOSED:
            authority.close()
    restarted_authority = DataRootAuthority(root).acquire()
    try:
        restarted = restarted_authority.open_store()
        assert restarted_authority.state is AuthorityState.READY
        assert restarted._attachment_manager.inventory("captures") == ()
        restarted.close()
    finally:
        if restarted_authority.state is not AuthorityState.CLOSED:
            restarted_authority.close()


def test_n1_t1_first_abnormal_row_rollback_failure_is_irrevocably_poisoned(
    tmp_path: Path, monkeypatch
):
    root = tmp_path / "root"
    source = tmp_path / "abnormal.txt"
    source.write_bytes(b"abnormal existing row")
    authority, store = _open_store(root)
    with authority.operation():
        captured = store._attachment_manager.capture(source)
        with _private_engine_connection(store) as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            arm_phase6_blob_transition(
                connection,
                captured.digest,
                "",
                "staging",
                operation_id=captured.operation_id,
                stage_name=captured.operation_id,
                byte_size=captured.byte_size,
            )
            try:
                connection.exec_driver_sql(
                    "INSERT INTO attachment_blobs "
                    "(digest, byte_size, state, operation_id, stage_name, gc_id, created_at) "
                    "VALUES (?, ?, 'staging', ?, ?, NULL, ?)",
                    (
                        captured.digest,
                        captured.byte_size,
                        captured.operation_id,
                        captured.operation_id,
                        datetime.now(UTC).isoformat(timespec="milliseconds").replace(
                            "+00:00", "Z"
                        ),
                    ),
                )
                require_phase6_consumed(connection)
            finally:
                clear_phase6(connection)
            connection.commit()
    original_rollback = Connection.rollback
    rollback_calls = 0

    def fail_first_rollback(connection):
        nonlocal rollback_calls
        rollback_calls += 1
        if rollback_calls == 1:
            raise OSError("injected first T1 rollback failure")
        return original_rollback(connection)

    monkeypatch.setattr(Connection, "rollback", fail_first_rollback)
    try:
        with pytest.raises(StateError, match="rollback is uncertain"):
            store.ingest_attachment(source)
        assert rollback_calls >= 1
        assert store._poisoned is True
        assert authority.state is AuthorityState.POISONED
        assert tuple((root / "attachments" / "captures").iterdir())
        with pytest.raises(StateError, match="not admitting work"):
            store.list_attachments()
        with pytest.raises(StateError, match="not admitting work"):
            store.create_chat(
                Chat(
                    id=str(uuid7()),
                    title="blocked",
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
    finally:
        if authority.state is not AuthorityState.CLOSED:
            authority.close()
    restarted_authority = DataRootAuthority(root).acquire()
    try:
        restarted = restarted_authority.open_store()
        assert restarted_authority.state is AuthorityState.READY
        assert restarted._attachment_manager.inventory("captures") == ()
        restarted.close()
    finally:
        if restarted_authority.state is not AuthorityState.CLOSED:
            restarted_authority.close()


def test_n1_k0_rollback_uncertainty_and_capture_cleanup_uncertainty_poison(
    tmp_path: Path, monkeypatch
):
    source = tmp_path / "uncertain.txt"
    source.write_bytes(b"uncertain K0")
    authority, store = _open_store(tmp_path / "rollback")
    original_execute = Connection.execute
    original_rollback = Connection.rollback
    statement_failed = False

    def fail_statement(self, statement, *args, **kwargs):
        nonlocal statement_failed
        if not statement_failed and "INSERT INTO ATTACHMENT_BLOBS" in str(statement).upper():
            statement_failed = True
            raise OSError("injected statement failure")
        return original_execute(self, statement, *args, **kwargs)

    def fail_rollback(self):
        raise OSError("injected rollback failure")

    monkeypatch.setattr(Connection, "execute", fail_statement)
    monkeypatch.setattr(Connection, "rollback", fail_rollback)
    try:
        with pytest.raises(StateError, match="rollback is uncertain"):
            store.ingest_attachment(source)
        assert store._poisoned is True
        assert authority.state is AuthorityState.POISONED
    finally:
        if authority.state is not AuthorityState.CLOSED:
            authority.close()

    authority, store = _open_store(tmp_path / "cleanup")
    statement_failed = False
    original_discard = type(store._attachment_manager).discard_capture
    monkeypatch.setattr(Connection, "rollback", original_rollback)

    def fail_discard(manager, operation_id):
        if manager is store._attachment_manager:
            raise OSError("injected capture cleanup failure")
        return original_discard(manager, operation_id)

    monkeypatch.setattr(Connection, "execute", fail_statement)
    monkeypatch.setattr(type(store._attachment_manager), "discard_capture", fail_discard)
    try:
        with pytest.raises(StateError, match="capture cleanup failed"):
            store.ingest_attachment(source)
        assert store._poisoned is True
        assert authority.state is AuthorityState.POISONED
    finally:
        if authority.state is not AuthorityState.CLOSED:
            authority.close()


def test_n1_close_after_prior_rollback_poison_remains_terminal(
    tmp_path: Path, monkeypatch
):
    root = tmp_path / "root"
    source = tmp_path / "prior-poison.txt"
    source.write_bytes(b"prior rollback poison then close")
    authority, store = _open_store(root)
    original_execute = Connection.execute
    original_rollback = Connection.rollback
    original_discard = type(store._attachment_manager).discard_capture
    statement_failed = False
    rollback_calls = 0
    discard_calls = 0
    poison_events: list[tuple[str, AuthorityState, bool]] = []
    original_poison = authority.poison

    def record_poison(reason):
        poison_events.append((reason, authority.state, store._poisoned))
        return original_poison(reason)

    def fail_statement(self, statement, *args, **kwargs):
        nonlocal statement_failed
        if not statement_failed and "INSERT INTO ATTACHMENT_BLOBS" in str(statement).upper():
            statement_failed = True
            raise OSError("injected statement failure before prior poison")
        return original_execute(self, statement, *args, **kwargs)

    def fail_rollback(self):
        nonlocal rollback_calls
        rollback_calls += 1
        raise OSError("injected prior rollback failure")

    def count_discard(manager, operation_id):
        nonlocal discard_calls
        if manager is store._attachment_manager:
            discard_calls += 1
        return original_discard(manager, operation_id)

    monkeypatch.setattr(authority, "poison", record_poison)
    monkeypatch.setattr(Connection, "execute", fail_statement)
    monkeypatch.setattr(Connection, "rollback", fail_rollback)
    monkeypatch.setattr(type(store._attachment_manager), "discard_capture", count_discard)
    selected = _arm_selected_connection_close(monkeypatch, post_real=True)
    try:
        with pytest.raises(StateError, match="connection close is uncertain") as raised:
            store.ingest_attachment(source)
        assert isinstance(raised.value.__cause__, OSError)
        assert statement_failed is True
        assert rollback_calls >= 1
        assert discard_calls == 0
        assert selected["connection"] is not None
        assert selected["close_calls"] == 1
        assert len(poison_events) >= 2
        assert poison_events[0][0].startswith(
            "attachment transaction rollback failed:"
        )
        assert poison_events[1][0].startswith("attachment lifecycle failure:")
        assert poison_events[1][1] is AuthorityState.POISONED
        assert poison_events[1][2] is True
        assert store._poisoned is True
        assert authority.state is AuthorityState.POISONED

        before_rejection = _tree_fingerprint(root)
        with pytest.raises(StateError, match="not admitting work"):
            store.list_attachments()
        with pytest.raises(StateError, match="not admitting work"):
            store.create_chat(
                Chat(
                    id=str(uuid7()),
                    title="rejected after prior poison",
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
        assert _tree_fingerprint(root) == before_rejection

        # A later successful release cannot reopen this same poisoned authority.
        monkeypatch.undo()
        selected["connection"].close()
        assert selected["close_calls"] == 1
        assert store._poisoned is True
        assert authority.state is AuthorityState.POISONED
    finally:
        if authority.state is not AuthorityState.CLOSED:
            authority.close()

    blobs, attachments, areas = _raw_attachment_lifecycle(root)
    assert blobs == []
    assert attachments == []
    assert areas["captures"]
    assert areas["staging"] == ()
    assert areas["objects"] == ()
    recovered_authority, recovered = _open_store(root)
    try:
        assert recovered_authority.state is AuthorityState.READY
        assert _raw_attachment_lifecycle(root) == (
            [],
            [],
            {"captures": (), "staging": (), "objects": (), "gc": ()},
        )
    finally:
        if recovered_authority.state is not AuthorityState.CLOSED:
            recovered_authority.close()


def test_n3_legacy_auth_provenance_mismatch_rejects_before_any_effect(tmp_path: Path):
    authority, store = _open_store(tmp_path / "root")
    backend = LegacyRecordingBackend()
    backend.api_key_env = "BACKEND_KEY"
    ids = Uuid7Factory()
    try:
        with pytest.raises(StateError, match="binding is inconsistent"):
            BotsApplication(
                store,
                EventBus(SystemClock(), ids),
                backend,
                generation_mode=GenerationMode.LEGACY_PHASE3_LOCAL_OPENAI,
                backend_id=backend.backend_id,
                provider_id=backend.provider_id,
                base_url=backend.base_url,
                api_key_env="CONSTRUCTOR_KEY",
            )
        assert backend.requests == []
        assert store.list_chats() == ()
        with _private_engine_connection(store) as connection:
            assert connection.exec_driver_sql(
                "SELECT count(*) FROM generation_attempts"
            ).scalar_one() == 0
    finally:
        if authority.state is not AuthorityState.CLOSED:
            authority.close()


def test_n2_acquisition_replays_all_ten_labelled_topology_edges(
    tmp_path: Path, monkeypatch
):
    observed: list[str] = []
    original = DataRootAuthority._sync_topology_parent

    def record(self, parent_fd, edge_label):
        observed.append(edge_label)
        return original(self, parent_fd, edge_label)

    monkeypatch.setattr(DataRootAuthority, "_sync_topology_parent", record)
    authority = DataRootAuthority(tmp_path / "root").acquire()
    try:
        assert tuple(observed) == TOPOLOGY_EDGE_LABELS
    finally:
        authority.close()


@pytest.mark.parametrize("edge_label", TOPOLOGY_EDGE_LABELS)
def test_n2_persistent_topology_barrier_failure_is_retried_before_ready(
    tmp_path: Path, monkeypatch, edge_label: str
):
    original = DataRootAuthority._sync_topology_parent
    attempts: list[str] = []

    def fail_selected(self, parent_fd, current_label):
        attempts.append(current_label)
        if current_label == edge_label:
            raise OSError(f"persistent barrier fault: {current_label}")
        return original(self, parent_fd, current_label)

    monkeypatch.setattr(DataRootAuthority, "_sync_topology_parent", fail_selected)
    with pytest.raises(AuthorityError):
        DataRootAuthority(tmp_path / "root").acquire()
    first_count = attempts.count(edge_label)
    with pytest.raises(AuthorityError):
        DataRootAuthority(tmp_path / "root").acquire()
    assert attempts.count(edge_label) > first_count
    monkeypatch.setattr(DataRootAuthority, "_sync_topology_parent", original)
    authority = DataRootAuthority(tmp_path / "root").acquire()
    try:
        assert authority.acquired
    finally:
        authority.close()


def _revision(database: Path) -> str:
    with sqlite3.connect(database) as connection:
        return str(
            connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        )


def _initialise_root(root: Path) -> None:
    authority = _new_authority(root)
    authority.close()


def _historical_root(root: Path, revision: str) -> None:
    seed = root.parent / f".{root.name}-{revision}.sqlite3"
    upgrade_to(seed, revision)
    os.chmod(seed, 0o600)
    _initialise_root(root)
    os.rename(seed, root / "database" / "state.sqlite3")


def _leave_source_sidecar(database: Path, mode: str) -> None:
    source = """
import os
import sqlite3
import sys

database, mode = sys.argv[1], sys.argv[2]
connection = sqlite3.connect(database)
if mode == 'wal':
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute('PRAGMA wal_autocheckpoint=0')
    connection.execute('PRAGMA user_version=17')
    connection.commit()
else:
    connection.execute('PRAGMA journal_mode=DELETE')
    connection.execute('BEGIN IMMEDIATE')
    connection.execute('PRAGMA user_version=23')
os._exit(0)
"""
    subprocess.run(
        [sys.executable, "-c", source, os.fspath(database), mode],
        check=True,
        cwd=REPO,
    )
    suffix = "-wal" if mode == "wal" else "-journal"
    assert database.with_name(database.name + suffix).is_file()


def _fault_process(root: Path, point: str, *, verify_error: bool = False) -> None:
    source = """
import os
import sys
from bots5.infrastructure.data_root_authority import DataRootAuthority
from bots5.infrastructure.persistence import migration_runner

root, target, verify_error = sys.argv[1], sys.argv[2], sys.argv[3] == '1'
if verify_error:
    original = migration_runner._verify_database
    calls = 0
    issued = False
    def fail_second(*args, **kwargs):
        global calls, issued
        calls += int(bool(kwargs.get('phase6')))
        if calls == 2 and not issued:
            issued = True
            raise RuntimeError('injected promoted validation failure')
        return original(*args, **kwargs)
    migration_runner._verify_database = fail_second
else:
    def die(name):
        if name == target or (target.endswith('*') and name.startswith(target[:-1])):
            os._exit(91)
    migration_runner._TEST_FAULT_HOOK = die
authority = DataRootAuthority(root).acquire()
try:
    authority.open_store()
except RuntimeError:
    if verify_error:
        os._exit(92)
    raise
os._exit(0)
"""
    completed = subprocess.run(
        [sys.executable, "-c", source, os.fspath(root), point, "1" if verify_error else "0"],
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": os.fspath(REPO / "src")},
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    expected = 92 if verify_error else 91
    assert completed.returncode == expected, (
        completed.returncode,
        completed.stdout,
        completed.stderr,
    )


def _restore_fault_process(root: Path, point: str) -> None:
    source = """
import os
import sys
from bots5.infrastructure.data_root_authority import DataRootAuthority
from bots5.infrastructure.persistence import migration_runner

root, target = sys.argv[1], sys.argv[2]
original = migration_runner._verify_database
calls = 0
issued = False
def fail_second(*args, **kwargs):
    global calls, issued
    calls += int(bool(kwargs.get('phase6')))
    if calls == 2 and not issued:
        issued = True
        raise RuntimeError('injected promoted validation failure')
    return original(*args, **kwargs)
def die(name):
    if name == target or (target.endswith('*') and name.startswith(target[:-1])):
        os._exit(91)
migration_runner._verify_database = fail_second
migration_runner._TEST_FAULT_HOOK = die
DataRootAuthority(root).acquire().open_store()
os._exit(0)
"""
    completed = subprocess.run(
        [sys.executable, "-c", source, os.fspath(root), point],
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": os.fspath(REPO / "src")},
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 91, (
        completed.returncode,
        completed.stdout,
        completed.stderr,
    )


def _converge_after_restore_fault(root: Path) -> None:
    for _ in range(4):
        authority = _new_authority(root)
        try:
            try:
                store = authority.open_store()
            except RuntimeError:
                continue
            store.close()
        finally:
            if authority.state not in {
                AuthorityState.CLOSED,
                AuthorityState.FAILED_CLOSED,
            }:
                authority.close()
        if _revision(root / "database" / "state.sqlite3") == HEAD:
            break
    _restart_twice(root)


def _persistent_phase6_validation_failure(root: Path) -> None:
    source = """
import os
import sys
from bots5.infrastructure.data_root_authority import DataRootAuthority
from bots5.infrastructure.persistence import migration_runner

root = sys.argv[1]
original = migration_runner._verify_database
def fail_phase6(*args, **kwargs):
    if kwargs.get('phase6'):
        raise RuntimeError('persistent normal VFS validation failure')
    return original(*args, **kwargs)
migration_runner._verify_database = fail_phase6
authority = DataRootAuthority(root).acquire()
try:
    authority.open_store()
except RuntimeError:
    os._exit(92)
os._exit(0)
"""
    completed = subprocess.run(
        [sys.executable, "-c", source, os.fspath(root)],
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": os.fspath(REPO / "src")},
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 92, (
        completed.returncode,
        completed.stdout,
        completed.stderr,
    )


def _attachment_fault_process(root: Path, source_path: Path, point: str) -> None:
    source = """
import os
import sys
from bots5.infrastructure import attachments
from bots5.infrastructure.data_root_authority import DataRootAuthority
from bots5.infrastructure.persistence import sqlite

root, source_path, target = sys.argv[1:]
def die(name):
    if name == target:
        os._exit(91)
attachments._TEST_FAULT_HOOK = die
sqlite._TEST_FAULT_HOOK = die
store = DataRootAuthority(root).acquire().open_store()
store.ingest_attachment(source_path)
os._exit(0)
"""
    completed = subprocess.run(
        [sys.executable, "-c", source, os.fspath(root), os.fspath(source_path), point],
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": os.fspath(REPO / "src")},
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 91, (
        completed.returncode,
        completed.stdout,
        completed.stderr,
    )


def _gc_fault_process(root: Path, point: str) -> None:
    source = """
import os
import sys
from bots5.infrastructure import attachments
from bots5.infrastructure.data_root_authority import DataRootAuthority
from bots5.infrastructure.persistence import sqlite

root, target = sys.argv[1:]
def die(name):
    if name == target:
        os._exit(91)
attachments._TEST_FAULT_HOOK = die
sqlite._TEST_FAULT_HOOK = die
store = DataRootAuthority(root).acquire().open_store()
store.gc_attachments()
os._exit(0)
"""
    completed = subprocess.run(
        [sys.executable, "-c", source, os.fspath(root), point],
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": os.fspath(REPO / "src")},
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 91, (
        completed.returncode,
        completed.stdout,
        completed.stderr,
    )


def _restart_twice(root: Path) -> None:
    for _ in range(2):
        authority, store = _open_store(root)
        assert authority.state is AuthorityState.READY
        store.close()
    assert _revision(root / "database" / "state.sqlite3") == HEAD
    assert list((root / "database" / "migration").iterdir()) == []
    assert list((root / "recovery").iterdir()) == []


def _competing_acquire(root: Path) -> subprocess.CompletedProcess[str]:
    source = """
import sys
from bots5.infrastructure.data_root_authority import DataRootAuthority
try:
    value = DataRootAuthority(sys.argv[1]).acquire()
except BaseException:
    raise SystemExit(73)
else:
    value.close()
"""
    return subprocess.run(
        [sys.executable, "-c", source, os.fspath(root)],
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": os.fspath(REPO / "src")},
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )


def _close_failure_process(root: Path, claim_class: str, mode: str) -> None:
    source = """
import errno
import os
import sys
from bots5.infrastructure.data_root_authority import AuthorityState, DataRootAuthority

root, claim_class, mode = sys.argv[1:]
authority = DataRootAuthority(root).acquire()
store = authority.open_store()
if claim_class == 'main':
    claim = authority._main_claim
elif claim_class == 'root':
    claim = authority._root_claim
elif claim_class == 'descendant':
    claim = authority._descendant_claims['attachments/objects']
else:
    claim = authority._ancestor_claims[0]
target = claim.fd
original = os.close
issued = False
def fail(fd):
    global issued
    if fd == target and not issued:
        issued = True
        if mode == 'uncertain':
            original(fd)
            raise OSError(errno.EINTR, 'issued but uncertain')
        raise OSError(errno.EIO, 'not issued')
    return original(fd)
os.close = fail
try:
    authority.close()
except BaseException:
    pass
assert authority.state is AuthorityState.FAILED_CLOSED
assert claim.status == 'UNKNOWN'
assert ('logical-root', 'HELD') in authority.claim_inventory
try:
    DataRootAuthority(root).acquire()
except BaseException:
    os._exit(0)
os._exit(3)
"""
    completed = subprocess.run(
        [sys.executable, "-c", source, os.fspath(root), claim_class, mode],
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": os.fspath(REPO / "src")},
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, (
        completed.returncode,
        completed.stdout,
        completed.stderr,
    )


def _close_barrier_failure_process(root: Path, barrier: str, mode: str = "before") -> None:
    source = """
import errno
import os
import sys
from bots5.infrastructure import data_root_authority as module
from bots5.infrastructure.data_root_authority import AuthorityState, DataRootAuthority

root, barrier, mode = sys.argv[1:]
authority = DataRootAuthority(root).acquire()
store = authority.open_store()
if barrier == 'store-close':
    def fail_store():
        raise OSError(errno.EIO, 'store close failed')
    store._close_under_authority = fail_store
elif barrier == 'main-fsync':
    target = authority._main_claim.fd
    original = os.fsync
    def fail_fsync(fd):
        if fd == target:
            raise OSError(errno.EIO, 'checkpoint fsync failed')
        return original(fd)
    os.fsync = fail_fsync
elif barrier == 'vfs-close':
    def fail_vfs(self):
        raise OSError(errno.EIO, 'VFS unregister failed')
    type(authority._vfs).close = fail_vfs
else:
    original = module._close_socket
    def fail_socket(value):
        if mode == 'after':
            original(value)
        raise OSError(errno.EINTR, 'logical close uncertain')
    module._close_socket = fail_socket
try:
    authority.close()
except BaseException:
    pass
assert authority.state is AuthorityState.FAILED_CLOSED
if barrier in {'store-close', 'main-fsync'}:
    assert authority._physical_release_complete is True
    assert ('logical-root', 'RELEASED') in authority.claim_inventory
    if barrier == 'main-fsync':
        os.fsync = original
    successor = DataRootAuthority(root).acquire()
    successor.close()
    os._exit(0)
assert ('logical-root', 'HELD' if barrier == 'vfs-close' else 'UNKNOWN') in authority.claim_inventory
if barrier == 'vfs-close':
    assert any(status == 'HELD' for label, status in authority.claim_inventory if label != 'logical-root')
try:
    DataRootAuthority(root).acquire()
except BaseException:
    os._exit(0)
os._exit(3)
"""
    completed = subprocess.run(
        [sys.executable, "-c", source, os.fspath(root), barrier, mode],
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": os.fspath(REPO / "src")},
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, (
        completed.returncode,
        completed.stdout,
        completed.stderr,
    )


def _vfs_private_close_failure_process(root: Path, slot: int, mode: int) -> None:
    source = r'''
import fcntl
import os
import sys
from bots5.core.errors import AuthorityError
from bots5.infrastructure.attachments import AttachmentIntegrityError
from bots5.infrastructure.data_root_authority import AuthorityState, DataRootAuthority

root, slot_text, mode_text = sys.argv[1:]
slot = int(slot_text)
mode = int(mode_text)
authority = DataRootAuthority(root).acquire()
store = authority.open_store()
manager = store._attachment_manager
vfs = authority._vfs
old_fds = vfs._test_private_fds()
vfs._test_inject_close_fault(slot, mode)
try:
    authority.close()
except AuthorityError as exc:
    error = str(exc)
else:
    os._exit(20)
assert authority.state is AuthorityState.FAILED_CLOSED
assert 'rooted VFS private close incomplete' in error
try:
    authority.close()
except AuthorityError as replayed:
    assert str(replayed) == error
else:
    os._exit(25)
expected = ['RELEASED', 'RELEASED', 'RELEASED', 'RELEASED']
expected[slot - 1] = 'UNKNOWN'
assert [status for _, status in vfs.close_inventory] == expected
authority_vfs = [
    status for label, status in authority.claim_inventory
    if label.startswith('rooted-vfs[')
]
assert authority_vfs[-4:] == expected
assert ('logical-root', 'HELD') in authority.claim_inventory
assert all(
    status == 'RELEASED'
    for label, status in authority.claim_inventory
    if label.startswith('descendant:') or label == 'root'
)

def assert_wrappers_reject():
    for operation in (lambda: vfs.open_count, vfs.connect, vfs.close):
        try:
            operation()
        except AuthorityError:
            pass
        else:
            os._exit(21)
    try:
        manager.inventory('objects')
    except (AttachmentIntegrityError, AuthorityError):
        pass
    else:
        os._exit(22)
if mode == 2:
    # All three retained private close calls consumed their original fds. Force every old
    # number to identify an unrelated directory and prove retained wrappers
    # never close or use a recycled number.
    identities = []
    for index, fd in enumerate(old_fds):
        directory = root + '-external-' + str(index)
        os.mkdir(directory, 0o700)
        with open(os.path.join(directory, 'sentinel'), 'wb') as stream:
            stream.write(('sentinel-' + str(index)).encode())
        opened = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        high = fcntl.fcntl(opened, fcntl.F_DUPFD_CLOEXEC, 200)
        os.close(opened)
        os.dup2(high, fd, inheritable=False)
        os.close(high)
        identities.append(os.fstat(fd))
    assert_wrappers_reject()
    for fd, identity in zip(old_fds, identities):
        current = os.fstat(fd)
        assert (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino)
    for index in range(3):
        with open(root + '-external-' + str(index) + '/sentinel', 'rb') as stream:
            assert stream.read() == ('sentinel-' + str(index)).encode()
else:
    # The deterministic pre-issue failure leaves this one fd intentionally
    # process-held; the other private fds were still each attempted once.
    assert_wrappers_reject()
    os.fstat(old_fds[slot - 1])
    for index, fd in enumerate(old_fds):
        if index == slot - 1:
            continue
        try:
            os.fstat(fd)
        except OSError as exc:
            assert exc.errno == 9
        else:
            os._exit(24)
try:
    DataRootAuthority(root).acquire()
except BaseException:
    pass
else:
    os._exit(23)
if mode == 2:
    for fd in old_fds:
        os.close(fd)
os._exit(0)
'''
    completed = subprocess.run(
        [sys.executable, "-c", source, os.fspath(root), str(slot), str(mode)],
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": os.fspath(REPO / "src")},
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, (
        completed.returncode,
        completed.stdout,
        completed.stderr,
    )


def test_context_builder_uses_exact_whole_turn_suffix_and_closed_budget():
    builder = ContextBuilder(ExactAdapter())
    current = ContextSource("current", "current_user", "user", "now")
    history = (
        (
            ContextSource("u1", "history", "user", "old"),
            ContextSource("a1", "history", "assistant", "reply"),
        ),
        (
            ContextSource("u2", "history", "user", "new"),
            ContextSource("a2", "history", "assistant", "answer"),
        ),
    )
    plan = builder.build(
        current_user=current,
        historical_turns=history,
        context_window=len("user:new\nassistant:answer\nuser:now") + 1,
        context_window_provenance="test:exact",
        output_reserve=1,
    )
    assert plan.included_sources == ("u2", "a2", "current")
    assert plan.excluded_sources == ("u1", "a1")
    assert plan.budget.headroom == 0
    assert len(plan.sources) == len({item.source_id for item in plan.sources})
    with pytest.raises(ContextBuildError, match="mandatory context exceeds"):
        builder.build(
            current_user=current,
            context_window=1,
            context_window_provenance="test:overflow",
            output_reserve=0,
        )


def test_send_persists_one_frozen_v3_plan_and_exact_wire(tmp_path: Path):
    async def scenario():
        backend = RecordingBackend()
        application, store, _ = _configured_application(tmp_path / "root", backend)
        try:
            chat = await application.create_chat()
            attempt = await application.send_message(chat.id, "hello")
            await _finish(application, attempt.id)
            snapshot = json.loads(attempt.request_snapshot)
            plan = snapshot["context_plan"]
            with _private_engine_connection(store) as connection:
                persisted = connection.execute(
                    text(
                        "SELECT canonical_digest, wire_representation_digest "
                        "FROM context_plans WHERE attempt_id = :id"
                    ),
                    {"id": attempt.id},
                ).one()
            assert persisted.canonical_digest == plan["canonical_digest"]
            assert persisted.wire_representation_digest == plan[
                "wire_representation_sha256"
            ]
            assert len(backend.requests) == 1
            assert hashlib.sha256(backend.requests[0].wire_representation).hexdigest() == plan[
                "wire_representation_sha256"
            ]
        finally:
            await application.close()

    asyncio.run(scenario())


def test_no_public_path_store_or_migration_facade_and_no_legacy_schema_fields(tmp_path: Path):
    root = tmp_path / "root"
    authority, store = _open_store(root)
    try:
        assert not hasattr(store, "engine")
        with pytest.raises(TypeError):
            SQLiteAppStateStore.open(root / "database" / "state.sqlite3")
        with pytest.raises(TypeError):
            upgrade_database(root / "database" / "state.sqlite3")
        paths = resolve_app_paths(root)
        assert not hasattr(paths, "database")
        assert not hasattr(paths, "authority_lock")
        with _private_engine_connection(store) as connection:
            columns = {
                row[1]
                for row in connection.exec_driver_sql(
                    "PRAGMA table_info(attachment_blobs)"
                ).fetchall()
            }
        assert "storage_path" not in columns
        assert not any("reservation" in name for name in columns)
        assert authority._vfs is not None
        assert not authority._vfs.synthetic.startswith("/")
    finally:
        store.close()


def test_exact_logical_claim_survives_root_path_replacement(tmp_path: Path):
    root = tmp_path / "root"
    authority = _new_authority(root)
    old = tmp_path / "held-root"
    os.rename(root, old)
    root.mkdir(mode=0o700)
    sentinel = root / "sentinel"
    sentinel.write_bytes(b"replacement")
    try:
        assert _competing_acquire(root).returncode == 73
        store = authority.open_store()
        now = datetime.now(UTC)
        store.create_chat(Chat(str(uuid7()), "Pinned", now, now))
        assert sentinel.read_bytes() == b"replacement"
        assert (old / "database" / "state.sqlite3").is_file()
        store.close()
    finally:
        if authority.state not in {AuthorityState.CLOSED, AuthorityState.FAILED_CLOSED}:
            authority.close()
    contender = _new_authority(root)
    contender.close()


def test_nested_roots_exclude_in_both_orders_and_siblings_coexist(tmp_path: Path):
    parent = tmp_path / "parent"
    first = _new_authority(parent)
    try:
        assert _competing_acquire(parent / "attachments").returncode == 73
    finally:
        first.close()
    nested = _new_authority(parent / "attachments")
    try:
        assert _competing_acquire(parent).returncode == 73
    finally:
        nested.close()

    siblings = tmp_path / "siblings"
    siblings.mkdir(mode=0o700)
    sibling_a = _new_authority(siblings / "a")
    sibling_b = _new_authority(siblings / "b")
    sibling_b.close()
    sibling_a.close()


def test_symlink_alias_and_unsafe_root_or_descendant_modes_fail_closed(tmp_path: Path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(AuthorityError):
        DataRootAuthority(alias.absolute()).acquire()

    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o755)
    with pytest.raises(AuthorityError):
        DataRootAuthority(unsafe.absolute()).acquire()
    assert not (unsafe / "database").exists()

    root = tmp_path / "descendant"
    _initialise_root(root)
    os.chmod(root / "database", 0o755)
    with pytest.raises(AuthorityError):
        DataRootAuthority(root.absolute()).acquire()
    assert not (root / "database" / "state.sqlite3").exists()

    wrong_type = tmp_path / "not-a-directory"
    wrong_type.write_bytes(b"evidence")
    with pytest.raises(AuthorityError):
        DataRootAuthority(wrong_type.absolute()).acquire()
    assert wrong_type.read_bytes() == b"evidence"

    for invalid in ("relative", "/", "/tmp/../tmp/root", "\x00"):
        with pytest.raises(AuthorityError):
            DataRootAuthority(invalid)


@pytest.mark.parametrize(
    "relative",
    (
        "database",
        "attachments",
        "attachments/objects",
        "attachments/staging",
        "attachments/captures",
        "attachments/gc",
        "database/migration",
        "database/temp",
        "recovery",
    ),
)
def test_each_fixed_descendant_unsafe_mode_rejects_before_database_open(
    tmp_path: Path, relative: str
):
    root = tmp_path / "root"
    _initialise_root(root)
    target = root / relative
    os.chmod(target, 0o755)
    with pytest.raises(AuthorityError):
        DataRootAuthority(root.absolute()).acquire()
    assert not (root / "database" / "state.sqlite3").exists()


def test_missing_statx_or_rename_primitive_has_no_fallback(tmp_path: Path, monkeypatch):
    def no_statx(fd):
        del fd
        raise AuthorityError("statx mount identity is unavailable")

    monkeypatch.setattr(authority_module, "_identity", no_statx)
    with pytest.raises(AuthorityError, match="statx"):
        DataRootAuthority((tmp_path / "statx-root").absolute()).acquire()
    monkeypatch.undo()

    import bots5.infrastructure.attachments as attachment_module

    def unsupported(*args):
        del args
        raise OSError(errno.ENOSYS, "unsupported")

    monkeypatch.setattr(attachment_module, "_rename_exchange", unsupported)
    with pytest.raises(AuthorityError):
        DataRootAuthority((tmp_path / "rename-root").absolute()).acquire()
    assert not (tmp_path / "rename-root" / "database" / "state.sqlite3").exists()


def test_hard_linked_database_is_rejected_before_sqlite_open(tmp_path: Path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    _initialise_root(first)
    _initialise_root(second)
    seed = tmp_path / "seed.sqlite3"
    upgrade_to(seed, "0008_catalogue_refresh_outcomes")
    os.chmod(seed, 0o600)
    os.rename(seed, first / "database" / "state.sqlite3")
    os.link(
        first / "database" / "state.sqlite3",
        second / "database" / "state.sqlite3",
    )
    authority = _new_authority(first)
    try:
        with pytest.raises(AuthorityError, match="unsafe identity"):
            authority.open_store()
    finally:
        authority.close()
    assert not (first / "database" / "state.sqlite3-journal").exists()


def test_rooted_vfs_open_count_mixing_and_forbidden_sql(tmp_path: Path):
    first_authority, first = _open_store(tmp_path / "first")
    second_authority, second = _open_store(tmp_path / "second")
    try:
        assert first_authority._vfs is not None
        vfs = first_authority._vfs
        assert vfs.open_count == 0
        with _private_engine_connection(first) as connection:
            assert vfs.open_count == 1
            for statement in (
                "ATTACH DATABASE 'escape.sqlite3' AS escape",
                "DETACH DATABASE main",
                "PRAGMA journal_mode=WAL",
                "PRAGMA journal_mode=MEMORY",
                "PRAGMA writable_schema=ON",
                "VACUUM",
                "VACUUM INTO 'escape.sqlite3'",
            ):
                with pytest.raises(SqlAlchemyDatabaseError):
                    connection.exec_driver_sql(statement)
            assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "delete"
            assert connection.exec_driver_sql("PRAGMA synchronous").scalar_one() == 2
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA temp_store").scalar_one() == 2
            raw = connection.connection.driver_connection
            raw.enable_load_extension(True)
            with pytest.raises(sqlite3.DatabaseError):
                raw.load_extension("definitely-not-a-library")
            raw.enable_load_extension(False)
        assert vfs.open_count == 0
        assert not (REPO / "escape.sqlite3").exists()
        wrong_uri = f"file:{vfs.synthetic}?mode=rw&vfs={second_authority._vfs.name}"
        with pytest.raises(sqlite3.DatabaseError):
            sqlite3.connect(wrong_uri, uri=True)
    finally:
        second.close()
        first.close()


def test_rooted_vfs_uses_no_ambient_tmpdir_and_static_sqlite_fails_closed(tmp_path: Path):
    ambient = tmp_path / "ambient"
    ambient.mkdir()
    previous = os.environ.get("TMPDIR")
    os.environ["TMPDIR"] = os.fspath(ambient)
    authority, store = _open_store(tmp_path / "root")
    try:
        with _private_engine_connection(store) as connection:
            assert connection.exec_driver_sql("PRAGMA temp_store").scalar_one() == 2
            assert connection.exec_driver_sql(
                "WITH RECURSIVE values_to_sort(value) AS ("
                "SELECT 512 UNION ALL SELECT value - 1 FROM values_to_sort WHERE value > 1) "
                "SELECT value FROM values_to_sort ORDER BY value LIMIT 1"
            ).scalar_one() == 1
        assert tuple(ambient.iterdir()) == ()
    finally:
        store.close()
        if previous is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = previous
    static_python = REPO / ".venv" / "bin" / "python"
    completed = subprocess.run(
        [
            os.fspath(static_python),
            "-c",
            "from bots5.infrastructure.data_root_authority import DataRootAuthority; "
            "a=DataRootAuthority(__import__('sys').argv[1]).acquire(); a.open_store()",
            os.fspath(tmp_path / "static-root"),
        ],
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": os.fspath(REPO / "src")},
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode != 0
    error = completed.stdout + completed.stderr
    assert "SQLite" in error and ("instance" in error or "rooted" in error)


def _assert_fresh_inventory_has_an_independent_stream(root: Path) -> None:
    authority = _new_authority(root)
    retained = authority._directory_fd("attachments/objects")
    try:
        os.lseek(retained, 0, os.SEEK_SET)
        tuple(os.listdir(retained))
        retained_offset = os.lseek(retained, 0, os.SEEK_CUR)
        leaf = "0" * 64
        descriptor = os.open(
            leaf,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=retained,
        )
        os.close(descriptor)
        assert leaf in authority.fresh_directory_inventory("attachments/objects")
        assert os.lseek(retained, 0, os.SEEK_CUR) == retained_offset
        assert authority.claim_inventory[-2] == (
            "fresh-view:attachments/objects",
            "RELEASED",
        )
    finally:
        authority.close()


def test_f1_first_production_inventory_after_mutation_is_fresh_on_btrfs():
    build = REPO / "build"
    build.mkdir(exist_ok=True)
    filesystem = subprocess.run(
        ["findmnt", "-T", os.fspath(build), "-no", "FSTYPE"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert filesystem == "btrfs"
    with tempfile.TemporaryDirectory(prefix="phase6-f1-btrfs-", dir=build) as value:
        _assert_fresh_inventory_has_an_independent_stream(Path(value))


def test_f1_production_fresh_inventory_has_same_contract_on_tmpfs(tmp_path: Path):
    filesystem = subprocess.run(
        ["findmnt", "-T", os.fspath(tmp_path), "-no", "FSTYPE"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert filesystem == "tmpfs"
    _assert_fresh_inventory_has_an_independent_stream(tmp_path / "root")


def test_f1_fresh_view_identity_failure_returns_no_tuple_and_poisons(tmp_path: Path):
    authority = _new_authority(tmp_path / "root")
    objects_fd = authority._directory_fd("attachments/objects")
    os.fchmod(objects_fd, 0o755)
    try:
        with pytest.raises(AuthorityError, match="unsafe owner or permissions"):
            authority.fresh_directory_inventory("attachments/objects")
        assert authority.state is AuthorityState.FAILED_STARTUP
        assert authority.claim_inventory[-2] == (
            "fresh-view:attachments/objects",
            "RELEASED",
        )
    finally:
        os.fchmod(objects_fd, 0o700)
        authority.close()


def test_f1_fresh_view_close_uncertainty_is_process_terminal(tmp_path: Path):
    source = r'''
import os
import sys
from bots5.core.errors import AuthorityError
from bots5.infrastructure.data_root_authority import AuthorityState, DataRootAuthority
root = sys.argv[1]
authority = DataRootAuthority(root).acquire()
original = authority._close_claim
def uncertain(claim):
    if claim.label.startswith('fresh-view:'):
        claim.fd = None
        claim.status = 'UNKNOWN'
        raise AuthorityError('injected fresh close uncertainty')
    return original(claim)
authority._close_claim = uncertain
try:
    authority.fresh_directory_inventory('attachments/objects')
except AuthorityError:
    pass
else:
    os._exit(20)
assert authority.state is AuthorityState.FAILED_CLOSED
assert ('fresh-view:attachments/objects', 'UNKNOWN') in authority.claim_inventory
assert ('logical-root', 'HELD') in authority.claim_inventory
try:
    DataRootAuthority(root).acquire()
except AuthorityError:
    os._exit(0)
os._exit(21)
'''
    completed = subprocess.run(
        [sys.executable, "-c", source, os.fspath(tmp_path / "root")],
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": os.fspath(REPO / "src")},
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, (completed.stdout, completed.stderr)


@pytest.mark.parametrize("preexisting", (False, True))
def test_f3_native_delete_full_trace_orders_parent_barriers_before_main_write(
    tmp_path: Path, preexisting: bool
):
    root = tmp_path / "root"
    authority, store = _open_store(root)
    vfs = authority._vfs
    assert vfs is not None
    if preexisting:
        journal = root / "database" / "state.sqlite3-journal"
        journal.write_bytes(b"")
        journal.chmod(0o600)
    vfs._test_reset_trace()
    now = datetime.now(UTC)
    store.create_chat(Chat(str(uuid7()), "trace", now, now))
    trace = vfs._test_trace()
    assert 10 in trace  # MAIN_JOURNAL carried SQLITE_OPEN_CREATE.
    assert trace.index(3) < trace.index(5) < trace.index(1)
    assert trace.index(2) < trace.index(6) < trace.index(8)
    assert 12 not in trace
    store.close()


def test_f3_parent_sync_failure_excludes_cache_spill_main_writes(tmp_path: Path):
    source = r'''
import os
import sys
from datetime import UTC, datetime
from uuid6 import uuid7
from bots5.domain.clock import utc_iso
from bots5.infrastructure.data_root_authority import DataRootAuthority
root = sys.argv[1]
authority = DataRootAuthority(root).acquire()
store = authority.open_store()
vfs = authority._vfs
vfs._test_reset_trace()
vfs._test_inject_io_fault(5)
operation = authority.operation()
operation.__enter__()
connection = store._engine.connect()
failed = False
try:
    connection.exec_driver_sql('PRAGMA cache_size=1')
    connection.exec_driver_sql('BEGIN IMMEDIATE')
    try:
        for index in range(256):
            now = utc_iso(datetime.now(UTC))
            connection.exec_driver_sql(
                'INSERT INTO chats(id,title,created_at,updated_at,head_message_id,revision) '
                'VALUES(?,?,?,?,NULL,0)',
                (str(uuid7()), str(index) + ':' + ('x' * 16384), now, now),
            )
        connection.commit()
    except BaseException:
        failed = True
    trace = vfs._test_trace()
    assert failed
    assert 10 in trace and 3 in trace and 5 in trace
    assert 1 not in trace
finally:
    try:
        connection.rollback()
    except BaseException:
        pass
    connection.close()
    operation.__exit__(None, None, None)
os._exit(0)
'''
    completed = subprocess.run(
        [sys.executable, "-c", source, os.fspath(tmp_path / "root")],
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": os.fspath(REPO / "src")},
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, (completed.stdout, completed.stderr)


def test_f3_absent_delete_still_parent_syncs_and_reports_failure(tmp_path: Path):
    authority, store = _open_store(tmp_path / "root")
    vfs = authority._vfs
    assert vfs is not None
    vfs._test_reset_trace()
    vfs._test_inject_io_fault(8)
    assert vfs._test_delete_journal() != 0
    assert vfs._test_trace() == (8,)
    vfs._test_reset_trace()
    assert vfs._test_delete_journal() == 0
    assert vfs._test_trace() == (8,)
    store.close()


def test_f3_delete_proof_close_uncertainty_still_syncs_and_latches_unknown(
    tmp_path: Path,
):
    source = r'''
import os
import sys
from bots5.core.errors import AuthorityError
from bots5.infrastructure.data_root_authority import DataRootAuthority
root = sys.argv[1]
authority = DataRootAuthority(root).acquire()
store = authority.open_store()
journal = os.path.join(root, 'database', 'state.sqlite3-journal')
with open(journal, 'wb'):
    pass
os.chmod(journal, 0o600)
vfs = authority._vfs
vfs._test_reset_trace()
vfs._test_inject_io_fault(9)
assert vfs._test_delete_journal() != 0
trace = vfs._test_trace()
assert trace == (6, 9, 8)
try:
    store.close()
except AuthorityError:
    pass
else:
    os._exit(20)
assert ('native-open-files', 'UNKNOWN') in vfs.close_inventory
assert ('logical-root', 'HELD') in authority.claim_inventory
os._exit(0)
'''
    completed = subprocess.run(
        [sys.executable, "-c", source, os.fspath(tmp_path / "root")],
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": os.fspath(REPO / "src")},
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, (completed.stdout, completed.stderr)


@pytest.mark.parametrize("replacement", ("directory", "symlink"))
def test_database_directory_replacement_cannot_redirect_vfs(
    tmp_path: Path, replacement: str
):
    root = tmp_path / "root"
    authority, store = _open_store(root)
    held = root / "database-held"
    os.rename(root / "database", held)
    external = root / "database-replacement"
    external.mkdir(mode=0o700)
    if replacement == "directory":
        os.rename(external, root / "database")
        external = root / "database"
    else:
        (root / "database").symlink_to(external, target_is_directory=True)
    sentinel = external / "sentinel"
    sentinel.write_bytes(b"external")
    now = datetime.now(UTC)
    chat = Chat(str(uuid7()), "Pinned database", now, now)
    store.create_chat(chat)
    assert sentinel.read_bytes() == b"external"
    assert not (external / "state.sqlite3").exists()
    store.close()
    with sqlite3.connect(held / "state.sqlite3") as connection:
        assert connection.execute(
            "SELECT title FROM chats WHERE id = ?", (chat.id,)
        ).fetchone() == ("Pinned database",)
    assert authority.state is AuthorityState.CLOSED


def test_close_drains_admitted_operation_and_rejects_new_checkout(tmp_path: Path):
    root = tmp_path / "root"
    authority, store = _open_store(root)
    entered = threading.Event()
    release = threading.Event()
    closed = threading.Event()

    def hold_operation():
        with authority.operation():
            entered.set()
            release.wait(5)

    worker = threading.Thread(target=hold_operation)
    worker.start()
    assert entered.wait(2)

    def close_store():
        store.close()
        closed.set()

    closer = threading.Thread(target=close_store)
    closer.start()
    deadline = time.monotonic() + 2
    while authority.state is not AuthorityState.CLOSING and time.monotonic() < deadline:
        time.sleep(0.001)
    assert authority.state is AuthorityState.CLOSING
    assert not closed.is_set()
    with pytest.raises(AuthorityError):
        with authority.operation():
            pass
    with pytest.raises((AuthorityError, SqlAlchemyDatabaseError)):
        store._engine.connect()
    release.set()
    worker.join(5)
    closer.join(5)
    assert closed.is_set()
    assert authority.state is AuthorityState.CLOSED
    successor = _new_authority(root)
    successor.close()


@pytest.mark.parametrize("caller_count", (2, 8))
def test_concurrent_close_has_one_owner_and_one_success_result(
    tmp_path: Path, monkeypatch, caller_count: int
):
    root = tmp_path / "root"
    authority, store = _open_store(root)
    operation = authority.operation()
    operation.__enter__()
    connection = store._engine.connect()
    original = store._close_under_authority
    teardown_calls = 0
    teardown_lock = threading.Lock()
    barrier = threading.Barrier(caller_count + 1)
    results: list[BaseException | None] = []
    result_lock = threading.Lock()

    def counted_teardown():
        nonlocal teardown_calls
        with teardown_lock:
            teardown_calls += 1
        return original()

    monkeypatch.setattr(store, "_close_under_authority", counted_teardown)

    def close_caller():
        barrier.wait()
        result: BaseException | None = None
        try:
            store.close()
        except BaseException as exc:
            result = exc
        with result_lock:
            results.append(result)

    callers = [threading.Thread(target=close_caller) for _ in range(caller_count)]
    for caller in callers:
        caller.start()
    barrier.wait()
    _await_state(authority, AuthorityState.CLOSING)
    _await_condition_waiters(authority, caller_count)
    assert connection.exec_driver_sql("SELECT 1").scalar_one() == 1
    # The already-admitted owner may still nest while close drains it.
    with authority.operation():
        pass
    connection.close()
    operation.__exit__(None, None, None)
    for caller in callers:
        caller.join(10)
        assert not caller.is_alive()
    assert results == [None] * caller_count
    assert teardown_calls == 1
    assert authority.state is AuthorityState.CLOSED
    successor = _new_authority(root)
    successor.close()


def test_concurrent_close_keeps_logical_claim_across_path_replacement(
    tmp_path: Path, monkeypatch
):
    root = tmp_path / "root"
    authority, store = _open_store(root)
    held = tmp_path / "held"
    os.rename(root, held)
    root.mkdir(mode=0o700)
    sentinel = root / "sentinel"
    sentinel.write_bytes(b"replacement")
    entered = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []
    barrier = threading.Barrier(5)
    original = type(authority)._close_claim

    def paused_close_claim(self, claim):
        if self is authority and claim.label == "root":
            entered.set()
            assert release.wait(10)
        return original(self, claim)

    monkeypatch.setattr(type(authority), "_close_claim", paused_close_claim)

    def close_caller():
        barrier.wait()
        try:
            store.close()
        except BaseException as exc:
            errors.append(exc)

    callers = [threading.Thread(target=close_caller) for _ in range(4)]
    for caller in callers:
        caller.start()
    barrier.wait()
    assert entered.wait(10)
    _await_condition_waiters(authority, 3)
    assert _competing_acquire(root).returncode == 73
    assert sentinel.read_bytes() == b"replacement"
    release.set()
    for caller in callers:
        caller.join(10)
        assert not caller.is_alive()
    assert errors == []
    assert authority.state is AuthorityState.CLOSED
    assert sentinel.read_bytes() == b"replacement"
    successor = _new_authority(root)
    successor.close()


def test_concurrent_faulted_close_callers_receive_one_terminal_result(tmp_path: Path):
    root = tmp_path / "root"
    source = r'''
import os
import sys
import threading
from bots5.core.errors import AuthorityError
from bots5.infrastructure.data_root_authority import AuthorityState, DataRootAuthority

root = sys.argv[1]
authority = DataRootAuthority(root).acquire()
store = authority.open_store()
original = authority._close_claim
root_close_calls = 0
lock = threading.Lock()
fault_entered = threading.Event()
release_fault = threading.Event()

def fail_once(claim):
    global root_close_calls
    if claim.label == 'root':
        with lock:
            root_close_calls += 1
        fault_entered.set()
        assert release_fault.wait(10)
        claim.fd = None
        claim.status = 'UNKNOWN'
        raise AuthorityError('injected terminal close')
    return original(claim)

authority._close_claim = fail_once
barrier = threading.Barrier(9)
results = []

def caller():
    barrier.wait()
    try:
        store.close()
    except BaseException as exc:
        with lock:
            results.append((type(exc).__name__, str(exc)))

threads = [threading.Thread(target=caller) for _ in range(8)]
for thread in threads:
    thread.start()
barrier.wait()
assert fault_entered.wait(10)
deadline = __import__('time').monotonic() + 5
while len(authority._condition._waiters) < 7 and __import__('time').monotonic() < deadline:
    __import__('time').sleep(0.001)
assert len(authority._condition._waiters) >= 7
release_fault.set()
for thread in threads:
    thread.join(10)
assert all(not thread.is_alive() for thread in threads)
assert root_close_calls == 1
assert results == [('AuthorityError', 'data-root authority close failed')] * 8
assert authority.state is AuthorityState.FAILED_CLOSED
try:
    DataRootAuthority(root).acquire()
except BaseException:
    os._exit(0)
os._exit(3)
'''
    completed = subprocess.run(
        [sys.executable, "-c", source, os.fspath(root)],
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": os.fspath(REPO / "src")},
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    successor = _new_authority(root)
    successor.close()


def test_concurrent_close_preserves_fork_exec_boundaries_and_reacquires(tmp_path: Path):
    root = tmp_path / "root"
    source = r'''
import os
import sys
import threading
from bots5.infrastructure.data_root_authority import DataRootAuthority

root = sys.argv[1]
authority = DataRootAuthority(root).acquire()
store = authority.open_store()
operation = authority.operation()
operation.__enter__()
connection = store._engine.connect()
read_fd, write_fd = os.pipe()
fork_pid = os.fork()
if fork_pid == 0:
    os.close(read_fd)
    try:
        store.close()
    except BaseException:
        os.write(write_fd, b'failed-closed')
        os._exit(0)
    os._exit(4)
os.close(write_fd)
assert os.read(read_fd, 64) == b'failed-closed'
os.close(read_fd)
_, status = os.waitpid(fork_pid, 0)
assert status == 0
exec_pid = os.fork()
if exec_pid == 0:
    os.execl('/bin/sleep', 'sleep', '10')
barrier = threading.Barrier(5)
errors = []
lock = threading.Lock()
def caller():
    barrier.wait()
    try:
        store.close()
    except BaseException as exc:
        with lock:
            errors.append(exc)
threads = [threading.Thread(target=caller) for _ in range(4)]
for thread in threads:
    thread.start()
barrier.wait()
deadline = __import__('time').monotonic() + 5
while authority.state.value != 'CLOSING' and __import__('time').monotonic() < deadline:
    __import__('time').sleep(0.001)
assert authority.state.value == 'CLOSING'
deadline = __import__('time').monotonic() + 5
while len(authority._condition._waiters) < 4 and __import__('time').monotonic() < deadline:
    __import__('time').sleep(0.001)
assert len(authority._condition._waiters) >= 4
connection.close()
operation.__exit__(None, None, None)
for thread in threads:
    thread.join(10)
assert all(not thread.is_alive() for thread in threads)
assert errors == []
successor = DataRootAuthority(root).acquire()
successor.close()
os.kill(exec_pid, 9)
os.waitpid(exec_pid, 0)
os._exit(0)
'''
    completed = subprocess.run(
        [sys.executable, "-c", source, os.fspath(root)],
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": os.fspath(REPO / "src")},
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr


def test_admitted_ingest_retains_attachment_and_database_authority_during_close(
    tmp_path: Path, monkeypatch
):
    root = tmp_path / "root"
    source = tmp_path / "source.txt"
    source.write_bytes(b"admitted ingest")
    authority, store = _open_store(root)
    entered = threading.Event()
    release = threading.Event()
    manager = store._attachment_manager
    original = type(manager).capture
    results = []
    errors: list[BaseException] = []

    def paused_capture(self, value, *, filename=None):
        if self is manager:
            entered.set()
            assert release.wait(10)
        return original(self, value, filename=filename)

    monkeypatch.setattr(type(manager), "capture", paused_capture)

    def ingest():
        try:
            results.append(store.ingest_attachment(source))
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=ingest)
    worker.start()
    assert entered.wait(10)
    closer = threading.Thread(target=store.close)
    closer.start()
    _await_state(authority, AuthorityState.CLOSING)
    with pytest.raises(StateError):
        store.list_attachments()
    release.set()
    worker.join(10)
    closer.join(10)
    assert not worker.is_alive() and not closer.is_alive()
    assert errors == []
    assert len(results) == 1
    successor, reopened = _open_store(root)
    assert reopened.read_attachment_bytes(results[0].id) == b"admitted ingest"
    reopened.close()
    assert successor.state is AuthorityState.CLOSED


def test_admitted_gc_retains_filesystem_and_database_authority_during_close(
    tmp_path: Path, monkeypatch
):
    root = tmp_path / "root"
    source = tmp_path / "source.txt"
    source.write_bytes(b"admitted gc")
    authority, store = _open_store(root)
    attachment = store.ingest_attachment(source)
    store.delete_attachment(attachment.id)
    object_leaf = root / "attachments" / "objects" / attachment.blob_digest
    assert object_leaf.is_file()
    entered = threading.Event()
    release = threading.Event()
    original = store._enumerate_unreferenced_blobs
    results = []
    errors: list[BaseException] = []

    def paused_enumeration():
        entered.set()
        assert release.wait(10)
        return original()

    monkeypatch.setattr(store, "_enumerate_unreferenced_blobs", paused_enumeration)

    def collect():
        try:
            results.append(store.gc_attachments())
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=collect)
    worker.start()
    assert entered.wait(10)
    closer = threading.Thread(target=store.close)
    closer.start()
    _await_state(authority, AuthorityState.CLOSING)
    with pytest.raises(StateError):
        store.gc_attachments()
    release.set()
    worker.join(10)
    closer.join(10)
    assert not worker.is_alive() and not closer.is_alive()
    assert errors == []
    assert results == [(attachment.blob_digest,)]
    assert not object_leaf.exists()
    successor, reopened = _open_store(root)
    assert reopened.list_attachments() == ()
    reopened.close()
    assert successor.state is AuthorityState.CLOSED


def test_admitted_database_checkout_survives_close_and_new_checkout_rejects(
    tmp_path: Path, monkeypatch
):
    root = tmp_path / "root"
    authority, store = _open_store(root)
    entered = threading.Event()
    release = threading.Event()
    original = authority._connection_checkout
    worker_ident: list[int] = []
    results = []
    errors: list[BaseException] = []

    def paused_checkout():
        if worker_ident and threading.get_ident() == worker_ident[0]:
            entered.set()
            assert release.wait(10)
        return original()

    monkeypatch.setattr(authority, "_connection_checkout", paused_checkout)

    def query():
        worker_ident.append(threading.get_ident())
        try:
            results.append(store.list_attachments())
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=query)
    worker.start()
    assert entered.wait(10)
    closer = threading.Thread(target=store.close)
    closer.start()
    _await_state(authority, AuthorityState.CLOSING)
    with pytest.raises((AuthorityError, SqlAlchemyDatabaseError)):
        store._engine.connect()
    release.set()
    worker.join(10)
    closer.join(10)
    assert not worker.is_alive() and not closer.is_alive()
    assert errors == []
    assert results == [()]
    successor, reopened = _open_store(root)
    assert reopened.list_attachments() == ()
    reopened.close()
    assert successor.state is AuthorityState.CLOSED


@pytest.mark.parametrize(
    "claim_label",
    (
        "database-main",
        "descendant:database/temp",
        "descendant:database/migration",
        "descendant:recovery",
        "descendant:attachments/captures",
        "descendant:attachments/staging",
        "descendant:attachments/gc",
        "descendant:attachments/objects",
        "descendant:database",
        "descendant:attachments",
        "root",
        "ancestor:/",
    ),
)
@pytest.mark.parametrize("side", ("before", "after"))
def test_exact_logical_claim_blocks_at_every_physical_close_barrier(
    tmp_path: Path, monkeypatch, claim_label: str, side: str
):
    root = tmp_path / "root"
    authority, store = _open_store(root)
    del store
    held = tmp_path / "held"
    os.rename(root, held)
    root.mkdir(mode=0o700)
    sentinel = root / "sentinel"
    sentinel.write_bytes(b"replacement")
    entered = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []
    original = type(authority)._close_claim

    def pause(self, claim):
        if claim.label == claim_label and side == "before":
            entered.set()
            assert release.wait(10)
        result = original(self, claim)
        if claim.label == claim_label and side == "after":
            entered.set()
            assert release.wait(10)
        return result

    monkeypatch.setattr(type(authority), "_close_claim", pause)

    def close_owner():
        try:
            authority.close()
        except BaseException as exc:
            errors.append(exc)

    closer = threading.Thread(target=close_owner)
    closer.start()
    assert entered.wait(10), claim_label
    assert _competing_acquire(root).returncode == 73
    assert sentinel.read_bytes() == b"replacement"
    release.set()
    closer.join(10)
    assert not closer.is_alive()
    assert errors == []
    assert authority.state is AuthorityState.CLOSED
    successor = _new_authority(root)
    successor.close()


@pytest.mark.parametrize("claim_class", ("main", "descendant", "root", "ancestor"))
@pytest.mark.parametrize("mode", ("not_issued", "uncertain"))
def test_each_physical_close_class_failure_retains_logical_authority_until_exit(
    tmp_path: Path, claim_class: str, mode: str
):
    root = tmp_path / "root"
    _close_failure_process(root, claim_class, mode)
    successor = _new_authority(root)
    successor.close()


@pytest.mark.parametrize(
    "barrier,mode",
    (
        ("store-close", "before"),
        ("main-fsync", "before"),
        ("vfs-close", "before"),
        ("logical-close", "before"),
        ("logical-close", "after"),
    ),
)
def test_prephysical_and_final_logical_close_failures_never_admit_overlap(
    tmp_path: Path, barrier: str, mode: str
):
    root = tmp_path / "root"
    _close_barrier_failure_process(root, barrier, mode)
    successor = _new_authority(root)
    successor.close()


@pytest.mark.parametrize("slot", (1, 2, 3))
@pytest.mark.parametrize("mode", (1, 2))
def test_each_native_vfs_private_close_uncertainty_is_terminal_and_never_reused(
    tmp_path: Path, slot: int, mode: int
):
    root = tmp_path / f"root-{slot}-{mode}"
    _vfs_private_close_failure_process(root, slot, mode)
    successor = _new_authority(root)
    successor.close()


def test_clean_native_vfs_close_releases_exact_private_inventory(tmp_path: Path):
    authority, store = _open_store(tmp_path / "root")
    vfs = authority._vfs
    assert vfs is not None
    old_fds = vfs._test_private_fds()
    store.close()
    assert authority.state is AuthorityState.CLOSED
    assert vfs.close_inventory == (
        ("database-directory", "RELEASED"),
        ("main-claim", "RELEASED"),
        ("temp-directory", "RELEASED"),
        ("native-open-files", "RELEASED"),
    )
    for fd in old_fds:
        with pytest.raises(OSError) as captured:
            os.fstat(fd)
        assert captured.value.errno == errno.EBADF


def test_uncertain_physical_close_retains_logical_claim_until_process_exit(tmp_path: Path):
    root = tmp_path / "root"
    source = """
import errno
import os
import sys
from bots5.infrastructure.data_root_authority import AuthorityState, DataRootAuthority
root = sys.argv[1]
authority = DataRootAuthority(root).acquire()
target = authority._root_claim.fd
original = os.close
issued = False
def uncertain(fd):
    global issued
    if fd == target and not issued:
        issued = True
        original(fd)
        raise OSError(errno.EINTR, 'uncertain close')
    return original(fd)
os.close = uncertain
try:
    authority.close()
except BaseException:
    pass
assert authority.state is AuthorityState.FAILED_CLOSED
assert ('root', 'UNKNOWN') in authority.claim_inventory
try:
    DataRootAuthority(root).acquire()
except BaseException:
    os._exit(0)
os._exit(3)
"""
    completed = subprocess.run(
        [sys.executable, "-c", source, os.fspath(root)],
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": os.fspath(REPO / "src")},
        check=False,
        timeout=20,
    )
    assert completed.returncode == 0
    successor = _new_authority(root)
    successor.close()


@pytest.mark.parametrize("barrier", ("main", "database-directory"))
def test_r1_prephysical_close_failure_is_strongly_pinned_in_same_process(
    tmp_path: Path, barrier: str
):
    source = r'''
import gc
import os
import sys
import weakref
from bots5.core.errors import AuthorityError
from bots5.infrastructure import data_root_authority as module
from bots5.infrastructure.data_root_authority import AuthorityState, DataRootAuthority

root, barrier = sys.argv[1:]
authority = DataRootAuthority(root).acquire()
store = authority.open_store()
target = (
    authority._main_claim.fd
    if barrier == 'main'
    else authority._descendant_claims['database'].fd
)
original_fsync = os.fsync
def fail_target(fd):
    if fd == target:
        raise OSError(5, 'private injected payload')
    return original_fsync(fd)
os.fsync = fail_target
try:
    authority.close()
except AuthorityError as failure:
    first = str(failure)
else:
    os._exit(20)
finally:
    os.fsync = original_fsync
assert first == 'data-root authority close failed'
try:
    authority.close()
except AuthorityError as replay:
    assert str(replay) == first
else:
    os._exit(21)
assert authority.state is AuthorityState.FAILED_CLOSED
assert authority._physical_release_complete is True
assert ('logical-root', 'RELEASED') in authority.claim_inventory
assert all(status == 'RELEASED' for _, status in authority.claim_inventory)
reference = weakref.ref(authority)
store = None
authority = None
gc.collect()
retained = reference()
assert retained is None
successor = DataRootAuthority(root).acquire()
successor.close()
os._exit(0)
'''
    completed = subprocess.run(
        [sys.executable, "-c", source, os.fspath(tmp_path / barrier), barrier],
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": os.fspath(REPO / "src")},
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, (
        completed.returncode,
        completed.stdout,
        completed.stderr,
    )


@pytest.mark.parametrize(
    "scenario",
    (
        "rejected-xopen",
        "rejected-xdelete",
        "auxiliary-cleanup",
        "temporary-cleanup",
        "registration-unwind-1",
        "registration-unwind-2",
        "registration-unwind-3",
    ),
)
@pytest.mark.parametrize("mode", (1, 2))
def test_r2_untracked_native_close_uncertainty_is_terminal(
    tmp_path: Path, scenario: str, mode: int
):
    source = r'''
import fcntl
import gc
import os
import sys
import weakref
from bots5.core.errors import AuthorityError
from bots5.infrastructure import data_root_authority as module
from bots5.infrastructure.data_root_authority import AuthorityState, DataRootAuthority
from bots5.infrastructure.rooted_sqlite_vfs import (
    _test_inject_registration_cleanup_fault,
)

root, scenario, mode_text = sys.argv[1:]
mode = int(mode_text)
authority = DataRootAuthority(root).acquire()
external_fd = None
external_identity = None
if scenario.startswith('registration-unwind-'):
    slot = int(scenario.rsplit('-', 1)[1])
    _test_inject_registration_cleanup_fault(slot, mode)
    try:
        authority.open_store()
    except AuthorityError as failure:
        assert str(failure) == 'rooted VFS registration cleanup incomplete'
    else:
        os._exit(30)
    assert authority.state is AuthorityState.FAILED_CLOSED
else:
    store = authority.open_store()
    vfs = authority._vfs
    assert vfs is not None
    site = {
        'rejected-xopen': 1,
        'rejected-xdelete': 2,
        'auxiliary-cleanup': 3,
        'temporary-cleanup': 4,
    }[scenario]
    if scenario == 'rejected-xdelete':
        journal = os.path.join(root, 'database', 'state.sqlite3-journal')
        created = os.open(
            journal,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o600,
        )
        os.close(created)
    vfs._test_inject_untracked_close_fault(site, mode)
    result = (
        vfs._test_delete_journal()
        if scenario == 'rejected-xdelete'
        else vfs._test_open_cleanup_path(site)
    )
    assert result != 0
    recycled = vfs._test_last_untracked_fd()
    if scenario == 'rejected-xdelete':
        os.unlink(journal)
        os.fsync(authority._descendant_claims['database'].fd)
    if mode == 1:
        os.fstat(recycled)
    else:
        external = root + '-external'
        os.mkdir(external, 0o700)
        with open(os.path.join(external, 'sentinel'), 'wb') as stream:
            stream.write(b'unrelated')
        opened = os.open(external, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        high = fcntl.fcntl(opened, fcntl.F_DUPFD_CLOEXEC, 200)
        os.close(opened)
        os.dup2(high, recycled, inheritable=False)
        os.close(high)
        external_fd = recycled
        external_identity = os.fstat(external_fd)
    try:
        authority.close()
    except AuthorityError as failure:
        assert str(failure) == 'rooted VFS private close incomplete'
    else:
        os._exit(31)
    assert dict(vfs.close_inventory)['native-open-files'] == 'UNKNOWN'
    if mode == 1:
        os.fstat(recycled)
    else:
        current = os.fstat(external_fd)
        assert (current.st_dev, current.st_ino) == (
            external_identity.st_dev,
            external_identity.st_ino,
        )
        os.close(external_fd)
        external_fd = None
assert authority.state is AuthorityState.FAILED_CLOSED
assert any(
    label.endswith(':native-open-files') and status == 'UNKNOWN'
    for label, status in authority.claim_inventory
)
assert ('logical-root', 'HELD') in authority.claim_inventory
assert authority in module._TERMINAL_AUTHORITIES
reference = weakref.ref(authority)
store = None
vfs = None
authority = None
gc.collect()
assert reference() is not None
try:
    DataRootAuthority(root).acquire()
except AuthorityError:
    os._exit(0)
os._exit(32)
'''
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            source,
            os.fspath(tmp_path / f"{scenario}-{mode}"),
            scenario,
            str(mode),
        ],
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": os.fspath(REPO / "src")},
        check=False,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert completed.returncode == 0, (
        completed.returncode,
        completed.stdout,
        completed.stderr,
    )


def test_r3_fresh_inventory_admission_serializes_with_close(
    tmp_path: Path, monkeypatch
):
    authority, store = _open_store(tmp_path / "first")
    native = authority_module._native()
    entered = threading.Event()
    release = threading.Event()
    inventory: list[tuple[str, ...]] = []
    failures: list[BaseException] = []

    class PausedNative:
        def __getattr__(self, name):
            return getattr(native, name)

        def bots5_open_fresh_directory(self, retained_fd):
            entered.set()
            assert release.wait(10)
            return native.bots5_open_fresh_directory(retained_fd)

    monkeypatch.setattr(authority_module, "_native", lambda: PausedNative())

    def observe():
        try:
            inventory.append(
                authority.fresh_directory_inventory("attachments/objects")
            )
        except BaseException as exc:
            failures.append(exc)

    def close():
        try:
            authority.close()
        except BaseException as exc:
            failures.append(exc)

    observer = threading.Thread(target=observe)
    observer.start()
    assert entered.wait(10)
    closer = threading.Thread(target=close)
    closer.start()
    time.sleep(0.05)
    assert closer.is_alive()
    assert authority.state is AuthorityState.CLOSING
    release.set()
    observer.join(10)
    closer.join(10)
    assert not observer.is_alive() and not closer.is_alive()
    assert failures == []
    assert inventory == [()]
    assert authority.state is AuthorityState.CLOSED
    assert all(status == "RELEASED" for _, status in authority.claim_inventory)

    monkeypatch.undo()
    second, second_store = _open_store(tmp_path / "second")
    entered_close = threading.Event()
    release_close = threading.Event()
    close_errors: list[BaseException] = []
    original_store_close = second_store._close_under_authority

    def paused_store_close():
        entered_close.set()
        assert release_close.wait(10)
        return original_store_close()

    second_store._close_under_authority = paused_store_close

    def close_second():
        try:
            second.close()
        except BaseException as exc:
            close_errors.append(exc)

    second_closer = threading.Thread(target=close_second)
    second_closer.start()
    assert entered_close.wait(10)
    before = second.claim_inventory
    with pytest.raises(AuthorityError, match="rejected in CLOSING"):
        second.fresh_directory_inventory("attachments/objects")
    assert second.claim_inventory == before
    release_close.set()
    second_closer.join(10)
    assert not second_closer.is_alive()
    assert close_errors == []
    assert second.state is AuthorityState.CLOSED
    assert all(status == "RELEASED" for _, status in second.claim_inventory)

    third, third_store = _open_store(tmp_path / "third")
    admitted = threading.Event()
    continue_admitted = threading.Event()
    admitted_inventory: list[tuple[str, ...]] = []
    admitted_errors: list[BaseException] = []

    def admitted_observer():
        try:
            with third.operation():
                admitted.set()
                assert continue_admitted.wait(10)
                admitted_inventory.append(
                    third.fresh_directory_inventory("attachments/objects")
                )
        except BaseException as exc:
            admitted_errors.append(exc)

    admitted_thread = threading.Thread(target=admitted_observer)
    admitted_thread.start()
    assert admitted.wait(10)
    third_closer = threading.Thread(target=third.close)
    third_closer.start()
    deadline = time.monotonic() + 10
    while third.state is not AuthorityState.CLOSING and time.monotonic() < deadline:
        time.sleep(0.001)
    assert third.state is AuthorityState.CLOSING
    continue_admitted.set()
    admitted_thread.join(10)
    third_closer.join(10)
    assert not admitted_thread.is_alive() and not third_closer.is_alive()
    assert admitted_errors == []
    assert admitted_inventory == [()]
    assert third.state is AuthorityState.CLOSED
    assert all(status == "RELEASED" for _, status in third.claim_inventory)


def test_fork_child_is_failed_closed_and_parent_remains_live(tmp_path: Path):
    authority, store = _open_store(tmp_path / "root")
    manager = store._attachment_manager
    operation = authority.operation()
    operation.__enter__()
    connection = store._engine.connect()
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        results = []
        try:
            store.list_attachments()
        except BaseException:
            results.append("api-closed")
        try:
            connection.exec_driver_sql("SELECT count(*) FROM chats").scalar_one()
        except BaseException:
            results.append("vfs-closed")
        try:
            manager.inventory("objects")
        except BaseException:
            results.append("attachment-fs-closed")
        os.write(write_fd, ",".join(results).encode())
        os._exit(0)
    os.close(write_fd)
    child_result = os.read(read_fd, 1024)
    _, status = os.waitpid(pid, 0)
    os.close(read_fd)
    assert status == 0
    assert child_result == b"api-closed,vfs-closed,attachment-fs-closed"
    assert connection.exec_driver_sql("SELECT count(*) FROM chats").scalar_one() >= 0
    connection.close()
    operation.__exit__(None, None, None)
    exec_pid = os.fork()
    if exec_pid == 0:
        os.execl("/bin/true", "true")
    _, status = os.waitpid(exec_pid, 0)
    assert status == 0
    assert store.list_attachments() == ()
    store.close()


def test_all_authority_descriptors_are_cloexec_and_exec_child_holds_no_claim(
    tmp_path: Path,
):
    source = """
import fcntl
import os
import sys
from bots5.infrastructure.data_root_authority import DataRootAuthority

root = sys.argv[1]
authority = DataRootAuthority(root).acquire()
store = authority.open_store()
fds = [claim.fd for claim in authority._claims if claim.fd is not None]
fds += [authority._logical_socket.fileno()]
assert all(fcntl.fcntl(fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC for fd in fds)
pid = os.fork()
if pid == 0:
    os.execl('/bin/sleep', 'sleep', '2')
store.close()
try:
    contender = DataRootAuthority(root).acquire()
except BaseException:
    os.kill(pid, 9)
    os.waitpid(pid, 0)
    os._exit(3)
contender.close()
os.kill(pid, 9)
os.waitpid(pid, 0)
os._exit(0)
"""
    completed = subprocess.run(
        [sys.executable, "-c", source, os.fspath(tmp_path / "root")],
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": os.fspath(REPO / "src")},
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr


def test_concurrent_open_store_calls_publish_exactly_one_store(tmp_path: Path):
    authority = _new_authority(tmp_path / "root")
    barrier = threading.Barrier(3)

    def open_one():
        barrier.wait()
        return authority.open_store()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(open_one) for _ in range(2)]
        barrier.wait()
        stores = [future.result(timeout=30) for future in futures]
    assert stores[0] is stores[1]
    stores[0].close()
    assert authority.state is AuthorityState.CLOSED


@pytest.mark.parametrize("area", ("objects", "staging", "captures", "gc"))
@pytest.mark.parametrize("replacement", ("directory", "symlink"))
def test_pinned_attachment_directories_cannot_be_redirected(
    tmp_path: Path, area: str, replacement: str
):
    root = tmp_path / "root"
    _, store = _open_store(root)
    target = root / "attachments" / area
    held = root / "attachments" / f"{area}-held"
    os.rename(target, held)
    replacement_directory = root / "attachments" / f"{area}-replacement"
    replacement_directory.mkdir(mode=0o700)
    if replacement == "directory":
        os.rename(replacement_directory, target)
        replacement_directory = target
    else:
        target.symlink_to(replacement_directory, target_is_directory=True)
    sentinel = replacement_directory / "sentinel"
    sentinel.write_bytes(b"external")
    source = tmp_path / "payload.txt"
    source.write_text("capability rooted", encoding="utf-8")
    attachment = store.ingest_attachment(source)
    assert store.read_attachment_bytes(attachment.id) == b"capability rooted"
    assert sentinel.read_bytes() == b"external"
    assert tuple(replacement_directory.iterdir()) == (sentinel,)
    store.close()


def test_source_component_containment_fifo_symlink_and_mutation(tmp_path: Path, monkeypatch):
    _, store = _open_store(tmp_path / "root")
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    real = source_dir / "real.txt"
    real.write_bytes(b"a" * (1024 * 1024 + 1))
    final_alias = source_dir / "alias.txt"
    final_alias.symlink_to(real)
    intermediate_alias = tmp_path / "source-alias"
    intermediate_alias.symlink_to(source_dir, target_is_directory=True)
    fifo = source_dir / "fifo"
    os.mkfifo(fifo)
    socket_path = source_dir / "socket"
    unix_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    unix_socket.bind(os.fspath(socket_path))
    source_fd = os.open(real, os.O_RDONLY | os.O_CLOEXEC)
    try:
        for unsafe in (
            final_alias,
            intermediate_alias / "real.txt",
            Path(f"/proc/self/fd/{source_fd}"),
            fifo,
            Path("/dev/null"),
            socket_path,
            source_dir,
        ):
            with pytest.raises(StateError, match="safely|regular file"):
                store.ingest_attachment(unsafe)
    finally:
        os.close(source_fd)
        unix_socket.close()

    import bots5.infrastructure.attachments as attachment_module

    original = attachment_module._write_all
    changed = False

    def mutate_after_copy(fd, data):
        nonlocal changed
        original(fd, data)
        if not changed:
            changed = True
            real.write_bytes(b"changed")

    monkeypatch.setattr(attachment_module, "_write_all", mutate_after_copy)
    with pytest.raises(StateError, match="changed during capture"):
        store.ingest_attachment(real)
    monkeypatch.undo()
    assert store.list_attachments() == ()
    store.close()


def test_attachment_service_is_nonconstructible_and_tokens_are_not_bearers(tmp_path: Path):
    first_authority, first = _open_store(tmp_path / "first")
    _, second = _open_store(tmp_path / "second")
    source = tmp_path / "payload.txt"
    source.write_bytes(b"payload")
    try:
        with pytest.raises(TypeError, match="authority-private"):
            _AttachmentFS(first_authority, object())
        with first_authority.operation():
            captured = first._attachment_manager.capture(source)
        with second._authority.operation():
            with pytest.raises(AttachmentIntegrityError):
                second._attachment_manager.verify_capture(
                    captured.operation_id, captured.digest, captured.byte_size
                )
        with first_authority.operation():
            with pytest.raises(AttachmentIntegrityError, match="identity"):
                first._attachment_manager.verify_capture(
                    captured.operation_id, b"x" * 32, captured.byte_size
                )
            first._attachment_manager.capture_to_stage(
                captured.operation_id, captured.digest, captured.byte_size
            )
            first._attachment_manager.capture_to_stage(
                captured.operation_id, captured.digest, captured.byte_size
            )
            first._attachment_manager.discard_stage(captured.operation_id)
        first._attachment_manager.close()
        with pytest.raises(AttachmentIntegrityError, match="closed"):
            first._attachment_manager.capture(source)
    finally:
        second.close()
        first.close()


def test_retained_attachment_helper_rejects_every_fd_operation_after_reuse(
    tmp_path: Path,
):
    root = tmp_path / "root"
    authority, store = _open_store(root)
    manager = store._attachment_manager
    old_fds = (
        manager._objects_fd,
        manager._staging_fd,
        manager._captures_fd,
        manager._gc_fd,
    )
    source = tmp_path / "source.txt"
    source.write_bytes(b"source")
    operation_id = str(uuid7())
    stage_id = str(uuid7())
    gc_id = str(uuid7())
    digest = hashlib.sha256(b"object").digest()
    stage_digest = hashlib.sha256(b"stage").digest()
    store.close()
    assert authority.state is AuthorityState.CLOSED
    assert manager._closed
    assert manager._authority is None
    assert manager._mount_id is None
    assert (
        manager._objects_fd,
        manager._staging_fd,
        manager._captures_fd,
        manager._gc_fd,
    ) == (None, None, None, None)

    external = tuple(tmp_path / f"external-{index}" for index in range(4))
    payloads = (
        {digest.hex(): b"object", "sentinel": b"objects"},
        {stage_id: b"stage", "sentinel": b"staging"},
        {operation_id: b"capture", "sentinel": b"captures"},
        {gc_id: b"gc", "sentinel": b"gc-area"},
    )
    expected: list[dict[str, bytes]] = []
    recycled_identities = []
    for directory, fd, leaves in zip(external, old_fds, payloads, strict=True):
        directory.mkdir(mode=0o700)
        for leaf, payload in leaves.items():
            (directory / leaf).write_bytes(payload)
        expected.append(dict(leaves))
        opened = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        high = fcntl.fcntl(opened, fcntl.F_DUPFD_CLOEXEC, 200)
        os.close(opened)
        os.dup2(high, fd, inheritable=False)
        os.close(high)
        recycled_identities.append(os.fstat(fd))

    operations = (
        lambda: manager._open_source(source),
        lambda: manager.capture(source),
        lambda: manager._open_owned(old_fds[0], digest.hex()),
        lambda: manager._unlink_owned(old_fds[0], digest.hex()),
        lambda: manager._read_verified(old_fds[0], digest.hex(), digest, 6),
        lambda: manager._present(old_fds[0], digest.hex()),
        lambda: manager.capture_present(operation_id),
        lambda: manager.stage_present(stage_id),
        lambda: manager.object_present(digest),
        lambda: manager.verify_capture(operation_id, digest, 7),
        lambda: manager.verify_stage(stage_id, stage_digest, 5),
        lambda: manager.read_verified(digest, expected_size=6),
        lambda: manager.discard_capture(operation_id),
        lambda: manager.capture_to_stage(operation_id, digest, 6),
        lambda: manager.stage_to_object(stage_id, stage_digest, 5),
        lambda: manager.discard_stage(stage_id),
        lambda: manager.create_tombstone(gc_id, digest),
        lambda: manager._remove_gc_temp(gc_id, digest),
        lambda: manager.tombstone_present(gc_id, digest),
        lambda: manager.canonical_tombstone_present(gc_id, digest),
        lambda: manager.gc_payload_present(gc_id),
        lambda: manager.verify_gc_payload(gc_id, digest, 2),
        lambda: manager.exchange_object_to_gc(digest, gc_id),
        lambda: manager.delete_gc_payload(gc_id),
        lambda: manager.delete_canonical_tombstone(digest),
        lambda: manager.inventory("objects"),
    )
    try:
        for operation in operations:
            with pytest.raises(AttachmentIntegrityError, match="closed"):
                operation()
        for fd, identity in zip(old_fds, recycled_identities, strict=True):
            current = os.fstat(fd)
            assert (current.st_dev, current.st_ino) == (
                identity.st_dev,
                identity.st_ino,
            )
        for directory, leaves in zip(external, expected, strict=True):
            assert {
                path.name: path.read_bytes() for path in directory.iterdir()
            } == leaves
    finally:
        for fd in old_fds:
            os.close(fd)


def test_attachment_helper_rejects_unadmitted_call_while_authority_closes(
    tmp_path: Path, monkeypatch
):
    authority, store = _open_store(tmp_path / "root")
    manager = store._attachment_manager
    entered = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []
    original = store._close_under_authority

    def paused_close():
        entered.set()
        assert release.wait(10)
        original()

    monkeypatch.setattr(store, "_close_under_authority", paused_close)

    def close_owner():
        try:
            authority.close()
        except BaseException as exc:
            errors.append(exc)

    closer = threading.Thread(target=close_owner)
    closer.start()
    assert entered.wait(10)
    assert authority.state is AuthorityState.CLOSING
    with pytest.raises(AuthorityError, match="operation rejected"):
        manager.inventory("objects")
    release.set()
    closer.join(10)
    assert not closer.is_alive()
    assert errors == []
    assert authority.state is AuthorityState.CLOSED


def test_concurrent_identical_capture_deduplicates_without_reservations(tmp_path: Path):
    _, store = _open_store(tmp_path / "root")
    source = tmp_path / "same.txt"
    source.write_text("same immutable payload", encoding="utf-8")
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _: store.ingest_attachment(source), range(2)))
    assert results[0].id != results[1].id
    assert results[0].blob_digest == results[1].blob_digest
    with _private_engine_connection(store) as connection:
        assert connection.exec_driver_sql(
            "SELECT count(*) FROM attachment_blobs"
        ).scalar_one() == 1
    assert store._attachment_manager.inventory("captures") == ()
    assert store._attachment_manager.inventory("staging") == ()
    store.close()


@pytest.mark.parametrize(
    "method,after",
    (("capture_to_stage", False), ("capture_to_stage", True), ("stage_to_object", True)),
)
def test_staging_crash_states_recover_privately(
    tmp_path: Path, monkeypatch, method: str, after: bool
):
    root = tmp_path / "root"
    _, store = _open_store(root)
    source = tmp_path / "source.txt"
    source.write_text("recover staging", encoding="utf-8")
    owner = type(store._attachment_manager)
    original = getattr(owner, method)

    def fail(self, *args, **kwargs):
        if after:
            original(self, *args, **kwargs)
        raise OSError("injected publication interruption")

    monkeypatch.setattr(owner, method, fail)
    with pytest.raises(StateError, match="restart recovery"):
        store.ingest_attachment(source)
    store.close()
    monkeypatch.undo()
    _, recovered = _open_store(root)
    with _private_engine_connection(recovered) as connection:
        assert connection.exec_driver_sql(
            "SELECT state FROM attachment_blobs"
        ).scalar_one() == "ready"
    assert recovered._attachment_manager.inventory("captures") == ()
    assert recovered._attachment_manager.inventory("staging") == ()
    assert len(recovered._attachment_manager.inventory("objects")) == 1
    assert recovered.gc_attachments()
    recovered.close()


@pytest.mark.parametrize(
    "point",
    (
        "after-capture-file-fsync",
        "after-staging-row-commit",
        "before-capture-stage-rename",
        "after-capture-stage-rename",
        "after-captures-directory-fsync",
        "after-staging-directory-fsync",
        "before-stage-object-rename",
        "after-stage-object-rename",
        "after-stage-object-staging-fsync",
        "after-objects-directory-fsync",
        "before-ready-commit",
        "after-ready-commit",
    ),
)
def test_attachment_publication_forced_death_matrix_recovers_idempotently(
    tmp_path: Path, point: str
):
    root = tmp_path / "root"
    source = tmp_path / "capture.txt"
    source.write_bytes(b"durable capture")
    _attachment_fault_process(root, source, point)
    for _ in range(2):
        _, recovered = _open_store(root)
        with _private_engine_connection(recovered) as connection:
            rows = connection.exec_driver_sql(
                "SELECT state FROM attachment_blobs"
            ).fetchall()
            attachment_count = connection.exec_driver_sql(
                "SELECT count(*) FROM attachments"
            ).scalar_one()
        assert rows in ([], [("ready",)])
        assert recovered._attachment_manager.inventory("captures") == ()
        assert recovered._attachment_manager.inventory("staging") == ()
        if rows and attachment_count == 0:
            assert recovered.gc_attachments()
        recovered.close()


def _staging_row(root: Path) -> tuple[str, str, int]:
    with sqlite3.connect(root / "database" / "state.sqlite3") as connection:
        digest, byte_size, operation_id = connection.execute(
            "SELECT digest, byte_size, operation_id FROM attachment_blobs "
            "WHERE state='staging'"
        ).fetchone()
    return bytes(digest).hex(), int(byte_size), str(operation_id)


@pytest.mark.parametrize("duplicate", ("capture_stage", "stage_canonical"))
def test_f2_verified_duplicate_publication_recovers_then_survives_second_restart(
    tmp_path: Path, duplicate: str
):
    root = tmp_path / "root"
    source = tmp_path / "payload.txt"
    source.write_bytes(b"verified duplicate publication")
    point = (
        "before-capture-stage-rename"
        if duplicate == "capture_stage"
        else "before-stage-object-rename"
    )
    _attachment_fault_process(root, source, point)
    digest, _, operation_id = _staging_row(root)
    captures = root / "attachments" / "captures"
    staging = root / "attachments" / "staging"
    objects = root / "attachments" / "objects"
    if duplicate == "capture_stage":
        destination = staging / operation_id
        destination.write_bytes((captures / operation_id).read_bytes())
    else:
        destination = objects / digest
        destination.write_bytes((staging / operation_id).read_bytes())
    destination.chmod(0o600)
    for _ in range(2):
        authority, recovered = _open_store(root)
        assert authority.state is AuthorityState.READY
        with _private_engine_connection(recovered) as connection:
            assert connection.exec_driver_sql(
                "SELECT state FROM attachment_blobs"
            ).fetchall() == [("ready",)]
        assert recovered._attachment_manager.inventory("captures") == ()
        assert recovered._attachment_manager.inventory("staging") == ()
        assert recovered._attachment_manager.inventory("objects") == (digest,)
        recovered.close()


@pytest.mark.parametrize(
    "mixed",
    ("capture_canonical", "all_three", "conflicting_capture_stage"),
)
def test_f2_unauthorized_or_conflicting_mixed_publication_is_preserved(
    tmp_path: Path, mixed: str
):
    root = tmp_path / "root"
    source = tmp_path / "payload.txt"
    source.write_bytes(b"mixed publication evidence")
    _attachment_fault_process(root, source, "before-capture-stage-rename")
    digest, _, operation_id = _staging_row(root)
    capture = root / "attachments" / "captures" / operation_id
    if mixed == "conflicting_capture_stage":
        destination = root / "attachments" / "staging" / operation_id
        destination.write_bytes(b"conflicting bytes")
    else:
        destination = root / "attachments" / "objects" / digest
        destination.write_bytes(capture.read_bytes())
        if mixed == "all_three":
            stage = root / "attachments" / "staging" / operation_id
            stage.write_bytes(capture.read_bytes())
            stage.chmod(0o600)
    destination.chmod(0o600)
    before = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in (root / "attachments").glob("*/*")
    }
    authority = _new_authority(root)
    try:
        with pytest.raises(RuntimeError, match="attachment storage failed"):
            authority.open_store()
    finally:
        authority.close()
    after = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in (root / "attachments").glob("*/*")
    }
    assert after == before


def test_f2_second_recovery_interruption_converges_without_ambiguous_bytes(
    tmp_path: Path,
):
    root = tmp_path / "root"
    source_path = tmp_path / "payload.txt"
    source_path.write_bytes(b"second recovery interruption")
    _attachment_fault_process(root, source_path, "before-capture-stage-rename")
    recovery_source = r'''
import os
import sys
from bots5.infrastructure import attachments
from bots5.infrastructure.data_root_authority import DataRootAuthority
root = sys.argv[1]
def die(point):
    if point == 'after-staging-directory-fsync':
        os._exit(91)
attachments._TEST_FAULT_HOOK = die
DataRootAuthority(root).acquire().open_store()
os._exit(20)
'''
    interrupted = subprocess.run(
        [sys.executable, "-c", recovery_source, os.fspath(root)],
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": os.fspath(REPO / "src")},
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert interrupted.returncode == 91, (interrupted.stdout, interrupted.stderr)
    digest, _, _ = _staging_row(root)
    for _ in range(2):
        _, recovered = _open_store(root)
        assert recovered._attachment_manager.inventory("captures") == ()
        assert recovered._attachment_manager.inventory("staging") == ()
        assert recovered._attachment_manager.inventory("objects") == (digest,)
        recovered.close()


@pytest.mark.parametrize(
    "method,after",
    (
        ("create_tombstone", False),
        ("create_tombstone", True),
        ("exchange_object_to_gc", True),
        ("delete_gc_payload", True),
        ("delete_canonical_tombstone", True),
    ),
)
def test_gc_d0_through_d4_recover_idempotently(
    tmp_path: Path, monkeypatch, method: str, after: bool
):
    root = tmp_path / "root"
    _, store = _open_store(root)
    source = tmp_path / "garbage.txt"
    source.write_text("garbage collection", encoding="utf-8")
    attachment = store.ingest_attachment(source)
    digest = attachment.blob_digest
    store.delete_attachment(attachment.id)
    owner = type(store._attachment_manager)
    original = getattr(owner, method)

    def fail(self, *args, **kwargs):
        if after:
            original(self, *args, **kwargs)
        raise OSError("injected GC interruption")

    monkeypatch.setattr(owner, method, fail)
    with pytest.raises(StateError, match="deleting intent retained"):
        store.gc_attachments()
    store.close()
    with sqlite3.connect(root / "database" / "state.sqlite3") as connection:
        state, gc_id = connection.execute(
            "SELECT state, gc_id FROM attachment_blobs"
        ).fetchone()
    assert state == "deleting"
    assert gc_id is not None
    monkeypatch.undo()
    for _ in range(2):
        _, recovered = _open_store(root)
        with _private_engine_connection(recovered) as connection:
            assert connection.exec_driver_sql(
                "SELECT count(*) FROM attachment_blobs"
            ).scalar_one() == 0
        assert recovered._attachment_manager.inventory("objects") == ()
        assert recovered._attachment_manager.inventory("gc") == ()
        recovered.close()
    assert len(bytes.fromhex(digest)) == 32


@pytest.mark.parametrize(
    "point",
    (
        "before-gc-deleting-commit",
        "after-gc-deleting-commit",
        "after-gc-tombstone-temp-create",
        "after-gc-tombstone-write",
        "after-gc-tombstone-file-fsync",
        "before-gc-tombstone-rename",
        "after-gc-tombstone-rename",
        "after-gc-tombstone-directory-fsync",
        "before-gc-exchange",
        "after-gc-exchange",
        "after-gc-objects-directory-fsync",
        "after-gc-directory-fsync",
        "after-gc-payload-verification",
        "before-gc-payload-unlink",
        "after-gc-payload-unlink-fsync",
        "before-gc-tombstone-unlink",
        "after-gc-tombstone-unlink-fsync",
        "before-gc-row-delete",
        "before-gc-row-delete-commit",
        "after-gc-row-delete-commit",
    ),
)
def test_gc_forced_death_at_every_durable_edge_recovers_idempotently(
    tmp_path: Path, point: str
):
    root = tmp_path / "root"
    _, store = _open_store(root)
    source = tmp_path / "garbage.txt"
    source.write_bytes(b"gc crash matrix")
    attachment = store.ingest_attachment(source)
    store.delete_attachment(attachment.id)
    store.close()
    _gc_fault_process(root, point)
    for _ in range(2):
        _, recovered = _open_store(root)
        if recovered.list_attachments() == ():
            recovered.gc_attachments()
        assert recovered._attachment_manager.inventory("objects") == ()
        assert recovered._attachment_manager.inventory("gc") == ()
        with _private_engine_connection(recovered) as connection:
            assert connection.exec_driver_sql(
                "SELECT count(*) FROM attachment_blobs"
            ).scalar_one() == 0
        recovered.close()


def _deleting_state(root: Path, source: Path) -> tuple[str, str, bytes]:
    _, store = _open_store(root)
    attachment = store.ingest_attachment(source)
    store.delete_attachment(attachment.id)
    store.close()
    _gc_fault_process(root, "after-gc-deleting-commit")
    with sqlite3.connect(root / "database" / "state.sqlite3") as connection:
        digest, gc_id = connection.execute(
            "SELECT digest, gc_id FROM attachment_blobs WHERE state='deleting'"
        ).fetchone()
    return bytes(digest).hex(), str(gc_id), source.read_bytes()


@pytest.mark.parametrize(
    "object_state,gc_state",
    (
        ("payload", "absent"),
        ("payload", "tombstone"),
        ("tombstone", "payload"),
        ("payload", "payload"),
        ("tombstone", "tombstone"),
        ("tombstone", "absent"),
        ("absent", "payload"),
        ("absent", "absent"),
    ),
)
def test_f4_every_attributable_gc_mixed_state_converges_twice(
    tmp_path: Path, object_state: str, gc_state: str
):
    root = tmp_path / f"root-{object_state}-{gc_state}"
    source = tmp_path / f"source-{object_state}-{gc_state}.txt"
    source.write_bytes(b"total GC recovery matrix")
    digest, gc_id, payload = _deleting_state(root, source)
    object_leaf = root / "attachments" / "objects" / digest
    gc_leaf = root / "attachments" / "gc" / gc_id
    tombstone = _AttachmentFS.tombstone_bytes(gc_id, bytes.fromhex(digest))

    if object_state == "tombstone":
        object_leaf.write_bytes(tombstone)
        object_leaf.chmod(0o600)
    elif object_state == "absent":
        object_leaf.unlink()
    if gc_state == "payload":
        gc_leaf.write_bytes(payload)
        gc_leaf.chmod(0o600)
    elif gc_state == "tombstone":
        gc_leaf.write_bytes(tombstone)
        gc_leaf.chmod(0o600)

    for _ in range(2):
        _, recovered = _open_store(root)
        with _private_engine_connection(recovered) as connection:
            assert connection.exec_driver_sql(
                "SELECT count(*) FROM attachment_blobs"
            ).scalar_one() == 0
        assert recovered._attachment_manager.inventory("objects") == ()
        assert recovered._attachment_manager.inventory("gc") == ()
        recovered.close()


def test_f4_ready_to_deleting_database_handoff_precedes_first_file_mutation(
    tmp_path: Path, monkeypatch
):
    authority, store = _open_store(tmp_path / "root")
    source = tmp_path / "payload.txt"
    source.write_bytes(b"database handoff")
    attachment = store.ingest_attachment(source)
    store.delete_attachment(attachment.id)
    events: list[str] = []
    original_fence = authority.database_durability_fence
    original_tombstone = type(store._attachment_manager).create_tombstone

    def fence():
        events.append("database-fence")
        return original_fence()

    def tombstone(self, *args, **kwargs):
        events.append("file-mutation")
        return original_tombstone(self, *args, **kwargs)

    monkeypatch.setattr(authority, "database_durability_fence", fence)
    monkeypatch.setattr(type(store._attachment_manager), "create_tombstone", tombstone)
    assert store.gc_attachments()
    assert events[:2] == ["database-fence", "file-mutation"]
    store.close()


def test_f4_d4_sync_failure_retains_deleting_intent_until_clean_restart(
    tmp_path: Path, monkeypatch
):
    root = tmp_path / "root"
    source = tmp_path / "payload.txt"
    source.write_bytes(b"D4 durable absence")
    _deleting_state(root, source)
    (root / "attachments" / "objects" / next(
        path.name for path in (root / "attachments" / "objects").iterdir()
    )).unlink()
    original = _AttachmentFS.sync_namespace

    def fail_gc_sync(self, area):
        if area == "gc":
            raise OSError(errno.EIO, "injected D4 parent sync failure")
        return original(self, area)

    monkeypatch.setattr(_AttachmentFS, "sync_namespace", fail_gc_sync)
    authority = _new_authority(root)
    try:
        with pytest.raises(RuntimeError, match="attachment storage failed"):
            authority.open_store()
    finally:
        authority.close()
    with sqlite3.connect(root / "database" / "state.sqlite3") as connection:
        assert connection.execute(
            "SELECT state FROM attachment_blobs"
        ).fetchone() == ("deleting",)
    monkeypatch.undo()
    _, recovered = _open_store(root)
    with _private_engine_connection(recovered) as connection:
        assert connection.exec_driver_sql(
            "SELECT count(*) FROM attachment_blobs"
        ).scalar_one() == 0
    recovered.close()


def test_a5_shared_close_is_nonexceptional_data_and_drops_secret_traceback_graph(
    tmp_path: Path, monkeypatch
):
    class SecretPayload:
        pass

    async def scenario() -> BotsApplication:
        application, _, authority = _configured_application(tmp_path / "root")
        started = asyncio.Event()
        release = asyncio.Event()
        holder: dict[str, SecretPayload] = {"value": SecretPayload()}
        holder["value"].secret_api_key = "sk-stage-c-must-not-survive"
        reference = weakref.ref(holder["value"])

        async def fail_shutdown():
            payload = holder.pop("value")
            started.set()
            await release.wait()
            raise RuntimeError("custom nested shutdown failure", payload)

        monkeypatch.setattr(application._execution, "shutdown", fail_shutdown)
        cancelled = asyncio.create_task(application.close())
        await started.wait()
        joiner = asyncio.create_task(application.close())
        cancelled.cancel()
        result = (await asyncio.gather(cancelled, return_exceptions=True))[0]
        assert isinstance(result, asyncio.CancelledError)
        release.set()
        with pytest.raises(StateError) as first:
            await joiner
        assert str(first.value) == "application close failed during execution"
        with pytest.raises(StateError) as replay:
            await application.close()
        assert str(replay.value) == str(first.value)
        assert application._close_task is None
        assert application._close_result is not None
        assert application._close_result.errors[0].stage == "execution"
        assert "sk-stage-c" not in repr(application.__dict__)
        assert authority.state is AuthorityState.CLOSED
        del result, cancelled, joiner, first, replay
        await asyncio.sleep(0)
        gc.collect()
        assert reference() is None
        return application

    application = asyncio.run(scenario())
    with pytest.raises(StateError, match="application close failed during execution"):
        asyncio.run(application.close())


def test_gc_rechecks_after_stale_enumeration_before_tombstone(
    tmp_path: Path, monkeypatch
):
    _, store = _open_store(tmp_path / "root")
    source = tmp_path / "race.txt"
    source.write_text("fresh reference wins", encoding="utf-8")
    old = store.ingest_attachment(source)
    store.delete_attachment(old.id)
    entered = threading.Event()
    release = threading.Event()
    original = type(store)._enumerate_unreferenced_blobs

    def pause_after_enumeration(self):
        rows = original(self)
        entered.set()
        assert release.wait(5)
        return rows

    monkeypatch.setattr(type(store), "_enumerate_unreferenced_blobs", pause_after_enumeration)
    result: list[tuple[str, ...]] = []
    worker = threading.Thread(target=lambda: result.append(store.gc_attachments()))
    worker.start()
    assert entered.wait(2)
    attachment_id = str(uuid7())
    with _private_engine_connection(store, transaction=True) as connection:
        arm_phase6_attachment_insert(connection, attachment_id)
        try:
            connection.exec_driver_sql(
                "INSERT INTO attachments(id, blob_digest, filename, source_kind, "
                "source_name, text_representation_id, text_digest, "
                "ineligibility_reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    attachment_id,
                    bytes.fromhex(old.blob_digest),
                    "race.txt",
                    "filesystem",
                    "race.txt",
                    bytes.fromhex(old.text_representation_id),
                    bytes.fromhex(old.text_digest),
                    None,
                    datetime.now(UTC).isoformat(),
                ),
            )
            require_phase6_consumed(connection)
        finally:
            clear_phase6(connection)
    release.set()
    worker.join(5)
    assert not worker.is_alive()
    assert result == [()]
    assert store.read_attachment_bytes(attachment_id) == b"fresh reference wins"
    assert store._attachment_manager.inventory("gc") == ()
    store.close()


@pytest.mark.parametrize("row_state", ("absent", "ready"))
def test_orphan_gc_tombstone_is_preserved_and_startup_fails_closed(
    tmp_path: Path, row_state: str
):
    root = tmp_path / "root"
    _, store = _open_store(root)
    source = tmp_path / "orphan.txt"
    source.write_bytes(b"orphan evidence")
    attachment = store.ingest_attachment(source)
    digest = bytes.fromhex(attachment.blob_digest)
    if row_state == "ready":
        store.delete_attachment(attachment.id)
    else:
        store.delete_attachment(attachment.id)
        assert store.gc_attachments()
    gc_id = str(uuid7())
    payload = store._attachment_manager.tombstone_bytes(gc_id, digest)
    store.close()
    tombstone = root / "attachments" / "gc" / gc_id
    tombstone.write_bytes(payload)
    os.chmod(tombstone, 0o600)
    authority = _new_authority(root)
    try:
        with pytest.raises(RuntimeError, match="attachment storage failed"):
            authority.open_store()
    finally:
        authority.close()
    assert tombstone.read_bytes() == payload


@pytest.mark.parametrize("variant", ("wrong_content", "wrong_mode", "symlink", "unsafe_temp"))
def test_deleting_row_with_corrupt_gc_evidence_fails_closed_and_preserves_it(
    tmp_path: Path, monkeypatch, variant: str
):
    root = tmp_path / "root"
    _, store = _open_store(root)
    source = tmp_path / "garbage.txt"
    source.write_bytes(b"corrupt gc evidence")
    attachment = store.ingest_attachment(source)
    store.delete_attachment(attachment.id)
    owner = type(store._attachment_manager)
    monkeypatch.setattr(owner, "create_tombstone", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("stop")))
    with pytest.raises(StateError, match="deleting intent retained"):
        store.gc_attachments()
    store.close()
    monkeypatch.undo()
    database = root / "database" / "state.sqlite3"
    with sqlite3.connect(database) as connection:
        digest, gc_id = connection.execute(
            "SELECT digest, gc_id FROM attachment_blobs WHERE state='deleting'"
        ).fetchone()
    gc_dir = root / "attachments" / "gc"
    evidence = gc_dir / (f".{gc_id}.tmp" if variant == "unsafe_temp" else gc_id)
    if variant == "symlink":
        evidence.symlink_to(root / "attachments" / "objects" / bytes(digest).hex())
    else:
        evidence.write_bytes(b"wrong")
        os.chmod(evidence, 0o644 if variant == "wrong_mode" else 0o600)
    authority = _new_authority(root)
    try:
        with pytest.raises(RuntimeError, match="attachment storage failed"):
            authority.open_store()
    finally:
        authority.close()
    assert evidence.exists() or evidence.is_symlink()


def test_gc_captures_replaced_canonical_bytes_and_preserves_mismatch(
    tmp_path: Path, monkeypatch
):
    root = tmp_path / "root"
    _, store = _open_store(root)
    source = tmp_path / "garbage.txt"
    source.write_bytes(b"expected bytes")
    attachment = store.ingest_attachment(source)
    digest = attachment.blob_digest
    store.delete_attachment(attachment.id)
    owner = type(store._attachment_manager)
    original = owner.exchange_object_to_gc

    def replace_then_exchange(self, raw_digest, gc_id):
        object_leaf = root / "attachments" / "objects" / bytes(raw_digest).hex()
        replacement = root / "attachments" / "objects" / ".hostile"
        replacement.write_bytes(b"wrong bytes")
        os.chmod(replacement, 0o600)
        os.replace(replacement, object_leaf)
        original(self, raw_digest, gc_id)

    monkeypatch.setattr(owner, "exchange_object_to_gc", replace_then_exchange)
    with pytest.raises(StateError, match="deleting intent retained"):
        store.gc_attachments()
    store.close()
    monkeypatch.undo()
    with sqlite3.connect(root / "database" / "state.sqlite3") as connection:
        state, gc_id = connection.execute(
            "SELECT state, gc_id FROM attachment_blobs"
        ).fetchone()
    assert state == "deleting"
    assert (root / "attachments" / "gc" / gc_id).read_bytes() == b"wrong bytes"
    assert (root / "attachments" / "objects" / digest).exists()


def test_post_exchange_private_name_rewrite_is_explicitly_characterised_boundary(
    tmp_path: Path, monkeypatch
):
    root = tmp_path / "root"
    _, store = _open_store(root)
    source = tmp_path / "garbage.txt"
    source.write_bytes(b"expected bytes")
    attachment = store.ingest_attachment(source)
    store.delete_attachment(attachment.id)
    owner = type(store._attachment_manager)
    original = owner.delete_gc_payload
    observed: list[bytes] = []

    def hostile_rewrite_then_delete(self, gc_id):
        private = root / "attachments" / "gc" / gc_id
        replacement = root / "attachments" / "gc" / ".hostile"
        replacement.write_bytes(b"excluded same-UID rewrite")
        os.chmod(replacement, 0o600)
        os.replace(replacement, private)
        observed.append(private.read_bytes())
        original(self, gc_id)

    monkeypatch.setattr(owner, "delete_gc_payload", hostile_rewrite_then_delete)
    assert store.gc_attachments() == (attachment.blob_digest,)
    assert observed == [b"excluded same-UID rewrite"]
    store.close()


def test_reference_and_attachment_deletion_both_linearization_orders(tmp_path: Path):
    async def reference_first():
        application, store, _ = _configured_application(tmp_path / "first")
        source = tmp_path / "first.txt"
        source.write_text("reference wins", encoding="utf-8")
        chat = await application.create_chat()
        attachment = await application.attach_file(source)
        await application.stage_attachment(chat.id, attachment.id)
        attempt = await application.send_message(chat.id, "keep it")
        with pytest.raises(StateError, match="historical references"):
            store.delete_attachment(attachment.id)
        await _finish(application, attempt.id)
        await application.close()

    async def deletion_first():
        application, store, _ = _configured_application(tmp_path / "second")
        source = tmp_path / "second.txt"
        source.write_text("deletion wins", encoding="utf-8")
        chat = await application.create_chat()
        attachment = await application.attach_file(source)
        await application.stage_attachment(chat.id, attachment.id)
        store.delete_attachment(attachment.id)
        with pytest.raises(StateError):
            await application.send_message(chat.id, "cannot reference")
        assert await application.list_message_history(chat.id) == ()
        assert await application.list_generation_attempts(chat.id) == ()
        await application.close()

    asyncio.run(reference_first())
    asyncio.run(deletion_first())


def test_raw_fk_off_history_and_lifecycle_guards_are_unconditional(tmp_path: Path):
    async def prepare():
        application, _, _ = _configured_application(tmp_path / "root")
        chat = await application.create_chat()
        source = tmp_path / "history.txt"
        source.write_text("historical", encoding="utf-8")
        attachment = await application.attach_file(source)
        await application.stage_attachment(chat.id, attachment.id)
        attempt = await application.send_message(chat.id, "remember")
        await _finish(application, attempt.id)
        await application.close()
        return attachment, attempt

    attachment, attempt = asyncio.run(prepare())
    database = tmp_path / "root" / "database" / "state.sqlite3"
    digest = bytes.fromhex(attachment.blob_digest)
    statements = (
        ("DELETE FROM attachments WHERE id = ?", (attachment.id,)),
        ("DELETE FROM message_attachments WHERE message_id = ? AND attachment_id = ?", (attempt.user_message_id, attachment.id)),
        ("DELETE FROM attempt_attachments WHERE attempt_id = ? AND attachment_id = ?", (attempt.id, attachment.id)),
        ("DELETE FROM attachment_blobs WHERE digest = ?", (digest,)),
        ("UPDATE attachments SET blob_digest = ? WHERE id = ?", (b"x" * 32, attachment.id)),
        ("INSERT INTO message_attachments(message_id, attachment_id, ordinal) VALUES (?, ?, 99)", ("missing-message", attachment.id)),
        ("INSERT INTO attempt_attachments(attempt_id, attachment_id, ordinal) VALUES (?, ?, 99)", ("missing-attempt", attachment.id)),
        ("UPDATE attachment_blobs SET state = 'deleting', gc_id = ? WHERE digest = ?", (str(uuid7()), digest)),
    )
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        for statement, parameters in statements:
            with pytest.raises(sqlite3.DatabaseError):
                connection.execute(statement, parameters)
            connection.rollback()
        assert connection.execute(
            "SELECT state FROM attachment_blobs WHERE digest = ?", (digest,)
        ).fetchone() == ("ready",)
        assert connection.execute(
            "SELECT count(*) FROM message_attachments WHERE attachment_id = ?",
            (attachment.id,),
        ).fetchone() == (1,)


def test_deleting_blob_rejects_service_and_stock_fk_off_attachment_insertion(
    tmp_path: Path, monkeypatch
):
    root = tmp_path / "root"
    _, store = _open_store(root)
    source = tmp_path / "deleting.txt"
    source.write_bytes(b"deleting")
    attachment = store.ingest_attachment(source)
    store.delete_attachment(attachment.id)
    owner = type(store._attachment_manager)
    monkeypatch.setattr(owner, "create_tombstone", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("stop")))
    with pytest.raises(StateError, match="deleting intent retained"):
        store.gc_attachments()
    with pytest.raises(StateError, match="not admitting work"):
        store.ingest_attachment(source)
    store.close()
    monkeypatch.undo()
    database = root / "database" / "state.sqlite3"
    raw_id = str(uuid7())
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        digest = connection.execute(
            "SELECT digest FROM attachment_blobs WHERE state='deleting'"
        ).fetchone()[0]
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute(
                "INSERT INTO attachments(id, blob_digest, filename, source_kind, "
                "source_name, text_representation_id, text_digest, "
                "ineligibility_reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    raw_id,
                    digest,
                    "raw.txt",
                    "filesystem",
                    "raw.txt",
                    digest,
                    digest,
                    None,
                    datetime.now(UTC).isoformat(),
                ),
            )
        connection.rollback()
        assert connection.execute(
            "SELECT count(*) FROM attachments WHERE id=?", (raw_id,)
        ).fetchone() == (0,)


@pytest.mark.parametrize("tamper", ("missing", "inert", "extra"))
def test_exact_phase6_ddl_inventory_rejects_tampering(tmp_path: Path, tamper: str):
    root = tmp_path / "root"
    _, store = _open_store(root)
    store.close()
    database = root / "database" / "state.sqlite3"
    with sqlite3.connect(database) as connection:
        if tamper == "missing":
            connection.execute("DROP TRIGGER phase6_context_plan_update_guard")
        elif tamper == "inert":
            connection.execute("DROP TRIGGER phase6_context_plan_insert_guard")
            connection.execute(
                "CREATE TRIGGER phase6_context_plan_insert_guard "
                "BEFORE INSERT ON context_plans WHEN 0 BEGIN "
                "SELECT RAISE(ABORT, 'inert'); END"
            )
        else:
            connection.execute(
                "CREATE TRIGGER phase6_unexpected_extra AFTER INSERT ON attachments "
                "BEGIN SELECT 1; END"
            )
    authority = _new_authority(root)
    try:
        with pytest.raises(RuntimeError, match="Phase 6"):
            authority.open_store()
    finally:
        authority.close()


@pytest.mark.parametrize("trigger", tuple(sorted(REQUIRED_TRIGGERS)))
def test_each_required_phase6_trigger_is_individually_mandatory(
    tmp_path: Path, trigger: str
):
    root = tmp_path / "root"
    _, store = _open_store(root)
    store.close()
    database = root / "database" / "state.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(f'DROP TRIGGER "{trigger}"')
    authority = _new_authority(root)
    try:
        with pytest.raises(RuntimeError):
            authority.open_store()
    finally:
        authority.close()


@pytest.mark.parametrize("index", tuple(sorted(REQUIRED_INDEXES)))
def test_each_required_phase6_index_is_individually_mandatory(
    tmp_path: Path, index: str
):
    root = tmp_path / "root"
    _, store = _open_store(root)
    store.close()
    database = root / "database" / "state.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(f'DROP INDEX "{index}"')
    authority = _new_authority(root)
    try:
        with pytest.raises(RuntimeError):
            authority.open_store()
    finally:
        authority.close()


@pytest.mark.parametrize("table", tuple(sorted(REQUIRED_TABLES)))
def test_each_phase6_table_definition_is_exact_under_writable_schema_tamper(
    tmp_path: Path, table: str
):
    root = tmp_path / "root"
    _, store = _open_store(root)
    store.close()
    database = root / "database" / "state.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA writable_schema=ON")
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()[0]
        connection.execute(
            "UPDATE sqlite_master SET sql=? WHERE type='table' AND name=?",
            (sql.replace("CREATE TABLE", "CREATE TABLE /* changed */", 1), table),
        )
        connection.execute("PRAGMA writable_schema=OFF")
    authority = _new_authority(root)
    try:
        with pytest.raises(RuntimeError):
            authority.open_store()
    finally:
        authority.close()


def test_phase6_sql_canonicalisation_is_quote_aware_and_escape_safe():
    canonical = (
        "CREATE TRIGGER IF NOT EXISTS \"trig\"\"ger\" BEFORE INSERT ON `ta``ble` "
        "BEGIN SELECT 'A'' quoted value', [spaced identifier]; END"
    )
    layout_only = (
        "create\n trigger   \"trig\"\"ger\" before insert\tON `ta``ble`\n"
        "begin select 'A'' quoted value',[spaced identifier] ; end"
    )
    assert _normalise_sql(canonical) == _normalise_sql(layout_only)
    assert _normalise_sql(canonical) != _normalise_sql(
        canonical.replace("'A'' quoted value'", "'A''quotedvalue'")
    )
    assert _normalise_sql(canonical) != _normalise_sql(
        canonical.replace('"trig""ger"', '"trig-ger"')
    )
    assert _normalise_sql(canonical) != _normalise_sql(
        canonical.replace("`ta``ble`", "`table`")
    )
    assert _normalise_sql(canonical) != _normalise_sql(
        canonical.replace("[spaced identifier]", "[spacedidentifier]")
    )
    assert _normalise_sql("SELECT a b") != _normalise_sql("SELECT ab")
    assert _normalise_sql("SELECT X'AB'") != _normalise_sql("SELECT X 'AB'")
    assert _normalise_sql("SELECT K") != _normalise_sql("SELECT K")


@pytest.mark.parametrize(
    "needle,replacement",
    (
        (
            "NEW.adapter_id = 'bots5.deterministic-json'",
            "NEW.adapter_id = 'BOTS5.DETERMINISTIC-JSON'",
        ),
        (
            "NEW.adapter_id",
            'NEW."adapter-ID"',
        ),
        (
            "'Phase 6 attachment identity is immutable'",
            "'Phase6attachmentidentityisimmutable'",
        ),
    ),
)
def test_changed_quoted_phase6_trigger_content_rejects_before_ready(
    tmp_path: Path, needle: str, replacement: str
):
    root = tmp_path / "root"
    _, store = _open_store(root)
    store.close()
    database = root / "database" / "state.sqlite3"
    trigger = (
        "phase6_attachment_immutable"
        if "attachment identity" in needle
        else "phase6_context_plan_insert_guard"
    )
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA writable_schema=ON")
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
            (trigger,),
        ).fetchone()[0]
        assert needle in sql
        connection.execute(
            "UPDATE sqlite_master SET sql=? WHERE type='trigger' AND name=?",
            (sql.replace(needle, replacement, 1), trigger),
        )
        connection.execute("PRAGMA writable_schema=OFF")
    authority = _new_authority(root)
    try:
        with pytest.raises(RuntimeError):
            authority.open_store()
        assert authority.state is AuthorityState.FAILED_STARTUP
    finally:
        authority.close()


def test_outside_quote_phase6_trigger_whitespace_is_insignificant(tmp_path: Path):
    root = tmp_path / "root"
    _, store = _open_store(root)
    store.close()
    database = root / "database" / "state.sqlite3"
    trigger = "phase6_context_plan_insert_guard"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA writable_schema=ON")
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
            (trigger,),
        ).fetchone()[0]
        changed = sql.replace(
            "BEFORE INSERT ON context_plans",
            "BEFORE\n  INSERT\tON   context_plans",
            1,
        )
        assert changed != sql
        connection.execute(
            "UPDATE sqlite_master SET sql=? WHERE type='trigger' AND name=?",
            (changed, trigger),
        )
        connection.execute("PRAGMA writable_schema=OFF")
    authority, reopened = _open_store(root)
    reopened.close()
    assert authority.state is AuthorityState.CLOSED


def test_every_prior_revision_migrates_privately_to_head(tmp_path: Path):
    for index, revision in enumerate(PRIOR_REVISIONS):
        root = tmp_path / f"root-{index}"
        _historical_root(root, revision)
        _restart_twice(root)


def test_committed_prior_migration_bytes_are_exactly_unchanged():
    directory = REPO / "src" / "bots5" / "infrastructure" / "persistence" / "migrations" / "versions"
    assert {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.glob("000[1-8]_*.py"))
    } == PRIOR_MIGRATION_SHA256


def test_f3_migration_connection_is_delete_full_before_first_alembic_write(
    tmp_path: Path, monkeypatch
):
    from bots5.infrastructure.persistence import migration_runner

    root = tmp_path / "root"
    _historical_root(root, "0008_catalogue_refresh_outcomes")
    original = migration_runner.command.upgrade
    observed: list[tuple[str, int]] = []

    def checked_upgrade(config, revision):
        connection = config.attributes["connection"]
        observed.append(
            (
                str(
                    connection.exec_driver_sql("PRAGMA journal_mode").scalar_one()
                ).lower(),
                int(connection.exec_driver_sql("PRAGMA synchronous").scalar_one()),
            )
        )
        return original(config, revision)

    monkeypatch.setattr(migration_runner.command, "upgrade", checked_upgrade)
    authority, store = _open_store(root)
    assert observed == [("delete", 2)]
    assert _revision(root / "database" / "state.sqlite3") == HEAD
    store.close()
    assert all(
        status == "RELEASED"
        for label, status in authority.claim_inventory
        if label.startswith("scoped:migration:")
    )


@pytest.mark.parametrize(
    "point",
    tuple(
        f"{edge}-{phase}"
        for phase in EXISTING_PHASES
        for edge in JOURNAL_EDGES
    )
    + (
        "before-source-recovery",
        "after-source-recovery",
        "after-source-delete-mode",
        "after-source-file-fsync",
        "after-source-directory-fsync",
        "after-backup-temp-create",
        "after-backup-temp-file-fsync",
        "after-backup-rename",
        "after-backup-directory-fsync",
        "after-candidate-create",
        "after-candidate-seed-copy-fsync",
        "after-candidate-seed-directory-fsync",
        "after-candidate-alembic",
        "after-candidate-delete-mode",
        "after-candidate-file-fsync",
        "after-candidate-directory-fsync",
        "after-candidate-hash",
        "after-candidate-claim",
        "after-publication-exchange",
        "after-publication-database-fsync",
        "after-publication-migration-fsync",
        "before-promoted-claim",
        "after-promoted-claim",
        "after-promoted-normal-validation",
        "after-unlink-phase6-journal-v3.json",
        "after-unlink-directory-fsync-phase6-journal-v3.json",
    ),
)
def test_existing_migration_forced_death_restarts_to_exact_head(
    tmp_path: Path, point: str
):
    root = tmp_path / "root"
    _historical_root(root, "0008_catalogue_refresh_outcomes")
    _fault_process(root, point)
    _restart_twice(root)


@pytest.mark.parametrize(
    "start,target",
    tuple(
        (start, target)
        for index, start in enumerate(PRIOR_REVISIONS)
        for target in ALL_REVISIONS[index + 1 :]
    ),
)
def test_each_reachable_candidate_revision_fault_rebuilds_from_verified_backup(
    tmp_path: Path, start: str, target: str
):
    root = tmp_path / "root"
    _historical_root(root, start)
    _fault_process(root, f"after-candidate-revision-{target}")
    _restart_twice(root)


@pytest.mark.parametrize("revision", PRIOR_REVISIONS)
@pytest.mark.parametrize("mode", ("wal", "hot"))
@pytest.mark.parametrize("point", ("before-source-recovery", "after-source-recovery"))
def test_every_prior_revision_recovers_source_sidecars_before_hash(
    tmp_path: Path, revision: str, mode: str, point: str
):
    root = tmp_path / "root"
    _historical_root(root, revision)
    database = root / "database" / "state.sqlite3"
    _leave_source_sidecar(database, mode)
    _fault_process(root, point)
    record = json.loads(
        (root / "database" / "migration" / "phase6-journal-v3.json").read_text()
    )
    assert record["phase"] == "PREPARING"
    assert "source_sha256" not in record
    _restart_twice(root)
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (
            17 if mode == "wal" else 0,
        )


def test_existing_wal_intake_is_quiesced_before_first_source_hash(tmp_path: Path):
    root = tmp_path / "root"
    _historical_root(root, "0008_catalogue_refresh_outcomes")
    database = root / "database" / "state.sqlite3"
    source = """
import os, sqlite3, sys
database = sys.argv[1]
connection = sqlite3.connect(database)
connection.create_function('bots5_valid_timestamp', 1, lambda value: 1)
connection.execute('PRAGMA journal_mode=WAL')
connection.execute('PRAGMA wal_autocheckpoint=0')
connection.execute("INSERT INTO chats(id,title,created_at,updated_at,head_message_id,revision) VALUES ('wal-chat','WAL','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z',NULL,0)")
connection.commit()
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", source, os.fspath(database)], check=True)
    assert database.with_name("state.sqlite3-wal").exists()
    _fault_process(root, "after-source-wal-checkpoint")
    journal = json.loads(
        (root / "database" / "migration" / "phase6-journal-v3.json").read_text()
    )
    assert journal["phase"] == "PREPARING"
    assert "source_sha256" not in journal
    _restart_twice(root)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT title FROM chats WHERE id = 'wal-chat'"
        ).fetchone() == ("WAL",)


def test_candidate_hot_journal_bundle_is_disposable_and_rebuilt(tmp_path: Path):
    root = tmp_path / "root"
    _historical_root(root, "0001_desktop_state")
    _fault_process(root, "after-candidate-revision-0009_phase6_context_attachments")
    migration = root / "database" / "migration"
    journal = json.loads((migration / "phase6-journal-v3.json").read_text())
    assert journal["phase"] == "MIGRATING_COPY"
    candidate = migration / str(journal["candidate_leaf"])
    hot = migration / str(journal["candidate_rollback_journal_leaf"])
    assert candidate.is_file()
    assert hot.is_file()
    _restart_twice(root)


@pytest.mark.parametrize(
    "variant", ("both", "main_only", "journal_only", "torn_journal", "changed_main")
)
def test_prevalidated_candidate_safe_bundle_subsets_are_disposable(
    tmp_path: Path, variant: str
):
    root = tmp_path / "root"
    _historical_root(root, "0001_desktop_state")
    _fault_process(root, "after-candidate-revision-0009_phase6_context_attachments")
    migration = root / "database" / "migration"
    record = json.loads((migration / "phase6-journal-v3.json").read_text())
    candidate = migration / str(record["candidate_leaf"])
    rollback = migration / str(record["candidate_rollback_journal_leaf"])
    if variant == "main_only":
        rollback.unlink()
    elif variant == "journal_only":
        candidate.unlink()
    elif variant == "torn_journal":
        rollback.write_bytes(b"torn-but-transaction-owned")
    elif variant == "changed_main":
        candidate.write_bytes(b"completed-looking-but-not-authoritative")
    _restart_twice(root)


@pytest.mark.parametrize(
    "edge",
    (
        "journal-unlink",
        "journal-fsync",
        "main-unlink",
        "main-fsync",
        "absence-proof",
        "bundle-fsync",
    ),
)
def test_candidate_bundle_disposal_forced_death_resumes_before_rebuild(
    tmp_path: Path, edge: str
):
    root = tmp_path / "root"
    _historical_root(root, "0001_desktop_state")
    _fault_process(root, "after-candidate-revision-0009_phase6_context_attachments")
    migration = root / "database" / "migration"
    record = json.loads((migration / "phase6-journal-v3.json").read_text())
    candidate = str(record["candidate_leaf"])
    rollback = str(record["candidate_rollback_journal_leaf"])
    point = {
        "journal-unlink": f"after-unlink-{rollback}",
        "journal-fsync": f"after-unlink-directory-fsync-{rollback}",
        "main-unlink": f"after-unlink-{candidate}",
        "main-fsync": f"after-unlink-directory-fsync-{candidate}",
        "absence-proof": "after-candidate-bundle-absence-proof",
        "bundle-fsync": "after-candidate-bundle-directory-fsync",
    }[edge]
    _fault_process(root, point)
    _restart_twice(root)


@pytest.mark.parametrize(
    "variant",
    (
        "main_wrong_mode",
        "journal_wrong_mode",
        "main_extra_link",
        "main_directory",
        "journal_symlink",
        "unexpected_wal",
        "unexpected_shm",
        "other_transaction",
        "unexpected_update_temp",
    ),
)
def test_prevalidated_candidate_unsafe_or_unattributed_bundle_preserves_evidence(
    tmp_path: Path, variant: str
):
    root = tmp_path / "root"
    _historical_root(root, "0001_desktop_state")
    _fault_process(root, "after-candidate-revision-0009_phase6_context_attachments")
    migration = root / "database" / "migration"
    journal = migration / "phase6-journal-v3.json"
    record = json.loads(journal.read_text())
    candidate = migration / str(record["candidate_leaf"])
    rollback = migration / str(record["candidate_rollback_journal_leaf"])
    evidence: list[Path] = []
    if variant == "main_wrong_mode":
        os.chmod(candidate, 0o644)
        evidence.append(candidate)
    elif variant == "journal_wrong_mode":
        os.chmod(rollback, 0o644)
        evidence.append(rollback)
    elif variant == "main_extra_link":
        linked = migration / "candidate-hardlink-evidence"
        os.link(candidate, linked)
        evidence.extend((candidate, linked))
    elif variant == "main_directory":
        candidate.unlink()
        candidate.mkdir(mode=0o700)
        evidence.append(candidate)
    elif variant == "journal_symlink":
        rollback.unlink()
        rollback.symlink_to(candidate.name)
        evidence.append(rollback)
    elif variant in {"unexpected_wal", "unexpected_shm"}:
        suffix = "-wal" if variant.endswith("wal") else "-shm"
        sidecar = migration / (candidate.name + suffix)
        sidecar.write_bytes(b"unattributed")
        os.chmod(sidecar, 0o600)
        evidence.append(sidecar)
    elif variant == "other_transaction":
        other = migration / "migrate-00000000-0000-7000-8000-000000000000.candidate.sqlite3"
        other.write_bytes(b"other")
        os.chmod(other, 0o600)
        evidence.append(other)
    else:
        other = migration / ".phase6-journal-v3-00000000-0000-7000-8000-000000000000-1.tmp"
        other.write_bytes(b"other")
        os.chmod(other, 0o600)
        evidence.append(other)
    authority = _new_authority(root)
    try:
        with pytest.raises((AuthorityError, RuntimeError)):
            authority.open_store()
    finally:
        authority.close()
    assert journal.exists()
    assert all(path.exists() or path.is_symlink() for path in evidence)


@pytest.mark.parametrize(
    "tamper",
    (
        "preparing_source_hash",
        "preparing_source_size",
        "wrong_prior",
        "unknown_field",
        "unsupported_revision",
        "duplicate_field",
        "noncanonical",
        "invalid_json",
        "changed_source_inode",
    ),
)
def test_strict_journal_v3_rejects_malformed_or_mismatched_evidence(
    tmp_path: Path, tamper: str
):
    root = tmp_path / "root"
    _historical_root(root, "0008_catalogue_refresh_outcomes")
    _fault_process(root, "after-journal-directory-fsync-PREPARING")
    journal = root / "database" / "migration" / "phase6-journal-v3.json"
    original = journal.read_bytes()
    record = json.loads(original)
    if tamper == "changed_source_inode":
        database = root / "database" / "state.sqlite3"
        replacement = root / "database" / "replacement"
        replacement.write_bytes(database.read_bytes())
        os.chmod(replacement, 0o600)
        os.replace(replacement, database)
    elif tamper == "duplicate_field":
        raw = original.decode().rstrip()
        journal.write_text('{"journal_version":3,' + raw[1:] + "\n", encoding="utf-8")
    elif tamper == "noncanonical":
        journal.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    elif tamper == "invalid_json":
        journal.write_bytes(b"{not-json\n")
    else:
        if tamper == "preparing_source_hash":
            record["source_sha256"] = "0" * 64
        elif tamper == "preparing_source_size":
            record["source_size"] = 1
        elif tamper == "wrong_prior":
            record["prior_phase"] = "ABSENT"
        elif tamper == "unknown_field":
            record["surprise"] = True
        else:
            record["expected_start_revision"] = "unsupported"
        journal.write_bytes(
            (json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
        )
    preserved = journal.read_bytes()
    authority = _new_authority(root)
    try:
        with pytest.raises(RuntimeError):
            authority.open_store()
    finally:
        authority.close()
    assert journal.read_bytes() == preserved


@pytest.mark.parametrize("artifact", ("source", "backup", "candidate", "source_wal", "source_shm"))
def test_migration_hash_or_sidecar_corruption_preserves_evidence(
    tmp_path: Path, artifact: str
):
    root = tmp_path / "root"
    _historical_root(root, "0008_catalogue_refresh_outcomes")
    stage = (
        "after-journal-directory-fsync-SOURCE_QUIESCED"
        if artifact in {"source", "source_wal", "source_shm"}
        else "after-journal-directory-fsync-BACKUP_VERIFIED"
        if artifact == "backup"
        else "after-journal-directory-fsync-CANDIDATE_VALIDATED"
    )
    _fault_process(root, stage)
    migration = root / "database" / "migration"
    journal = migration / "phase6-journal-v3.json"
    record = json.loads(journal.read_text())
    if artifact == "source":
        target = root / "database" / "state.sqlite3"
        with target.open("r+b") as stream:
            stream.seek(0)
            stream.write(b"X")
    elif artifact == "backup":
        target = root / "recovery" / str(record["backup_leaf"])
        with target.open("r+b") as stream:
            stream.seek(0)
            stream.write(b"X")
    elif artifact == "candidate":
        target = migration / str(record["candidate_leaf"])
        with target.open("r+b") as stream:
            stream.seek(0)
            stream.write(b"X")
    else:
        suffix = "-wal" if artifact.endswith("wal") else "-shm"
        target = root / "database" / ("state.sqlite3" + suffix)
        target.write_bytes(b"unexpected")
        os.chmod(target, 0o600)
    authority = _new_authority(root)
    try:
        with pytest.raises(RuntimeError):
            authority.open_store()
    finally:
        assert journal.exists()
        assert target.exists()
        if artifact in {"source_wal", "source_shm"}:
            target.unlink()
        authority.close()
    assert journal.exists()
    if artifact not in {"source_wal", "source_shm"}:
        assert target.exists()


@pytest.mark.parametrize(
    "point",
    tuple(
        dict.fromkeys(
            tuple(
                f"{edge}-{phase}"
                for phase in ABSENT_PHASES
                for edge in JOURNAL_EDGES
            )
            + tuple(f"after-candidate-revision-{revision}" for revision in ALL_REVISIONS)
            + (
        "after-candidate-create",
        "after-empty-candidate-fsync",
        "after-candidate-seed-directory-fsync",
        "after-candidate-alembic",
        "after-candidate-delete-mode",
        "after-candidate-file-fsync",
        "after-candidate-directory-fsync",
        "after-candidate-hash",
        "after-candidate-claim",
        "after-publication-rename-noreplace",
        "after-publication-database-fsync",
        "after-publication-migration-fsync",
        "before-promoted-claim",
        "after-promoted-claim",
        "after-promoted-normal-validation",
        "after-unlink-phase6-journal-v3.json",
        "after-unlink-directory-fsync-phase6-journal-v3.json",
            )
        )
    ),
)
def test_true_absent_fault_points_restart_twice_without_false_source(tmp_path: Path, point: str):
    root = tmp_path / "root"
    _initialise_root(root)
    assert not (root / "database" / "state.sqlite3").exists()
    _fault_process(root, point)
    canonical = root / "database" / "state.sqlite3"
    assert not canonical.exists() or canonical.stat().st_size > 0
    journal_path = root / "database" / "migration" / "phase6-journal-v3.json"
    if journal_path.exists():
        journal = json.loads(journal_path.read_text())
        assert journal["source_kind"] == "ABSENT"
        assert "source_sha256" not in journal
        assert "expected_start_revision" not in journal
        candidate = root / "database" / "migration" / str(journal["candidate_leaf"])
        assert not (canonical.exists() and candidate.exists())
    _restart_twice(root)


@pytest.mark.parametrize("kind", ("zero", "empty", "corrupt", "no_revision"))
def test_present_canonical_leaf_is_never_misclassified_as_absent(tmp_path: Path, kind: str):
    root = tmp_path / "root"
    _initialise_root(root)
    database = root / "database" / "state.sqlite3"
    if kind == "zero":
        database.touch(mode=0o600)
    elif kind == "empty":
        with sqlite3.connect(database):
            pass
        os.chmod(database, 0o600)
    elif kind == "corrupt":
        database.write_bytes(b"not sqlite")
        os.chmod(database, 0o600)
    else:
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE evidence(value TEXT)")
        os.chmod(database, 0o600)
    before = database.read_bytes()
    authority = _new_authority(root)
    try:
        with pytest.raises(RuntimeError):
            authority.open_store()
    finally:
        authority.close()
    assert database.read_bytes() == before
    assert list((root / "database" / "migration").iterdir()) == []


def test_absent_late_canonical_and_unattributed_artifacts_preserve_evidence(tmp_path: Path):
    root = tmp_path / "root"
    _initialise_root(root)
    _fault_process(root, "after-journal-directory-fsync-ABSENT")
    canonical = root / "database" / "state.sqlite3"
    canonical.write_bytes(b"collision")
    os.chmod(canonical, 0o600)
    unexpected = root / "database" / "migration" / "other-transaction"
    unexpected.write_bytes(b"evidence")
    os.chmod(unexpected, 0o600)
    authority = _new_authority(root)
    try:
        with pytest.raises(RuntimeError):
            authority.open_store()
    finally:
        authority.close()
    assert canonical.read_bytes() == b"collision"
    assert unexpected.read_bytes() == b"evidence"


def test_absent_persistent_postpublication_validation_preserves_one_candidate(
    tmp_path: Path,
):
    root = tmp_path / "root"
    _initialise_root(root)
    _fault_process(root, "after-journal-directory-fsync-PROMOTED")
    database = root / "database" / "state.sqlite3"
    journal = root / "database" / "migration" / "phase6-journal-v3.json"
    record = json.loads(journal.read_text())
    assert record["source_kind"] == "ABSENT"
    before = (os.stat(database).st_ino, hashlib.sha256(database.read_bytes()).hexdigest())
    for _ in range(2):
        _persistent_phase6_validation_failure(root)
        assert journal.exists()
        assert json.loads(journal.read_text())["phase"] == "PROMOTED"
        assert (os.stat(database).st_ino, hashlib.sha256(database.read_bytes()).hexdigest()) == before
        assert tuple(
            path.name
            for path in (root / "database" / "migration").iterdir()
            if path.name != "phase6-journal-v3.json"
        ) == ()
    _restart_twice(root)


def test_validated_candidate_hash_or_sidecar_collision_fails_closed(tmp_path: Path):
    for mode in ("hash", "sidecar"):
        root = tmp_path / mode
        _initialise_root(root)
        _fault_process(root, "after-journal-directory-fsync-CANDIDATE_VALIDATED")
        migration = root / "database" / "migration"
        record = json.loads((migration / "phase6-journal-v3.json").read_text())
        candidate = migration / str(record["candidate_leaf"])
        if mode == "hash":
            with candidate.open("r+b") as stream:
                stream.seek(0)
                stream.write(b"X")
                stream.flush()
                os.fsync(stream.fileno())
        else:
            sidecar = migration / (str(record["candidate_leaf"]) + "-wal")
            sidecar.write_bytes(b"unexpected")
            os.chmod(sidecar, 0o600)
        authority = _new_authority(root)
        try:
            with pytest.raises(RuntimeError):
                authority.open_store()
        finally:
            authority.close()
        assert candidate.exists()


def test_existing_promotion_validation_failure_restores_prior_source(tmp_path: Path):
    root = tmp_path / "root"
    _historical_root(root, "0008_catalogue_refresh_outcomes")
    database = root / "database" / "state.sqlite3"
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    _fault_process(root, "unused", verify_error=True)
    assert _revision(database) == "0008_catalogue_refresh_outcomes"
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    assert list((root / "database" / "migration").iterdir()) == []
    _restart_twice(root)


@pytest.mark.parametrize(
    "point",
    tuple(
        f"{edge}-{phase}"
        for phase in ("RESTORE_INTENT", "ROLLED_BACK")
        for edge in JOURNAL_EDGES
    )
    + (
        "before-restore-exchange",
        "after-restore-exchange",
        "after-restore-database-fsync",
        "after-restore-migration-fsync",
        "after-restore-claim",
        "after-restore-validation",
        "after-unlink-phase6-journal-v3.json",
        "after-unlink-directory-fsync-phase6-journal-v3.json",
    ),
)
def test_existing_restore_forced_death_converges_without_ambiguous_authority(
    tmp_path: Path, point: str
):
    root = tmp_path / "root"
    _historical_root(root, "0008_catalogue_refresh_outcomes")
    _restore_fault_process(root, point)
    _converge_after_restore_fault(root)


def test_digest_and_tombstone_encoding_are_closed_and_exact(tmp_path: Path):
    _, store = _open_store(tmp_path / "root")
    digest = hashlib.sha256(b"x").digest()
    gc_id = str(uuid7())
    payload = store._attachment_manager.tombstone_bytes(gc_id, digest)
    assert len(payload) == 57
    assert payload.startswith(b"BOTS5GC1\0")
    assert payload[-32:] == digest
    for invalid in (
        "A" * 64,
        "0" * 63,
        "0" * 65,
        "../" + "0" * 64,
        "." * 64,
        "é" * 64,
        "\x00" * 64,
        b"0" * 64,
        None,
        0,
    ):
        with pytest.raises(AttachmentIntegrityError):
            digest_from_text(invalid)
    store.close()


def test_fifo_migration_journal_rejects_promptly_without_writer_and_preserves_evidence(
    tmp_path: Path,
):
    root = tmp_path / "root"
    _initialise_root(root)
    journal = root / "database" / "migration" / "phase6-journal-v3.json"
    os.mkfifo(journal, 0o600)
    before = journal.stat(follow_symlinks=False)
    source = r'''
import os
import sys
from bots5.infrastructure.data_root_authority import DataRootAuthority

authority = DataRootAuthority(sys.argv[1]).acquire()
try:
    authority.open_store()
except BaseException:
    try:
        authority.close()
    except BaseException:
        pass
    os._exit(0)
os._exit(3)
'''
    started = time.monotonic()
    completed = subprocess.run(
        [sys.executable, "-c", source, os.fspath(root)],
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": os.fspath(REPO / "src")},
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    elapsed = time.monotonic() - started
    after = journal.stat(follow_symlinks=False)
    assert completed.returncode == 0, completed.stderr
    assert elapsed < 5
    assert (after.st_dev, after.st_ino, after.st_mode, after.st_size) == (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_size,
    )


def test_declared_python_range_includes_validated_python_314_runtime():
    metadata = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert metadata["project"]["requires-python"] == ">=3.12,<3.15"
