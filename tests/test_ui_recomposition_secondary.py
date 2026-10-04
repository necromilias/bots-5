"""Secondary dialog geometry and real Qt consent contract checks."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from types import SimpleNamespace
from pathlib import Path
import pytest
from PySide6.QtCore import QTimer, QPoint
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QFrame
from PySide6.QtTest import QTest
from bots5.desktop.dialog_primitives import DialogScrollArea, normalized_question
from bots5.desktop.phase9_dialogs import (TranscriptExportDialog, ArchiveExportDialog,
    ArchiveImportAdmissionDialog, ContinuationResolutionDialog, BackupCreationDialog,
    BackupVerificationDialog, RestoreHandoffDialog, RestoreHandoffResultDialog)

@pytest.mark.parametrize('factory',[TranscriptExportDialog,ArchiveExportDialog,
    ArchiveImportAdmissionDialog,BackupCreationDialog,BackupVerificationDialog,
    RestoreHandoffDialog,
    lambda:ContinuationResolutionDialog(readiness=SimpleNamespace(requirements=(),resolution='UNAVAILABLE',choice_revision=0)),
    lambda:RestoreHandoffResultDialog(data_root='/fixture',argv=(),status=1,refusal_text='Refused')])
def test_secondary_forms_keep_actions_outside_scroll_when_constrained(factory):
    qt=QApplication.instance() or QApplication([])
    dialog=factory();dialog.resize(520,380);dialog.show();QTest.qWait(10)
    try:
        scroll=dialog.findChild(DialogScrollArea)
        footer=dialog.findChild(QFrame,'botsDialogFooter')
        assert scroll is not None and footer is not None
        assert not scroll.isAncestorOf(footer)
        origin=footer.mapTo(dialog,QPoint(0,0))
        assert origin.y()>=0 and origin.y()+footer.height()<=dialog.height()
        dialog.show_error('Typed fixture refusal: '+('long detail '*200));QTest.qWait(10)
        assert dialog.height()<=380
        assert scroll.verticalScrollBar().maximum()>0
        assert footer.isVisible()
    finally:dialog.close()

@pytest.mark.parametrize('default,clicked',[ (QMessageBox.StandardButton.No,QMessageBox.StandardButton.Yes),
    (QMessageBox.StandardButton.No,QMessageBox.StandardButton.No),
    (QMessageBox.StandardButton.Yes,QMessageBox.StandardButton.No)])
def test_real_question_preserves_default_and_selected_standard_result(default,clicked):
    qt=QApplication.instance() or QApplication([])
    observed=[]
    def interact():
        box=next(w for w in qt.topLevelWidgets() if isinstance(w,QMessageBox) and w.isVisible())
        observed.append(box.standardButton(box.defaultButton()))
        box.button(clicked).click()
    QTimer.singleShot(10,interact)
    result=normalized_question(None,'Confirm fixture','Consent is explicit.',QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,default,destructive=True)
    assert observed==[default]
    assert result==clicked

@pytest.mark.parametrize('proceed',[False,True])
def test_restore_custom_buttons_preserve_actual_qt_result_contract(proceed):
    qt=QApplication.instance() or QApplication([])
    dialog=RestoreHandoffDialog();box=dialog._build_consequence_message_box()
    try:
        assert box.defaultButton().text()=='Cancel'
        role=QMessageBox.ButtonRole.AcceptRole if proceed else QMessageBox.ButtonRole.RejectRole
        button=next(b for b in box.buttons() if box.buttonRole(b)==role)
        QTimer.singleShot(10,button.click)
        result=box.exec()
        # Verify Qt's actual custom-button result, not a mocked QDialog result.
        assert result==box.result()
        assert box.clickedButton() is button
        assert box.text().startswith('Whole-installation restore')
    finally:box.close();dialog.close()

@pytest.mark.parametrize('action,expected',[('proceed',True),('cancel',False),('escape',False),('close',False)])
def test_restore_consequence_requires_actual_explicit_proceed(action,expected):
    qt=QApplication.instance() or QApplication([])
    dialog=RestoreHandoffDialog()
    def interact():
        box=next(w for w in qt.topLevelWidgets() if isinstance(w,QMessageBox) and w.isVisible())
        assert box.defaultButton().text()=='Cancel'
        if action=='escape':
            from PySide6.QtCore import Qt
            QTest.keyClick(box,Qt.Key.Key_Escape)
        elif action=='close':box.close()
        else:
            role=QMessageBox.ButtonRole.AcceptRole if action=='proceed' else QMessageBox.ButtonRole.RejectRole
            next(b for b in box.buttons() if box.buttonRole(b)==role).click()
    QTimer.singleShot(10,interact)
    try:assert dialog.confirm_consequence() is expected
    finally:dialog.close()


@pytest.mark.parametrize("segments",[24,500])
def test_long_destination_confirmation_keeps_real_actions_on_screen(segments):
    qt=QApplication.instance() or QApplication([])
    evidence=[]
    def inspect_and_refuse():
        box=next(w for w in qt.topLevelWidgets() if isinstance(w,QMessageBox) and w.isVisible())
        box.resize(520,380)
        button=box.button(QMessageBox.StandardButton.No)
        origin=button.mapTo(box,QPoint(0,0))
        scroll=box.findChild(DialogScrollArea)
        evidence.append((box.height(),qt.primaryScreen().availableGeometry().height(),origin.y(),button.height(),box.width(),0 if scroll is None else scroll.horizontalScrollBar().maximum()))
        button.click()
    QTimer.singleShot(10,inspect_and_refuse)
    result=normalized_question(None,'Overwrite existing backup?',('/fixture/'+('folder/'*segments)+'backup.botsbackup\nAlready exists. Overwrite it?'),QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No,destructive=True)
    assert result==QMessageBox.StandardButton.No
    height,screen_height,button_y,button_height,width,horizontal_overflow=evidence[0]
    assert height<=screen_height
    assert button_y>=0 and button_y+button_height<=height
    assert width>=min(480,qt.primaryScreen().availableGeometry().width()-48)
    assert horizontal_overflow==0


def test_backup_uncertainty_is_visible_before_inputs_in_constrained_dialog():
    qt = QApplication.instance() or QApplication([])
    dialog = BackupCreationDialog()
    try:
        dialog.resize(520, 380)
        dialog.show()
        dialog.begin_run()
        dialog.publication_uncertain('Typed fixture refusal; staging preserved.')
        QTest.qWait(20)
        scroll = dialog.findChild(DialogScrollArea)
        label = dialog._error_label
        assert dialog.outcome == 'uncertain'
        assert 'UNCERTAIN' in label.text()
        assert label.isVisible()
        assert 0 <= label.mapTo(scroll.viewport(), QPoint()).y() < scroll.viewport().height()
        assert label.mapTo(scroll.widget(), QPoint()).y() < dialog.destination_edit.mapTo(scroll.widget(), QPoint()).y()
        assert dialog.cancel_button.isVisible()
    finally:
        dialog.close()


def test_verification_keeps_accept_semantics_and_prioritizes_retained_receipt():
    qt = QApplication.instance() or QApplication([])
    dialog = BackupVerificationDialog()
    accepted = []
    dialog.accepted.connect(lambda: accepted.append(True))
    try:
        dialog.resize(520, 380)
        dialog.show()
        dialog.verification_succeeded(SimpleNamespace(receipt=None))
        assert accepted == [True]
        assert not dialog.isVisible()
        assert dialog.outcome == 'success'
        # Re-show only to inspect the retained presentation, not to invent a
        # persistent success workflow in the production dialog.
        dialog.show()
        QTest.qWait(20)
        scroll = dialog.findChild(DialogScrollArea)
        assert dialog.summary_label.text() == 'Backup package verified.'
        assert 0 <= dialog.summary_label.mapTo(scroll.viewport(), QPoint()).y() < scroll.viewport().height()
        assert dialog.summary_label.mapTo(scroll.widget(), QPoint()).y() < dialog.package_edit.mapTo(scroll.widget(), QPoint()).y()
    finally:
        dialog.close()


def test_long_verification_receipt_wraps_without_widening_constrained_body():
    qt = QApplication.instance() or QApplication([])
    dialog = BackupVerificationDialog()
    receipt = SimpleNamespace(backup_id='fixture', verified_at='2026-10-03',
        artifact_size=4096, artifact_sha256='a' * 64, outcome='matched',
        backup_logical_content_digest='b' * 64, source_db_migration_revision='0019',
        passed_checks=('identity', 'checksum'), failed_check_ids=(), reason_code=None)
    try:
        dialog.verification_succeeded(SimpleNamespace(receipt=receipt))
        dialog.resize(520, 380)
        dialog.show()
        QTest.qWait(30)
        scroll = dialog.findChild(DialogScrollArea)
        assert scroll.horizontalScrollBar().maximum() == 0
        assert 'a' * 64 in dialog.summary_label.text()
        assert 'b' * 64 in dialog.summary_label.text()
        assert dialog.summary_label._view.verticalScrollBar().maximum() == 0
        layout = dialog.summary_label.parentWidget().layout()
        index = layout.indexOf(dialog.summary_label)
        following = layout.itemAt(index + 1).widget()
        assert following is not None
        assert dialog.summary_label.geometry().bottom() < following.geometry().top()
    finally:
        dialog.close()
