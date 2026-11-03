"""Start page: one list of Shimmers (paired over Bluetooth and/or docked in a base), setup from the
base, Bluetooth scan for Shimmers that are not docked, guide, keypoints and log."""
import concurrent.futures
import datetime as dt
import logging
import time
from dataclasses import dataclass

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPlainTextEdit,
                               QProgressBar, QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget,
                               QTextBrowser, QVBoxLayout, QWidget)

from .. import dock_setup, sdlog
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
from .sd_page import StartSdDialog

log = logging.getLogger("shimmerlive.devices")

SCAN_SECONDS = 12
ROW_H = 40
BLUE = "#1f4e8c"
COLS = ["Use", "Shimmer", "Where", "Bluetooth", "Firmware", "Status", ""]
TIPS = {
    "refresh": "Read again which Shimmers are paired with this computer and, if a base is connected, which are "
               "docked in it.",
    "setup": "For every ticked Shimmer in the base: pair it over Bluetooth (the PIN 1234 is entered for you), test "
             "the connection and set its clock. Progress and results appear in the Status column.",
    "test": "Briefly connect to the ticked (or highlighted) Shimmers and read their firmware, sampling rate, sensors "
            "and battery. Nothing is changed on the Shimmer.",
    "forget": "Remove this computer's Bluetooth pairing with the ticked (or highlighted) Shimmers. Docked Shimmers "
              "can be paired again with one click on 'Set up docked Shimmers'.",
    "live": "Open the live view and start streaming from the ticked Shimmers.",
    "sd": "Make the ticked Shimmers record on their own SD cards (no Bluetooth while recording, so more than 7 "
          "at once). Take them out of the base first. You mark keypoints and stop them from this app.",
    "sd_open": "Open the SD recording in progress: keypoints, status, stop and import.",
    "settings": "Firmware information and device configuration: sampling rate, sensors, ECG gains, GSR range, name.",
    "scan": f"Search for Shimmers nearby over Bluetooth for {SCAN_SECONDS} s. Use this for Shimmers that are not in "
            f"a base.",
    "pair": "Pair this Shimmer with this computer (the PIN 1234 is entered for you), then test the connection.",
    "others": "Also list Bluetooth devices that are not Shimmers (headphones, phones, …).",
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
    if chk.get("ok"):
        batt = chk.get("battery_pct")
        b = f" · battery {batt:.0f}%" if batt is not None else ""
        color = GOOD if batt is None or batt >= 20 else WARNING
        return (f"✔ {chk.get('kind', '')} · {chk.get('rate_hz', 0):.0f} Hz · {chk.get('firmware', '')}{b}"
                f" · tested {when}"), color
    return f"✖ {chk.get('error', 'failed')} · tested {when}", CRITICAL


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


class QtLogHandler(logging.Handler):
    def __init__(self, widget: QPlainTextEdit):
        super().__init__(logging.INFO)
        self.widget = widget
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))

    def emit(self, record):
        ui_call(self.widget.appendPlainText, self.format(record))


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
        self.found: list[BtDevice] = []
        self.rows: list[Row] = []
        self._sd_macs = set()           # Shimmers recording on their SD card (sdlog session)
        self.live_blocked = False
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
        card = Card("Your Shimmers", "Tick the ones to stream. Shimmers in the base show their slot; "
                                     "the ones not yet paired can be set up from the base with one click.")
        self.btn_refresh = _button("Refresh", TIPS["refresh"], "small")
        self.btn_refresh.clicked.connect(self._refresh_all)
        card.actions.addWidget(self.btn_refresh, 0, Qt.AlignTop)
        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.verticalHeader().setVisible(False)
        style_table(self.table, ROW_H)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setWordWrap(False)
        hh = self.table.horizontalHeader()
        for i, mode in enumerate((QHeaderView.ResizeToContents,) * 5 + (QHeaderView.Stretch, QHeaderView.ResizeToContents)):
            hh.setSectionResizeMode(i, mode)
        for i, tip in enumerate(("Tick to stream this Shimmer in live view", "Bluetooth name (hover for its address)",
                                 "Base slot, if the Shimmer is docked", "Whether this computer is paired with it",
                                 "LogAndStream version. ⬆ old: update recommended · ↑ a newer version exists "
                                 "(hover a version for details)",
                                 "Last test, or setup progress", "Device settings")):
            hh.model().setHeaderData(i, Qt.Horizontal, tip, Qt.ToolTipRole)
        self.table.itemChanged.connect(self._on_check_changed)
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
        self.btn_test = _button("Test connection", TIPS["test"])
        self.btn_test.clicked.connect(self._test_selected)
        self.btn_forget = _button("Forget pairing", TIPS["forget"])
        self.btn_forget.clicked.connect(self._forget_selected)
        self.btn_live = _button("Show live data  ▶", TIPS["live"], "primary")
        self.btn_live.clicked.connect(self._go_live)
        for b in (self.btn_setup, self.btn_test, self.btn_forget):
            acts.addWidget(b)
        acts.addStretch(1)
        self.btn_sd = _button("Record on SD cards…", TIPS["sd"])
        self.btn_sd.clicked.connect(self._sd_clicked)
        acts.addWidget(self.btn_sd)
        acts.addWidget(self.btn_live)
        card.body.addLayout(acts)
        lv.addWidget(card, 1)

        # ---- Bluetooth scan for Shimmers that are not docked ----
        scan = Card("Add a Shimmer that is not in a base",
                    "Switch it on (blue LED blinking briefly every 2 s), keep it within 1–2 m and scan.")
        r2 = QHBoxLayout()
        self.btn_scan = _button(f"Scan for Shimmers ({SCAN_SECONDS} s)", TIPS["scan"])
        self.btn_scan.clicked.connect(self._scan)
        self.scan_progress = QProgressBar()
        self.scan_progress.setRange(0, 0)
        self.scan_progress.setMaximumWidth(140)
        self.scan_progress.setVisible(False)
        self.scan_label = set_role(QLabel(""), "muted")
        self.only_shimmer = QCheckBox("Show other Bluetooth devices")
        self.only_shimmer.setToolTip(TIPS["others"])
        self.only_shimmer.toggled.connect(self._render_all)
        r2.addWidget(self.btn_scan)
        r2.addWidget(self.scan_progress)
        r2.addWidget(self.scan_label, 1)
        r2.addWidget(self.only_shimmer)
        scan.body.addLayout(r2)
        self.tbl_found = QTableWidget(0, 4)
        self.tbl_found.setHorizontalHeaderLabels(["Found nearby", "Address", "Signal", ""])
        self.tbl_found.verticalHeader().setVisible(False)
        style_table(self.tbl_found, ROW_H)
        self.tbl_found.setMinimumHeight(ROW_H * 2 + 6)
        self.tbl_found.setMaximumHeight(ROW_H * 4 + 6)        # the Shimmers list gets the room
        self.tbl_found.setEditTriggers(QTableWidget.NoEditTriggers)
        fh = self.tbl_found.horizontalHeader()
        fh.setSectionResizeMode(0, QHeaderView.Stretch)
        for i in (1, 2, 3):
            fh.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        self.tbl_found.setVisible(False)                     # shown once a scan has been started
        scan.body.addWidget(self.tbl_found)
        self.pair_status = QLabel("")
        self.pair_status.setWordWrap(True)
        self.pair_status.setVisible(False)
        scan.body.addWidget(self.pair_status)
        lv.addWidget(scan, 0)
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
        split.setSizes([860, 520])

        if not backend:
            for b in (self.btn_scan, self.btn_forget, self.btn_power, self.btn_setup):
                b.setEnabled(False)
        self._render_all()

    # ---------------- data ----------------
    def _refresh_all(self):
        """Refresh button: look for the base and read its slots again, and re-read the paired list."""
        self.base.rescan()
        self.refresh()

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

    def _visible(self, devs):
        return [d for d in devs if d.looks_like_shimmer or self.only_shimmer.isChecked()]

    def _build_rows(self):
        rows = {d.address: Row(d.address, d.name, True, device=d,
                               firmware=(self.cfg.device(d.address).get("last_check") or {}).get("firmware", ""))
                for d in self._visible(self.paired)}
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
        return sorted(rows.values(), key=lambda r: (r.slot == 0, order.get(r.mac or f"{r.base}/{r.slot}", 0),
                                                    r.name.lower()))

    def _status_of(self, r: Row):
        if r.reading:
            return "reading it over USB…", WARNING
        if r.mac in self._sd_macs:
            if r.slot:
                return "⚠ docked while recording to SD: open the SD recording page", CRITICAL
            return f"● recording on its SD card (session {sdlog.active(self.cfg)['name']})", CRITICAL
        if r.mac in self.busy:
            return self.busy[r.mac], BLUE
        res = self.base.result(r.base, r.slot) if r.slot else None
        if res:
            return res[1], {"busy": BLUE, dock_setup.READY: GOOD, dock_setup.PROBLEM: CRITICAL}.get(res[0], MUTED)
        if r.docked_error:
            return "could not read it over USB: push it back into the slot", CRITICAL
        if r.slot and not r.can_stream:
            return f"⚠ {r.firmware} firmware: cannot stream over Bluetooth (install LogAndStream with Consensys)", \
                CRITICAL
        if not r.paired:
            return "not paired yet: tick it, then press 'Set up docked Shimmers'", MUTED
        return describe_check(self.cfg.device(r.mac).get("last_check"))

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
                               else "No Shimmers yet.\nPut them in the Shimmer Base and switch them on: they appear "
                                    "here within a few seconds.\nShimmers that are not in a base: use "
                                    "'Scan for Shimmers' below.")
        self.table.setVisible(bool(self.rows))
        self.empty.setVisible(not self.rows)
        self._render_table()
        self._render_found()

    def _render_table(self):
        selected = set(self.cfg.data.get("selected", []))
        self._sd_macs = sdlog.recording_macs(self.cfg)
        t = self.table
        t.blockSignals(True)
        t.setRowCount(len(self.rows))
        for i, r in enumerate(self.rows):
            # paired: tick to stream it; docked but not paired yet: tick to include it in the setup
            usable = bool(r.mac) and r.can_stream and (r.paired or bool(r.slot)) and r.mac not in self._sd_macs
            chk = QTableWidgetItem()
            chk.setFlags((Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable) if usable
                         else Qt.ItemIsSelectable)
            chk.setCheckState(Qt.Checked if usable and r.mac in selected else Qt.Unchecked)
            chk.setData(Qt.UserRole, r.mac)
            chk.setToolTip("Tick to stream it in live view" + (" (and to include it in 'Set up docked "
                                                                 "Shimmers')" if r.slot else "")
                           if r.paired else
                           "Tick to pair it with 'Set up docked Shimmers'" if usable else
                           "Its firmware cannot stream" if r.mac else "Still reading it over USB")
            t.setItem(i, 0, chk)
            name = QTableWidgetItem(r.name)
            name.setToolTip(f"{r.name}\nBluetooth address {r.mac}" + (f"\nfirmware {r.firmware}" if r.firmware else ""))
            t.setItem(i, 1, name)
            unknown = not r.slot and (self.base.state == "checking" or self.base.reading)
            where = QTableWidgetItem(r.where if r.slot else "checking…" if unknown else "not docked")
            where.setForeground(QColor("#1f1f1e" if r.slot else WARNING if unknown else MUTED))
            if unknown:
                where.setToolTip("Still reading the base: this Shimmer may be docked")
            t.setItem(i, 2, where)
            bt = QTableWidgetItem("paired" if r.paired else "not paired")
            bt.setForeground(QColor(GOOD if r.paired else MUTED))
            t.setItem(i, 3, bt)
            level, short, why = fwmod.assess(r.firmware)
            fw = QTableWidgetItem(short)
            fw.setForeground(QColor({fwmod.OLD: WARNING, fwmod.NOT_STREAMING: CRITICAL,
                                     fwmod.NEWER_EXISTS: MUTED, fwmod.UNKNOWN: MUTED}.get(level, "#1f1f1e")))
            fw.setToolTip(f"{r.firmware}\n\n{why}" if r.firmware else why)
            t.setItem(i, 4, fw)
            text, color = self._status_of(r)
            st = QTableWidgetItem(text)
            st.setForeground(QColor(color))
            st.setToolTip(text)
            t.setItem(i, 5, st)
            if r.paired:
                gear = _button("⚙ Settings", TIPS["settings"], "small")
                gear.setEnabled(r.mac not in self.busy and not self.base.setting_up and r.mac not in self._sd_macs)
                gear.clicked.connect(lambda _=False, mac=r.mac, nm=r.name: self._settings(mac, nm))
                t.setCellWidget(i, 6, gear)
            else:
                t.removeCellWidget(i, 6)
                t.setItem(i, 6, QTableWidgetItem(""))
        t.blockSignals(False)
        self._update_buttons()

    def _render_found(self):
        known = {r.mac for r in self.rows}
        devs = [d for d in self._visible(self.found) if d.address not in known]
        t = self.tbl_found
        t.setRowCount(len(devs))
        for i, d in enumerate(sorted(devs, key=lambda d: -(d.rssi or -999))):
            t.setItem(i, 0, QTableWidgetItem(d.name or "(no name)"))
            t.setItem(i, 1, QTableWidgetItem(d.address))
            t.setItem(i, 2, QTableWidgetItem(self._signal_text(d.rssi)))
            b = _button("Pair", TIPS["pair"], "small")
            b.setEnabled(d.address not in self.busy)
            b.clicked.connect(lambda _=False, dev=d: self._pair(dev))
            t.setCellWidget(i, 3, b)

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
        """Ticked Shimmers that are paired (ticked unpaired ones are only waiting for the docked setup)."""
        return [(r.mac, r.name) for r in self._checked_rows() if r.paired]

    def _checked_slots(self):
        return [(r.base, r.slot) for r in self._checked_rows() if r.slot]

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
            self.cfg.data["selected"] = [r.mac for r in self._checked_rows()]
            self.cfg.save()
            self._update_buttons()

    def _update_buttons(self):
        sel = self.checked_devices()
        busy = any(m in self.busy for m, _ in sel) or self.base.setting_up
        self.btn_live.setEnabled(bool(sel) and not busy and not self.live_blocked)
        self.btn_live.setText(f"Show live data ({len(sel)})  ▶" if sel else "Show live data  ▶")
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
    def _test_selected(self):
        devs = self._target_devices()
        if not devs:
            QMessageBox.information(self, "Test connection", "Tick or highlight a paired Shimmer first.")
            return
        for mac, name in devs:
            self._test(mac, name)

    def _test(self, mac, name, then=None):
        if mac in self.busy:
            return
        self.busy[mac] = "testing…"
        self._render_table()

        def progress(text):
            self.busy[mac] = f"testing… {text}"
            ui_call(self._render_table)

        def done(res):
            self.busy.pop(mac, None)
            self._render_table()
            log.info("%s test: %s", name, describe_check(res)[0])
            if then:
                then(res)

        run_bg(lambda: probe_device(mac, name, self.cfg, progress=progress), done,
               lambda e: done({"ok": False, "error": str(e), "when": ""}), f"test-{name}")

    def _settings(self, mac, name):
        self.busy[mac] = "settings open"
        self._render_table()
        dlg = DeviceSettingsDialog(self, mac, name, self.cfg)
        dlg.exec()
        self.busy.pop(mac, None)
        self._render_table()
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

    def _scan(self):
        self.btn_scan.setEnabled(False)
        self.scan_progress.setVisible(True)
        self.scan_label.setText("scanning… keep the Shimmer switched on and close by")
        self.tbl_found.setVisible(True)
        self.found = []
        self._render_found()

        def done(devs):
            self._set_found(devs)
            self.btn_scan.setEnabled(True)
            self.scan_progress.setVisible(False)
            n = len([d for d in devs if d.looks_like_shimmer])
            self.scan_label.setText(f"found {n} Shimmer(s)" + ("" if n else " — not there? see the Guide tab"))

        def fail(e):
            self.btn_scan.setEnabled(True)
            self.scan_progress.setVisible(False)
            self.scan_label.setText(f"scan failed: {e}")

        run_bg(lambda: self.backend.discover(SCAN_SECONDS, lambda devs: ui_call(self._set_found, devs)), done, fail,
               "scan")

    def _set_found(self, devs):
        self.found = devs
        self._render_found()

    def _sd_clicked(self):
        if sdlog.active(self.cfg):
            self.open_sd.emit()
            return
        devs = self.checked_devices()
        docked = [r.name for r in self.rows if r.slot and r.mac in {m for m, _ in devs}]
        if docked and QMessageBox.question(
                self, "Record on SD cards", f"{', '.join(docked)} {'is' if len(docked) == 1 else 'are'} in the base: "
                "a docked Shimmer does not record. Take them out (switched on), then continue.\n\n"
                "Continue anyway?") != QMessageBox.Yes:
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
        self.busy[dev.address] = "pairing"
        self._render_found()

        def status(text, color=BLUE):
            self.pair_status.setVisible(True)
            self.pair_status.setText(text)
            self.pair_status.setStyleSheet(f"color: {color}; font-weight: 700;")

        def work():
            provide = dock_setup.auto_pin(self._ask_pin)
            for attempt in range(1, dock_setup.PAIR_ATTEMPTS + 1):
                ui_call(status, f"Pairing {dev.name} — attempt {attempt}/{dock_setup.PAIR_ATTEMPTS}. "
                                f"Keep it switched on and close to the computer…")
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
            status(f"✔ {dev.name} paired. Testing the connection…", GOOD)
            self.busy.pop(dev.address, None)
            self.cfg.data.setdefault("selected", [])
            if dev.address not in self.cfg.data["selected"]:
                self.cfg.data["selected"].append(dev.address)
            self.cfg.save()
            self.found = [d for d in self.found if d.address != dev.address]
            self.paired.append(BtDevice(dev.address, dev.name, True, platform_id=dev.platform_id))
            self._render_all()
            self._test(dev.address, dev.name, then=tested)
            QTimer.singleShot(1000, self.refresh)

        def tested(res):
            if res.get("ok"):
                status(f"✔ {dev.name} paired and working: {describe_check(res)[0]}", GOOD)
            else:
                status(f"{dev.name} is paired, but the test connection failed: {res.get('error')}", WARNING)

        def failed(e):
            self.busy.pop(dev.address, None)
            self._render_found()
            status(f"✖ Pairing {dev.name} failed: {e}", CRITICAL)
            QMessageBox.warning(self, "Pairing failed", f"{dev.name}: {e}\n\n{getattr(e, 'hint', '')}".strip())

        run_bg(work, paired, failed, f"pair-{dev.name}")

    def showEvent(self, ev):
        super().showEvent(ev)
        self.base.start()                    # watch USB for a base only while this page is visible

    def hideEvent(self, ev):
        self.base.stop()
        super().hideEvent(ev)

    def _go_live(self):
        devs = self.checked_devices()
        if devs:
            self.show_live.emit(devs)
