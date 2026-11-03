"""Watches USB for a Shimmer Base/Dock, knows which Shimmer is in which slot, and runs the one-click
setup of docked Shimmers. It has no widgets of its own: the start page shows its state in the
Shimmers list (slot, firmware, setup progress)."""
import logging

from PySide6.QtCore import QObject, QTimer, Signal

from .. import base as basemod
from .. import dock_setup
from .common import run_bg, ui_call

log = logging.getLogger("shimmerlive.ui.base")

POLL_MS = 3000


class BaseWatcher(QObject):
    changed = Signal()                # base connected/removed, slots read, setup progress
    setup_finished = Signal(list)     # [SetupResult]

    def __init__(self, cfg, backend, ask_pin):
        super().__init__()
        self.cfg, self.backend, self.ask_pin = cfg, backend, ask_pin
        self.device = None            # basemod.BaseDevice, or None when no base is connected
        self.link = None
        self.slots = []               # [SlotInfo]
        self.results = {}             # slot -> (status, text): setup progress and results
        self.occupancy = None
        self.busy = False             # reading slots or running setup
        self.setting_up = False
        self.timer = QTimer(self, interval=POLL_MS, timeout=self._poll)

    # ---------------- watching the USB ----------------
    def start(self):
        self.timer.start()
        self._poll()

    def stop(self):
        self.timer.stop()

    def slot_of(self, mac):
        return next((s for s in self.slots if s.occupied and s.mac == mac), None)

    def _poll(self):
        if self.busy:
            return
        self.busy = True

        def work():
            devs = basemod.detect(self.cfg)
            if not devs:
                return None, None, None
            dev = devs[0]
            link = self.link if self.device and self.device.serial == dev.serial else basemod.BaseLink(dev)
            return dev, link, link.occupancy()

        def done(res):
            self.busy = False
            dev, link, occ = res
            if dev is None:
                if self.device is not None:
                    log.info("%s disconnected", self.device.label)
                    if self.link:
                        self.link.close()
                    self.device, self.link, self.slots, self.occupancy, self.results = None, None, [], None, {}
                    self.changed.emit()
                return
            if self.device is None or dev.serial != self.device.serial:
                log.info("%s connected", dev.label)
                self.results = {}
            self.device, self.link = dev, link
            if occ != self.occupancy:
                self.occupancy = occ
                self.read_slots()
            self.changed.emit()

        def failed(e):
            self.busy = False
            log.info("base poll failed: %s", e)

        run_bg(work, done, failed, "base-poll")

    def read_slots(self):
        if self.link is None:
            return
        self.busy = True

        def done(infos):
            self.busy = False
            before = {s.slot: s.mac for s in self.slots}
            self.slots = infos
            for s in infos:                                  # a different Shimmer in that slot: forget old result
                if before.get(s.slot) != s.mac:
                    self.results.pop(s.slot, None)
            self.changed.emit()

        def failed(e):
            self.busy = False
            log.info("could not read the base: %s", e)

        run_bg(lambda: self.link.read_all(), done, failed, "base-read")

    # ---------------- one-click setup ----------------
    def setup_all(self):
        if self.link is None or self.busy or not self.backend:
            return
        targets = [s for s in self.slots if s.occupied]
        if not targets:
            return
        self.busy = self.setting_up = True
        for s in targets:
            self.results[s.slot] = ("busy", "waiting…")
        self.changed.emit()

        def on_progress(slot, text):
            ui_call(self._set_result, slot, "busy", text)

        def on_result(res):
            ui_call(self._set_result, res.slot, res.status, res.message)

        def work():
            return dock_setup.setup_all(self.link, targets, self.backend, self.cfg, self.ask_pin, on_progress, on_result)

        def done(results):
            self.busy = self.setting_up = False
            log.info("docked setup: %d of %d ready", sum(r.status == dock_setup.READY for r in results), len(results))
            self.changed.emit()
            self.setup_finished.emit(results)

        def failed(e):
            self.busy = self.setting_up = False
            log.info("docked setup stopped: %s", e)
            self.changed.emit()

        run_bg(work, done, failed, "docked-setup")

    def _set_result(self, slot, status, text):
        self.results[slot] = (status, text)
        self.changed.emit()
