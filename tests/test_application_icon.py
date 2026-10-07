"""Application badge resource, transparency and launcher identity contract."""
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QResource
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication
from bots5.desktop.application_icon import (
    APPLICATION_NAME, DESKTOP_FILE_NAME, ICON_SIZES, application_icon,
)


def test_embedded_application_badge_renders_every_taskbar_size():
    app = QApplication.instance() or QApplication([])
    for variant in ("bots5.svg", "bots5-small.svg", "bots5-symbolic.svg"):
        assert QResource(f":/bots/application/{variant}").isValid()
    icon = application_icon()
    for size in ICON_SIZES:
        pixmap = icon.pixmap(size, size)
        assert not pixmap.isNull()
        assert (pixmap.width(), pixmap.height()) == (size, size)
        image = pixmap.toImage()
        assert image.hasAlphaChannel()
        assert image.pixelColor(0, 0).alpha() == 0
        assert image.pixelColor(size // 2, size // 2).alpha() == 255


def test_linux_launcher_identity_and_exports_match_qt():
    root = Path(__file__).resolve().parents[1]
    desktop = (root / f"share/applications/{DESKTOP_FILE_NAME}.desktop").read_text()
    assert f"Icon={DESKTOP_FILE_NAME}\n" in desktop
    assert f"StartupWMClass={APPLICATION_NAME}\n" in desktop
    assert "Exec=bots5-desktop\n" in desktop
    for size in ICON_SIZES:
        image = QImage(str(root / f"share/icons/hicolor/{size}x{size}/apps/{DESKTOP_FILE_NAME}.png"))
        assert (image.width(), image.height()) == (size, size)
        assert image.hasAlphaChannel() and image.pixelColor(0, 0).alpha() == 0
