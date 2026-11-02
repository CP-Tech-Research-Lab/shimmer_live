"""Device management page: paired devices, discovery + pairing, guide, log."""
import concurrent.futures
import datetime as dt
import logging
import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                               QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
                               QSplitter, QTableWidget, QTableWidgetItem, QTabWidget,
                               QTextBrowser, QVBoxLayout, QWidget)

from ..bt import BtDevice, PairingError
from ..session import probe_device
from .common import CRITICAL, FONT_M, GOOD, MUTED, WARNING, Banner, run_bg, set_role, style_table, ui_call
from .device_settings import DeviceSettingsDialog
from .dialogs import PinDialog
from .guide import GUIDE_HTML
from .icons import logo_pixmap
from .keypoints_editor import KeypointsEditor

log = logging.getLogger("shimmerlive.devices")

PAIR_ATTEMPTS = 3
SCAN_SECONDS = 12


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


class QtLogHandler(logging.Handler):
    def __init__(self, widget: QPlainTextEdit):
        super().__init__(logging.INFO)
        self.widget = widget
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))

    def emit(self, record):
        msg = self.format(record)
        ui_call(self.widget.appendPlainText, msg)


class DevicesPage(QWidget):
    show_live = Signal(list)          # [(mac, name), ...]
    show_plugins = Signal()

    def __init__(self, cfg, backend, backend_error):
        super().__init__()
        self.cfg, self.backend = cfg, backend
        self.busy = {}                 # mac -> text (testing / pairing)
        self.paired: list[BtDevice] = []
        self.found: list[BtDevice] = []
        self.live_blocked = False

        root = QHBoxLayout(self)
        split = QSplitter(Qt.Horizontal)
        root.addWidget(split)

        # ---------------- left: devices ----------------
        left = QWidget()
        lv = QVBoxLayout(left)
        head = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(logo_pixmap(46))
        head.addWidget(logo)
        title = QLabel("Devices")
        set_role(title, "title")
        head.addWidget(title)
        head.addStretch(1)
        self.btn_plugins = QPushButton("Plugins…")
        self.btn_plugins.setToolTip("Choose plugins (e.g. real-time HRV) that run in live view")
        self.btn_plugins.clicked.connect(self.show_plugins.emit)
        head.addWidget(self.btn_plugins)
        lv.addLayout(head)

        self.adapter_row = QHBoxLayout()
        self.adapter_label = QLabel("checking Bluetooth…")
        set_role(self.adapter_label, "muted")
        self.btn_power = QPushButton("Turn Bluetooth on")
        self.btn_power.setVisible(False)
        self.btn_power.clicked.connect(self._power_on)
        self.adapter_row.addWidget(self.adapter_label, 1)
        self.adapter_row.addWidget(self.btn_power)
        lv.addLayout(self.adapter_row)
        if backend_error:
            lv.addWidget(Banner("critical", f"Bluetooth pairing unavailable: {backend_error}. "
                                            f"Previously used devices can still be streamed."))

        self.only_shimmer = QCheckBox("Show only Shimmer devices")
        self.only_shimmer.setChecked(True)
        self.only_shimmer.toggled.connect(self._render_all)
        lv.addWidget(self.only_shimmer)

        # paired
        g1 = QGroupBox("Paired devices — tick the ones to use")
        g1v = QVBoxLayout(g1)
        self.tbl_paired = QTableWidget(0, 5)
        self.tbl_paired.setHorizontalHeaderLabels(["Use", "Name", "Address", "Status / last test", ""])
        self.tbl_paired.verticalHeader().setVisible(False)
        style_table(self.tbl_paired, 42)
        self.tbl_paired.setSelectionBehavior(QTableWidget.SelectRows)
        self.tbl_paired.setEditTriggers(QTableWidget.NoEditTriggers)
        hh = self.tbl_paired.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        hh.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.tbl_paired.itemChanged.connect(self._on_check_changed)
        g1v.addWidget(self.tbl_paired)
        r = QHBoxLayout()
        self.btn_refresh = QPushButton("Refresh")
        self.btn_refresh.clicked.connect(self.refresh)
        self.btn_test = QPushButton("Test connection")
        self.btn_test.setToolTip("Connect to the highlighted/ticked devices and read firmware, sensors and battery")
        self.btn_test.clicked.connect(self._test_selected)
        self.btn_forget = QPushButton("Forget (unpair)")
        self.btn_forget.clicked.connect(self._forget_selected)
        self.btn_live = QPushButton("Show live data  ▶")
        set_role(self.btn_live, "primary")
        self.btn_live.clicked.connect(self._go_live)
        for b in (self.btn_refresh, self.btn_test, self.btn_forget):
            r.addWidget(b)
        r.addStretch(1)
        r.addWidget(self.btn_live)
        g1v.addLayout(r)
        lv.addWidget(g1, 3)

        # discovery
        g2 = QGroupBox("Add a new Shimmer")
        g2v = QVBoxLayout(g2)
        hint = QLabel("Switch the Shimmer on (blue LED blinking briefly every 2 s), keep it within 1–2 m, then scan. "
                      "See the Guide tab if it does not show up.")
        hint.setWordWrap(True)
        set_role(hint, "muted")
        g2v.addWidget(hint)
        r2 = QHBoxLayout()
        self.btn_scan = QPushButton(f"Scan for devices ({SCAN_SECONDS} s)")
        self.btn_scan.clicked.connect(self._scan)
        self.scan_progress = QProgressBar()
        self.scan_progress.setRange(0, 0)
        self.scan_progress.setMaximumWidth(160)
        self.scan_progress.setVisible(False)
        self.scan_label = QLabel("")
        set_role(self.scan_label, "muted")
        r2.addWidget(self.btn_scan)
        r2.addWidget(self.scan_progress)
        r2.addWidget(self.scan_label, 1)
        g2v.addLayout(r2)
        self.tbl_found = QTableWidget(0, 4)
        self.tbl_found.setHorizontalHeaderLabels(["Name", "Address", "Signal", ""])
        self.tbl_found.verticalHeader().setVisible(False)
        style_table(self.tbl_found, 42)
        self.tbl_found.setEditTriggers(QTableWidget.NoEditTriggers)
        fh = self.tbl_found.horizontalHeader()
        fh.setSectionResizeMode(0, QHeaderView.Stretch)
        for i in (1, 2, 3):
            fh.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        g2v.addWidget(self.tbl_found)
        self.pair_status = QLabel("")
        self.pair_status.setWordWrap(True)
        g2v.addWidget(self.pair_status)
        lv.addWidget(g2, 2)
        split.addWidget(left)

        # ---------------- right: guide + log ----------------
        tabs = QTabWidget()
        guide = QTextBrowser()
        guide.setOpenExternalLinks(True)
        guide.setHtml(GUIDE_HTML)
        tabs.addTab(guide, "Guide")
        self.keypoints = KeypointsEditor(cfg)
        tabs.addTab(self.keypoints, "Keypoints")
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        tabs.addTab(self.log_view, "Log")
        logging.getLogger("shimmerlive").addHandler(QtLogHandler(self.log_view))
        split.addWidget(tabs)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([840, 540])

        if not backend:
            self.btn_scan.setEnabled(False)
            self.btn_forget.setEnabled(False)
            self.btn_power.setEnabled(False)

    # ---------------- data ----------------
    def refresh(self):
        if not self.backend:
            self.adapter_label.setText("Bluetooth backend unavailable")
            self.paired = [BtDevice(mac, d.get("name", mac), True) for mac, d in self.cfg.data["devices"].items()]
            self._render_all()
            return
        self.btn_refresh.setEnabled(False)

        def work():
            return self.backend.adapter_status(), self.backend.list_paired()

        def done(res):
            (ok, text), paired = res
            self.btn_refresh.setEnabled(True)
            self.adapter_label.setText(("✔ " if ok else "✖ ") + text)
            self.adapter_label.setStyleSheet(f"color: {GOOD if ok else CRITICAL}; font-weight: 700; font-size: {FONT_M}pt;")
            self.btn_power.setVisible(not ok)
            self.paired = paired
            self._render_all()

        def fail(e):
            self.btn_refresh.setEnabled(True)
            self.adapter_label.setText(f"✖ Bluetooth error: {e}")
            self.adapter_label.setStyleSheet(f"color: {CRITICAL}; font-weight: 700; font-size: {FONT_M}pt;")

        run_bg(work, done, fail, "refresh")

    def _power_on(self):
        run_bg(self.backend.power_on, lambda _: self.refresh(),
               lambda e: QMessageBox.warning(self, "Bluetooth", f"Could not switch Bluetooth on:\n{e}"))

    def _visible(self, devs):
        return [d for d in devs if d.looks_like_shimmer or not self.only_shimmer.isChecked()]

    def _render_all(self):
        self._render_paired()
        self._render_found()

    def _render_paired(self):
        selected = set(self.cfg.data.get("selected", []))
        t = self.tbl_paired
        t.blockSignals(True)
        devs = self._visible(self.paired)
        t.setRowCount(len(devs))
        for r, d in enumerate(devs):
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            chk.setCheckState(Qt.Checked if d.address in selected else Qt.Unchecked)
            chk.setData(Qt.UserRole, d.address)
            t.setItem(r, 0, chk)
            t.setItem(r, 1, QTableWidgetItem(d.name))
            t.setItem(r, 2, QTableWidgetItem(d.address))
            if d.address in self.busy:
                text, color = self.busy[d.address], "#1f4e8c"
            else:
                text, color = describe_check(self.cfg.device(d.address).get("last_check"))
            it = QTableWidgetItem(text)
            it.setForeground(QColor(color))
            it.setToolTip(text)
            t.setItem(r, 3, it)
            gear = set_role(QPushButton("⚙ Settings"), "small")
            gear.setToolTip("Firmware info and device configuration (sampling rate, sensors, gains, name)")
            gear.setEnabled(d.address not in self.busy)
            gear.clicked.connect(lambda _=False, mac=d.address, name=d.name: self._settings(mac, name))
            t.setCellWidget(r, 4, gear)
        t.blockSignals(False)
        self._update_buttons()

    def _render_found(self):
        paired = {d.address for d in self.paired}
        devs = [d for d in self._visible(self.found) if d.address not in paired]
        t = self.tbl_found
        t.setRowCount(len(devs))
        for r, d in enumerate(sorted(devs, key=lambda d: -(d.rssi or -999))):
            t.setItem(r, 0, QTableWidgetItem(d.name or "(no name)"))
            t.setItem(r, 1, QTableWidgetItem(d.address))
            t.setItem(r, 2, QTableWidgetItem(self._signal_text(d.rssi)))
            b = QPushButton("Pair")
            b.setEnabled(d.address not in self.busy)
            b.clicked.connect(lambda _=False, dev=d: self._pair(dev))
            t.setCellWidget(r, 3, b)

    @staticmethod
    def _signal_text(rssi):
        if rssi is None:
            return "—"
        q = "strong" if rssi > -65 else "ok" if rssi > -80 else "weak – move closer"
        return f"{rssi} dBm ({q})"

    def checked_devices(self):
        out = []
        for r in range(self.tbl_paired.rowCount()):
            it = self.tbl_paired.item(r, 0)
            if it.checkState() == Qt.Checked:
                out.append((it.data(Qt.UserRole), self.tbl_paired.item(r, 1).text()))
        return out

    def _target_devices(self):
        """Ticked devices, or the highlighted rows if nothing is ticked."""
        devs = self.checked_devices()
        if devs:
            return devs
        rows = {i.row() for i in self.tbl_paired.selectedItems()}
        return [(self.tbl_paired.item(r, 2).text(), self.tbl_paired.item(r, 1).text()) for r in sorted(rows)]

    def _on_check_changed(self, item):
        if item.column() == 0:
            self.cfg.data["selected"] = [m for m, _ in self.checked_devices()]
            self.cfg.save()
            self._update_buttons()

    def _update_buttons(self):
        sel = self.checked_devices()
        busy = any(m in self.busy for m, _ in sel)
        self.btn_live.setEnabled(bool(sel) and not busy and not self.live_blocked)
        self.btn_live.setText(f"Show live data ({len(sel)})  ▶" if sel else "Show live data  ▶")

    def set_live_blocked(self, blocked: bool, why: str = ""):
        self.live_blocked = blocked
        self.btn_live.setToolTip(why)
        self._update_buttons()

    # ---------------- actions ----------------
    def _test_selected(self):
        devs = self._target_devices()
        if not devs:
            QMessageBox.information(self, "Test connection", "Tick or highlight a device first.")
            return
        for mac, name in devs:
            self._test(mac, name)

    def _test(self, mac, name, then=None):
        if mac in self.busy:
            return
        self.busy[mac] = "testing…"
        self._render_paired()

        def progress(text):
            self.busy[mac] = f"testing… {text}"
            ui_call(self._render_paired)

        def done(res):
            self.busy.pop(mac, None)
            self._render_paired()
            log.info("%s test: %s", name, describe_check(res)[0])
            if then:
                then(res)

        run_bg(lambda: probe_device(mac, name, self.cfg, progress=progress), done,
               lambda e: done({"ok": False, "error": str(e), "when": ""}), f"test-{name}")

    def _settings(self, mac, name):
        self.busy[mac] = "settings open"
        self._render_paired()
        dlg = DeviceSettingsDialog(self, mac, name, self.cfg)
        dlg.exec()
        self.busy.pop(mac, None)
        self._render_paired()
        if dlg.applied:                        # refresh the row with what the device now reports
            QTimer.singleShot(1500, lambda: self._test(mac, name))

    def _forget_selected(self):
        devs = self._target_devices()
        if not devs:
            QMessageBox.information(self, "Forget", "Tick or highlight a device first.")
            return
        names = ", ".join(n for _, n in devs)
        if QMessageBox.question(self, "Forget devices",
                                f"Remove the pairing of {names}?\nYou will need to scan and pair again to use it.") \
                != QMessageBox.Yes:
            return
        by_mac = {d.address: d for d in self.paired}

        def work():
            for mac, _ in devs:
                self.backend.forget(by_mac.get(mac, BtDevice(mac, mac, True)))

        run_bg(work, lambda _: self.refresh(),
               lambda e: QMessageBox.warning(self, "Forget", f"Could not remove pairing:\n{e}"))

    def _scan(self):
        self.btn_scan.setEnabled(False)
        self.scan_progress.setVisible(True)
        self.scan_label.setText("scanning… keep the Shimmer switched on and close by")
        self.found = []
        self._render_found()

        def on_update(devs):
            ui_call(self._set_found, devs)

        def done(devs):
            self._set_found(devs)
            self.btn_scan.setEnabled(True)
            self.scan_progress.setVisible(False)
            n = len([d for d in devs if d.looks_like_shimmer])
            self.scan_label.setText(f"scan finished: {len(devs)} device(s), {n} Shimmer(s)" +
                                    ("" if n else " — not there? see the Guide tab"))

        def fail(e):
            self.btn_scan.setEnabled(True)
            self.scan_progress.setVisible(False)
            self.scan_label.setText(f"scan failed: {e}")

        run_bg(lambda: self.backend.discover(SCAN_SECONDS, on_update), done, fail, "scan")

    def _set_found(self, devs):
        self.found = devs
        self._render_found()

    def _pin_provider(self, device_name):
        fut = concurrent.futures.Future()
        ui_call(lambda: fut.set_result(PinDialog.ask(self, device_name)))
        return fut.result(timeout=180)

    def _pair(self, dev: BtDevice):
        self.busy[dev.address] = "pairing"
        self._render_found()

        def status(text, color="#1f4e8c"):
            self.pair_status.setText(text)
            self.pair_status.setStyleSheet(f"color: {color}; font-weight: 700; font-size: {FONT_M}pt;")

        def work():
            for attempt in range(1, PAIR_ATTEMPTS + 1):
                ui_call(status, f"Pairing {dev.name} — attempt {attempt}/{PAIR_ATTEMPTS}. "
                                f"Keep it switched on and close to the computer…")
                try:
                    self.backend.pair(dev, self._pin_provider)
                    return attempt
                except PairingError as e:
                    log.info("pairing %s attempt %d: %s", dev.name, attempt, e)
                    if not e.retryable or attempt == PAIR_ATTEMPTS:
                        raise
                    time.sleep(2.0)

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
            self._test(dev.address, dev.name, then=lambda res: tested(res))
            QTimer.singleShot(1000, self.refresh)

        def tested(res):
            if res.get("ok"):
                status(f"✔ {dev.name} paired and working: {describe_check(res)[0]}", GOOD)
            else:
                status(f"{dev.name} is paired, but the test connection failed: {res.get('error')}", WARNING)
                QMessageBox.warning(self, "Paired, but not responding",
                                    f"{dev.name} was paired but did not answer the test connection:\n"
                                    f"{res.get('error')}\n\nSwitch it off and on, keep it close, then press "
                                    f"'Test connection'. See the Guide tab for LED meanings.")

        def failed(e):
            self.busy.pop(dev.address, None)
            self._render_found()
            msg = str(e)
            hint = getattr(e, "hint", "")
            status(f"✖ Pairing {dev.name} failed: {msg}", CRITICAL)
            QMessageBox.warning(self, "Pairing failed", f"{dev.name}: {msg}\n\n{hint}".strip())

        run_bg(work, paired, failed, f"pair-{dev.name}")

    def _go_live(self):
        devs = self.checked_devices()
        if devs:
            self.show_live.emit(devs)
