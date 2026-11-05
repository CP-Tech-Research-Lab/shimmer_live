"""Start page: one list of Shimmers (paired over Bluetooth and/or docked in a base), setup from the
base, Bluetooth scan for Shimmers that are not docked, guide, keypoints and log."""
import concurrent.futures
import datetime as dt
import logging
import time
from dataclasses import dataclass

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QMessageBox,
                               QPlainTextEdit, QProgressBar, QPushButton, QSplitter, QStyle, QStyledItemDelegate,
                               QStyleOptionViewItem, QTableWidget, QTableWidgetItem, QTabWidget, QTextBrowser, QVBoxLayout,
                               QWidget)

from .. import dock_setup, labels, sdlog
from .. import firmware as fwmod
from ..bt import BtDevice, PairingError
from ..session import probe_device
from .base_panel import BaseWatcher
from .branding import BrandHeader
from .common import (CRITICAL, GOOD, MUTED, WARNING, Banner, Card, StatusPill, divider, run_bg, set_role,
                     style_table, ui_call)
from .device_settings import DeviceSettingsDialog
from .dialogs import PinDialog
from .guide import GUIDE_HTML
from .keypoints_editor import KeypointsEditor
from .firmware_dialog import FirmwareDialog
from .sd_page import StartSdDialog

log = logging.getLogger("shimmerlive.devices")

ROW_H = 40
BLUE = "#1f4e8c"
COLS = ["Use", "Shimmer", "Where", "Firmware", "Sensors", "Rate", "Battery", "Status", ""]
C_STATUS, C_GEAR = 7, 8
IDLE_GREY = "#a6a6a2"         # not docked and not found nearby: switched off, far away or an old pairing
PRESENCE_SCAN_S = 12          # each round of knocks on paired Shimmers that are not docked (Bluetooth budget)...
PRESENCE_PAUSE_MS = 3000      # ...then a pause, so Bluetooth is free part of the time
PRESENT_S = 120               # answered less than this long ago (found ones are re-checked every round)
DISCOVER_EVERY_S = 45         # look for Shimmers that are not paired yet this often (a short discovery)...
DISCOVER_S = 6                # ...this long (Windows' inquiry takes as long as it takes)
DROP_CANDIDATE_MISSES = 3     # an unpaired Shimmer seen by a discovery but not answering is forgotten after this
READY, BASE, NEARBY, AWAY = "ready", "base", "nearby", "away"
GROUPS = {   # order in the table: title, explanation, title background, row background
    READY: ("Ready", "paired and switched on nearby: tick them for live data or SD recording", "#dcefe0", "#f3faf4"),
    BASE: ("In the base", "tick to set them up · take them out, switched on, to use them", "#dde8f6", "#f4f7fc"),
    NEARBY: ("Found nearby, not paired", "switched on and answering: press Pair", "#f7ebcf", "#fdf9ef"),
    AWAY: ("Paired, not found", "switched off, out of range or connected to another program", "#ececea", "#ffffff"),
}
HEADER_H = 30
READ_EVERY_S = 300            # battery and sensors of the Shimmers in Ready are read again this often
READ_RESTART_S = 20           # a Shimmer just updated (to be switched off and on) is tried this often
TICKED_HEAD_START_S = 20      # ticked Shimmers that are not found are checked again sooner than the others
MISSES_TO_GREY = 2            # a found Shimmer turns grey after this many knocks in a row without answer
TIPS = {
    "refresh": "Read again which Shimmers are paired with this computer and, if a base is connected, which are "
               "docked in it.",
    "setup": "For every ticked Shimmer in the base: pair it over Bluetooth (the PIN 1234 is entered for you), test "
             "the connection and set its clock. Progress and results appear in the Status column.",
    "forget": "Remove this computer's Bluetooth pairing with the ticked (or highlighted) Shimmers. Docked Shimmers "
              "can be paired again with one click on 'Set up docked Shimmers'.",
    "live": "Open the live view and start streaming from the ticked Shimmers in Ready.",
    "sd": "Make the ticked Shimmers in Ready record on their own SD cards (no Bluetooth while recording, so more "
          "than 7 at once). You mark keypoints and stop them from this app.",
    "sd_open": "Open the SD recording in progress: keypoints, status, stop and import.",
    "firmware": "Update the firmware of the highlighted Shimmer (it must be in a base). Its configuration is "
                "saved first and restored afterwards.",
    "settings": "Firmware information and device configuration: sampling rate, sensors, ECG gains, GSR range, name.",
    "pair": "Pair this Shimmer with this computer (the PIN 1234 is entered for you), then test the connection.",
    "plugins": "Choose plugins (e.g. real-time HRV) that run in live view.",
    "power": "Switch this computer's Bluetooth adapter on.",
}


def _local_time(iso: str) -> str:
    try:
        return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime("%d %b %H:%M")
    except ValueError:
        return iso


def describe_check(chk: dict | None) -> tuple[str, str]:
    if not chk:
        return "not tested yet", MUTED
    when = _local_time(chk.get("when", ""))
    if chk.get("ok") and chk.get("channels") == []:
        return "⚠ no sensors enabled: nothing to stream. Turn some on in ⚙ Settings", WARNING
    if chk.get("ok"):
        batt = chk.get("battery_pct")
        b = f" · battery {batt:.0f}%" if batt is not None else ""
        color = GOOD if batt is None or batt >= 20 else WARNING
        return (f"✔ {chk.get('kind', '')} · {chk.get('rate_hz', 0):.0f} Hz · {chk.get('firmware', '')}{b}"
                f" · tested {when}"), color
    return f"✖ {chk.get('error', 'failed')} · tested {when}", CRITICAL


def _short_time(iso: str) -> str:
    """'13:16' for today, '03 nov' otherwise (the full time is in the tooltip)."""
    try:
        t = dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    except (ValueError, AttributeError):
        return iso or "?"
    return t.strftime("%H:%M") if t.date() == dt.date.today() else t.strftime("%d %b")


def check_status(chk: dict | None) -> tuple[str, str]:
    """Short status for the table (sensors, rate, battery and firmware have their own columns)."""
    if not chk:
        return "not tested yet", MUTED
    when = _short_time(chk.get("when", ""))
    if chk.get("ok") and chk.get("channels") == []:
        return "⚠ no sensors enabled: turn some on in ⚙ Settings", WARNING
    if chk.get("ok"):
        return f"✔ {when}", GOOD
    return f"✖ {chk.get('error', 'failed')} · tested {when}", CRITICAL


def seconds_since(iso: str) -> float:
    try:
        return (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))).total_seconds()
    except (ValueError, TypeError):
        return float("inf")


@dataclass
class Row:
    mac: str
    name: str
    paired: bool = False
    slot: int = 0                  # base slot, 0 = not docked
    base: str = ""                 # serial of the base it is docked in
    where: str = ""                # e.g. "base slot 3" or "base B · slot 3"
    firmware: str = ""
    can_stream: bool = True
    docked_error: str = ""
    reading: bool = False          # docked, Shimmer not read over USB yet
    device: BtDevice = None
    group: str = ""                # READY / BASE / NEARBY / AWAY
    header: str = ""               # a group's title row (no Shimmer): its group


class QtLogHandler(logging.Handler):
    def __init__(self, widget: QPlainTextEdit):
        super().__init__(logging.INFO)
        self.widget = widget
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))

    def emit(self, record):
        ui_call(self._append, self.format(record))

    def _append(self, text):
        # The Log tab may be gone (page deleted): detach instead of failing, since a failure would be logged
        # here again, forever.
        try:
            self.widget.appendPlainText(text)
        except RuntimeError:
            logging.getLogger("shimmerlive").removeHandler(self)


LABEL_ROLE = Qt.UserRole + 1


class NameDelegate(QStyledItemDelegate):
    """The Shimmer column: its name, then its label (e.g. the participant) in grey and in brackets."""

    def paint(self, painter, option, index):
        label = index.data(LABEL_ROLE)
        if not label:
            return super().paint(painter, option, index)
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        name, opt.text = opt.text, ""
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, opt.widget)   # background, selection
        rect = style.subElementRect(QStyle.SE_ItemViewItemText, opt, opt.widget)
        fg = index.data(Qt.ForegroundRole)
        selected = bool(opt.state & QStyle.State_Selected)
        color = opt.palette.highlightedText().color() if selected else (fg.color() if fg else opt.palette.text().color())
        painter.save()
        painter.setFont(opt.font)
        painter.setPen(color)
        painter.drawText(rect, Qt.AlignVCenter | Qt.AlignLeft, name)
        painter.setPen(color if selected else QColor(labels.GREY))
        painter.drawText(rect.adjusted(opt.fontMetrics.horizontalAdvance(name + " "), 0, 0, 0),
                         Qt.AlignVCenter | Qt.AlignLeft, f"({label})")
        painter.restore()

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        label = index.data(LABEL_ROLE)
        if label:
            size.setWidth(size.width() + option.fontMetrics.horizontalAdvance(f" ({label})"))
        return size


def _button(text, tip, role=None):
    b = QPushButton(text)
    b.setToolTip(tip)
    return set_role(b, role) if role else b


class DevicesPage(QWidget):
    show_live = Signal(list)          # [(mac, name), ...]
    show_plugins = Signal()
    start_sd = Signal(str, list)      # session name, [(mac, name), ...]
    open_sd = Signal()

    def __init__(self, cfg, backend, backend_error):
        super().__init__()
        self.cfg, self.backend = cfg, backend
        self.busy = {}                 # mac -> progress text (testing / pairing)
        self.paired: list[BtDevice] = []
        self.candidates = {}           # mac -> BtDevice: not paired, seen by a discovery (shown once it answers)
        self._last_discover = 0.0
        self.rows: list[Row] = []
        self._sd_macs = set()
        self.needs_restart = set()      # firmware just updated: must be switched off and on           # Shimmers recording on their SD card (sdlog session)
        self.live_blocked = False
        # Background search for paired Shimmers that are not docked: only those found nearby (or docked)
        # can be ticked for live data or SD recording.
        self.presence = backend is not None   # False: the backend cannot tell, every pairing stays usable
        self.nearby = {}                       # mac -> time.monotonic() of its last answer
        self.misses = {}                       # mac -> knocks in a row without answer
        self.checked = set()                   # macs knocked at least once since the page was shown
        self.last_knock = {}                   # mac -> time.monotonic() of its last knock (rotation)
        self._presence_running = False
        self._shown = False
        self._presence_timer = QTimer(self, singleShot=True, timeout=self._presence_scan)
        self._read_timer = QTimer(self, interval=3000, timeout=self._auto_read)   # battery & sensors of Ready ones
        self._knock_queue = []                 # this round's knocks, in order: the first is being checked now
        self._activity = ""                    # "knock" | "discover" | "paused" | "" (between rounds)
        self.base = BaseWatcher(cfg, backend, self._ask_pin)
        self.base.changed.connect(self._render_all)
        self.base.setup_finished.connect(lambda _: self.refresh())

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        head = QHBoxLayout()
        head.addWidget(BrandHeader("Devices"))
        head.addStretch(1)
        self.btn_plugins = _button("Plugins…", TIPS["plugins"])
        self.btn_plugins.clicked.connect(self.show_plugins.emit)
        head.addWidget(self.btn_plugins, 0, Qt.AlignVCenter)
        root.addLayout(head)
        root.addWidget(divider())
        split = QSplitter(Qt.Horizontal)
        split.setHandleWidth(14)
        root.addWidget(split, 1)

        # ======================= left column =======================
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 8, 0, 0)
        lv.setSpacing(12)

        # ---- status: Bluetooth and base ----
        status = QHBoxLayout()
        status.setSpacing(10)
        self.pill_bt = StatusPill("checking Bluetooth…")
        self.pill_bt.setToolTip("This computer's Bluetooth adapter: it must be on to pair and stream.")
        self.pill_base = StatusPill("no Shimmer base connected")
        self.pill_base.setToolTip("A Shimmer Base or Dock connected by USB: docked Shimmers are recognised and can be "
                                  "set up with one click.")
        self.btn_power = _button("Turn Bluetooth on", TIPS["power"])
        self.btn_power.setVisible(False)
        self.btn_power.clicked.connect(self._power_on)
        status.addWidget(self.pill_bt)
        status.addWidget(self.pill_base)
        status.addWidget(self.btn_power)
        status.addStretch(1)
        lv.addLayout(status)
        if backend_error:
            lv.addWidget(Banner("critical", f"Bluetooth pairing unavailable: {backend_error}. "
                                            f"Previously used Shimmers can still be streamed."))

        # ---- the Shimmers ----
        card = Card("Your Shimmers", "Found automatically: in the base (USB) and switched on nearby (Bluetooth, "
                                     "checked every few seconds). Live data and SD recording use the ticked "
                                     "Shimmers in Ready.")
        self.btn_refresh = _button("Refresh", TIPS["refresh"], "small")
        self.btn_refresh.clicked.connect(self._refresh_all)
        card.actions.addWidget(self.btn_refresh, 0, Qt.AlignTop)
        # ---- what the background search is doing right now (so you know whether to wait) ----
        act = QHBoxLayout()
        act.setSpacing(8)
        self.activity_bar = QProgressBar()
        self.activity_bar.setRange(0, 0)                    # moving: busy
        self.activity_bar.setTextVisible(False)
        self.activity_bar.setFixedSize(90, 10)
        self.activity_label = set_role(QLabel(""), "muted")
        self.activity_label.setToolTip("While this page is open the app checks, one after the other, which paired "
                                       "Shimmers are switched on nearby, and looks for Shimmers not paired yet. "
                                       "A switched-off Shimmer takes ~5 s to check.")
        act.addWidget(self.activity_bar)
        act.addWidget(self.activity_label, 1)
        card.body.addLayout(act)
        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.verticalHeader().setVisible(False)
        style_table(self.table, ROW_H)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setWordWrap(False)
        hh = self.table.horizontalHeader()
        for i, mode in enumerate((QHeaderView.ResizeToContents,) * C_STATUS + (QHeaderView.Stretch, QHeaderView.ResizeToContents)):
            hh.setSectionResizeMode(i, mode)
        hh.setSectionResizeMode(0, QHeaderView.Fixed)       # the group titles live in this cell (spanned):
        hh.resizeSection(0, 52)                             # sized to them, it would take the whole width
        for i, tip in enumerate(("Ready: tick for live data / SD recording · In the base: tick to set it up", "Bluetooth name (hover for its address)",
                                 "Base slot if the Shimmer is docked; otherwise whether it was found switched on "
                                 "nearby (checked every few seconds)",
                                 "LogAndStream version. ⬆ old: update recommended · ↑ a newer version exists "
                                 "(hover a version for details)",
                                 "Sensors switched on (at the last reading; hover for the channels)",
                                 "Sampling rate (at the last reading)", "Battery (at the last reading; Ready Shimmers are read every 5 minutes)",
                                 "✔ time of the last reading of battery and sensors (hover for details), or setup progress / "
                                 "problems",
                                 "Device settings")):
            hh.model().setHeaderData(i, Qt.Horizontal, tip, Qt.ToolTipRole)
        self.table.verticalHeader().setMinimumSectionSize(HEADER_H)
        self.table.setItemDelegateForColumn(1, NameDelegate(self.table))
        self.table.cellDoubleClicked.connect(self._edit_label)
        self.table.itemChanged.connect(self._on_check_changed)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        card.body.addWidget(self.table, 1)
        self.empty = QLabel()
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setWordWrap(True)
        self.empty.setStyleSheet(f"color: {MUTED}; font-size: 12pt; padding: 30px; border: none;")
        card.body.addWidget(self.empty, 1)
        acts = QHBoxLayout()
        acts.setSpacing(8)
        self.btn_setup = _button("Set up docked Shimmers", TIPS["setup"])
        self.btn_setup.clicked.connect(self._setup_ticked)
        self.btn_forget = _button("Forget pairing", TIPS["forget"])
        self.btn_forget.clicked.connect(self._forget_selected)
        self.btn_live = _button("Show live data  ▶", TIPS["live"], "primary")
        self.btn_live.clicked.connect(self._go_live)
        self.btn_fw = _button("Update firmware…", TIPS["firmware"])
        self.btn_fw.clicked.connect(self._update_firmware)
        # Two rows, so the page fits narrow screens (1440 px logical at 2x scaling): managing Shimmers
        # above, starting an acquisition below on the right.
        for b in (self.btn_setup, self.btn_forget, self.btn_fw):
            acts.addWidget(b)
        acts.addStretch(1)
        self.btn_sd = _button("Record on SD cards…", TIPS["sd"])
        self.btn_sd.clicked.connect(self._sd_clicked)
        go = QHBoxLayout()
        go.setSpacing(8)
        go.addStretch(1)
        go.addWidget(self.btn_sd)
        go.addWidget(self.btn_live)
        card.body.addLayout(acts)
        card.body.addLayout(go)
        lv.addWidget(card, 1)

        split.addWidget(left)

        # ======================= right column =======================
        tabs = QTabWidget()
        guide = QTextBrowser()
        guide.setOpenExternalLinks(True)
        guide.setHtml(GUIDE_HTML)
        tabs.addTab(guide, "Guide")
        tabs.setTabToolTip(0, "How to connect Shimmers, LED meanings, troubleshooting")
        self.keypoints = KeypointsEditor(cfg)
        tabs.addTab(self.keypoints, "Keypoints")
        tabs.setTabToolTip(1, "The moments you can mark during a recording (rest start, task end, …)")
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        tabs.addTab(self.log_view, "Log")
        tabs.setTabToolTip(2, "Everything the app does, step by step: useful when something does not work")
        logging.getLogger("shimmerlive").addHandler(QtLogHandler(self.log_view))
        split.addWidget(tabs)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([1060, 360])          # the Shimmers list needs the width; the guide can be widened

        if not backend:
            for b in (self.btn_forget, self.btn_power, self.btn_setup):
                b.setEnabled(False)
        self._render_all()

    # ---------------- data ----------------
    def _refresh_all(self):
        """Refresh button: look for the base and read its slots again, and re-read the paired list."""
        self.base.rescan()
        self.refresh()
        self.checked = set()                 # check every Shimmer again, and look for new ones, right away
        self._last_discover = 0.0
        if not self._presence_running:
            self._presence_next(300)

    def refresh(self):
        if not self.backend:
            self.pill_bt.set("Bluetooth backend unavailable", "bad")
            self.paired = [BtDevice(mac, d.get("name", mac), True) for mac, d in self.cfg.data["devices"].items()]
            self._render_all()
            return
        self.btn_refresh.setEnabled(False)

        def work():
            return self.backend.adapter_status(), self.backend.list_paired()

        def done(res):
            (ok, text), paired = res
            self.btn_refresh.setEnabled(True)
            self.pill_bt.set("Bluetooth on" if ok else "Bluetooth OFF", "good" if ok else "bad")
            self.pill_bt.setToolTip(text)
            self.btn_power.setVisible(not ok)
            self.paired = paired
            self._render_all()

        def fail(e):
            self.btn_refresh.setEnabled(True)
            self.pill_bt.set(f"Bluetooth error: {e}", "bad")

        run_bg(work, done, fail, "refresh")

    def _power_on(self):
        run_bg(self.backend.power_on, lambda _: self.refresh(),
               lambda e: QMessageBox.warning(self, "Bluetooth", f"Could not switch Bluetooth on:\n{e}"))

    def _build_rows(self):
        rows = {d.address: Row(d.address, d.name, True, device=d,
                               firmware=(self.cfg.device(d.address).get("last_check") or {}).get("firmware", ""))
                for d in self.paired if d.looks_like_shimmer}
        order = {}
        for k, (u, s) in enumerate(self.base.docked()):
            where = self.base.where(u, s.slot)
            if not s.mac:                                     # docked: being read, or not readable
                rows[f"{u.serial}/{s.slot}"] = Row("", "…", slot=s.slot, base=u.serial, where=where,
                                                   docked_error=s.error, reading=not s.error)
                order[f"{u.serial}/{s.slot}"] = k
                continue
            r = rows.setdefault(s.mac, Row(s.mac, s.name))
            r.slot, r.base, r.where, r.firmware, r.can_stream = s.slot, u.serial, where, s.firmware, s.can_stream
            order[s.mac] = k
        for mac, d in self.candidates.items():                # not paired, not docked, answering
            if mac not in rows and mac in self.nearby:
                rows[mac] = Row(mac, d.name or mac, False, device=d)
        for r in rows.values():
            r.group = self._group(r)
        names = list(GROUPS)
        data = sorted(rows.values(), key=lambda r: (names.index(r.group), order.get(r.mac or f"{r.base}/{r.slot}", 0),
                                                    r.name.lower()))
        out = []
        for g in names:
            members = [r for r in data if r.group == g]
            if members:
                out.append(Row("", GROUPS[g][0], header=g, group=g))
                out.extend(members)
        return out

    def _group(self, r: Row) -> str:
        if r.slot:
            return BASE
        if not r.paired:
            return NEARBY
        return READY if self._present(r) else AWAY

    def _status_of(self, r: Row):
        if r.reading:
            return "reading it over USB…", WARNING
        if r.mac in self._sd_macs:
            if r.slot:
                return "⚠ docked while recording to SD: open the SD recording page", CRITICAL
            return f"● recording on its SD card (session {sdlog.active(self.cfg)['name']})", CRITICAL
        if r.mac in self.busy:
            return self.busy[r.mac], BLUE
        if r.mac in self.needs_restart:
            return ("⚠ switch it off and on to finish the firmware update (it is checked again automatically)",
                    WARNING)
        res = self.base.result(r.base, r.slot) if r.slot else None
        if res:
            return res[1], {"busy": BLUE, dock_setup.READY: GOOD, dock_setup.PROBLEM: CRITICAL}.get(res[0], MUTED)
        if r.docked_error:
            return "could not read it over USB: push it back into the slot", CRITICAL
        if r.slot and not r.can_stream:
            return "⚠ can't stream (SDLog firmware: update it with 'Update firmware…')", CRITICAL
        if r.group == NEARBY:
            return "switched on nearby, not paired yet: press Pair", MUTED
        if not r.paired:
            return "not paired yet: tick it, then press 'Set up docked Shimmers'", MUTED
        return check_status(self.cfg.device(r.mac).get("last_check"))

    def _present(self, r: Row) -> bool:
        """Docked, or found switched on nearby by the background search (or a successful test) just now."""
        if r.slot or not self.presence:
            return True
        seen = self.nearby.get(r.mac)
        return seen is not None and time.monotonic() - seen < PRESENT_S

    def _is_idle(self, r: Row) -> bool:
        """Paired, not in a base and not found nearby: shown in grey, cannot be ticked."""
        return r.group == AWAY and r.mac not in self.busy and r.mac not in self._sd_macs

    # ---------------- background search for Shimmers nearby ----------------
    def _presence_should_stop(self) -> bool:
        """Bluetooth is needed for something else (a test, pairing, setup) or the page is hidden."""
        return not self._shown or bool(self.busy) or self.base.setting_up

    def _presence_next(self, ms=PRESENCE_PAUSE_MS):
        if self._shown and self.presence:
            self._presence_timer.start(ms)

    def _knock_order(self, macs):
        """Found ones first (confirm they are still on, ~1 s each), then the ones not checked yet since the page
        was shown, then the rest by the time of their last knock, oldest first; ticked ones count as knocked
        TICKED_HEAD_START_S earlier, so they come round more often without starving the others."""
        ticked = set(self.cfg.data.get("selected", []))
        return sorted(macs, key=lambda m: (m not in self.nearby, m in self.checked,
                                           self.last_knock.get(m, 0.0) - (TICKED_HEAD_START_S if m in ticked else 0)))

    def _presence_scan(self):
        """One round: a short discovery for Shimmers not paired yet (every DISCOVER_EVERY_S), otherwise knocks on
        the paired ones that are not docked and on the unpaired ones the discoveries saw."""
        if self._presence_running or not self.presence:
            return
        if self._presence_should_stop():
            self._set_activity("paused")
            self._presence_next(2000)
            return
        if time.monotonic() - self._last_discover > DISCOVER_EVERY_S:
            self._discover_round()
            return
        docked = {s.mac for _, s in self.base.docked() if s.mac}
        paired = {d.address for d in self.paired if d.looks_like_shimmer}
        want = self._knock_order([m for m in paired | set(self.candidates) if m not in docked])
        if not want:
            self._presence_next(2000)
            return
        self._presence_running = True
        stop = self._presence_should_stop
        self._knock_queue = list(want)
        self._set_activity("knock")

        def work():
            return self.backend.nearby(want, PRESENCE_SCAN_S, stop,
                                       lambda mac, ok: ui_call(self._knock_result, mac, ok))

        def done(answers):
            self._presence_running = False
            self._knock_queue = []
            self._set_activity("")
            if answers is None:                            # this computer cannot tell: keep every pairing usable
                log.info("checking which Shimmers are switched on is not supported here")
                self.presence = False
                self._render_table()
                return
            self._presence_next()

        def failed(e):
            self._presence_running = False
            self._knock_queue = []
            self._set_activity("")
            log.info("checking for Shimmers nearby failed: %s", e)
            self._presence_next(PRESENCE_PAUSE_MS * 3)

        run_bg(work, done, failed, "nearby")

    def _auto_read(self):
        """Read battery, sensors and rate of the Shimmers in Ready every READ_EVERY_S (and try one waiting to be
        switched off and on after a firmware update every READ_RESTART_S), one at a time and never while the
        background search is using Bluetooth (it pauses while this runs)."""
        if not self._shown or self.busy or self.base.setting_up or self._presence_running or not self.backend:
            return
        due = []
        for r in self.rows:
            if r.header or not r.mac or not r.paired or r.mac in self._sd_macs:
                continue
            wait = READ_RESTART_S if r.mac in self.needs_restart else READ_EVERY_S if r.group == READY else None
            if wait is None:
                continue
            age = seconds_since((self.cfg.device(r.mac).get("last_check") or {}).get("when", ""))
            if age >= wait:
                due.append((age, r))
        if due:
            _, r = max(due, key=lambda x: x[0])
            self._test(r.mac, r.name)

    def _to_check(self):
        """Paired Shimmers that are not docked (the ones the knocks are for)."""
        return [r for r in self.rows if not r.header and r.paired and not r.slot and r.mac]

    def _set_activity(self, what):
        self._activity = what
        self._update_activity()

    def _update_activity(self):
        """The line above the table: what the background search is doing, so you know whether to wait."""
        if not self.presence or not self._shown:
            self.activity_bar.setVisible(False)
            self.activity_label.setText("")
            return
        todo = self._to_check()
        done = sum(1 for r in todo if r.mac in self.checked)
        count = f"  ({done} of {len(todo)} paired Shimmers checked)" if todo else ""
        current = self._knock_queue[0] if self._activity == "knock" and self._knock_queue else None
        if current:
            text = f"Checking {self._name_of(current)}…{count}"
        elif self._activity == "discover":
            text = "Looking for Shimmers that are not paired yet…"
        elif self._activity == "paused":
            text = "Paused while Bluetooth is busy (test, pairing or setup)…"
        elif todo and done < len(todo):
            text = f"Checking which Shimmers are switched on…{count}"
        else:
            text = ("All checked · checking again every few seconds" if todo else
                    "Looking for Shimmers every few seconds")
        busy = bool(current) or self._activity in ("discover", "paused") or bool(todo and done < len(todo))
        self.activity_bar.setVisible(busy)
        self.activity_label.setText(text)

    def _discover_round(self):
        self._presence_running = True
        self._last_discover = time.monotonic()
        self._set_activity("discover")

        def done(devs):
            self._presence_running = False
            self._set_activity("")
            paired = {d.address for d in self.paired}
            docked = {s.mac for _, s in self.base.docked() if s.mac}
            for d in devs or []:
                if d.looks_like_shimmer and d.address not in paired and d.address not in docked \
                        and d.address not in self.candidates:
                    self.candidates[d.address] = d
                    self.last_knock.setdefault(d.address, 0.0)
            self._presence_next(200)                         # knock on what it found right away

        def failed(e):
            self._presence_running = False
            self._set_activity("")
            log.info("looking for Shimmers not paired yet failed: %s", e)
            self._presence_next(PRESENCE_PAUSE_MS)

        run_bg(lambda: self.backend.discover(DISCOVER_S, lambda devs: None), done, failed, "discover")

    def _knock_result(self, mac, ok):
        """One Shimmer checked: shown at once (not at the end of the round)."""
        if mac in self._knock_queue:                         # the next one in the queue is being checked now
            self._knock_queue = self._knock_queue[self._knock_queue.index(mac) + 1:]
        now = time.monotonic()
        self.checked.add(mac)
        self.last_knock[mac] = now
        if ok:
            if mac not in self.nearby:
                log.info("%s is switched on nearby", self._name_of(mac))
            self.nearby[mac], self.misses[mac] = now, 0
        else:
            self.misses[mac] = self.misses.get(mac, 0) + 1
            if mac in self.nearby and self.misses[mac] >= MISSES_TO_GREY:
                log.info("%s no longer answers (switched off or out of range?)", self._name_of(mac))
                self.nearby.pop(mac, None)
            if mac in self.candidates and mac not in self.nearby and self.misses[mac] >= DROP_CANDIDATE_MISSES:
                self.candidates.pop(mac, None)              # seen once, but not here (any more)
        self._render_all()

    def _name_of(self, mac) -> str:
        if mac in self.candidates:
            return self.candidates[mac].name or mac
        return next((r.name for r in self.rows if r.mac == mac), mac)

    def _render_all(self):
        has_base = bool(self.base.units)
        if self.base.state == "checking":
            self.pill_base.set("looking for Shimmer bases…", "warn")
        elif not has_base:
            self.pill_base.set("no Shimmer base connected", "neutral")
        else:
            self.pill_base.set(self.base.summary() + (" · setting up…" if self.base.setting_up else ""), "good")
            self.pill_base.setToolTip("\n".join(f"Base {u.letter}: {u.device.label}, USB serial {u.serial}"
                                                for u in self.base.ordered()))
        self.btn_setup.setVisible(has_base and self.base.state != "checking")
        self.rows = self._build_rows()
        if not self.rows:
            self.empty.setText("Looking for Shimmers in a base…" if self.base.state == "checking" or self.base.reading
                               else "No Shimmers yet.\nPut them in the Shimmer Base and switch them on, or switch "
                                    "one on near this computer: they appear here by themselves (a Shimmer that is "
                                    "not in a base can take up to a minute).")
        self.table.setVisible(bool(self.rows))
        self.empty.setVisible(not self.rows)
        self._render_table()

    def _render_header(self, i, g):
        t = self.table
        title, why, bg, _ = GROUPS[g]
        n = sum(1 for r in self.rows if r.group == g and not r.header)
        t.removeCellWidget(i, C_GEAR)
        for c in range(1, len(COLS)):
            t.setItem(i, c, QTableWidgetItem(""))
        it = QTableWidgetItem(f"{title} ({n})   ·   {why}")
        it.setFlags(Qt.ItemIsEnabled)
        f = it.font()
        f.setBold(True)
        it.setFont(f)
        it.setBackground(QColor(bg))
        it.setForeground(QColor("#1f1f1e"))
        t.setItem(i, 0, it)
        t.setSpan(i, 0, 1, len(COLS))
        t.setRowHeight(i, HEADER_H)

    def _render_table(self):
        selected = set(self.cfg.data.get("selected", []))
        self._sd_macs = sdlog.recording_macs(self.cfg)
        t = self.table
        t.blockSignals(True)
        t.clearSpans()
        t.setRowCount(len(self.rows))
        for i, r in enumerate(self.rows):
            if r.header:
                self._render_header(i, r.header)
                continue
            t.setRowHeight(i, ROW_H)
            # Ready: tick to stream / record on SD; in the base: tick to include it in the setup
            present = self._present(r)
            usable = bool(r.mac) and r.can_stream and r.mac not in self._sd_macs and r.group in (READY, BASE)
            chk = QTableWidgetItem()
            chk.setFlags((Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable) if usable
                         else Qt.ItemIsSelectable)
            chk.setCheckState(Qt.Checked if usable and r.mac in selected else Qt.Unchecked)
            chk.setData(Qt.UserRole, r.mac)
            chk.setToolTip("Tick it for live data or SD recording" if usable and r.group == READY else
                           "Tick it to include it in 'Set up docked Shimmers'. To use it for live data or SD "
                           "recording, take it out of the base switched on: it moves to Ready." if usable else
                           "Its firmware cannot stream" if r.mac and not r.can_stream else
                           "Recording on its SD card" if r.mac in self._sd_macs else
                           "Not paired yet: press Pair" if r.group == NEARBY else
                           "Not found nearby: switch it on and bring it closer. It moves to Ready as soon as the app "
                           "finds it (it checks every few seconds)."
                           if r.mac and not present else "Still reading it over USB")
            t.setItem(i, 0, chk)
            name = QTableWidgetItem(r.name)
            label = labels.get(self.cfg, r.mac)
            name.setData(LABEL_ROLE, label)
            name.setToolTip(labels.plain(r.name, label) + f"\nBluetooth address {r.mac}"
                            + (f"\nfirmware {r.firmware}" if r.firmware else "")
                            + ("\n\nDouble-click to change its label (e.g. the participant wearing it)" if r.mac else ""))
            t.setItem(i, 1, name)
            unknown = not r.slot and (self.base.state == "checking" or self.base.reading)
            where = QTableWidgetItem(self._where_text(r, unknown))
            searching = not r.slot and self.presence and not self._present(r) and r.mac not in self.checked
            where.setForeground(QColor("#1f1f1e" if r.slot else WARNING if unknown or searching
                                       else GOOD if self.presence and self._present(r) else MUTED))
            where.setToolTip(self._where_tip(r, unknown))
            t.setItem(i, 2, where)
            level, short, why = fwmod.assess(r.firmware)
            fw = QTableWidgetItem(short)
            fw.setForeground(QColor({fwmod.OLD: WARNING, fwmod.NOT_STREAMING: CRITICAL,
                                     fwmod.NEWER_EXISTS: MUTED, fwmod.UNKNOWN: MUTED}.get(level, "#1f1f1e")))
            fw.setToolTip(f"{r.firmware}\n\n{why}" if r.firmware else why)
            t.setItem(i, 3, fw)
            last = (self.cfg.device(r.mac).get("last_check") or {}) if r.mac else {}
            chk = last if last.get("ok") else (last.get("last_ok") or {})   # a failed reading keeps the last values
            ok = bool(chk.get("ok"))
            kind = QTableWidgetItem(chk.get("kind", "") if ok else "")
            kind.setToolTip(", ".join(chk.get("channels", [])) if ok else "")
            t.setItem(i, 4, kind)
            rate = QTableWidgetItem(f"{chk['rate_hz']:.0f} Hz" if ok and chk.get("rate_hz") else "")
            t.setItem(i, 5, rate)
            batt_pct = chk.get("battery_pct") if ok else None
            batt = QTableWidgetItem(f"{batt_pct:.0f}%" if batt_pct is not None else "")
            if batt_pct is not None:
                batt.setForeground(QColor(CRITICAL if batt_pct < 10 else WARNING if batt_pct < 20 else GOOD))
                batt.setToolTip(f"read at {_local_time(chk.get('when', ''))} (read again every "
                                f"{READ_EVERY_S // 60} minutes while it is in Ready)")
            t.setItem(i, 6, batt)
            text, color = self._status_of(r)
            st = QTableWidgetItem(text)
            st.setForeground(QColor(color))
            last = (self.cfg.device(r.mac).get("last_check") or {}).get("when") if r.mac else None
            st.setToolTip(text + (f"\nlast reading: {_local_time(last)}" if last else ""))
            t.setItem(i, C_STATUS, st)
            idle = self._is_idle(r)
            for c in range(C_GEAR):
                it = t.item(i, c)
                if it is not None:
                    it.setBackground(QColor(GROUPS[r.group][3]))
                    if idle:                         # switched off or away: keep it out of the way
                        it.setForeground(QColor(IDLE_GREY))
            if idle:
                name.setToolTip(name.toolTip() + "\nNot in a base and not found nearby (switched off or out of "
                                                 "range?): it cannot be used for live data or SD recording now")
            if r.group == NEARBY:
                pb = _button("Pair", TIPS["pair"], "small")
                pb.setEnabled(r.mac not in self.busy and self.backend is not None)
                pb.clicked.connect(lambda _=False, dev=r.device: self._pair(dev))
                t.setCellWidget(i, C_GEAR, pb)
            elif r.paired:
                gear = _button("⚙", TIPS["settings"], "small")
                gear.setEnabled(r.mac not in self.busy and not self.base.setting_up and r.mac not in self._sd_macs)
                gear.clicked.connect(lambda _=False, mac=r.mac, nm=r.name: self._settings(mac, nm))
                t.setCellWidget(i, C_GEAR, gear)
            else:
                t.removeCellWidget(i, C_GEAR)
                t.setItem(i, C_GEAR, QTableWidgetItem(""))
        t.blockSignals(False)
        self._update_activity()
        self._update_buttons()

    def _edit_label(self, row, col):
        """Double-click on a Shimmer's name: set its label (e.g. the participant). Only on this computer."""
        if col != 1 or row >= len(self.rows) or self.rows[row].header or not self.rows[row].mac:
            return
        r = self.rows[row]
        text, ok = QInputDialog.getText(
            self, "Label", f"Label for {r.name}, e.g. the participant wearing it.\n\nShown next to its name here, in "
                           f"live view and in recordings. Only on this computer: the Shimmer and its Bluetooth "
                           f"name do not change. Empty = no label.", text=labels.get(self.cfg, r.mac))
        if ok:
            new = labels.set_label(self.cfg, r.mac, text)
            log.info("%s: label %s", r.name, f"'{new}'" if new else "removed")
            self._render_table()

    def _where_text(self, r: Row, unknown: bool) -> str:
        if r.slot:
            return r.where
        if unknown:
            return "checking…"
        if not self.presence:
            return "not docked"
        if self._present(r):
            return "nearby"
        if self._knock_queue and self._knock_queue[0] == r.mac and self._activity == "knock":
            return "checking…"
        return "waiting…" if r.mac not in self.checked else "not found"

    def _where_tip(self, r: Row, unknown: bool) -> str:
        if r.slot:
            return "In the base (read over USB)"
        if unknown:
            return "Still reading the base: this Shimmer may be docked"
        if not self.presence:
            return "Not in a base"
        if self._present(r):
            return (f"Switched on and in range: it answered {time.monotonic() - self.nearby[r.mac]:.0f} s ago "
                    "(checked again every few seconds)")
        if r.mac not in self.checked:
            return ("Not checked yet: the app checks the paired Shimmers one after the other (the line above the "
                    "table shows which one now)")
        return ("Not in a base and it did not answer: switched off, out of range or connected to another program. "
                "Switch it on (blue blink every 2 s): it turns bright at its next check (switched-off Shimmers "
                "take ~5 s each to check, so with many of them this can take up to a minute).")

    @staticmethod
    def _signal_text(rssi):
        if rssi is None:
            return "—"
        q = "strong" if rssi > -65 else "ok" if rssi > -80 else "weak – move closer"
        return f"{rssi} dBm ({q})"

    def _checked_rows(self):
        return [r for i, r in enumerate(self.rows)
                if i < self.table.rowCount() and self.table.item(i, 0) is not None
                and self.table.item(i, 0).checkState() == Qt.Checked]

    def checked_devices(self):
        """Ticked Shimmers in Ready: paired, not docked, switched on nearby (docked ticks are for the setup)."""
        return [(r.mac, r.name) for r in self._checked_rows() if r.group == READY]

    def _checked_slots(self):
        return [(r.base, r.slot) for r in self._checked_rows() if r.group == BASE and r.slot]

    def _setup_ticked(self):
        slots = self._checked_slots()
        if slots:
            self.base.setup_all(slots)

    def _target_devices(self):
        """Ticked Shimmers, or the highlighted paired ones if nothing is ticked."""
        devs = self.checked_devices()
        if devs:
            return devs
        rows = sorted({i.row() for i in self.table.selectedItems()})
        return [(self.rows[i].mac, self.rows[i].name) for i in rows
                if self.rows[i].paired and self.rows[i].mac not in self._sd_macs]

    def _on_check_changed(self, item):
        if item.column() == 0:
            # ticks of rows that cannot be ticked right now (e.g. not found nearby) are kept for when they can
            locked = {r.mac for i, r in enumerate(self.rows) if i < self.table.rowCount() and r.mac
                      and self.table.item(i, 0) is not None and not self.table.item(i, 0).flags() & Qt.ItemIsEnabled}
            kept = [m for m in self.cfg.data.get("selected", []) if m in locked]
            self.cfg.data["selected"] = kept + [r.mac for r in self._checked_rows() if r.mac not in kept]
            self.cfg.save()
            self._update_buttons()

    def mark_needs_restart(self, mac):
        self.needs_restart.add(mac)
        self._render_all()

    def mark_restarted(self, mac):
        self.needs_restart.discard(mac)
        self._render_all()

    def _firmware_target(self):
        """The highlighted row if it is one readable Shimmer in a base, else None."""
        rows = sorted({i.row() for i in self.table.selectedItems()})
        if len(rows) != 1 or rows[0] >= len(self.rows):
            return None
        r = self.rows[rows[0]]
        return r if r.slot and r.mac and r.mac not in self._sd_macs else None

    def _update_firmware(self):
        r = self._firmware_target()
        docked = self.base.locate(r.mac) if r else None
        if docked is None:
            QMessageBox.information(self, "Update firmware", "Highlight one Shimmer that is in the base.")
            return
        self._open_firmware(docked, r.name)

    def _open_firmware(self, docked, name):
        if self.base.busy:
            QMessageBox.information(self, "Update firmware", "The base is busy (reading slots or setting up): "
                                                             "try again in a few seconds.")
            return
        FirmwareDialog(self, self.base, docked, name, docked.info.firmware, self.cfg).exec()
        self.refresh()

    def _update_buttons(self):
        sel = self.checked_devices()
        busy = any(m in self.busy for m, _ in sel) or self.base.setting_up
        self.btn_live.setEnabled(bool(sel) and not busy and not self.live_blocked)
        self.btn_live.setText(f"Show live data ({len(sel)})  ▶" if sel else "Show live data  ▶")
        self.btn_fw.setEnabled(self._firmware_target() is not None and not self.base.busy)
        over = len(sel) > self.bt_limit()
        self.btn_live.setToolTip(TIPS["live"] + (f"\n\n⚠ {len(sel)} ticked: this computer streams at most "
                                                 f"{self.bt_limit()} at once." if over else ""))
        if sdlog.active(self.cfg):
            self.btn_sd.setText("SD recording in progress  ▶")
            self.btn_sd.setToolTip(TIPS["sd_open"])
            self.btn_sd.setEnabled(True)
        else:
            self.btn_sd.setText(f"Record on SD cards ({len(sel)})…" if sel else "Record on SD cards…")
            self.btn_sd.setToolTip(TIPS["sd"])
            self.btn_sd.setEnabled(bool(sel) and not busy and self.backend is not None)
        slots = self._checked_slots()
        self.btn_setup.setText(f"Set up docked Shimmers ({len(slots)})" if slots else "Set up docked Shimmers")
        self.btn_setup.setEnabled(self.backend is not None and not self.base.busy and bool(slots))

    def set_live_blocked(self, blocked: bool, why: str = ""):
        self.live_blocked = blocked
        self.btn_live.setToolTip(why or TIPS["live"])
        self._update_buttons()

    # ---------------- actions ----------------
    def _test(self, mac, name, then=None):
        if mac in self.busy:
            return
        self.busy[mac] = "reading battery and sensors…"
        self._render_table()

        def progress(text):
            self.busy[mac] = f"reading battery and sensors… {text}"
            ui_call(self._render_table)

        def done(res):
            self.busy.pop(mac, None)
            if res.get("ok"):
                self.needs_restart.discard(mac)       # it answers over Bluetooth: the update is finished
                self.nearby[mac] = time.monotonic()                # it answered: it is switched on and in range
                self.misses[mac] = 0
                self.checked.add(mac)
            self._render_table()
            log.info("%s test: %s", name, describe_check(res)[0])
            if then:
                then(res)

        run_bg(lambda: probe_device(mac, name, self.cfg, progress=progress), done,
               lambda e: done({"ok": False, "error": str(e), "when": ""}), f"test-{name}")

    def _settings(self, mac, name):
        self.busy[mac] = "settings open"
        self._render_table()
        docked = self.base.locate(mac)
        dlg = DeviceSettingsDialog(self, mac, name, self.cfg,
                                   docked_where=self.base.where(docked.unit, docked.slot) if docked else None)
        dlg.exec()
        self.busy.pop(mac, None)
        self._render_table()
        if dlg.wants_firmware and docked is not None:
            QTimer.singleShot(800, lambda: self._open_firmware(docked, name))   # let the Bluetooth link close first
            return
        if dlg.applied:                        # refresh the row with what the device now reports
            QTimer.singleShot(1500, lambda: self._test(mac, name))

    def _forget_selected(self):
        devs = self._target_devices()
        if not devs:
            QMessageBox.information(self, "Forget pairing", "Tick or highlight a paired Shimmer first.")
            return
        names = ", ".join(n for _, n in devs)
        if QMessageBox.question(self, "Forget pairing",
                                f"Remove this computer's Bluetooth pairing with {names}?\n\nDocked Shimmers can be "
                                f"paired again with 'Set up docked Shimmers'; others need a scan.") != QMessageBox.Yes:
            return
        by_mac = {d.address: d for d in self.paired}

        def work():
            for mac, _ in devs:
                self.backend.forget(by_mac.get(mac, BtDevice(mac, mac, True)))

        run_bg(work, lambda _: self.refresh(),
               lambda e: QMessageBox.warning(self, "Forget pairing", f"Could not remove the pairing:\n{e}"))

    def _sd_clicked(self):
        if sdlog.active(self.cfg):
            self.open_sd.emit()
            return
        devs = self.checked_devices()
        picked = {m for m, _ in devs}
        risky = [r.name for r in self.rows if r.mac in picked
                 and (fwmod.parse(r.firmware) or ("", (9, 9, 9)))[1][:2] in ((0, 15), (0, 16))]
        if risky and QMessageBox.question(
                self, "Record on SD cards",
                f"{', '.join(risky)} run{'s' if len(risky) == 1 else ''} LogAndStream 0.15/0.16. With that firmware "
                "Bluetooth can lock up right after the SD recording starts (seen on 3 of these units; fixed by "
                "Shimmer in 1.00.015). The app then cannot check or stop them: you would stop them with their "
                "own button BEFORE docking them.\n\nUpdating their firmware avoids this. Continue anyway?"
        ) != QMessageBox.Yes:
            return
        dlg = StartSdDialog(self, [n for _, n in devs])
        if dlg.exec():
            self.start_sd.emit(dlg.session_name(), devs)

    def _ask_pin(self, device_name):
        """Ask the user for a PIN (only used if the standard Shimmer PIN 1234 was refused)."""
        fut = concurrent.futures.Future()
        ui_call(lambda: fut.set_result(PinDialog.ask(self, device_name)))
        return fut.result(timeout=180)

    def _pair(self, dev: BtDevice):
        self.busy[dev.address] = "pairing…"
        self._render_table()

        def status(text, color=BLUE):
            if dev.address in self.busy:
                self.busy[dev.address] = text
            self._render_table()

        def work():
            provide = dock_setup.auto_pin(self._ask_pin)
            for attempt in range(1, dock_setup.PAIR_ATTEMPTS + 1):
                ui_call(status, f"pairing… attempt {attempt}/{dock_setup.PAIR_ATTEMPTS}: keep it switched on "
                                f"and close")
                try:
                    self.backend.pair(dev, provide)
                    return attempt
                except PairingError as e:
                    log.info("pairing %s attempt %d: %s", dev.name, attempt, e)
                    if not e.retryable or attempt == dock_setup.PAIR_ATTEMPTS:
                        raise
                    time.sleep(dock_setup.PAIR_BACKOFF_S * attempt)

        def paired(_):
            log.info("%s paired", dev.name)
            self.busy.pop(dev.address, None)
            self.cfg.data.setdefault("selected", [])
            if dev.address not in self.cfg.data["selected"]:
                self.cfg.data["selected"].append(dev.address)
            self.cfg.save()
            self.candidates.pop(dev.address, None)
            self.paired.append(BtDevice(dev.address, dev.name, True, platform_id=dev.platform_id))
            self._render_all()
            self._test(dev.address, dev.name, then=tested)
            QTimer.singleShot(1000, self.refresh)

        def tested(res):
            log.info("%s paired%s", dev.name, " and working" if res.get("ok") else
                     f", but the test connection failed: {res.get('error')}")

        def failed(e):
            self.busy.pop(dev.address, None)
            self._render_table()
            log.info("pairing %s failed: %s", dev.name, e)
            QMessageBox.warning(self, "Pairing failed", f"{dev.name}: {e}\n\n{getattr(e, 'hint', '')}".strip())

        run_bg(work, paired, failed, f"pair-{dev.name}")

    def showEvent(self, ev):
        super().showEvent(ev)
        self.base.start()                    # watch USB for a base only while this page is visible
        self._shown = True
        self._read_timer.start()
        self.checked = set()                 # "waiting…" again until each has been checked after coming back
        # paired Shimmers first: the first look for unpaired ones comes ~15 s after the page is shown
        self._last_discover = max(self._last_discover, time.monotonic() - DISCOVER_EVERY_S + 15)
        self._presence_next(300)

    def hideEvent(self, ev):
        self.base.stop()
        self._shown = False
        self._activity, self._knock_queue = "", []
        try:
            self._read_timer.stop()
        except RuntimeError:
            pass
        # a running search stops within half a second (live view needs the radio)
        try:
            self._presence_timer.stop()
        except RuntimeError:
            pass
        super().hideEvent(ev)

    def bt_limit(self) -> int:
        """Most Shimmers this PC can stream at once: Bluetooth allows 7; the adapter may allow fewer
        (learnt the first time it refuses one: 6 on an Intel AX211)."""
        return int(self.cfg.data.get("bt_max_links") or 7)

    def _go_live(self):
        devs = self.checked_devices()
        if not devs:
            return
        limit = self.bt_limit()
        if len(devs) > limit:
            learnt = "measured on this computer" if self.cfg.data.get("bt_max_links") else "the Bluetooth maximum"
            box = QMessageBox(QMessageBox.Warning, "Too many Shimmers for live view",
                              f"You ticked {len(devs)} Shimmers, but this computer's Bluetooth can stream at most "
                              f"{limit} at once ({learnt}). The extra ones would not connect.\n\n"
                              f"To use all {len(devs)}: record them on their SD cards ('Record on SD cards…').",
                              parent=self)
            first = box.addButton(f"Stream the first {limit}", QMessageBox.AcceptRole)
            box.addButton(QMessageBox.Cancel)
            box.exec()
            if box.clickedButton() is not first:
                return
            devs = devs[:limit]
        self.show_live.emit(devs)
