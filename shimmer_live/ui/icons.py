"""Small icons drawn from inline SVG (no emoji: fonts differ between Windows and Linux)."""
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

ASSETS = Path(__file__).resolve().parent.parent / "assets"

_EYE = ('<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z" fill="none" stroke="{c}" stroke-width="2"/>'
        '<circle cx="12" cy="12" r="3.2" fill="{c}"/>')
_EYE_OFF = _EYE + '<line x1="4" y1="20" x2="20" y2="4" stroke="{c}" stroke-width="2.2" stroke-linecap="round"/>'
_REC_ON = '<circle cx="12" cy="12" r="7" fill="{c}"/>'
_REC_OFF = '<circle cx="12" cy="12" r="6.5" fill="none" stroke="{c}" stroke-width="2"/>'
SHAPES = {"eye": _EYE, "eye_off": _EYE_OFF, "rec_on": _REC_ON, "rec_off": _REC_OFF}


@lru_cache(maxsize=None)
def icon(name: str, color: str = "#1f1f1e", size: int = 18) -> QIcon:
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">{SHAPES[name].format(c=color)}</svg>'
    renderer = QSvgRenderer(QByteArray(svg.encode()))
    pm = QPixmap(QSize(size * 2, size * 2))
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    renderer.render(p)
    p.end()
    ic = QIcon()
    # Same picture when disabled: a locked "record" button must still show whether it records.
    ic.addPixmap(pm, QIcon.Normal)
    ic.addPixmap(pm, QIcon.Disabled)
    return ic


LOGO_SVG = ASSETS / "cptech.svg"


def _render_logo(px: int) -> QPixmap:
    pm = QPixmap(QSize(px, px))
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    QSvgRenderer(str(LOGO_SVG)).render(p)
    p.end()
    return pm


def logo_pixmap(height: int) -> QPixmap:
    """The logo from its vector source, rendered at the screen's real pixel density (sharp on HiDPI)."""
    screen = QGuiApplication.primaryScreen()
    dpr = screen.devicePixelRatio() if screen else 1.0
    pm = _render_logo(round(height * dpr))
    pm.setDevicePixelRatio(dpr)
    return pm


def app_icon() -> QIcon:
    ic = QIcon()
    for px in (16, 24, 32, 48, 64, 128, 256, 512):
        ic.addPixmap(_render_logo(px))
    return ic
