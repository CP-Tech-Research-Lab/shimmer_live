"""Shimmer Base (Base6U / Base15U "SmartDock") and single Shimmer Dock over USB.

A base is a USB hub with an FTDI FT4232H (4 serial ports). One port is the base's control
channel, which speaks short ASCII commands; another carries the UART of whichever slot the base
currently routes to it, and on that port the docked Shimmer speaks the normal dock protocol
(pyshimmer ShimmerDock: Bluetooth address, firmware, clock).

Base control commands (from Shimmer's SmartDock driver, verified on a Base6U):
    SDV$      -> V,<hw>,<fw id>,<major>,<minor>,<internal>     hw 1 = Base15U, 2 = Base6U
    SDQ$      -> Q,<one 0/1 per slot>                          which slots hold a Shimmer
    SDP$      -> P,NN | C                                       active slot (C = none)
    SDP,NN$   -> P,NN                                           route slot NN, without SD access
    SDC,NN$   -> C,NN                                           route slot NN WITH its SD card (USB drive)
    SDD$      -> D                                              disconnect all slots
The base also sends S,<map> on its own when a Shimmer is inserted or removed. After routing a
slot, the Shimmer's UART needs ~1.5 s to settle and the first read may fail (retried).

A single Shimmer Dock (FTDI FT2232H) has no control channel: the Shimmer UART is one of its
two ports directly.

Everything here is read-only for the Shimmer except set_clock().
"""
import logging
import sys
import threading
import time
from dataclasses import dataclass, field

import serial
from serial.tools import list_ports

log = logging.getLogger("shimmerlive.base")

FTDI_VID = 0x0403
FT4232H_PID = 0x6011        # Base6U / Base15U
FT2232H_PID = 0x6010        # single Shimmer Dock
BAUD = 115200
SETTLE_S = 1.5
SLOT_CHANGE_TIMEOUT_S = 10.0
BASE_MODELS = {1: ("Base15U", 15), 2: ("Base6U", 6)}


@dataclass
class SlotInfo:
    slot: int                       # 1-based (single dock: 1)
    occupied: bool
    mac: str = ""
    firmware: str = ""              # e.g. "LogAndStream 1.0.5"
    can_stream: bool = False        # LogAndStream firmware streams over Bluetooth; SDLog does not
    error: str = ""

    @property
    def name(self) -> str:
        """Bluetooth name the Shimmer advertises: Shimmer3-<last 4 hex digits of the address>."""
        return f"Shimmer3-{self.mac.replace(':', '')[-4:]}" if self.mac else ""


@dataclass
class BaseDevice:
    kind: str                       # "Base6U", "Base15U", "Dock"
    serial: str                     # USB serial number of the base
    ports: list = field(default_factory=list)
    control_port: str = ""          # base command channel ("" for a single dock)
    shimmer_port: str = ""          # routed Shimmer UART
    slots: int = 1
    firmware: str = ""

    @property
    def label(self) -> str:
        return f"Shimmer {self.kind}" + (f" ({self.slots} slots)" if self.slots > 1 else "")


# ---------------------------------------------------------------------------- discovery

def base_serial(serial_number: str, all_serials) -> str:
    """USB serial of the base itself. Windows' FTDI driver gives each of the chip's ports the base
    serial plus a channel letter (SR3VRR8AA, SR3VRR8AB, ...); Linux reports the same serial for all."""
    sn = serial_number or ""
    if len(sn) > 1 and sn[-1] in "ABCD":
        siblings = {s[-1] for s in all_serials if s and len(s) == len(sn) and s[:-1] == sn[:-1]}
        if len(siblings) > 1:
            return sn[:-1]
    return sn


def _maybe_shimmer(p) -> bool:
    """Never probe unrelated FTDI equipment: on Linux/macOS the base names itself "Shimmer" over USB.
    Windows' FTDI driver hides that name, so there every matching chip is a candidate (probed once)."""
    text = " ".join(filter(None, (getattr(p, "manufacturer", None), getattr(p, "product", None),
                                  getattr(p, "description", None)))).lower()
    return "shimmer" in text or sys.platform == "win32"


def _group_ftdi_ports(ports=None):
    """{(pid, base serial): [port names in interface order]} for Shimmer-type FTDI chips."""
    ports = [p for p in (ports if ports is not None else list_ports.comports())
             if p.vid == FTDI_VID and p.pid in (FT4232H_PID, FT2232H_PID) and _maybe_shimmer(p)]
    serials = [p.serial_number for p in ports]
    groups = {}
    for p in ports:
        sn = base_serial(p.serial_number, serials) or p.location or p.device
        groups.setdefault((p.pid, sn), []).append(p)
    return {key: [q.device for q in sorted(ps, key=lambda q: (q.serial_number or "", q.location or "", q.device))]
            for key, ps in groups.items()}


class BaseLink:
    """Serialised access to one base: control commands and the routed Shimmer UART."""

    def __init__(self, dev: BaseDevice):
        self.dev = dev
        self.lock = threading.RLock()
        self._ctl = None

    # ---- control channel ----
    def _control(self):
        if self._ctl is None:
            self._ctl = serial.Serial(self.dev.control_port, BAUD, timeout=0.2)
        return self._ctl

    def command(self, cmd: str, timeout: float = 2.0) -> str:
        """Send an SDx$ command; return the first reply line that answers it."""
        with self.lock:
            s = self._control()
            s.reset_input_buffer()
            s.write(cmd.encode("ascii"))
            buf, end = b"", time.monotonic() + timeout
            want = {"SDV$": "V", "SDQ$": "Q"}.get(cmd, "P" if cmd.startswith("SDP") else
                                                  "C" if cmd.startswith("SDC") else "D")
            while time.monotonic() < end:
                buf += s.read(256)
                while b"\r\n" in buf:
                    line, buf = buf.split(b"\r\n", 1)
                    text = line.decode("ascii", "replace").strip()
                    if text == "E":
                        raise IOError(f"base rejected {cmd}")
                    if text[:1] == want or (want == "P" and text == "C"):
                        return text
            raise TimeoutError(f"base did not answer {cmd}")

    def occupancy(self) -> list:
        if not self.dev.control_port:
            return [True]                                   # single dock: assume docked; read tells
        line = self.command("SDQ$")
        return [c == "1" for c in line[2:]]

    def select_sd(self, slot: int) -> None:
        """Route the slot WITH its SD card: the card appears on this PC as a USB drive (~4 s).
        The base answers C,NN (verified on a Base6U)."""
        if not self.dev.control_port:
            return
        reply = self.command(f"SDC,{slot:02d}$", timeout=SLOT_CHANGE_TIMEOUT_S)
        if reply != f"C,{slot:02d}":
            raise IOError(f"base routed {reply!r} instead of slot {slot} with SD")

    def select(self, slot: int) -> None:
        if not self.dev.control_port:
            return
        reply = self.command(f"SDP,{slot:02d}$", timeout=SLOT_CHANGE_TIMEOUT_S)
        if reply != f"P,{slot:02d}":
            raise IOError(f"base routed {reply!r} instead of slot {slot}")
        time.sleep(SETTLE_S)

    def disconnect(self) -> None:
        if self.dev.control_port:
            try:
                self.command("SDD$")
            except (IOError, TimeoutError):
                pass

    def close(self):
        with self.lock:
            if self._ctl is not None:
                self._ctl.close()
                self._ctl = None

    # ---- the routed Shimmer ----
    def _with_shimmer(self, fn, tries=3):
        from pyshimmer.uart.dock_api import ShimmerDock
        last = None
        for _ in range(tries):                               # first read after routing often fails
            ser = serial.Serial(self.dev.shimmer_port, BAUD, timeout=1.0)
            try:
                return fn(ShimmerDock(ser))
            except Exception as e:                           # noqa: BLE001 - reported to the caller
                last = e
            finally:
                ser.close()
            time.sleep(0.4)
        raise IOError(f"docked Shimmer did not answer ({type(last).__name__})")

    def read_slot(self, slot: int, occupied: bool = True) -> SlotInfo:
        info = SlotInfo(slot, occupied)
        if not occupied:
            return info
        with self.lock:
            try:
                self.select(slot)
                mac, fw = self._with_shimmer(lambda d: (d.get_mac_address(), d.get_firmware_version()))
                info.mac = ":".join(f"{b:02X}" for b in mac)
                _hw, fw_type, major, minor, rel = fw
                info.firmware = f"{fw_type.name} {major}.{minor}.{rel}"
                info.can_stream = fw_type.name == "LogAndStream"
            except Exception as e:                           # noqa: BLE001
                info.error = str(e)
        return info

    def read_all(self, progress=None) -> list:
        with self.lock:
            occ = self.occupancy()
            out = []
            for i, o in enumerate(occ, start=1):
                if progress and o:
                    progress(f"reading slot {i}…")
                out.append(self.read_slot(i, o))
            self.disconnect()
            return out

    def set_clock(self, slot: int) -> None:
        """Set the docked Shimmer's real-time clock to this PC's time."""
        with self.lock:
            self.select(slot)
            self._with_shimmer(lambda d: d.set_rtc(time.time()))


def _probe_control(port: str) -> str:
    """'V,…' if this port is a base control channel, else ''."""
    try:
        with serial.Serial(port, BAUD, timeout=0.2) as s:
            s.reset_input_buffer()
            s.write(b"SDV$")
            buf, end = b"", time.monotonic() + 1.0
            while time.monotonic() < end and b"\r\n" not in buf:
                buf += s.read(64)
            line = buf.split(b"\r\n")[0].decode("ascii", "replace").strip()
            return line if line.startswith("V,") else ""
    except (OSError, serial.SerialException):
        return ""


def _answers_dock_protocol(port: str) -> bool:
    from pyshimmer.uart.dock_api import ShimmerDock
    for _ in range(2):
        try:
            with serial.Serial(port, BAUD, timeout=1.0) as s:
                ShimmerDock(s).get_mac_address()
                return True
        except Exception:                                    # noqa: BLE001
            time.sleep(0.3)
    return False


def detect(cfg=None) -> list:
    """Find connected bases/docks and work out their port roles (cached in the config)."""
    found = []
    cache = cfg.data.setdefault("bases", {}) if cfg is not None else {}
    for (pid, sn), ports in _group_ftdi_ports().items():
        known = cache.get(sn, {})
        if known.get("ports") == ports and known.get("shimmer_port") in ports:
            dev = BaseDevice(known["kind"], sn, ports, known.get("control_port", ""), known["shimmer_port"],
                             known.get("slots", 1), known.get("firmware", ""))
            found.append(dev)
            continue
        dev = _identify(pid, sn, ports)
        if dev is not None:
            found.append(dev)
            cache[sn] = {"kind": dev.kind, "ports": ports, "control_port": dev.control_port,
                         "shimmer_port": dev.shimmer_port, "slots": dev.slots, "firmware": dev.firmware}
            if cfg is not None:
                cfg.save()
    return found


def _identify(pid, sn, ports):
    if pid == FT4232H_PID:
        # Shimmer bases put the control channel on the 2nd FTDI port and the Shimmer UART on the 4th:
        # try those first (any order still works, it is just slower).
        control_first = ports[1:2] + ports[:1] + ports[2:]
        for p in control_first:
            ver = _probe_control(p)
            if not ver:
                continue
            hw, fw_id, major, minor, internal = (int(x) for x in ver[2:].split(","))
            kind, slots = BASE_MODELS.get(hw, (f"Base (hw {hw})", 0))
            dev = BaseDevice(kind, sn, ports, p, "", slots, f"{major}.{minor}.{internal}")
            link = BaseLink(dev)
            try:
                occ = link.occupancy()
                dev.slots = len(occ) or slots
                occupied = [i for i, o in enumerate(occ, start=1) if o]
                if not occupied:
                    log.info("%s found but no Shimmer docked: cannot tell the Shimmer port yet", kind)
                    return dev
                # A docked Shimmer that is off (or not seated) does not answer: try the next slot.
                for slot in occupied[:3]:
                    link.select(slot)
                    dev.shimmer_port = next((q for q in reversed(ports) if q != p and _answers_dock_protocol(q)), "")
                    if dev.shimmer_port:
                        break
                if not dev.shimmer_port and len(ports) == 4:
                    # No Shimmer answered (all off?): use the 4th port, where every Shimmer base seen so far
                    # routes the Shimmer UART. A wrong guess only means slots read as "did not answer".
                    dev.shimmer_port = ports[3]
                    log.info("%s: no docked Shimmer answered; assuming the Shimmer UART is %s", kind, ports[3])
            finally:
                link.disconnect()
                link.close()
            log.info("found %s (%s): control %s, Shimmer UART %s", kind, sn, p, dev.shimmer_port or "?")
            return dev
        return None
    for q in ports:                                          # single dock
        if _answers_dock_protocol(q):
            return BaseDevice("Dock", sn, ports, "", q, 1, "")
    return None
