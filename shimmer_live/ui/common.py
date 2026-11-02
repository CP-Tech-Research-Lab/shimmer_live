"""Shared UI pieces: thread bridge, colours, stylesheet, alert banners."""
import logging
import threading

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

log = logging.getLogger("shimmerlive.ui")

# Validated categorical palette (fixed order; colour follows the device).
DEVICE_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]

INK = "#1f1f1e"
MUTED = "#6b6b68"
SURFACE = "#fcfcfb"
CRITICAL = "#c62828"
CRITICAL_BG = "#fdecea"
WARNING = "#8a5a00"
WARNING_BG = "#fff4d6"
INFO = "#1f4e8c"
INFO_BG = "#e8f0fb"
GOOD = "#1b7a3a"

STYLESHEET = f"""
QWidget {{ font-size: 10pt; color: {INK}; }}
QMainWindow, QStackedWidget > QWidget {{ background: {SURFACE}; }}
QGroupBox {{ font-weight: 600; border: 1px solid #dcdcd8; border-radius: 6px; margin-top: 14px; padding: 8px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; }}
QPushButton {{ padding: 5px 12px; border-radius: 5px; border: 1px solid #c9c9c4; background: #ffffff; }}
QPushButton:hover {{ background: #f1f1ee; }}
QPushButton:disabled {{ color: #a5a5a0; background: #f6f6f4; }}
QPushButton[role="primary"] {{ background: #2a78d6; color: white; border: 1px solid #1f63b5; font-weight: 600; }}
QPushButton[role="primary"]:hover {{ background: #1f6bc4; }}
QPushButton[role="primary"]:disabled {{ background: #a9c6ea; border-color: #a9c6ea; }}
QPushButton[role="record"] {{ background: #c62828; color: white; border: 1px solid #a31f1f; font-weight: 600; }}
QPushButton[role="record"]:disabled {{ background: #e8b0b0; border-color: #e8b0b0; }}
QTableWidget {{ background: white; gridline-color: #ececea; border: 1px solid #dcdcd8; }}
QHeaderView::section {{ background: #f4f4f2; border: none; border-bottom: 1px solid #dcdcd8; padding: 4px; font-weight: 600; }}
QTextBrowser, QPlainTextEdit {{ background: white; border: 1px solid #dcdcd8; }}
"""


class _Bridge(QObject):
    sig = Signal(object)

    def __init__(self):
        super().__init__()
        self.sig.connect(self._run, Qt.QueuedConnection)

    @Slot(object)
    def _run(self, fn):
        try:
            fn()
        except Exception:
            log.exception("UI callback failed")


_bridge = None


def init_bridge():
    global _bridge
    _bridge = _Bridge()


def ui_call(fn, *args, **kwargs):
    """Run fn on the GUI thread (safe from any thread)."""
    _bridge.sig.emit(lambda: fn(*args, **kwargs))


def run_bg(fn, on_done=None, on_fail=None, name="worker"):
    """Run fn on a background thread; deliver result/exception on the GUI thread."""
    def work():
        try:
            r = fn()
        except Exception as e:      # noqa: BLE001
            log.info("%s failed: %s", name, e)
            if on_fail:
                ui_call(on_fail, e)
        else:
            if on_done:
                ui_call(on_done, r)
    threading.Thread(target=work, daemon=True, name=name).start()


def set_role(button: QPushButton, role: str):
    button.setProperty("role", role)
    button.style().unpolish(button)
    button.style().polish(button)


def set_if_changed(label, text):
    """setText only when the text differs: every setText can trigger a relayout."""
    current = label.text() if callable(label.text) else label.text     # QLabel vs pg.LabelItem
    if current != text:
        label.setText(text)


def fmt_duration(s: float) -> str:
    s = max(0, int(s))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


SEVERITY = {"critical": (CRITICAL, CRITICAL_BG, "⚠"), "warning": (WARNING, WARNING_BG, "▲"),
            "info": (INFO, INFO_BG, "ℹ")}


class Banner(QFrame):
    def __init__(self, severity, text, action=None):
        super().__init__()
        self.lab = QLabel()
        self.lab.setWordWrap(True)
        self.lab.setTextFormat(Qt.RichText)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.addWidget(self.lab, 1)
        self.btn = None
        self._sev = self._text = None
        if action:
            self.btn = QPushButton(action[0])
            self.btn.clicked.connect(action[1])
            lay.addWidget(self.btn)
        self.update_content(severity, text)

    def update_content(self, severity, text):
        fg, bg, icon = SEVERITY[severity]
        if severity != self._sev:          # restyling is expensive; only on severity change
            self._sev = severity
            self.setStyleSheet(f"Banner {{ background: {bg}; border: 1px solid {fg}; border-left: 6px solid {fg};"
                               f" border-radius: 4px; }} QLabel {{ color: {fg}; font-weight: 600; }}")
        if text != self._text:
            self._text = text
            self.lab.setText(f"{icon}&nbsp; {text}")


class AlertArea(QWidget):
    """Keeps a set of keyed banners in sync with the current alert dict."""

    ORDER = {"critical": 0, "warning": 1, "info": 2}

    def __init__(self):
        super().__init__()
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(4)
        self.banners = {}
        self._order = []

    def sync(self, alerts: dict):
        """alerts: key -> (severity, html_text, action or None)"""
        for key in list(self.banners):
            if key not in alerts:
                self.banners.pop(key).deleteLater()
        ordered = sorted(alerts.items(), key=lambda kv: (self.ORDER[kv[1][0]], kv[0]))
        for key, (sev, text, action) in ordered:
            b = self.banners.get(key)
            if b is None:
                b = Banner(sev, text, action)
                self.banners[key] = b
            else:
                b.update_content(sev, text)
        order = [k for k, _ in ordered]
        if order != self._order:           # re-insert widgets only when the set/order changed
            self._order = order
            for k in order:
                self.lay.removeWidget(self.banners[k])
                self.lay.addWidget(self.banners[k])
        if self.isHidden() == bool(self.banners):
            self.setVisible(bool(self.banners))
