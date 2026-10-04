"""Temporary developer smoke mode is NOT production Phase 6 acceptance.

All provider traffic here uses MockTransport and synthetic credentials. Tests
enter through desktop bootstrap; the default is never globally changed.
"""
from __future__ import annotations

import asyncio
import json
import os

import httpx
import pytest
from sqlalchemy import text

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from bots5.bootstrap.desktop import _parser, build_runtime, main
from bots5.core.context import ContextBuilder, ContextSource
from bots5.core.errors import StateError
from bots5.domain.models import AttemptState
from bots5.infrastructure.secrets import FakeSecretStore
from tests.test_desktop_draft1 import _run_qasync
from tests.test_openrouter_capability_discovery import _setup, _discoverer
from tests.test_phase6_context_attachments import _private_engine_connection


@pytest.fixture
def qt_application():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def synthetic_secrets(monkeypatch):
    secrets = FakeSecretStore({"test-ref": "synthetic"})
    monkeypatch.setattr("bots5.bootstrap.desktop.secret_store_for", lambda _source: secrets)
    return secrets


def test_launch_opt_in_is_explicit_and_incompatible_modes_reject(tmp_path, monkeypatch):
    monkeypatch.setenv("BOTS5_DEVELOPER_PROVIDER_TEST_MODE", "1")
    assert _parser().parse_args([]).developer_provider_test_mode is False
    assert _parser().parse_args(["--developer-provider-test-mode"]).developer_provider_test_mode is True
    for other in (["--backend", "local_openai"], ["--restore-from", "/tmp/not-a-backup"]):
        with pytest.raises(SystemExit) as error:
            main(["--developer-provider-test-mode", *other])
        assert error.value.code == 2
    with pytest.raises(ValueError, match="explicit boolean"):
        build_runtime(tmp_path / "never-created", developer_provider_test_mode="yes")
    with pytest.raises(ValueError, match="configured-provider desktop"):
        build_runtime(tmp_path / "never-created", backend="local_openai", developer_provider_test_mode=True)
    assert not (tmp_path / "never-created").exists()


@pytest.mark.parametrize("developer_mode", [False, True])
def test_actual_desktop_bootstrap_gates_openrouter_and_marks_every_window(
    tmp_path, qt_application, synthetic_secrets, developer_mode
):
    async def scenario():
        runtime = build_runtime(tmp_path / "root", developer_provider_test_mode=developer_mode)
        app, store = runtime.application, runtime.application._store
        sent = []
        try:
            assert app.developer_provider_test_mode is developer_mode
            assert app.phase6_enabled is (not developer_mode)
            assert runtime.session.developer_provider_test_mode is developer_mode
            connection = await _setup(app)
            model, = await app.refresh_models(connection.id, _discoverer(["temperature", "max_tokens"]))
            app._backend._secret_stores["secret_service"] = synthetic_secrets

            def stream_handler(request):
                assert request.url.path == "/api/v1/chat/completions"
                sent.append(json.loads(request.content))
                return httpx.Response(200, text=(
                    'data: {"choices":[{"delta":{"content":"Smoke "},"finish_reason":null}]}\n\n'
                    'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":null}]}\n\n'
                    'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
                    'data: [DONE]\n\n'
                ), headers={"content-type": "text/event-stream"})

            app._backend._transports[connection.id] = httpx.MockTransport(stream_handler)
            chat = await app.create_chat()
            await app.select_model(chat.id, model.id)
            for _ in range(2):
                window = await runtime.open_window()
                assert window.provider_test_banner.isVisible() is developer_mode
                assert ("INEXACT / PROVIDER TEST MODE" in window.windowTitle()) is developer_mode
                if developer_mode:
                    assert "not Phase 6 compliant" in window.provider_test_banner.text()
                    assert "Current message only" in window.provider_test_banner.text()
                    assert "history and attachment guarantees are unavailable" in window.provider_test_banner.text()
                    assert not window.attachment_button.isEnabled()
                    window._set_generation_busy(False)
                    window._update_controls()
                    assert not window.attachment_button.isEnabled()
                    await window._refresh_pending_attachment_button(chat.id)
                    assert "unavailable" in window.attachment_button.toolTip()
                    await window._attach_file("/tmp/not-a-real-smoke-attachment")
                    assert "Attachments are unavailable" in window.statusBar().currentMessage()
                    assert window.grab().save(str(tmp_path / "provider-test-desktop.png"))

            if not developer_mode:
                from tests.test_phase6_context_attachments import _finish
                attempt = await app.send_message(chat.id, "Hello")
                await _finish(app, attempt.id)
                snapshot = json.loads(store.get_generation_attempt(attempt.id).request_snapshot)
                assert snapshot["snapshot_version"] == 5
                assert snapshot["accounting_mode"] == "provider-managed"
                assert len(sent) == 1
                assert "Context accounting: Provider-managed" in window.model_readiness_label.text()
            else:
                for prompt in ("First message", "Second message"):
                    attempt = await app.send_message(chat.id, prompt)
                    for _ in range(200):
                        current = store.get_generation_attempt(attempt.id)
                        if current.state is not AttemptState.RUNNING:
                            break
                        await asyncio.sleep(0.01)
                    assert current.state is AttemptState.COMPLETE
                    assert current.finish_reason == "stop"
                    assert store.get_message(current.assistant_message_id).content == "Smoke OK"
                    snapshot = json.loads(current.request_snapshot)
                    assert snapshot["snapshot_version"] == 2
                    assert "context_plan" not in snapshot
                    assert "developer_provider_test_mode" not in snapshot
                    with _private_engine_connection(store) as db:
                        assert db.execute(text("SELECT count(*) FROM context_plans WHERE attempt_id=:id"), {"id": attempt.id}).scalar_one() == 0
                assert len(sent) == 2
                assert sent[-1]["messages"] == [{"role": "user", "content": "Second message"}]
                assert sent[-1]["provider"] == {"require_parameters": True}
                assert sent[-1]["stream"] is True
                with pytest.raises(StateError, match="attachments are unavailable"):
                    await app.stage_attachment(chat.id, "any-attachment")
                with pytest.raises(StateError, match="context planning is unavailable"):
                    await app.build_context_plan(
                        chat.id, parent_message_id=None,
                        current_user=ContextSource("u", "current_user", "user", "Hi"),
                        builder=ContextBuilder(), context_window=32768,
                        context_window_provenance="test", output_reserve=10,
                    )
                # Even stale/injected staged state must not be silently ignored.
                app._pending_attachment_ids[chat.id] = ("stale",)
                with pytest.raises(StateError, match="attachments are unavailable"):
                    await app.send_message(chat.id, "Do not silently omit attachments")
                assert len(sent) == 2
        finally:
            # Dispose this test's windows while their qasync loop still exists.
            # Runtime shutdown closes the services, not the Qt widget objects.
            for opened_window in tuple(runtime.windows):
                await opened_window.stop_bridge_async()
                opened_window.deleteLater()
            await runtime.close()

        # The same durable root resumes normal production accounting without the flag.
        reopened = build_runtime(tmp_path / "root")
        try:
            assert reopened.application.phase6_enabled is True
            assert reopened.application.developer_provider_test_mode is False
            reopened.application._backend._secret_stores["secret_service"] = synthetic_secrets
            reopened.application._backend._transports[connection.id] = httpx.MockTransport(stream_handler)
            from tests.test_phase6_context_attachments import _finish
            attempt = await reopened.application.send_message(chat.id, "Default again")
            await _finish(reopened.application, attempt.id)
            snapshot = json.loads(reopened.application._store.get_generation_attempt(attempt.id).request_snapshot)
            assert snapshot["accounting_mode"] == "provider-managed"
            assert len(reopened.application._store.list_generation_attempts(chat.id)) == (3 if developer_mode else 2)
        finally:
            await reopened.close()

    _run_qasync(qt_application, scenario())


@pytest.mark.parametrize("parameters", [[], ["temperature"], ["max_tokens"]])
def test_developer_mode_does_not_bypass_required_capabilities(
    tmp_path, qt_application, synthetic_secrets, parameters
):
    async def scenario():
        runtime = build_runtime(tmp_path / "root", developer_provider_test_mode=True)
        app = runtime.application
        try:
            connection = await _setup(app)
            model, = await app.refresh_models(connection.id, _discoverer(parameters))
            chat = await app.create_chat()
            await app.select_model(chat.id, model.id)
            with pytest.raises(StateError, match="not established as supported"):
                await app.send_message(chat.id, "Hello")
            assert app._store.list_generation_attempts(chat.id) == ()
        finally:
            await runtime.close()
    _run_qasync(qt_application, scenario())
