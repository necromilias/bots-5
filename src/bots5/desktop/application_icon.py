"""Application identity and bundled, size-specific Qt icon artwork."""
from PySide6.QtGui import QIcon
from . import application_icon_resources  # register the compiled Qt resources

DESKTOP_FILE_NAME = "io.github.necromilias.bots5"
APPLICATION_NAME = "bots5-desktop"
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)


def application_icon() -> QIcon:
    """Use the simplified badge at taskbar sizes and metal artwork above 32px."""
    icon = QIcon()
    for size in ICON_SIZES:
        variant = "bots5-small.svg" if size <= 32 else "bots5.svg"
        source = QIcon(f":/bots/application/{variant}")
        icon.addPixmap(source.pixmap(size, size))
    return icon
