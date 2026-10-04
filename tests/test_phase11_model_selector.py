"""Phase 11 F8 — model-selector polish tests.

Covers the sealed design §4.4 hierarchy:
- primary line = the catalogue entry's ``display_name`` (adopting it here is
  the change — there was no display_name usage in the landed selector);
- secondary line = connection name + context window;
- a connection-health pill;
- entries grouped by connection;
- search/filter narrowing the list;
- a detail card showing context length, streaming support and pricing drawn
  from the catalogue metadata (pricing omitted cleanly when absent);
- the landed contracts: exactly one ``QLabel#modelPill`` and the legacy /
  no-Phase-5 hide-disable behaviour pinned by test_desktop_draft1.py.

Recents/favourites are SESSION-ONLY (fork R-6): no test here persists any
selector state and none touches the database schema.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QFrame, QLabel
from qasync import QEventLoop

from bots5.core.application import BotsApplication
from bots5.core.events import EventBus
from bots5.core.provider_configuration import ProviderConfiguration
from bots5.desktop.model_selector import (
    HEALTH_DOWN,
    HEALTH_OK,
    HEALTH_UNKNOWN,
    ModelSelectorButton,
    ModelSelectorEntry,
    ModelSelectorPopup,
    connection_health_state,
    entry_detail_rows,
)
from bots5.desktop.profile import DesktopSessionInfo
from bots5.desktop.window import MainWindow
from bots5.desktop.widgets import TopBar
from bots5.domain.clock import SystemClock
from bots5.domain.ids import Uuid7Factory
from bots5.infrastructure.generation.fake import FakeStreamingBackend
from tests._authority_test_support import SQLiteAppStateStore, upgrade_database


def _run_qasync(qt_application: QApplication, operation) -> None:
    qt_application.setQuitOnLastWindowClosed(False)
    event_loop = QEventLoop(qt_application)
    asyncio.set_event_loop(event_loop)
    with event_loop:
        event_loop.run_until_complete(operation)


async def _wait_until(predicate, *, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("timed out waiting for desktop state")
        await asyncio.sleep(0.005)


def _phase5_application(tmp_path: Path, backend=None):
    database = tmp_path / "state.sqlite3"
    upgrade_database(database)
    ids = Uuid7Factory()
    clock = SystemClock()
    store = SQLiteAppStateStore.open(database)
    configuration = ProviderConfiguration(store, ids, clock, phase6_enabled=False)
    application = BotsApplication(
        store,
        EventBus(clock, ids, queue_size=64),
        backend or FakeStreamingBackend(),
        ids=ids,
        clock=clock,
        configuration=configuration,
    )
    return application, store


def _legacy_application(tmp_path: Path, backend=None):
    database = tmp_path / "state.sqlite3"
    upgrade_database(database)
    ids = Uuid7Factory()
    clock = SystemClock()
    return BotsApplication(
        SQLiteAppStateStore.open(database),
        EventBus(clock, ids, queue_size=32),
        backend or FakeStreamingBackend(),
        ids=ids,
        clock=clock,
    )


_TWO_CONNECTION_ENTRIES = (
    ModelSelectorEntry(
        model_entry_id="model-a1",
        display_name="Qwen3.5 35B Instruct",
        provider_model_id="Qwen3.5:35B-A3B",
        connection_id="conn-1",
        connection_name="Local Ollama",
        availability="available",
        connection_refresh_status="succeeded",
        context_tokens=262144,
        metadata={"context_length": 262144, "streaming": True},
    ),
    ModelSelectorEntry(
        model_entry_id="model-a2",
        display_name="Qwen3.5 Coder",
        provider_model_id="Qwen3.5-Coder",
        connection_id="conn-1",
        connection_name="Local Ollama",
        availability="available",
        connection_refresh_status="succeeded",
    ),
    ModelSelectorEntry(
        model_entry_id="model-b1",
        display_name="Fake v0.1",
        provider_model_id="fake-v0.1",
        connection_id="conn-2",
        connection_name="Built-in fake",
        availability="available",
        connection_backend_type="fake",
    ),
)


def _popup_item_rows(popup: ModelSelectorPopup) -> list[tuple[str, str]]:
    """Return [(kind, text)] for every visible list item, in order."""

    rows = []
    for index in range(popup.list.count()):
        item = popup.list.item(index)
        widget = popup.list.itemWidget(item)
        if widget is not None and widget.objectName() == "modelSelectorGroupHeader":
            rows.append(("header", widget.findChild(QLabel, "modelSelectorGroupLabel").text()))
        else:
            rows.append(("entry", str(item.data(Qt.ItemDataRole.UserRole))))
    return rows


# =============================================================================
# Top-bar button: display_name hierarchy
# =============================================================================


def test_model_selector_primary_line_is_display_name_and_secondary_is_connection_context():
    qt_application = QApplication.instance() or QApplication([])
    bar = TopBar(DesktopSessionInfo(backend_id="fake", model="legacy"), phase5=True)
    try:
        # FEATURE-POSITIVE: the rich primary line exists only in the new selector.
        primary = bar.model_selector.findChild(QLabel, "modelSelectorButtonPrimary")
        assert primary is not None, "the model selector must expose a primary display_name line"

        bar.set_models((), "model-a1", records=[_TWO_CONNECTION_ENTRIES[0]])
        assert primary.text() == "Qwen3.5 35B Instruct"
        secondary = bar.model_selector.findChild(QLabel, "modelSelectorButtonSecondary")
        assert secondary is not None
        assert secondary.text() == "Local Ollama · 262,144 tokens"
        assert bar.model_selector.currentIndex() == 0
        assert bar.model_selector.current_model_entry_id() == "model-a1"

        # No durable selection → visibly unselected (landed behaviour shape).
        bar.set_models((), None, records=[_TWO_CONNECTION_ENTRIES[0]])
        assert bar.model_selector.currentIndex() == -1
        assert primary.text() == "Selection required"
    finally:
        bar.deleteLater()
        qt_application.processEvents()


def test_topbar_set_models_keeps_landed_flat_label_and_signal_contract():
    qt_application = QApplication.instance() or QApplication([])
    bar = TopBar(DesktopSessionInfo(backend_id="fake", model="legacy"), phase5=True)
    try:
        # FEATURE-POSITIVE: the selector is the new rich host, not the flat combo.
        assert isinstance(bar.model_selector, ModelSelectorButton)
        assert bar.model_selector.findChild(QLabel, "modelSelectorButtonPrimary") is not None

        selected: list[str] = []
        bar.model_selected.connect(selected.append)
        bar.set_models((("Built-in fake / fake-v0.1 [available]", "model-1"),), None)
        assert bar.model_selector.currentIndex() == -1
        bar.model_selector.setCurrentIndex(0)
        assert selected == ["model-1"]
        assert bar.model_selector.current_model_entry_id() == "model-1"
    finally:
        bar.deleteLater()
        qt_application.processEvents()


# =============================================================================
# Popup: connection grouping + health pill
# =============================================================================


def test_model_selector_popup_groups_entries_by_connection():
    qt_application = QApplication.instance() or QApplication([])
    popup = ModelSelectorPopup()
    try:
        popup.set_entries(_TWO_CONNECTION_ENTRIES, None)
        # FEATURE-POSITIVE: group headers exist only in the new selector.
        assert popup.findChildren(QFrame, "modelSelectorGroupHeader"), (
            "the selector popup must group entries by connection"
        )

        rows = _popup_item_rows(popup)
        assert rows == [
            ("header", "LOCAL OLLAMA"),
            ("entry", "model-a1"),
            ("entry", "model-a2"),
            ("header", "BUILT-IN FAKE"),
            ("entry", "model-b1"),
        ]
        assert len(popup.findChildren(QFrame, "modelSelectorGroupHeader")) == 2
    finally:
        popup.deleteLater()
        qt_application.processEvents()


def test_model_selector_popup_connection_health_pill_reflects_connection_state():
    qt_application = QApplication.instance() or QApplication([])
    popup = ModelSelectorPopup()
    try:
        assert connection_health_state(
            ModelSelectorEntry(model_entry_id="x", display_name="X", connection_refresh_status="succeeded")
        ) == HEALTH_OK
        assert connection_health_state(
            ModelSelectorEntry(model_entry_id="x", display_name="X", connection_refresh_status="never")
        ) == HEALTH_UNKNOWN
        assert connection_health_state(
            ModelSelectorEntry(
                model_entry_id="x", display_name="X", connection_refresh_status="never",
                connection_available=False,
            )
        ) == HEALTH_DOWN

        entries = (
            ModelSelectorEntry(
                model_entry_id="m-ok", display_name="Refreshed", connection_id="c1",
                connection_name="Refreshed connection", connection_refresh_status="succeeded",
            ),
            ModelSelectorEntry(
                model_entry_id="m-unknown", display_name="Unrefreshed", connection_id="c2",
                connection_name="Unrefreshed connection", connection_refresh_status="never",
            ),
            ModelSelectorEntry(
                model_entry_id="m-down", display_name="Disabled", connection_id="c3",
                connection_name="Disabled connection", connection_refresh_status="never",
                connection_available=False,
            ),
        )
        # FEATURE-POSITIVE: the health pill widget exists only in the new selector.
        pills = popup.findChildren(QLabel, "connectionHealthPill")
        assert not pills, "sanity: a fresh popup has no pills before entries are set"
        popup.set_entries(entries, None)

        pills = popup.findChildren(QLabel, "connectionHealthPill")
        assert len(pills) == 3, "each connection group must carry one health pill"
        states = {pill.property("health"): pill.text() for pill in pills}
        assert states == {HEALTH_OK: "Catalogue refreshed", HEALTH_UNKNOWN: "Catalogue not refreshed", HEALTH_DOWN: "Connection unavailable"}
    finally:
        popup.deleteLater()
        qt_application.processEvents()


# =============================================================================
# Popup: search / filter
# =============================================================================


def test_model_selector_search_narrows_the_list_and_restores_it():
    qt_application = QApplication.instance() or QApplication([])
    popup = ModelSelectorPopup()
    try:
        # FEATURE-POSITIVE: the searchable list exists only in the new selector.
        assert popup.search_edit.objectName() == "modelSelectorSearch"
        popup.set_entries(_TWO_CONNECTION_ENTRIES, None)
        assert popup.status_label.text() == "3 models"

        popup.search_edit.setText("qwen")
        rows = _popup_item_rows(popup)
        assert [kind for kind, _ in rows].count("entry") == 2
        assert [text for kind, text in rows if kind == "entry"] == ["model-a1", "model-a2"]
        assert [text for kind, text in rows if kind == "header"] == ["LOCAL OLLAMA"]
        assert popup.status_label.text() == "2 of 3 models"

        popup.search_edit.setText("fake-v0.1")
        rows = _popup_item_rows(popup)
        assert [text for kind, text in rows if kind == "entry"] == ["model-b1"]
        assert popup.status_label.text() == "1 of 3 models"

        popup.search_edit.setText("no-such-model-anywhere")
        assert _popup_item_rows(popup) == []
        assert popup.status_label.text() == "0 of 3 models"

        popup.search_edit.clear()
        rows = _popup_item_rows(popup)
        assert [kind for kind, _ in rows].count("entry") == 3
        assert popup.status_label.text() == "3 models"
    finally:
        popup.deleteLater()
        qt_application.processEvents()


def test_model_selector_search_matches_connection_name_and_display_name():
    qt_application = QApplication.instance() or QApplication([])
    popup = ModelSelectorPopup()
    try:
        popup.set_entries(_TWO_CONNECTION_ENTRIES, None)
        # FEATURE-POSITIVE: filtering over entries requires the new popup.
        assert popup.list.count() > 0

        popup.search_edit.setText("built-in fake")
        assert [text for kind, text in _popup_item_rows(popup) if kind == "entry"] == ["model-b1"]

        popup.search_edit.setText("coder")
        assert [text for kind, text in _popup_item_rows(popup) if kind == "entry"] == ["model-a2"]
    finally:
        popup.deleteLater()
        qt_application.processEvents()


# =============================================================================
# Popup: detail card
# =============================================================================


def test_model_selector_detail_card_shows_context_streaming_pricing():
    qt_application = QApplication.instance() or QApplication([])
    popup = ModelSelectorPopup()
    try:
        # FEATURE-POSITIVE: the detail card exists only in the new selector.
        card = popup.findChild(QFrame, "modelSelectorDetailCard")
        assert card is not None, "the selector popup must expose a detail card"

        entry = ModelSelectorEntry(
            model_entry_id="m-rich",
            display_name="Qwen3.5 35B Instruct",
            provider_model_id="Qwen3.5:35B-A3B",
            connection_id="c1",
            connection_name="Local Ollama",
            availability="available",
            metadata={
                "context_length": 262144,
                "streaming": True,
                "pricing": "$0.90 per 1M tokens",
            },
        )
        popup.set_entries((entry,), "m-rich")
        current = popup.list.currentItem()
        assert current is not None
        assert str(current.data(Qt.ItemDataRole.UserRole)) == "m-rich"

        keys = [label.text() for label in popup.findChildren(QLabel, "modelSelectorDetailKey")]
        values = [label.text() for label in popup.findChildren(QLabel, "modelSelectorDetailValue")]
        assert list(zip(keys, values)) == [
            ("Context", "262,144 tokens"),
            ("Streaming", "supported"),
            ("Pricing", "$0.90 per 1M tokens"),
        ]
        title = popup.findChild(QLabel, "modelSelectorDetailTitle")
        assert title.text() == "Qwen3.5 35B Instruct"
        subtitle = popup.findChild(QLabel, "modelSelectorDetailSubtitle")
        assert "Qwen3.5:35B-A3B" in subtitle.text()
        assert "Provider and model ready" in subtitle.text()
        assert "Model available" in subtitle.text()
        assert "validated when you send" in subtitle.text()
    finally:
        popup.deleteLater()
        qt_application.processEvents()


def test_model_selector_detail_card_omits_pricing_cleanly_when_metadata_is_absent():
    qt_application = QApplication.instance() or QApplication([])
    popup = ModelSelectorPopup()
    try:
        card = popup.findChild(QFrame, "modelSelectorDetailCard")
        assert card is not None, "the selector popup must expose a detail card"

        # Context only: pricing is omitted cleanly, nothing else is invented.
        assert entry_detail_rows(
            ModelSelectorEntry(
                model_entry_id="m1",
                display_name="Context only",
                metadata={"context_length": 131072},
            )
        ) == (("Context", "131,072 tokens"),)

        # Streaming-only metadata: context and pricing absent.
        assert entry_detail_rows(
            ModelSelectorEntry(model_entry_id="m2", display_name="S", metadata={"streaming": False})
        ) == (("Streaming", "not supported"),)

        # No metadata at all: the card says so instead of inventing rows.
        assert entry_detail_rows(ModelSelectorEntry(model_entry_id="m3", display_name="Bare")) == ()
        popup.set_entries((ModelSelectorEntry(model_entry_id="m3", display_name="Bare"),), "m3")
        empty = popup.findChild(QLabel, "modelSelectorDetailEmpty")
        assert empty is not None
        assert empty.isVisibleTo(card)
        assert popup.findChildren(QLabel, "modelSelectorDetailKey") == []
    finally:
        popup.deleteLater()
        qt_application.processEvents()


# =============================================================================
# Landed contracts: single modelPill + legacy hide/disable
# =============================================================================


def test_topbar_keeps_exactly_one_model_pill_and_phase5_selector_visibility():
    qt_application = QApplication.instance() or QApplication([])
    bar = TopBar(DesktopSessionInfo(backend_id="fake", model="fake-v0.1"), phase5=True)
    try:
        # FEATURE-POSITIVE: the pill plus the new selector host must coexist.
        assert isinstance(bar.model_selector, ModelSelectorButton)
        assert len(bar.findChildren(QLabel, "modelPill")) == 1
        assert bar.model_selector.isVisibleTo(bar)
        assert bar.model_pill.text() == "fake-v0.1"
    finally:
        bar.deleteLater()
        qt_application.processEvents()


def test_topbar_legacy_mode_hides_selector_and_disables_tune_settings():
    qt_application = QApplication.instance() or QApplication([])
    bar = TopBar(DesktopSessionInfo(backend_id="fake", model="fake-v0.1"), phase5=False)
    try:
        # FEATURE-POSITIVE: the legacy contract is asserted against the new host.
        assert isinstance(bar.model_selector, ModelSelectorButton)
        assert not bar.model_selector.isVisibleTo(bar)
        assert len(bar.findChildren(QLabel, "modelPill")) == 1
        for button in (bar.tune_button, bar.settings_button):
            assert not button.isEnabled()
            assert button.toolTip()
    finally:
        bar.deleteLater()
        qt_application.processEvents()


# =============================================================================
# Window integration (Phase 5 mode)
# =============================================================================


def test_phase5_window_selector_uses_display_name_single_pill_and_grouped_popup(tmp_path: Path):
    qt_application = QApplication.instance() or QApplication([])

    async def scenario():
        application, _store = _phase5_application(tmp_path)
        window = MainWindow(application)
        try:
            await window.initialize()
            await asyncio.sleep(0.05)

            connections = await application.list_provider_connections()
            connection = connections[0]
            rich_model = await application.add_manual_model(
                connection_id=connection.id,
                provider_model_id="Qwen3.5:35B-A3B",
                display_name="Qwen3.5 35B Instruct",
                metadata={"context_length": 131072, "streaming": True},
            )

            # FEATURE-POSITIVE: the whole window keeps exactly one landed pill.
            assert len(window.findChildren(QLabel, "modelPill")) == 1

            await window._select_model(window._current_chat_id, rich_model.id)
            await _wait_until(
                lambda: window.top_bar.model_selector.current_model_entry_id() == rich_model.id
            )

            # Primary line is the display_name, secondary is connection + context.
            primary = window.top_bar.model_selector.findChild(QLabel, "modelSelectorButtonPrimary")
            secondary = window.top_bar.model_selector.findChild(QLabel, "modelSelectorButtonSecondary")
            assert primary.text() == "Qwen3.5 35B Instruct"
            assert secondary.text() == "Built-in fake · 131,072 tokens"

            # The popup groups the seeded fake entry and the manual entry under
            # one connection header and carries the health pill.
            window._open_model_selector()
            popup = window._model_selector_popup
            assert popup is not None, "the window must host the model-selector popup"
            try:
                rows = _popup_item_rows(popup)
                headers = [text for kind, text in rows if kind == "header"]
                entry_ids = [text for kind, text in rows if kind == "entry"]
                fake_entry_id = next(
                    model.id
                    for model in await application.list_model_catalogue()
                    if model.provider_model_id == "fake-v0.1"
                )
                assert headers == ["BUILT-IN FAKE"]
                assert set(entry_ids) == {rich_model.id, fake_entry_id}
                assert popup.findChildren(QLabel, "connectionHealthPill")
                assert popup.detail_card.findChild(QLabel, "modelSelectorDetailValue").text() == (
                    "131,072 tokens"
                )

                # Choosing the other entry runs the landed durable selection path.
                for index in range(popup.list.count()):
                    item = popup.list.item(index)
                    if item.data(Qt.ItemDataRole.UserRole) == fake_entry_id:
                        popup.list.setCurrentItem(item)
                        break
                popup.choose_button.click()
                await _wait_until(
                    lambda: window.top_bar.model_selector.current_model_entry_id() == fake_entry_id
                )
                assert primary.text() == "fake-v0.1"
            finally:
                popup.close()
                popup.deleteLater()
            assert window._model_selector_popup is None
        finally:
            await window.stop_bridge_async()
            window.deleteLater()
            await application.close()

    _run_qasync(qt_application, scenario())


def test_legacy_window_hides_selector_and_disables_tune_settings_tool(tmp_path: Path):
    qt_application = QApplication.instance() or QApplication([])

    async def scenario():
        application = _legacy_application(tmp_path)
        window = MainWindow(application)
        try:
            await window.initialize()
            await asyncio.sleep(0.05)

            # FEATURE-POSITIVE: the legacy contract asserted against the new host.
            assert isinstance(window.top_bar.model_selector, ModelSelectorButton)
            assert not window.top_bar.model_selector.isVisibleTo(window.top_bar)
            assert len(window.findChildren(QLabel, "modelPill")) == 1
            assert window.top_bar.model_pill.text() == "fake-v0.1"
            for button in (
                window.top_bar.tune_button,
                window.top_bar.settings_button,
                window.tool_button,
            ):
                assert not button.isEnabled()
                assert button.toolTip()
            # No records are presented in legacy mode and the popup never opens.
            assert window.top_bar.model_selector.has_entries() is False
            window._open_model_selector()
            assert window._model_selector_popup is None
        finally:
            await window.stop_bridge_async()
            window.deleteLater()
            await application.close()
            qt_application.processEvents()

    _run_qasync(qt_application, scenario())
