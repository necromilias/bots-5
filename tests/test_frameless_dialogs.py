"""Shared dialog chrome preserves Qt results and compositor dispatch."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from types import SimpleNamespace
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, QTimer, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QFileDialog, QLineEdit, QMessageBox, QVBoxLayout
from bots5.desktop.actions import ActionRegistry
from bots5.desktop.dialog_primitives import DialogHeader, normalize_dialog, normalized_question
from bots5.desktop.model_selector import ModelSelectorPopup
from bots5.desktop.palette import CommandPaletteDialog
from bots5.desktop.phase9_dialogs import (TranscriptExportDialog, ArchiveExportDialog,
    ArchiveImportAdmissionDialog, ContinuationResolutionDialog, BackupCreationDialog,
    BackupVerificationDialog, RestoreHandoffDialog, RestoreHandoffResultDialog)
from bots5.desktop.theme import apply_draft1_theme
from bots5.desktop.widgets import SettingsDialog, TuneDialog, AddConnectionDialog, DeleteChatConfirmationDialog, MoveToFolderDialog
from bots5.domain.models import ChatDeletionInventory

_application = None
@pytest.fixture
def ui():
    global _application
    _application = QApplication.instance() or QApplication([])
    _application.setQuitOnLastWindowClosed(False)
    apply_draft1_theme(_application)
    previous = set(_application.topLevelWidgets())
    yield _application
    for widget in _application.topLevelWidgets():
        if widget in previous:
            continue
        widget.close()
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

FACTORIES = [SettingsDialog,TuneDialog,AddConnectionDialog,ModelSelectorPopup,
    lambda:CommandPaletteDialog(ActionRegistry()),
    lambda:MoveToFolderDialog('Fixture',(),None),
    lambda:DeleteChatConfirmationDialog(ChatDeletionInventory('fixture','Fixture',2,0,1)),
    TranscriptExportDialog,ArchiveExportDialog,ArchiveImportAdmissionDialog,
    BackupCreationDialog,BackupVerificationDialog,RestoreHandoffDialog,
    lambda:ContinuationResolutionDialog(readiness=SimpleNamespace(requirements=(),resolution='UNAVAILABLE',choice_revision=0)),
    lambda:RestoreHandoffResultDialog(data_root='/fixture',argv=(),status=1,refusal_text='Refused')]

@pytest.mark.parametrize('factory',FACTORIES)
def test_owned_dialog_has_one_shared_header_and_preserves_rejection(ui,factory):
    dialog=factory()
    modal=dialog.windowModality()
    dialog.resize(560,420)
    dialog.show();ui.processEvents()
    header=dialog.findChild(DialogHeader)
    assert len(dialog.findChildren(DialogHeader))==1
    assert dialog.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert dialog.windowModality()==modal
    assert header.title_label.text()==dialog.windowTitle()
    dialog.setWindowTitle('Updated title')
    assert header.title_label.text()=='Updated title'
    corner=header.close_button.mapTo(dialog,header.close_button.rect().bottomRight())
    assert 0<=corner.x()<dialog.width() and 0<=corner.y()<dialog.height()
    accepted=[];rejected=[]
    dialog.accepted.connect(lambda:accepted.append(True))
    dialog.rejected.connect(lambda:rejected.append(True))
    QTest.mouseClick(header.close_button,Qt.MouseButton.LeftButton)
    assert not accepted and rejected==[True]
    assert QDialog.result(dialog)==QDialog.DialogCode.Rejected

@pytest.mark.parametrize('modal',[False,True])
@pytest.mark.parametrize('action',['escape','close','return'])
def test_initial_focus_modal_and_default_action_are_preserved(ui,modal,action):
    dialog=QDialog();dialog.setWindowTitle('Fixture');dialog.setModal(modal)
    layout=QVBoxLayout(dialog);field=QLineEdit(dialog);layout.addWidget(field)
    buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel,dialog)
    buttons.accepted.connect(dialog.accept);buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    buttons.button(QDialogButtonBox.StandardButton.Ok).setDefault(True)
    normalize_dialog(dialog)
    field.setFocus()
    def inspect():
        assert dialog.isModal()==modal
        assert dialog.focusWidget() is field
        assert buttons.button(QDialogButtonBox.StandardButton.Ok).isDefault()
        if action=='close':dialog._bots_dialog_header.close_button.click()
        else:QTest.keyClick(field,Qt.Key.Key_Return if action=='return' else Qt.Key.Key_Escape)
    # show() preserves nonmodal semantics; exec() would intentionally make it modal.
    dialog.show();ui.processEvents();inspect()
    assert dialog.result()==(QDialog.DialogCode.Accepted if action=='return' else QDialog.DialogCode.Rejected)

@pytest.mark.parametrize('accepted',[False,True])
def test_header_uses_native_move_without_synthetic_geometry_or_close_dispatch(ui,monkeypatch,accepted):
    dialog=SettingsDialog();dialog.show();ui.processEvents()
    calls=[]
    monkeypatch.setattr(dialog,'windowHandle',lambda:SimpleNamespace(startSystemMove=lambda:calls.append('move') or accepted))
    before=dialog.geometry()
    QTest.mouseClick(dialog._bots_dialog_header.title_label,Qt.MouseButton.LeftButton)
    assert calls==['move']
    assert dialog.geometry()==before
    QTest.mouseClick(dialog._bots_dialog_header.close_button,Qt.MouseButton.LeftButton)
    assert calls==['move']

@pytest.mark.parametrize('point,edges',[
    ((0,0),Qt.Edge.LeftEdge|Qt.Edge.TopEdge),((599,0),Qt.Edge.RightEdge|Qt.Edge.TopEdge),
    ((0,399),Qt.Edge.LeftEdge|Qt.Edge.BottomEdge),((599,399),Qt.Edge.RightEdge|Qt.Edge.BottomEdge),
    ((0,200),Qt.Edge.LeftEdge),((599,200),Qt.Edge.RightEdge),((300,0),Qt.Edge.TopEdge),((300,399),Qt.Edge.BottomEdge)])
def test_dialog_edge_dispatch_and_fixed_axis_constraints(ui,monkeypatch,point,edges):
    dialog=TuneDialog();dialog.resize(600,400)
    calls=[]
    monkeypatch.setattr(dialog,'windowHandle',lambda:SimpleNamespace(startSystemResize=lambda edge:calls.append(edge) or True))
    local=QPoint(*point)
    event=QMouseEvent(QEvent.Type.MouseButtonPress,QPointF(local),QPointF(dialog.mapToGlobal(local)),Qt.MouseButton.LeftButton,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier)
    before=dialog.geometry()
    assert dialog._native_edges.eventFilter(dialog,event)
    assert calls==[edges] and dialog.geometry()==before
    dialog.setFixedWidth(600)
    assert not dialog._native_edges.edges_at(local)&(Qt.Edge.LeftEdge|Qt.Edge.RightEdge)
    dialog.setFixedHeight(400)
    assert not dialog._native_edges.edges_at(local)


def test_nonresizable_chrome_and_platform_chooser_are_excluded_from_resize(ui):
    dialog=QDialog();DialogHeader(dialog,resizable=False)
    assert not hasattr(dialog,'_native_edges')
    chooser=QFileDialog(dialog)
    assert not chooser.windowFlags()&Qt.WindowType.FramelessWindowHint
    assert chooser.property('botsOwnedDialog') is None
    assert not hasattr(chooser,'_native_edges')


def test_static_confirmation_chrome_closes_to_no_and_leaves_other_boxes_alone(ui):
    other=QMessageBox();other.setWindowTitle('OS owned example')
    observed=[]
    def inspect():
        other.ensurePolished()
        assert not other.property('botsOwnedDialog')
        box=next(w for w in ui.topLevelWidgets() if isinstance(w,QMessageBox) and w.isVisible())
        assert box.windowFlags()&Qt.WindowType.FramelessWindowHint
        assert box.defaultButton() is box.button(QMessageBox.StandardButton.No)
        observed.append(True)
        box._bots_dialog_header.close_button.click()
    QTimer.singleShot(20,inspect)
    result=normalized_question(None,'Owned consent','Proceed?',QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)
    assert observed==[True] and result==QMessageBox.StandardButton.No


def test_restore_close_does_not_authorize_handoff(ui):
    dialog=RestoreHandoffDialog()
    def close():
        box=next(w for w in ui.topLevelWidgets() if isinstance(w,QMessageBox) and w.isVisible())
        box._bots_dialog_header.close_button.click()
    QTimer.singleShot(20,close)
    assert dialog.confirm_consequence() is False
