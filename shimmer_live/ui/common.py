"""Shared UI pieces: thread bridge, colours, stylesheet, alert banners."""
import logging
import threading

from PySide6.QtCore import QObject, QPoint, QRect, QSize, Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QLayout, QPushButton, QVBoxLayout, QWidget

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

# ---- type scale (pt) and sizes: one place, so importance reads the same everywhere ----
FONT_XS = 9.5      # plot axes, fine print
FONT_S = 10        # helper text, secondary details
FONT_BASE = 11     # body text, buttons, tables
FONT_M = 12.5      # section headings, device names, important values
FONT_L = 15        # status headlines
FONT_XL = 20       # page titles
FONT_XXL = 26      # the lost-connection panel while recording
ROW_H = 34         # table rows
ICON_BTN = 34      # square icon buttons (eye, record, gear)

STYLESHEET = f"""
QWidget {{ font-size: {FONT_BASE}pt; color: {INK}; }}
QMainWindow, QStackedWidget > QWidget, QDialog {{ background: {SURFACE}; }}
QLabel[role="title"] {{ font-size: {FONT_XL}pt; font-weight: 700; }}
QLabel[role="section"] {{ font-size: {FONT_M}pt; font-weight: 700; }}
QLabel[role="muted"] {{ font-size: {FONT_S}pt; color: {MUTED}; }}
QGroupBox {{ font-size: {FONT_M}pt; font-weight: 700; border: 1px solid #dcdcd8; border-radius: 8px;
             margin-top: 18px; padding: 12px 10px 10px 10px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 6px; }}
QGroupBox > QWidget, QGroupBox QLabel, QGroupBox QPushButton, QGroupBox QTableWidget, QGroupBox QCheckBox,
QGroupBox QComboBox, QGroupBox QLineEdit {{ font-size: {FONT_BASE}pt; font-weight: 400; }}

QPushButton {{ padding: 6px 14px; min-height: 22px; border-radius: 6px; border: 1px solid #c4c4bf;
               background: #ffffff; }}
QPushButton:hover {{ background: #f1f1ee; border-color: #9e9e99; }}
QPushButton:pressed {{ background: #e6e6e2; }}
QPushButton:disabled {{ color: #a5a5a0; background: #f6f6f4; border-color: #dcdcd8; }}
QPushButton[role="primary"] {{ font-size: {FONT_M}pt; font-weight: 700; padding: 9px 22px; min-height: 24px;
                               background: #2a78d6; color: white; border: 1px solid #1f63b5; }}
QPushButton[role="primary"]:hover {{ background: #1f6bc4; }}
QPushButton[role="primary"]:disabled {{ background: #a9c6ea; border-color: #a9c6ea; color: #eef4fb; }}
QPushButton[role="record"] {{ font-size: {FONT_M + 0.5}pt; font-weight: 700; padding: 10px 18px; min-height: 26px;
                              background: #c62828; color: white; border: 1px solid #a31f1f; }}
QPushButton[role="record"]:hover {{ background: #b02323; }}
QPushButton[role="record"]:disabled {{ background: #ecc0c0; border-color: #ecc0c0; color: #fbeeee; }}
QPushButton[role="stop"] {{ font-size: {FONT_M + 0.5}pt; font-weight: 700; padding: 10px 18px; min-height: 26px;
                            background: #ffffff; color: #c62828; border: 2px solid #c62828; }}
QPushButton[role="stop"]:hover {{ background: #fdecea; }}
QPushButton[role="stop"]:disabled {{ color: #c9c9c4; border-color: #dcdcd8; background: #f6f6f4; }}
QPushButton[role="keypoint"] {{ font-size: {FONT_BASE + 0.5}pt; font-weight: 600; padding: 8px 14px; min-height: 24px;
                                text-align: left; }}
QPushButton[role="small"] {{ font-size: {FONT_S}pt; padding: 4px 10px; min-height: 18px; }}
QToolButton {{ border-radius: 6px; }}
QToolButton:hover {{ background: #ececea; }}

QComboBox, QLineEdit {{ padding: 5px 8px; min-height: 22px; border: 1px solid #c4c4bf; border-radius: 5px;
                        background: white; }}
QComboBox:focus, QLineEdit:focus, QPlainTextEdit:focus {{ border: 1px solid #2a78d6; }}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 18px; height: 18px; }}
QTabBar::tab {{ padding: 8px 18px; font-size: {FONT_BASE}pt; }}
QTabBar::tab:selected {{ font-weight: 700; }}
QTableWidget {{ background: white; gridline-color: #ececea; border: 1px solid #dcdcd8; border-radius: 4px; }}
QHeaderView::section {{ background: #f4f4f2; border: none; border-bottom: 1px solid #dcdcd8; padding: 6px 8px;
                        font-weight: 700; font-size: {FONT_S + 0.5}pt; }}
QTextBrowser, QPlainTextEdit {{ background: white; border: 1px solid #dcdcd8; border-radius: 4px; padding: 4px; }}
QProgressBar {{ min-height: 14px; max-height: 14px; border-radius: 4px; }}
QToolTip {{ font-size: {FONT_S}pt; padding: 4px; }}
"""


def divider() -> QFrame:
    """Thin horizontal rule under page headers."""
    line = QFrame()
    line.setFixedHeight(1)
    line.setStyleSheet("background: #dcdcd8; border: none;")
    return line


def style_table(table, row_height=ROW_H):
    """Comfortable row height (not settable from a stylesheet)."""
    table.verticalHeader().setDefaultSectionSize(row_height)
    table.verticalHeader().setMinimumSectionSize(row_height)
    table.horizontalHeader().setMinimumHeight(row_height)


def fit_to_screen(widget, width, height, margin=0.92):
    """Resize to (width, height), but never larger than the screen the widget is on."""
    screen = widget.screen() if hasattr(widget, "screen") and widget.screen() else None
    if screen is not None:
        geo = screen.availableGeometry()
        width = min(width, int(geo.width() * margin))
        height = min(height, int(geo.height() * margin))
    widget.resize(width, height)


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


def collect_garbage_on_gui_thread(parent):
    """Python's cyclic garbage collector runs on whichever thread happens to allocate when it triggers. On a
    worker thread (Bluetooth, plugins, TCP keypoints, ...) it destroys unreachable Qt widgets there, which
    later crashes Qt (seen: segfaults on a click and at exit). So automatic collection is switched off and
    run here instead, on the GUI thread: young objects every second, everything every minute."""
    import gc
    gc.disable()
    state = {"n": 0}

    def tick():
        state["n"] += 1
        gc.collect(2 if state["n"] % 60 == 0 else 1 if state["n"] % 10 == 0 else 0)

    t = QTimer(parent)
    t.timeout.connect(tick)
    t.start(1000)
    return t


def ui_call(fn, *args, **kwargs):
    """Run fn on the GUI thread (safe from any thread). Dropped if the app has already closed."""
    try:
        _bridge.sig.emit(lambda: fn(*args, **kwargs))
    except RuntimeError:                 # "Signal source has been deleted": a worker finishing after exit
        pass


def _deliver(payload):
    """GUI thread: run the callback, then drop every reference the worker had (see run_bg)."""
    callback, arg = payload[1], payload[2]
    try:
        if callback is not None:
            callback(arg)
    finally:
        payload.clear()


def run_bg(fn, on_done=None, on_fail=None, name="worker"):
    """Run fn on a background thread; deliver result/exception on the GUI thread.

    The worker never drops the last reference to fn or the callbacks: they usually capture widgets, and a
    Qt object whose Python wrapper dies on a worker thread is destroyed later from the GUI thread at an
    arbitrary moment, which can crash the app (seen: a crash on a button click after a background test).
    Everything is handed back to the GUI thread and released there."""
    box = [fn, on_done, on_fail]

    def work():
        f, done, fail = box
        box.clear()
        try:
            payload = [(f, done, fail), done, f()]
        except Exception as e:      # noqa: BLE001
            log.info("%s failed: %s", name, e)
            payload = [(f, done, fail), fail, e]
        del f, done, fail
        ui_call(_deliver, payload)

    threading.Thread(target=work, daemon=True, name=name).start()


def set_role(widget, role: str):
    """Apply a stylesheet role (title, section, muted, primary, record, stop, keypoint, small)."""
    widget.setProperty("role", role)
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    return widget


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
    def __init__(self, severity, text, action=None, light=False, on_hide=None):
        """light: a thin one-line note (live-view warnings); on_hide: show a ✕ that calls it."""
        super().__init__()
        self.light = light
        self.lab = QLabel()
        self.lab.setWordWrap(not light)
        self.lab.setTextFormat(Qt.RichText)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(*((10, 2, 4, 2) if light else (12, 8, 12, 8)))
        lay.setSpacing(6)
        lay.addWidget(self.lab, 1)
        self.btn = None
        self._sev = self._text = None
        if action:
            self.btn = QPushButton(action[0])
            self.btn.setToolTip(action[2] if len(action) > 2 else action[0])
            self.btn.clicked.connect(action[1])
            if light:
                set_role(self.btn, "small")
            lay.addWidget(self.btn)
        if on_hide:
            hide = QPushButton("✕  Hide")
            set_role(hide, "small")
            hide.setToolTip("Don't show this message again until the app is closed")
            hide.clicked.connect(lambda _checked=False: on_hide())   # clicked passes a bool: don't forward it
            self.btn_hide = hide
            lay.addWidget(hide)
        self.update_content(severity, text)

    def update_content(self, severity, text):
        fg, bg, icon = SEVERITY[severity]
        if severity != self._sev:          # restyling is expensive; only on severity change
            self._sev = severity
            if self.light:
                self.setStyleSheet(f"Banner {{ background: {bg}; border: none; border-left: 3px solid {fg}; "
                                   f"border-radius: 3px; }} QLabel {{ color: {fg}; font-weight: 400; "
                                   f"font-size: {FONT_S}pt; }}")
            else:
                size = FONT_BASE + 0.5 if severity == "critical" else FONT_BASE
                self.setStyleSheet(f"Banner {{ background: {bg}; border: 1px solid {fg}; border-left: 8px solid {fg};"
                                   f" border-radius: 6px; }} QLabel {{ color: {fg}; font-weight: 700; "
                                   f"font-size: {size}pt; }}")
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
        self.lay.setSpacing(6)
        self.lay.setSpacing(3)
        self.banners = {}
        self._order = []
        self.hidden = set()                # keys the user hid: not shown again until the app closes

    def _hide(self, key):
        self.hidden.add(key)
        b = self.banners.pop(key, None)
        if b is not None:
            b.deleteLater()
        self._order = [k for k in self._order if k != key]
        self.setVisible(bool(self.banners))

    def sync(self, alerts: dict):
        """alerts: key -> (severity, html_text, action or None). Critical alerts stay big and cannot be hidden;
        warnings and notes are thin lines with a ✕ Hide."""
        alerts = {k: v for k, v in alerts.items() if k not in self.hidden}
        for key in list(self.banners):
            if key not in alerts:
                self.banners.pop(key).deleteLater()
        ordered = sorted(alerts.items(), key=lambda kv: (self.ORDER[kv[1][0]], kv[0]))
        for key, (sev, text, action) in ordered:
            b = self.banners.get(key)
            if b is None:
                light = sev != "critical"
                b = Banner(sev, text, action, light=light, on_hide=(lambda k=key: self._hide(k)) if light else None)
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


class FlowLayout(QLayout):
    """Lays widgets out left to right and wraps to a new row when the width runs out."""

    def __init__(self, parent=None, spacing=8):
        super().__init__(parent)
        self._items = []
        self._spacing = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientations(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._arrange(QRect(0, 0, width, 0), dry_run=True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._arrange(rect, dry_run=False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for it in self._items:
            size = size.expandedTo(it.minimumSize())
        return size

    def _arrange(self, rect, dry_run):
        x, y, row_h = rect.x(), rect.y(), 0
        for it in self._items:
            hint = it.sizeHint()
            if x + hint.width() > rect.right() + 1 and row_h > 0:
                x, y, row_h = rect.x(), y + row_h + self._spacing, 0
            if not dry_run:
                it.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._spacing
            row_h = max(row_h, hint.height())
        return y + row_h - rect.y()


class Card(QFrame):
    """A white, rounded panel with a title, an optional one-line explanation and its own content.

    Use .body (a QVBoxLayout) for the content and .actions (a QHBoxLayout in the title row,
    right-aligned) for header buttons."""

    def __init__(self, title: str, subtitle: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.setStyleSheet("QFrame#card { background: #ffffff; border: 1px solid #dcdcd8; border-radius: 10px; }")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 12, 16, 14)
        outer.setSpacing(8)
        head = QHBoxLayout()
        text = QVBoxLayout()
        text.setSpacing(0)
        self.title = QLabel(title)
        self.title.setStyleSheet(f"font-size: {FONT_M}pt; font-weight: 700; border: none;")
        text.addWidget(self.title)
        self.subtitle = QLabel(subtitle)
        self.subtitle.setWordWrap(True)
        self.subtitle.setStyleSheet(f"font-size: {FONT_S}pt; color: {MUTED}; border: none;")
        self.subtitle.setVisible(bool(subtitle))
        text.addWidget(self.subtitle)
        head.addLayout(text, 1)
        self.actions = QHBoxLayout()
        self.actions.setSpacing(8)
        head.addLayout(self.actions)
        outer.addLayout(head)
        self.body = QVBoxLayout()
        self.body.setSpacing(8)
        outer.addLayout(self.body, 1)


class StatusPill(QLabel):
    """Rounded status chip: ● text, coloured by state (good / bad / neutral)."""

    COLORS = {"good": (GOOD, "#e7f4ec"), "bad": (CRITICAL, CRITICAL_BG), "warn": (WARNING, WARNING_BG),
              "neutral": (MUTED, "#f1f1ee")}

    def __init__(self, text="", state="neutral"):
        super().__init__()
        self.set(text, state)

    def set(self, text, state="neutral"):
        fg, bg = self.COLORS[state]
        self.setText(f"<span style='color:{fg}'>●</span>&nbsp; {text}")
        self.setStyleSheet(f"QLabel {{ background: {bg}; color: {INK}; border: 1px solid {fg}; border-radius: 14px;"
                           f" padding: 5px 14px; font-weight: 600; }}")
