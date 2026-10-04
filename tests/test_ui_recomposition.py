"""Behavioral presentation checks for bounded UI recomposition."""
from dataclasses import replace
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from bots5.desktop.model_selector import ModelSelectorEntry, ModelSelectorPopup, model_readiness
from bots5.desktop.widgets import AddConnectionDialog, SettingsDialog
from bots5.desktop.dialog_primitives import DialogScrollArea
from bots5.desktop.theme import apply_draft1_theme
from bots5.domain.provider import BackendType, ProviderProfile, CredentialSource, ProviderConnection, ModelCatalogueEntry, CatalogueOrigin, CatalogueAvailability

_ui_application = None

@pytest.fixture
def ui():
    global _ui_application
    application = QApplication.instance() or QApplication([])
    _ui_application = application
    application.setQuitOnLastWindowClosed(False)
    apply_draft1_theme(application)
    return application

@pytest.mark.parametrize('fields,reason', [
    ({'connection_retired':True,'credential_status':'available'}, 'connection retired'),
    ({'connection_enabled':False}, 'connection disabled'),
    ({'credential_required':True,'credential_status':'missing'}, 'credential missing'),
    ({'availability':'stale'}, 'model stale'),
    ({'connection_refresh_status':'failed'}, 'Provider and model ready'),
])
def test_readiness_keeps_lifecycle_credentials_refresh_and_model_distinct(fields, reason):
    entry = ModelSelectorEntry('m','Example',**fields)
    heading, detail = model_readiness(entry)
    assert reason in heading
    if fields.get('connection_refresh_status') == 'failed':
        assert 'validated when you send' in detail


def test_large_picker_provider_filter_and_unavailable_selection_intent(ui):
    dialog = ModelSelectorPopup()
    entries = [ModelSelectorEntry(f'm{i}',f'Model {i}',connection_id=f'c{i%2}',connection_name=f'Connection {i%2}', connection_retired=i%2==1) for i in range(466)]
    chosen=[]
    dialog.model_entry_chosen.connect(chosen.append)
    dialog.set_entries(entries,'m123')
    dialog.provider_filter.setCurrentIndex(dialog.provider_filter.findData('c1'))
    dialog.search_edit.setText('Model 123')
    items = [dialog.list.item(i) for i in range(dialog.list.count()) if dialog.list.item(i).data(Qt.ItemDataRole.UserRole)]
    assert len(items)==1 and items[0].data(Qt.ItemDataRole.UserRole)=='m123'
    assert 'connection retired' in dialog.detail_card._subtitle.text()
    assert dialog.choose_button.isEnabled()
    assert dialog.choose_button.text()=='Select unavailable model'
    dialog.choose_button.click()
    assert chosen==['m123']


def test_settings_filter_keeps_truth_and_recovers_to_selected_provider(ui):
    dialog = SettingsDialog()
    connection = ProviderConnection('c','Retired OpenRouter',BackendType.OPENAI_COMPATIBLE_HTTP,ProviderProfile.OPENROUTER,'https://openrouter.ai/api/v1',CredentialSource.ENVIRONMENT,retired=True)
    models=[ModelCatalogueEntry(f'm{i}','c',f'provider/m{i}',f'Model {i}',CatalogueOrigin.DISCOVERED,CatalogueAvailability.DISCONNECTED) for i in range(466)]
    dialog.set_connections([connection],{'c':'available'})
    dialog.set_models(models,{'c':connection})
    dialog.model_search.setText('Model 123')
    visible=[dialog.model_list.item(i) for i in range(dialog.model_list.count()) if not dialog.model_list.item(i).isHidden()]
    assert len(visible)==1
    dialog.model_list.setCurrentItem(visible[0])
    dialog.model_view_connection_button.click()
    assert dialog.section_nav.currentRow()==dialog.SECTION_PROVIDERS
    assert 'connection retired' in dialog.connection_readiness.text()
    assert 'Credential available' in dialog.connection_readiness.text()
    assert not dialog.enable_connection_button.isEnabled()
    assert connection.retired and all(m.availability is CatalogueAvailability.DISCONNECTED for m in models)


def test_add_connection_constrained_content_scrolls_with_reachable_actions(ui):
    dialog=AddConnectionDialog()
    dialog.resize(520,380)
    dialog.show()
    ui.processEvents()
    scroll=dialog.findChild(DialogScrollArea)
    assert scroll is not None
    assert scroll.verticalScrollBar().maximum()>0
    assert dialog.save_button.isVisible() and dialog.save_refresh_button.isVisible()
    assert not scroll.isAncestorOf(dialog.save_refresh_button)
    assert dialog.save_refresh_button.mapTo(dialog,dialog.save_refresh_button.rect().bottomRight()).y()<dialog.height()
    dialog.hide()

@pytest.mark.parametrize('key,default,expected', [
    (Qt.Key.Key_Return, 'yes', 'yes'),
    (Qt.Key.Key_Return, 'no', 'no'),
    (Qt.Key.Key_Escape, 'yes', 'no'),
])
def test_confirmation_text_focus_preserves_default_return_and_escape(ui,key,default,expected):
    from PySide6.QtCore import QTimer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QMessageBox
    from bots5.desktop.dialog_primitives import normalized_question
    values={'yes':QMessageBox.StandardButton.Yes,'no':QMessageBox.StandardButton.No}
    def interact():
        box=next(w for w in ui.topLevelWidgets() if isinstance(w,QMessageBox) and w.isVisible())
        box._bots_confirmation_text.setFocus()
        QTest.keyClick(box._bots_confirmation_text,key)
    QTimer.singleShot(15,interact)
    fuse=QTimer()
    fuse.setSingleShot(True)
    fuse.timeout.connect(lambda: next(w for w in ui.topLevelWidgets() if isinstance(w,QMessageBox) and w.isVisible()).reject())
    fuse.start(1000)
    try:
        result=normalized_question(None,'Keyboard consent','The complete warning stays selectable.',values['yes']|values['no'],values[default])
    finally:
        fuse.stop()
    assert result==values[expected]


def test_static_confirmation_gc_keeps_screen_and_new_window_alive(ui):
    import gc
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QMessageBox,QMainWindow
    from bots5.desktop.dialog_primitives import normalized_question
    for _ in range(10):
        fuse=QTimer()
        fuse.setSingleShot(True)
        def refuse():
            box=next(w for w in ui.topLevelWidgets() if isinstance(w,QMessageBox) and w.isVisible())
            box.button(QMessageBox.StandardButton.No).click()
        fuse.timeout.connect(refuse)
        fuse.start(1000)
        QTimer.singleShot(15,refuse)
        try:
            assert normalized_question(None,'Lifetime fixture','Full warning.',QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)==QMessageBox.StandardButton.No
        finally:
            fuse.stop()
        gc.collect()
        assert ui.primaryScreen().availableGeometry().isValid()
        window=QMainWindow()
        window.show()
        ui.processEvents()
        window.close()
        window.deleteLater()
        ui.processEvents()



def test_context_menu_marks_unimplemented_actions_unavailable(ui):
    from bots5.desktop.widgets import LeftRail
    rail=LeftRail()
    menu=rail._build_chat_menu('fixture-chat',rail)
    actions={action.text():action for action in menu.actions()}
    for name in ('Rename Title…','Duplicate Chat…'):
        assert not actions[name].isEnabled()
        assert actions[name].toolTip()=='Not available in this desktop interface.'
    assert menu.toolTipsVisible()
    assert actions['Move to folder…'].isEnabled()
    assert actions['Delete chat…'].isEnabled()
    menu.deleteLater()
    rail.deleteLater()


def test_campaign_regeneration_uses_shared_cancel_before_primary(ui):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QDialog,QDialogButtonBox
    from bots5.desktop.campaign_dock import CampaignDockWidget
    dock=CampaignDockWidget(bridge_factory=lambda _runs:object())
    dock._bridge=object()
    dock._selected_stage=lambda:{'stage_id':'fixture-stage','requested_model':'fixture-model'}
    observed=[]
    def inspect_and_cancel():
        dialog=next(w for w in ui.topLevelWidgets() if isinstance(w,QDialog) and w.objectName()=='campaignRegenerationDialog' and w.isVisible())
        box=dialog.findChild(QDialogButtonBox)
        cancel=box.button(QDialogButtonBox.StandardButton.Cancel)
        primary=box.button(QDialogButtonBox.StandardButton.Ok)
        observed.append((cancel.x(),primary.x(),box.buttonRole(cancel),box.buttonRole(primary)))
        cancel.click()
    fuse=QTimer()
    fuse.setSingleShot(True)
    fuse.timeout.connect(inspect_and_cancel)
    fuse.start(1000)
    QTimer.singleShot(15,inspect_and_cancel)
    try:
        dock._on_regenerate()
    finally:
        fuse.stop()
        dock.deleteLater()
    assert len(observed)==1
    cancel_x,primary_x,cancel_role,primary_role=observed[0]
    assert cancel_x<primary_x
    assert cancel_role==QDialogButtonBox.ButtonRole.RejectRole
    assert primary_role==QDialogButtonBox.ButtonRole.AcceptRole



def test_details_wraps_populated_ids_and_preserves_exact_clipboard(ui):
    from PySide6.QtWidgets import QLabel
    from bots5.desktop.widgets import InspectorPanel
    from bots5.core.inspection import InspectionProjection,InspectionField
    values=('01a0fed8-1dde-74fc-aab4-7df833c99a98','01a0fed8-214c-7886-81d9-675b2359dd92','01a0fed8-214e-75c3-8cd3-75066bfd2e54')
    panel=InspectorPanel()
    panel.show_projection(InspectionProjection('fixture',None,None,'available',tuple(InspectionField(name,value) for name,value in zip(('Chat ID','Active head','History attempt'),values))))
    panel.resize(230,400)
    panel.show()
    ui.processEvents()
    labels=panel.findChildren(QLabel,'inspectorValue')[1:]
    assert tuple(label.text() for label in labels)==values
    for label,value in zip(labels,values):
        assert label.height()>=label.heightForWidth(label.width())
        assert label.heightForWidth(label.width())>label.fontMetrics().lineSpacing()+4
        assert label._view.horizontalScrollBar().maximum()==0
        label._view.selectAll()
        label._view.copy()
        assert ui.clipboard().text()==value
    panel.close()
    panel.deleteLater()
