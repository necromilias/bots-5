"""Finite frozen-runtime diagnostic, compiled with the M7 desktop entrypoint.

Only the explicit packaging diagnostic calls this module. All roots/packages
come from disposable test fixtures. Observers never replace the real child,
restore service, authority close, admission, or GUI handoff implementation.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path


def _record(config: dict, event: str, **values) -> None:
    row = {"event": event, "pid": os.getpid(), "time_ns": time.monotonic_ns(), **values}
    descriptor = os.open(config["trace"], os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(descriptor, (json.dumps(row, sort_keys=True) + "\n").encode())
    finally:
        os.close(descriptor)


def _child(main, config: dict) -> int:
    from bots5.bootstrap import desktop
    from bots5.infrastructure.authority_lock import AuthorityLock
    from bots5.infrastructure.restore_service import RestoreService

    assert os.getpid() != config["parent_pid"]
    assert not any(name == "PySide6" or name.startswith("PySide6.") for name in sys.modules)
    _record(config, "child_bootstrap", qt_absent=True, executable=sys.executable,
            compiled_bootstrap=hasattr(desktop, "__compiled__"), argv=sys.argv[1:])

    original_acquire = AuthorityLock.acquire
    original_before_store = desktop.RestoreStartupCoordinator.before_store_open
    original_restore = RestoreService.restore
    reconciled = []

    def acquire(self, *args, **kwargs):
        try:
            result = original_acquire(self, *args, **kwargs)
        except BaseException:
            _record(config, "child_authority_refused")
            raise
        _record(config, "child_authority_acquired")
        return result

    def before_store(self):
        _record(config, "child_before_store")
        result = original_before_store(self)
        reconciled.append(True)
        return result

    def restore(self, *args, **kwargs):
        assert reconciled, "restore ran before pre-store reconciliation"
        _record(config, "child_restore_service")
        return original_restore(self, *args, **kwargs)

    def forbid_store(*args, **kwargs):
        raise AssertionError("pre-store restore child opened a store")

    AuthorityLock.acquire = acquire
    AuthorityLock.open_store = forbid_store
    desktop.RestoreStartupCoordinator.before_store_open = before_store
    RestoreService.restore = restore
    code = main()
    _record(config, "child_exit", status=code)
    return code


def run(main) -> int:
    config_path = Path(os.environ["BOTS5_PACKAGING_RESTORE_CONFIG"])
    config = json.loads(config_path.read_text())
    if "--restore-from" in sys.argv[1:]:
        return _child(main, config)

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QDialog, QMessageBox
    from bots5.bootstrap import desktop
    from bots5.core.application import ApplicationCloseState
    from bots5.desktop import phase9_dialogs
    from bots5.infrastructure.authority_lock import AuthorityLock
    from bots5.infrastructure.restore_service import RestoreService

    assert hasattr(desktop, "__compiled__"), "diagnostic must run in the real frozen desktop"
    config["parent_pid"] = os.getpid()
    config_path.write_text(json.dumps(config))
    context = {"case": config["case"], "parent_pid": os.getpid(), "results": []}
    original_build = desktop.build_runtime
    original_open = desktop.DesktopRuntime.open_window
    original_close = desktop.DesktopRuntime.close
    original_child = desktop._create_restore_child
    result_base = phase9_dialogs.RestoreHandoffResultDialog
    operations = []

    def forbid_parent_restore(*args, **kwargs):
        raise AssertionError("whole-installation restore ran in the parent desktop")

    def build(*args, **kwargs):
        runtime = original_build(*args, **kwargs)
        context["runtime"] = runtime
        original_release = runtime.authority.release

        def release():
            result = original_release()
            _record(config, "parent_authority_released", acquired=runtime.authority.acquired)
            return result

        runtime.authority.release = release
        return runtime

    async def close(self):
        await original_close(self)
        context["close_returned"] = True
        _record(config, "parent_close_returned")

    async def child(argv):
        runtime = context["runtime"]
        assert context.get("close_returned") is True
        assert runtime._close_state is ApplicationCloseState.CLOSED
        assert runtime.application._store.closed
        assert not runtime.authority.acquired
        assert not runtime.windows
        assert argv[0] == os.readlink("/proc/self/exe") and "-m" not in argv
        # Actually reacquire the released lock; flags alone are insufficient.
        authority = AuthorityLock(runtime.paths.data_root).acquire()
        authority.release()
        context["child_argv"] = argv
        context["fresh_authority_after_close"] = True
        _record(config, "parent_spawn_after_release", argv=argv,
                store_closed=True, runtime_closed=True, fresh_authority=True,
                process_executable=os.readlink("/proc/self/exe"),
                sys_executable=sys.executable)
        return await original_child(argv)

    class ResultDialog(result_base):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            context["results"].append({"status": self.status_code,
                "stdout": self.raw_receipt_text, "stderr": self.raw_refusal_text})
            _record(config, "parent_result", status=self.status_code)

        def show(self):
            super().show()
            QTimer.singleShot(0, lambda: self.done(QDialog.DialogCode.Accepted))

    async def request(window):
        runtime = context["runtime"]
        assert runtime.authority.acquired and not runtime.application._store.closed
        _record(config, "parent_live")
        # A real frozen child must refuse while the live parent holds authority.
        locked = await asyncio.create_subprocess_exec(
            *desktop._restore_bootstrap_command(), "--data-root", config["root"], "--restore-from",
            config["package"], "--expected-backup-id", config["backup_id"],
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await locked.communicate()
        assert locked.returncode == 2, stderr.decode()
        assert b"restore not committed" in stderr and b"AuthorityError" in stderr
        context["while_live_refusal"] = {"status": locked.returncode,
            "stdout": stdout.decode(), "stderr": stderr.decode()}
        _record(config, "parent_lock_refusal_proven")

        if config["case"] == "success":
            window.restore_from_backup_action.trigger()
            dialog = window._phase9._dialogs["restore_handoff"]
            assert dialog is not None and dialog.isVisible()
            dialog.package_edit.setText(config["package"])
            context["real_restore_dialog"] = True
            _record(config, "parent_dialog_request")
            dialog.confirm()
        else:
            # Same post-close request seam used by the source refusal tests.
            if config["case"] == "fail_closed":
                journal = Path(config["root"]) / "database/.bots5-restore-journal.json"
                journal.write_bytes(b'{"journal_version": 1, "restore_tr')
                journal.chmod(0o600)
            pin = "deliberately-wrong" if config["case"] == "refusal" else config["backup_id"]
            runtime.handoff.register(desktop.RestoreHandoffRequest(Path(config["package"]), pin))
            outcome = await runtime.handoff.request_orderly_close()
            assert outcome.all_closed

    async def open_window(self, *args, **kwargs):
        window = await original_open(self, *args, **kwargs)
        if not operations:
            operation = asyncio.create_task(request(window))
            operations.append(operation)

            def finish(task):
                if not task.cancelled() and task.exception() is not None:
                    _record(config, "probe_operation_failed", error=str(task.exception()))
                    # Let main finish so the diagnostic reports the actual
                    # harness failure instead of hiding it behind a timeout.
                    window.close()

            operation.add_done_callback(finish)
        return window

    def accept_consequence(box):
        box.done(QDialog.DialogCode.Accepted)

    desktop.build_runtime = build
    desktop.DesktopRuntime.open_window = open_window
    desktop.DesktopRuntime.close = close
    desktop._create_restore_child = child
    desktop._initiate_restore = forbid_parent_restore
    RestoreService.restore = forbid_parent_restore
    phase9_dialogs.RestoreHandoffResultDialog = ResultDialog
    QMessageBox.exec = accept_consequence
    code = main(["--data-root", config["root"]])
    for operation in operations:
        assert operation.done() and not operation.cancelled()
        operation.result()
    runtime = context.pop("runtime")
    assert context.get("fresh_authority_after_close")
    assert runtime.restore_exit_code == code
    assert context["results"] and context["results"][0]["status"] == code
    context["parent_exit_status"] = code
    context["parent_restore_prohibited"] = True
    print("M7_RESTORE_PROBE_JSON:" + json.dumps(context, sort_keys=True))
    return code
