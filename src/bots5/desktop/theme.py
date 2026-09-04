from __future__ import annotations

from PySide6.QtWidgets import QApplication


DRAFT1_STYLE_SHEET = """
QWidget {
    color: #d9e2ea;
    font-size: 13px;
}
QMainWindow, QWidget#draft1Root {
    background: #11161b;
}
QFrame#topBar, QFrame#composerFrame, QWidget#leftRail, QDockWidget > QWidget {
    background: #171d24;
}
QFrame#topBar {
    border-bottom: 1px solid #29343f;
}
QLabel#brandLabel {
    color: #f2f7fb;
    font-size: 15px;
    font-weight: 700;
}
QLabel#modelPill {
    background: #202b36;
    border: 1px solid #3b9ddd;
    border-radius: 11px;
    color: #8ed2ff;
    padding: 4px 10px;
}
QLabel#chatTitle {
    color: #aab8c4;
    font-size: 12px;
    font-weight: 600;
    padding: 3px 4px;
}
QToolButton, QPushButton {
    background: transparent;
    border: 1px solid transparent;
    border-radius: 7px;
    color: #c5d0d9;
    padding: 5px 8px;
}
QToolButton:hover, QPushButton:hover {
    background: #233342;
    border-color: #31536a;
}
QToolButton:checked, QPushButton:pressed {
    background: #18354a;
    border-color: #3b9ddd;
    color: #a8ddff;
}
QToolButton:disabled, QPushButton:disabled {
    color: #66727d;
    border-color: transparent;
}
QToolButton#railIcon {
    min-width: 28px;
    max-width: 28px;
    min-height: 28px;
    max-height: 28px;
    padding: 0;
    background: #202a34;
    border-color: #2a3946;
}
QToolButton#railIcon:hover, QToolButton#railIcon:checked {
    background: #173a52;
    border-color: #3b9ddd;
    color: #a8ddff;
}
QToolButton#disabledAffordance {
    background: #1b2229;
    border-color: #27313a;
}
QListWidget#chatList {
    background: transparent;
    border: 0;
    outline: 0;
    padding: 2px;
}
QListWidget#chatList::item {
    border-radius: 6px;
    padding: 7px 8px;
}
QListWidget#chatList::item:selected {
    background: #1a3b52;
    color: #d8f0ff;
}
QScrollArea#transcriptView {
    background: #11161b;
    border: 0;
}
QWidget#transcriptContent {
    background: #11161b;
}
QFrame#messageBubble {
    background: #1b232b;
    border: 1px solid #2b3945;
    border-radius: 10px;
}
QFrame#messageBubble[role="user"] {
    background: #193247;
    border-color: #2d6f99;
}
QLabel#messageBody {
    color: #dce6ed;
}
QLabel#messageState {
    color: #7f93a2;
    font-size: 10px;
}
QLabel#messageActivity, QLabel#generationIndicator {
    color: #8ed2ff;
    font-size: 11px;
    font-weight: 700;
}
QLabel#messageActivity {
    padding-top: 1px;
}
QFrame#messageBubble[generationActive="true"] {
    background: #1c3b52;
    border-color: #3b9ddd;
}
QLabel#assistantAvatar, QLabel#userAvatar {
    border-radius: 8px;
    font-size: 12px;
    font-weight: 700;
    qproperty-alignment: AlignCenter;
}
QLabel#assistantAvatar {
    background: #1e4f6d;
    color: #a9defd;
}
QLabel#userAvatar {
    background: #344553;
    color: #e3edf4;
}
QFrame#composerFrame {
    border-top: 1px solid #29343f;
}
QPlainTextEdit#composer {
    background: #1b232b;
    border: 1px solid #33424e;
    border-radius: 9px;
    color: #e5edf2;
    padding: 7px;
    selection-background-color: #245d80;
}
QPlainTextEdit#composer:focus {
    border-color: #3b9ddd;
}
QPushButton#sendButton {
    background: #17618e;
    border-color: #3b9ddd;
    color: #eff9ff;
    font-weight: 700;
    padding: 8px 14px;
}
QPushButton#sendButton:hover {
    background: #1c78ad;
}
QPushButton#stopButton {
    background: #432e35;
    border-color: #8d5563;
    color: #f2c5ce;
    padding: 8px 14px;
}
QLabel#editingLabel {
    color: #8ed2ff;
    font-size: 11px;
    padding-left: 3px;
}
QDockWidget {
    color: #d9e2ea;
}
QDockWidget::title {
    background: #1b232b;
    border-bottom: 1px solid #29343f;
    padding: 7px;
}
QLabel#inspectorValue {
    color: #becbd4;
}
QLabel#emptyTranscript {
    color: #6f7e8a;
    padding: 36px;
}
"""


def apply_draft1_theme(application: QApplication) -> None:
    application.setStyleSheet(DRAFT1_STYLE_SHEET)
