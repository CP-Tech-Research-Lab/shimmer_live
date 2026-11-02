"""Brand header shown at the top of every page: logo, lab name, app and page name."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from .common import INK, MUTED
from .icons import logo_pixmap

LAB_NAME = ("CPTech", "Lab")         # (accent part, rest): change here if the lab's name changes
APP_NAME = "Shimmer Live"
BRAND_BLUE = "#1e7be6"               # the logo's blue


def lab_name() -> str:
    return " ".join(LAB_NAME)


class BrandHeader(QWidget):
    """[logo]  CPTech Lab
               SHIMMER LIVE · <PAGE>"""

    def __init__(self, page: str, compact: bool = False):
        super().__init__()
        logo_px, name_pt, sub_pt = (46, 16, 8) if compact else (84, 26, 10)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10 if compact else 18)
        logo = QLabel()
        logo.setPixmap(logo_pixmap(logo_px))
        lay.addWidget(logo, 0, Qt.AlignVCenter)
        text = QVBoxLayout()
        text.setSpacing(0 if compact else 2)
        accent, rest = LAB_NAME
        name = QLabel(f"<span style='color:{BRAND_BLUE}'>{accent}</span> <span style='color:{INK}'>{rest}</span>")
        name.setStyleSheet(f"font-size: {name_pt}pt; font-weight: 800;")
        self.sub = QLabel()
        spacing = 1 if compact else 2
        self.sub.setStyleSheet(f"font-size: {sub_pt}pt; font-weight: 700; color: {MUTED}; letter-spacing: {spacing}px;")
        text.addStretch(1)
        text.addWidget(name)
        text.addWidget(self.sub)
        text.addStretch(1)
        lay.addLayout(text)
        self.set_page(page)

    def set_page(self, page: str):
        self.sub.setText(f"{APP_NAME.upper()} · {page.upper()}")
