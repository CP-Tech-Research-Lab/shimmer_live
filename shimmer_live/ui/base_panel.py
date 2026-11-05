"""Watches USB for Shimmer Bases/Docks (any number), knows which Shimmer is in which slot of which
base, and runs the one-click setup of docked Shimmers. It has no widgets of its own: the start page
shows its state in the Shimmers list (base, slot, firmware, setup progress).

Each base has its own serial ports, so bases are read in parallel; within one base the slots are
read one after the other (the base routes one slot at a time)."""
import logging

from PySide6.QtCore import QObject, QTimer, Signal

from .. import base as basemod
from .. import dock_setup
from .common import run_bg, ui_call

log = logging.getLogger("shimmerlive.ui.base")

POLL_MS = 3000


class BaseUnit:
    """One connected base (or single dock) and what is known about its slots."""

    def __init__(self, device, link):
        self.device, self.link = device, link
        self.letter = "A"
        self.slots = []               # [SlotInfo]
        self.occupancy = None
        self.reading = False
        self.results = {}             # slot -> (status, text): setup progress and results

    @property
    def serial(self):
        return self.device.serial

    def where(self, slot: int, many: bool) -> str:
        return f"base {self.letter} · slot {slot}" if many else f"base slot {slot}"


class Docked:
    """A Shimmer found in a slot of a base."""

    def __init__(self, unit, info):
        self.unit, self.info = unit, info

    @property
    def slot(self):
        return self.info.slot

    @property
    def link(self):
        return self.unit.link


class BaseWatcher(QObject):
    changed = Signal()                # base connected/removed, slots read, setup progress
    setup_finished = Signal(list)     # [SetupResult]

    def __init__(self, cfg, backend, ask_pin):
        super().__init__()
        self.cfg, self.backend, self.ask_pin = cfg, backend, ask_pin
        self.units = {}               # serial -> BaseUnit
        self.state = "checking"       # checking (not looked yet) | none (no base) | base
        self.polling = False
        self.setting_up = False
        self.updating = False           # a firmware update owns the base's ports
        self.timer = QTimer(self, interval=POLL_MS, timeout=self._poll)

    # ---------------- queries ----------------
    def ordered(self) -> list:
        return [self.units[k] for k in sorted(self.units)]

    @property
    def reading(self) -> bool:
        return any(u.reading for u in self.units.values())

    @property
    def busy(self) -> bool:
        return self.setting_up or self.reading or self.updating

    def docked(self) -> list:
        """[(unit, SlotInfo)] for every occupied slot of every base."""
        return [(u, s) for u in self.ordered() for s in u.slots if s.occupied]

    def locate(self, mac):
        """Docked(unit, SlotInfo) for the Shimmer with this address, or None if it is in no base."""
        for u, s in self.docked():
            if s.mac == mac:
                return Docked(u, s)
        return None

    def where(self, unit, slot) -> str:
        return unit.where(slot, len(self.units) > 1)

    def result(self, serial, slot):
        u = self.units.get(serial)
        return u.results.get(slot) if u else None

    def summary(self) -> str:
        n = sum(s.occupied for _, s in self.docked()) or sum(sum(u.occupancy or []) for u in self.units.values())
        if len(self.units) == 1:
            return f"{self.ordered()[0].device.label} · {n} docked"
        return f"{len(self.units)} Shimmer bases · {n} docked"

    # ---------------- watching the USB ----------------
    def start(self):
        self.timer.start()
        self._poll()

    def stop(self):
        self.timer.stop()

    def rescan(self):
        """Forget what is known and look again from scratch (the Refresh button)."""
        if self.setting_up:
            return
        self.state = "checking"
        for u in self.units.values():
            if not u.reading:
                u.slots, u.occupancy, u.results = [], None, {}
        self.changed.emit()
        self._poll()

    def _poll(self):
        if self.polling or self.setting_up or self.updating:
            return
        self.polling = True
        known = dict(self.units)

        def work():
            out = []
            for dev in basemod.detect(self.cfg):
                old = known.get(dev.serial)
                same = old is not None and old.device.ports == dev.ports
                link = old.link if same else basemod.BaseLink(dev)
                occ = None
                if not (same and old.reading):
                    try:
                        occ = link.occupancy()
                    except Exception as e:             # noqa: BLE001 - retried at the next poll
                        log.info("%s: %s", dev.label, e)
                out.append((dev, link, occ))
            return out

        def done(found):
            self.polling = False
            was_checking = self.state == "checking"
            seen = set()
            for dev, link, occ in found:
                seen.add(dev.serial)
                u = self.units.get(dev.serial)
                if u is None or u.link is not link:
                    log.info("%s connected (%s)", dev.label, dev.serial)
                    u = self.units[dev.serial] = BaseUnit(dev, link)
                if occ is not None and occ != u.occupancy and not u.reading:
                    u.occupancy = occ
                    self.read_slots(u)
            for serial in list(self.units):
                if serial not in seen:
                    u = self.units.pop(serial)
                    log.info("%s disconnected", u.device.label)
                    if not u.reading:
                        u.link.close()
            for i, u in enumerate(self.ordered()):
                u.letter = chr(ord("A") + i)
            self.state = "base" if self.units else "none"
            if was_checking or seen != set(known):
                self.changed.emit()

        def failed(e):
            self.polling = False
            log.info("base poll failed: %s", e)

        run_bg(work, done, failed, "base-poll")

    def read_slots(self, unit):
        """Read every slot of one base, publishing each Shimmer as soon as it is read (~2 s per slot)."""
        unit.reading = True
        self.changed.emit()
        link = unit.link

        def work():
            with link.lock:
                occ = link.occupancy()
                ui_call(self._placeholders, unit, occ)
                out = []
                for i, occupied in enumerate(occ, start=1):
                    info = link.read_slot(i, occupied) if occupied else basemod.SlotInfo(i, False)
                    out.append(info)
                    ui_call(self._slot_read, unit, info)
                link.disconnect()
                return out

        def done(infos):
            unit.reading = False
            unit.slots = infos
            self.changed.emit()

        def failed(e):
            unit.reading = False
            log.info("could not read %s: %s", unit.device.label, e)
            self.changed.emit()

        run_bg(work, done, failed, f"base-read-{unit.serial}")

    def _placeholders(self, unit, occ):
        """Occupied slots are known at once; who is in them follows slot by slot."""
        old = {s.slot: s for s in unit.slots}
        unit.slots = [old[i] if i in old and old[i].occupied == o and old[i].mac else basemod.SlotInfo(i, o)
                      for i, o in enumerate(occ, start=1)]
        self.changed.emit()

    def _slot_read(self, unit, info):
        prev = next((s for s in unit.slots if s.slot == info.slot), None)
        if prev is not None and prev.mac != info.mac:        # a different Shimmer in that slot: old result is stale
            unit.results.pop(info.slot, None)
        unit.slots = [info if s.slot == info.slot else s for s in unit.slots]
        self.changed.emit()

    # ---------------- one-click setup ----------------
    def setup_all(self, targets=None):
        """Set up the Shimmers in targets = [(base serial, slot)] (every docked Shimmer if None).
        Bluetooth pairing is done one Shimmer at a time, whatever base it is in."""
        if self.busy or not self.backend:
            return
        chosen = [(u, s) for u, s in self.docked() if targets is None or (u.serial, s.slot) in targets]
        if not chosen:
            return
        self.setting_up = True
        for u, s in chosen:
            u.results[s.slot] = ("busy", "waiting…")
        self.changed.emit()

        def work():
            out = []
            for u, s in chosen:
                res = dock_setup.setup_slot(u.link, s, self.backend, self.cfg, self.ask_pin,
                                            lambda text, u=u, s=s: ui_call(self._set_result, u, s.slot, "busy", text))
                ui_call(self._set_result, u, s.slot, res.status, res.message)
                out.append(res)
            return out

        def done(results):
            self.setting_up = False
            log.info("docked setup: %d of %d ready", sum(r.status == dock_setup.READY for r in results), len(results))
            self.changed.emit()
            self.setup_finished.emit(results)

        def failed(e):
            self.setting_up = False
            log.info("docked setup stopped: %s", e)
            self.changed.emit()

        run_bg(work, done, failed, "docked-setup")

    def _set_result(self, unit, slot, status, text):
        unit.results[slot] = (status, text)
        self.changed.emit()
