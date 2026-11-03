"""'Docked in base' panel: watches for a Shimmer Base/Dock on USB, shows what is in each slot,
and sets up every docked Shimmer (pairing, clock, connection test) with one click."""
import logging

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QHBoxLayout, QHeaderView, QLabel, QPushButton, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from .. import base as basemod
from .. import dock_setup
from .common import CRITICAL, GOOD, MUTED, run_bg, set_role, style_table, ui_call

log = logging.getLogger("shimmerlive.ui.base")

POLL_MS = 3000
COLS = ["Slot", "Shimmer", "Status"]


class BasePanel(QWidget):
    docked_changed = Signal(int)          # number of docked Shimmers (-1 = no base connected)
    setup_finished = Signal()

    def __init__(self, cfg, backend, ask_pin):
        super().__init__()
        self.cfg, self.backend, self.ask_pin = cfg, backend, ask_pin
        self.device = None                # basemod.BaseDevice
        self.link = None
        self.slots = []                   # [SlotInfo]
        self.results = {}                 # slot -> (status, text)
        self.paired = set()
        self._occupancy = None
        self._busy = False

        lay = QVBoxLayout(self)
        self.status = QLabel()
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.NoSelection)
        style_table(self.table)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.setWordWrap(True)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        lay.addWidget(self.table, 1)
        row = QHBoxLayout()
        self.btn_setup = set_role(QPushButton("Set up docked Shimmers"), "primary")
        self.btn_setup.setToolTip("For every docked Shimmer: pair it over Bluetooth (PIN 1234 is entered for you), "
                                  "set its clock and test the connection")
        self.btn_setup.clicked.connect(self.setup_all)
        self.btn_read = QPushButton("Read slots again")
        self.btn_read.clicked.connect(lambda: self._read_slots(force=True))
        row.addWidget(self.btn_read)
        row.addStretch(1)
        row.addWidget(self.btn_setup)
        lay.addLayout(row)
        self.timer = QTimer(self, interval=POLL_MS, timeout=self._poll)
        self._render()

    # ---------------- watching the USB ----------------
    def start(self):
        self.timer.start()
        self._poll()

    def stop(self):
        self.timer.stop()

    def set_paired(self, addresses):
        self.paired = set(addresses)
        self._render()

    def _poll(self):
        if self._busy:
            return
        self._busy = True

        def work():
            devs = basemod.detect(self.cfg)
            dev = devs[0] if devs else None
            occ = None
            if dev is not None:
                link = self.link if self.device and self.device.serial == dev.serial else basemod.BaseLink(dev)
                occ = link.occupancy()
                return dev, link, occ
            return None, None, None

        def done(res):
            self._busy = False
            dev, link, occ = res
            if dev is None:
                if self.device is not None:
                    log.info("%s disconnected", self.device.label)
                    if self.link:
                        self.link.close()
                self.device, self.link, self.slots, self._occupancy = None, None, [], None
                self.docked_changed.emit(-1)
                self._render()
                return
            if self.device is None or dev.serial != self.device.serial:
                log.info("%s connected", dev.label)
                self.results = {}
            self.device, self.link = dev, link
            if occ != self._occupancy:
                self._occupancy = occ
                self._read_slots()
            self._render()

        def failed(e):
            self._busy = False
            log.info("base poll failed: %s", e)

        run_bg(work, done, failed, "base-poll")

    def _read_slots(self, force=False):
        if self.link is None or (self._busy and not force):
            return
        self._busy = True
        self.status.setText("Reading the docked Shimmers over USB…")

        def done(infos):
            self._busy = False
            known = {s.slot: s.mac for s in self.slots}
            self.slots = infos
            for s in infos:                                  # a different Shimmer in a slot: old result is stale
                if known.get(s.slot) != s.mac:
                    self.results.pop(s.slot, None)
            self.docked_changed.emit(sum(s.occupied for s in infos))
            self._render()

        def failed(e):
            self._busy = False
            self.status.setText(f"Could not read the base: {e}")

        run_bg(lambda: self.link.read_all(), done, failed, "base-read")

    # ---------------- one-click setup ----------------
    def setup_all(self):
        if self.link is None or self._busy or not self.backend:
            return
        targets = [s for s in self.slots if s.occupied]
        if not targets:
            return
        self._busy = True
        self.btn_setup.setEnabled(False)
        self.btn_read.setEnabled(False)
        for s in targets:
            self.results[s.slot] = ("busy", "waiting…")
        self._render()

        def on_progress(slot, text):
            ui_call(self._set_result, slot, "busy", text)

        def on_result(res):
            ui_call(self._set_result, res.slot, res.status, res.message)

        def work():
            return dock_setup.setup_all(self.link, targets, self.backend, self.cfg, self.ask_pin, on_progress, on_result)

        def done(results):
            self._busy = False
            self.btn_setup.setEnabled(True)
            self.btn_read.setEnabled(True)
            ready = sum(r.status == dock_setup.READY for r in results)
            log.info("docked setup: %d of %d ready", ready, len(results))
            self.setup_finished.emit()
            self._render()

        def failed(e):
            self._busy = False
            self.btn_setup.setEnabled(True)
            self.btn_read.setEnabled(True)
            self.status.setText(f"Setup stopped: {e}")

        run_bg(work, done, failed, "docked-setup")

    def _set_result(self, slot, status, text):
        self.results[slot] = (status, text)
        self._render()

    # ---------------- view ----------------
    def _render(self):
        if self.device is None:
            self.status.setText("<b>No Shimmer Base connected.</b> Put your Shimmers in a Shimmer Base or Dock connected "
                                "to this computer by USB, switch them on, and they appear here. Then one click pairs "
                                "them, sets their clocks and tests them.")
            self.status.setStyleSheet(f"color: {MUTED};")
            self.table.setRowCount(0)
            self.btn_setup.setEnabled(False)
            self.btn_read.setEnabled(False)
            return
        docked = [s for s in self.slots if s.occupied]
        done = [r for r in self.results.values() if r[0] in (dock_setup.READY, dock_setup.PROBLEM)]
        ready = sum(r[0] == dock_setup.READY for r in done)
        text = f"<b>{self.device.label}</b> connected · {len(docked)} Shimmer(s) docked"
        if done and not any(r[0] == "busy" for r in self.results.values()):
            text += f" · setup: {ready} ready, {len(done) - ready} need attention"
        self.status.setText(text)
        self.status.setStyleSheet("")
        if not self._busy:
            self.btn_setup.setEnabled(bool(docked) and self.backend is not None)
            self.btn_read.setEnabled(True)
        rows = self.slots or [basemod.SlotInfo(i + 1, o) for i, o in enumerate(self._occupancy or [])]
        self.table.setRowCount(len(rows))
        for r, s in enumerate(rows):
            status, msg = self.results.get(s.slot, ("", ""))
            tip = ""
            if not s.occupied:
                name, msg, color = "—", "empty", MUTED
            elif s.error:
                name, msg, color = "?", "could not read it over USB: push it back into the slot", CRITICAL
            elif not s.mac:
                name, msg, color = "…", "reading…", MUTED
            else:
                name = s.name
                tip = f"{s.name}\naddress {s.mac}\nfirmware {s.firmware}"
                if not msg:                                  # before setup: what we know from the dock
                    if not s.can_stream:
                        status, msg = dock_setup.PROBLEM, f"{s.firmware}: this firmware cannot stream over Bluetooth"
                    else:
                        paired = s.mac in self.paired
                        msg = ("paired" if paired else "not paired yet") + f" · {s.firmware}"
                        status = dock_setup.READY if paired else ""
                color = {dock_setup.READY: GOOD, dock_setup.PROBLEM: CRITICAL, "busy": "#1f4e8c"}.get(status, MUTED)
            for c, v in enumerate((str(s.slot), name, msg)):
                it = QTableWidgetItem(v)
                it.setToolTip(tip or v)
                if c == 0:
                    it.setTextAlignment(Qt.AlignCenter)
                if c == 2:
                    it.setForeground(QColor(color))
                self.table.setItem(r, c, it)
        self.table.resizeRowsToContents()
