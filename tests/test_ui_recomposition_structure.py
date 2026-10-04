"""Operator-visible invariants across recomposed shell containers.

These local fake-backend UI tests establish no live-provider or Phase 6 claim.
"""
import asyncio
import os
from pathlib import Path
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication
from bots5.desktop.window import MainWindow
from bots5.domain.models import MessageState
from tests.test_phase11_model_selector import _phase5_application, _run_qasync, _wait_until
from tests.test_desktop_draft1 import LifecycleBackend


def test_global_navigation_stays_above_open_details_and_constrained_send_remains_reachable(tmp_path: Path):
    qt = QApplication.instance() or QApplication([])

    async def scenario():
        app, _ = _phase5_application(tmp_path)
        window = MainWindow(app)
        try:
            await window.initialize()
            window.resize(1600, 900)
            window.show()
            window.top_bar.details_button.setChecked(True)
            await asyncio.sleep(.05)
            # A side dock must never occupy the global navigation's vertical span.
            header_bottom = window.top_bar.mapTo(window, window.top_bar.rect().bottomRight()).y()
            assert window.inspector_dock.mapTo(window, QPoint()).y() > header_bottom
            assert window.top_bar.width() >= window.width() - 40
            actions = tuple(window.menuBar().actions())
            assert any(action.menu() for action in actions)
            window.resize(1000, 700)
            window.composer.setPlainText('A reachable send action at constrained size.')
            await asyncio.sleep(.05)
            assert window.transcript.width() >= 500
            assert window.send_button.isEnabled()
            bottom_right = window.send_button.mapTo(window, window.send_button.rect().bottomRight())
            assert window.rect().contains(bottom_right)
            assert window.send_button.isVisible()
            # Re-rendering content and provider state must respect a closed Details pane.
            window.top_bar.details_button.setChecked(False)
            await window._refresh_transcript(window._current_chat_id)
            await window._refresh_phase5_state()
            assert not window.inspector_dock.isVisible()
            assert tuple(window.menuBar().actions()) == actions
        finally:
            await window.stop_bridge_async()
            window.hide()
            window.deleteLater()
            await app.close()

    _run_qasync(qt, scenario())


def test_failed_turn_surfaces_existing_inspection_reason_and_does_not_leak_into_new_chat(tmp_path: Path):
    qt = QApplication.instance() or QApplication([])

    async def scenario():
        backend = LifecycleBackend(modes={1: 'failed'})
        app, _ = _phase5_application(tmp_path, backend)
        window = MainWindow(app)
        try:
            await window.initialize()
            window.show()
            window.composer.setPlainText('Trigger the deterministic local failure.')
            window.send_button.click()
            await _wait_until(lambda: bool(window._current_messages) and window._current_messages[-1].state is MessageState.FAILED)
            message_id = window._current_messages[-1].id
            await _wait_until(lambda: window.transcript.message_rows[message_id].error_label.isVisible())
            projection = await app.inspect_chat(window._current_chat_id, message_id=message_id)
            reasons = [field.value for field in projection.fields if field.name.startswith('Attempt ') and field.name.endswith(' error') and field.value != 'none']
            label = window.transcript.message_rows[message_id].error_label
            assert label.text() == '\n'.join(reasons) == 'deterministic test failure'
            await window._create_chat()
            assert message_id not in window.transcript.message_rows
            assert not window._generation_busy
        finally:
            await window.stop_bridge_async()
            window.hide()
            window.deleteLater()
            await app.close()

    _run_qasync(qt, scenario())


def test_developer_warning_survives_toolbar_visibility_action(tmp_path: Path):
    from bots5.bootstrap.desktop import build_runtime
    qt = QApplication.instance() or QApplication([])

    async def scenario():
        runtime = build_runtime(tmp_path / 'root', developer_provider_test_mode=True)
        try:
            window = await runtime.open_window()
            assert window.provider_test_banner.isVisible()
            window.console_toolbar.toggleViewAction().trigger()
            await asyncio.sleep(.05)
            assert window.provider_test_banner.isVisible()
            assert 'not Phase 6 compliant' in window.provider_test_banner.text()
        finally:
            await runtime.close()

    _run_qasync(qt, scenario())


def test_advisory_dock_layout_cannot_restore_hidden_developer_warning(tmp_path: Path):
    from bots5.bootstrap.desktop import build_runtime
    qt = QApplication.instance() or QApplication([])

    async def scenario():
        runtime = build_runtime(tmp_path / 'root', developer_provider_test_mode=True)
        try:
            window = await runtime.open_window()
            # A stale Qt layout blob may contain a hidden global toolbar; it
            # must not override the explicit runtime mode's permanent notice.
            window.console_toolbar.hide()
            hidden_layout = bytes(window.saveState())
            window.console_toolbar.show()
            await runtime.application.save_dock_layout(window._window_id, hidden_layout)
            await window._restore_dock_layout()
            await asyncio.sleep(.05)
            assert window.provider_test_banner.isVisible()
            assert window.top_bar.isVisible()
        finally:
            await runtime.close()

    _run_qasync(qt, scenario())


def test_reparented_navigation_shortcuts_fire_once_with_composer_focus(tmp_path: Path):
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtTest import QTest
    from bots5.desktop.palette import CommandPaletteDialog
    qt = QApplication.instance() or QApplication([])

    async def scenario():
        app, _ = _phase5_application(tmp_path)
        window = MainWindow(app)
        opened = []
        window.new_window_requested.connect(lambda: opened.append(True))
        try:
            await window.initialize()
            window.show()
            window.activateWindow()
            window.composer.setFocus()
            await asyncio.sleep(.05)
            QTest.keyClick(window.composer, Qt.Key.Key_N, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
            await _wait_until(lambda: bool(opened))
            assert opened == [True]
            QTest.keyClick(window.composer, Qt.Key.Key_K, Qt.KeyboardModifier.ControlModifier)
            await _wait_until(lambda: window.search_dock.isVisible())
            window.search_dock.hide()
            window.composer.setFocus()
            QTest.keyClick(window.composer, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
            await _wait_until(lambda: window.search_dock.isVisible())
            window.search_dock.hide()
            window.composer.setFocus()
            palettes = []
            def dismiss_palette():
                visible = [item for item in qt.topLevelWidgets() if isinstance(item, CommandPaletteDialog) and item.isVisible()]
                palettes.extend(visible)
                for item in visible:
                    item.reject()
            # Deliver a modal shortcut from the Qt event dispatcher, as a real
            # keyboard event arrives, with the async test suspended. Calling
            # exec() inside an executing asyncio Task corrupts nested qasync
            # task dispatch and is not the product's native input path.
            completed = asyncio.get_running_loop().create_future()
            def deliver_palette_shortcut():
                timer = QTimer(window)
                timer.setSingleShot(True)
                timer.timeout.connect(dismiss_palette)
                timer.start(100)
                try:
                    QTest.keyClick(window.composer, Qt.Key.Key_P, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
                    completed.set_result(None)
                except BaseException as exc:
                    completed.set_exception(exc)
                finally:
                    timer.stop()
            QTimer.singleShot(0, deliver_palette_shortcut)
            await asyncio.wait_for(completed, 3)
            assert len(palettes) == 1
        finally:
            await window.stop_bridge_async()
            window.hide()
            window.deleteLater()
            await app.close()

    _run_qasync(qt, scenario())


def test_delayed_failed_turn_inspection_cannot_replace_new_chat_projection(tmp_path: Path):
    qt = QApplication.instance() or QApplication([])

    async def scenario():
        app, _ = _phase5_application(tmp_path, LifecycleBackend(modes={1: 'failed'}))
        real_inspect = app.inspect_chat
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed_inspection(*args, **kwargs):
            projection = await real_inspect(*args, **kwargs)
            entered.set()
            await release.wait()
            return projection
        app.inspect_chat = delayed_inspection
        window = MainWindow(app)
        try:
            await window.initialize()
            window.show()
            first_chat = window._current_chat_id
            window.composer.setPlainText('Fail locally, then delay the inspection projection.')
            window.send_button.click()
            await asyncio.wait_for(entered.wait(), 3)
            await window._create_chat()
            new_chat = window._current_chat_id
            assert new_chat != first_chat
            release.set()
            await asyncio.sleep(.05)
            assert window._current_chat_id == new_chat
            assert window._current_messages == ()
            assert not window.transcript.message_rows
            assert not window._generation_busy
        finally:
            release.set()
            await window.stop_bridge_async()
            window.hide()
            window.deleteLater()
            await app.close()

    _run_qasync(qt, scenario())


def test_picker_readiness_fits_actual_rendered_font_and_keeps_full_copyable_text():
    from PySide6.QtTest import QTest
    from bots5.desktop.model_selector import ModelSelectorEntry, ModelSelectorPopup
    from bots5.desktop.dialog_primitives import FittedWrappedLabel
    from bots5.desktop.theme import apply_draft1_theme
    qt = QApplication.instance() or QApplication([])
    old_style = qt.styleSheet()
    apply_draft1_theme(qt)
    popup = ModelSelectorPopup()
    try:
        entry = ModelSelectorEntry(model_entry_id='fixture', display_name='Fixture provider model',
            provider_model_id='fixture/provider-with-a-long-model-identifier',
            connection_id='fixture-connection', connection_name='Fixture connection',
            availability='available', connection_refresh_status='succeeded',
            metadata={'context_length': 1048576})
        popup.set_entries((entry,), 'fixture')
        for width in (820, 600):
            popup.resize(width, 620)
            popup.show()
            QTest.qWait(30)
            label = popup.findChild(FittedWrappedLabel, 'modelSelectorDetailSubtitle')
            assert label is not None
            view = label._view
            assert view.document().size().height() <= view.viewport().height() + 1
            assert view.verticalScrollBar().maximum() == 0
            view.selectAll()
            assert view.textCursor().selectedText().replace('\u2029', '\n') == label.text()
    finally:
        popup.close()
        qt.setStyleSheet(old_style)


def test_settings_late_summary_height_does_not_overlap_following_editor(tmp_path: Path):
    """Real app/qasync refresh and page switches must reserve rendered facts."""
    from dataclasses import replace
    from bots5.desktop.dialog_primitives import DialogScrollArea
    qt = QApplication.instance() or QApplication([])
    old_style = qt.styleSheet()

    async def scenario():
        app, _ = _phase5_application(tmp_path)
        window = MainWindow(app)
        try:
            await window.initialize()
            window.show()
            await asyncio.sleep(.1)
            window._on_settings_requested()
            await asyncio.sleep(.3)
            dialog = window._settings_dialog
            assert dialog is not None and dialog.isVisible()
            dialog.section_nav.setCurrentRow(dialog.SECTION_GENERAL)
            await asyncio.sleep(.05)
            dialog.section_nav.setCurrentRow(dialog.SECTION_PROVIDERS)
            await asyncio.sleep(.05)
            label = dialog.connection_readiness
            label.setWordWrap(True)
            await asyncio.sleep(.05)
            editor = label.parentWidget().findChild(DialogScrollArea)
            assert editor is not None

            def assert_readable_without_overlap():
                assert label.geometry().bottom() < editor.geometry().top(), (
                    label.geometry().getRect(), editor.geometry().getRect())
                assert label._view.document().size().height() <= label._view.viewport().height() + 1
                assert label._view.verticalScrollBar().maximum() == 0
                label._view.selectAll()
                assert label._view.textCursor().selectedText().replace('\u2029', '\n') == label.text()

            assert_readable_without_overlap()
            # Updating a hidden page and then showing it at another width is
            # the production ordering that a standalone QWidget test missed.
            connection = (await app.list_provider_connections())[0]
            for width, height in ((720, 520), (1080, 760)):
                dialog.section_nav.setCurrentRow(dialog.SECTION_GENERAL)
                dialog.set_connection_fields(replace(connection,
                    name='A longer connection identity with operator-owned context'))
                label.setWordWrap(True)
                dialog.resize(width, height)
                dialog.section_nav.setCurrentRow(dialog.SECTION_PROVIDERS)
                await asyncio.sleep(.05)
                assert_readable_without_overlap()
        finally:
            if window._settings_dialog is not None:
                window._settings_dialog.close()
            await window.stop_bridge_async()
            window.hide()
            window.deleteLater()
            await app.close()

    try:
        _run_qasync(qt, scenario())
    finally:
        qt.setStyleSheet(old_style)


def test_settings_scaled_nested_editor_bottom_contains_final_setting(tmp_path: Path):
    """Production qasync/style-refresh route must not scroll into stale spacer."""
    from dataclasses import replace
    from bots5.domain.provider import BackendType, ProviderProfile, CredentialSource, CatalogueAvailability, CatalogueRefreshStatus
    from bots5.desktop.dialog_primitives import DialogScrollArea
    from bots5.desktop.theme import build_theme_stylesheet
    qt = QApplication.instance() or QApplication([])
    old_style = qt.styleSheet()

    async def scenario():
        app, _ = _phase5_application(tmp_path)
        window = MainWindow(app)
        try:
            await window.initialize()
            window.show()
            window._on_settings_requested()
            await asyncio.sleep(.3)
            dialog = window._settings_dialog
            dialog.resize(1080, 760)
            dialog.section_nav.setCurrentRow(dialog.SECTION_MODELS)
            await asyncio.sleep(.15)
            dialog.grab()
            connection = (await app.list_provider_connections())[0]
            model = (await app.list_model_catalogue())[0]
            retired = replace(connection, id='fixture-retired',
                name='OpenRouter — archived operational connection',
                backend_type=BackendType.OPENAI_COMPATIBLE_HTTP,
                profile=ProviderProfile.OPENROUTER, endpoint='https://openrouter.ai/api/v1',
                credential_source=CredentialSource.ENVIRONMENT,
                credential_reference='FIXTURE_ONLY_MASKED', retired=True,
                catalogue_refresh_status=CatalogueRefreshStatus.SUCCEEDED)
            entry = replace(model, id='fixture-model', connection_id=retired.id,
                provider_model_id='provider/model-123',
                display_name='Model 123 · operational catalogue example',
                availability=CatalogueAvailability.DISCONNECTED)
            dialog.set_connections((connection, retired), {connection.id: 'available', retired.id: 'available'})
            dialog.set_models((model, entry), {connection.id: connection, retired.id: retired})
            dialog.model_list.setCurrentRow(1)
            await asyncio.sleep(.15)
            dialog.grab()
            dialog.hide()
            dialog.resize(720, 520)
            dialog.show()
            dialog.section_nav.setCurrentRow(dialog.SECTION_MODELS)
            await asyncio.sleep(.15)
            dialog.grab()
            outer = dialog.section_stack.currentWidget()
            inner = outer.widget().findChild(DialogScrollArea)
            assert inner is not None
            outer.verticalScrollBar().setValue(outer.verticalScrollBar().maximum())
            inner.verticalScrollBar().setValue(inner.verticalScrollBar().maximum())
            await asyncio.sleep(.15)
            dialog.grab()
            outer.verticalScrollBar().setValue(0)
            qt.setStyleSheet(build_theme_stylesheet(scale=1.25))
            await asyncio.sleep(.15)
            dialog.grab()
            outer.verticalScrollBar().setValue(outer.verticalScrollBar().maximum())
            inner.verticalScrollBar().setValue(inner.verticalScrollBar().maximum())
            await asyncio.sleep(.15)
            dialog.grab()
            final_row = dialog.model_settings_editor._rows['xtc_threshold'].container
            top = final_row.mapTo(inner.viewport(), final_row.rect().topLeft()).y()
            bottom = top + final_row.height()
            assert 0 <= top and bottom <= inner.viewport().height(), (
                top, bottom, inner.viewport().height(), inner.verticalScrollBar().maximum(),
                dialog.model_settings_editor.geometry().getRect(),
                dialog.model_settings_editor.minimumSizeHint().toTuple(),
                final_row.geometry().getRect(),
                inner.widget().layout().itemAt(inner.widget().layout().count()-1).geometry().getRect())
            label = dialog.model_settings_editor._rows['dynamic_temperature_range'].label
            assert label.heightForWidth(label.width()) <= label.height(), (
                label.geometry().getRect(), label.heightForWidth(label.width()))
        finally:
            if window._settings_dialog is not None:
                window._settings_dialog.close()
            await window.stop_bridge_async()
            window.hide()
            window.deleteLater()
            await app.close()

    try:
        _run_qasync(qt, scenario())
    finally:
        qt.setStyleSheet(old_style)
