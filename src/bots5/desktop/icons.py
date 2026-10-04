"""Bundled vector icons for existing desktop actions."""
from PySide6.QtCore import QSize
from PySide6.QtGui import QIcon
from . import ui_icons  # register resources


def action_icon(name: str) -> QIcon:
    icon = QIcon(f":/bots/icons/{name}.svg")
    for state in (QIcon.State.Off, QIcon.State.On):
        icon.addFile(f":/bots/icons/{name}-disabled.svg", QSize(), QIcon.Mode.Disabled, state)
    return icon


def icon_action(button, name: str, accessible_name: str) -> None:
    button.setText("")
    button.setIcon(action_icon(name))
    button.setIconSize(QSize(18, 18))
    button.setProperty("iconOnly", True)
    button.setAccessibleName(accessible_name)
    button.setFixedSize(32, 30)
