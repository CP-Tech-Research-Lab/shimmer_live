"""Render the app logo (shimmer_live/assets/cptech.svg) to packaging/shimmer_live.ico for the Windows .exe."""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication, QImage, QPainter  # noqa: E402
from PySide6.QtSvg import QSvgRenderer  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
app = QGuiApplication(sys.argv)
img = QImage(256, 256, QImage.Format_ARGB32)
img.fill(Qt.transparent)
p = QPainter(img)
QSvgRenderer(str(ROOT / "shimmer_live" / "assets" / "cptech.svg")).render(p)
p.end()
out = ROOT / "packaging" / "shimmer_live.ico"
if not img.save(str(out), "ICO"):
    sys.exit(f"could not write {out}")
print("wrote", out)
