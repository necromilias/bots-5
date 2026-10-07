"""Regenerate launcher PNGs and embedded Qt resources from the authored SVGs.

Run with QT_QPA_PLATFORM=offscreen and the project's PySide6 interpreter.
"""
from pathlib import Path
import subprocess
import sys

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication
import PySide6

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "src/bots5/desktop/assets/application"
IDENTITY = "io.github.necromilias.bots5"
SIZES = (16, 24, 32, 48, 64, 128, 256)


def main():
    app = QApplication([])
    for size in SIZES:
        variant = "bots5-small.svg" if size <= 32 else "bots5.svg"
        pixmap = QIcon(str(ASSETS / variant)).pixmap(size, size)
        if pixmap.isNull():
            raise RuntimeError(f"Cannot render {variant} at {size}px")
        dest = ROOT / f"share/icons/hicolor/{size}x{size}/apps/{IDENTITY}.png"
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not pixmap.save(str(dest), "PNG"):
            raise RuntimeError(f"Cannot save {dest}")
    for source, name in (("bots5.svg", IDENTITY), ("bots5-symbolic.svg", IDENTITY + "-symbolic")):
        dest = ROOT / f"share/icons/hicolor/scalable/apps/{name}.svg"
        dest.write_bytes((ASSETS / source).read_bytes())
    rcc = Path(PySide6.__file__).parent / "Qt/libexec/rcc"
    subprocess.run([str(rcc), "-g", "python", str(ASSETS / "application.qrc"), "-o",
                    str(ROOT / "src/bots5/desktop/application_icon_resources.py")], check=True)


if __name__ == "__main__":
    main()
