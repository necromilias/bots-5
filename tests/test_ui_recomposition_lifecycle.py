"""Exercise readiness and recovery through the real local core and Qt actions."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from bots5.desktop.window import MainWindow
from bots5.desktop.dialog_primitives import DialogScrollArea
from bots5.desktop.widgets import TuneDialog
from bots5.domain.provider import BackendType, CatalogueAvailability, ProviderProfile
from tests.test_phase11_model_selector import _phase5_application, _run_qasync, _wait_until


def test_disabled_connection_explains_send_gate_and_exposes_real_enable_recovery(tmp_path: Path):
    qt = QApplication.instance() or QApplication([])

    async def scenario():
        application, store = _phase5_application(tmp_path)
        window = MainWindow(application)
        try:
            await window.initialize()
            window.show()
            await asyncio.sleep(0.05)
            chat_id = window._current_chat_id
            selected = await application.chat_model_selection(chat_id)
            model = store.get_model_catalogue_entry(selected.model_entry_id)
            connection = next(c for c in await application.list_provider_connections() if c.id == model.connection_id)
            window.composer.setPlainText("Preserve this draft while repairing the connection.")
            assert window.send_button.isEnabled()

            await application.set_provider_connection_enabled(connection.id, False, expected_revision=connection.revision)
            await _wait_until(lambda: "connection disabled" in window.model_readiness_label.text())
            assert not window.send_button.isEnabled()
            assert window.model_recovery_button.isVisible()
            QTest.keyClick(window.composer, Qt.Key.Key_Return)
            await asyncio.sleep(0.05)
            assert not await application.list_generation_attempts(chat_id)
            assert window.composer.toPlainText() == "Preserve this draft while repairing the connection."

            window.model_recovery_button.click()
            await _wait_until(lambda: window._settings_dialog is not None and "connection disabled" in window._settings_dialog.connection_readiness.text().casefold())
            settings = window._settings_dialog
            assert settings.section_nav.currentRow() == settings.SECTION_PROVIDERS
            assert settings.connection_list.currentItem().data(Qt.ItemDataRole.UserRole) == connection.id
            assert settings.enable_connection_button.isEnabled()
            settings.enable_connection_button.click()
            await _wait_until(lambda: store.get_provider_connection(connection.id).enabled)
            await _wait_until(lambda: "connection disabled" not in window.model_readiness_label.text())
            # Catalogue truth remains authoritative after lifecycle recovery.
            refreshed_model = store.get_model_catalogue_entry(model.id)
            assert window.send_button.isEnabled() is (refreshed_model.availability is CatalogueAvailability.AVAILABLE)
            assert not await application.list_generation_attempts(chat_id)
        finally:
            if window._settings_dialog is not None:
                window._settings_dialog.close()
            await window.stop_bridge_async()
            window.hide()
            window.deleteLater()
            await application.close()

    _run_qasync(qt, scenario())


@pytest.mark.parametrize("input_kind", ["keyboard", "mouse_wheel", "touchpad_pixels", "scrollbar"])
def test_tune_scroll_area_handles_each_operator_input(input_kind: str):
    qt = QApplication.instance() or QApplication([])
    dialog = TuneDialog()
    dialog.resize(480, 360)
    dialog.show()
    qt.processEvents()
    try:
        area = dialog.findChild(DialogScrollArea)
        bar = area.verticalScrollBar()
        assert bar.maximum() > 0
        bar.setValue(0)
        if input_kind == "keyboard":
            area.setFocus()
            QTest.keyClick(area, Qt.Key.Key_PageDown)
            after_down = bar.value()
            assert after_down > 0
            QTest.keyClick(area, Qt.Key.Key_PageUp)
            assert bar.value() < after_down
            return
        if input_kind == "scrollbar":
            bar.setValue(bar.maximum())
        else:
            pixels = QPoint(0, -80) if input_kind == "touchpad_pixels" else QPoint()
            angles = QPoint(0, -120) if input_kind == "mouse_wheel" else QPoint()
            event = QWheelEvent(
                QPointF(30, 30), QPointF(area.mapToGlobal(QPoint(30, 30))),
                pixels, angles, Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.ScrollUpdate, False,
            )
            QApplication.sendEvent(area.viewport(), event)
            assert event.isAccepted()
        assert bar.value() > 0
        assert area.horizontalScrollBar().maximum() == 0
    finally:
        dialog.close()


def test_retired_model_can_remain_explicit_selection_without_claiming_generation_ready(tmp_path: Path):
    qt = QApplication.instance() or QApplication([])

    async def scenario():
        application, store = _phase5_application(tmp_path)
        window = MainWindow(application)
        try:
            await window.initialize()
            window.show()
            await asyncio.sleep(0.05)
            chat_id = window._current_chat_id
            connection = await application.create_provider_connection(
                name="Disposable local fixture", backend_type=BackendType.FAKE,
                profile=ProviderProfile.GENERIC,
            )
            model = await application.add_manual_model(
                connection_id=connection.id, provider_model_id="fixture-model",
                display_name="Fixture model",
            )
            selection = await application.chat_model_selection(chat_id)
            await application.select_model(chat_id, model.id, expected_revision=selection.revision)
            connection = store.get_provider_connection(connection.id)
            await application.retire_provider_connection(connection.id, expected_revision=connection.revision)
            # Existing product semantics permit an explicit unavailable selection.
            selection = await application.chat_model_selection(chat_id)
            await application.select_model(chat_id, model.id, expected_revision=selection.revision)
            await _wait_until(lambda: "connection retired" in window.model_readiness_label.text())
            window.composer.setPlainText("Do not dispatch this unavailable model.")
            assert not window.send_button.isEnabled()
            assert window.model_recovery_button.isVisible()
            QTest.keyClick(window.composer, Qt.Key.Key_Return)
            await asyncio.sleep(0.05)
            assert not await application.list_generation_attempts(chat_id)
            assert store.get_provider_connection(connection.id).retired
            assert (await application.chat_model_selection(chat_id)).model_entry_id == model.id
        finally:
            await window.stop_bridge_async()
            window.hide()
            window.deleteLater()
            await application.close()

    _run_qasync(qt, scenario())
