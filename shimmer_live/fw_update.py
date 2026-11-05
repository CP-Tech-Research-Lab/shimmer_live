"""Firmware update of one Shimmer3 docked in a Shimmer base (MSP430 bootloader over the base).

Same procedure as tools/fw_flash.py (which now calls this), verified on real Shimmer3 units
(0.15 <-> 1.1.9, 1.0.x, 0.16 -> 1.1.9) in a Base6U:
  1. dock: check the slot holds the expected Shimmer; save its InfoMem (configuration and
     calibration, 512 B) to disk
  2. bootloader (base channel C, lines unmasked only meanwhile): mass erase, unlock, write,
     verify every segment with the chip's CRC, reset into the new firmware
  3. dock: save the new firmware's defaults, write the saved configuration back, read it back.
     Going from 0.x to 1.x, the 'Bluetooth disabled' bit (ignored by 0.x, obeyed by 1.x) is cleared.
The firmware reads its configuration at power-on: switch the Shimmer off and on afterwards.
Only images with a known SHA-256 are accepted. A failed update leaves the bootloader intact:
running it again recovers the Shimmer.
"""
import datetime as dt
import hashlib
import logging
import struct
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import serial
from pyshimmer.uart.dock_api import ShimmerDock
from pyshimmer.uart.dock_const import UART_COMP_SHIMMER, UART_PROP_INFOMEM, UART_SET

from . import bsl

log = logging.getLogger("shimmerlive.firmware")

FIRMWARE_DIR = Path.home() / "Documents" / "ShimmerFirmware"
BACKUP_DIR = Path.home() / "Documents" / "ShimmerFirmwareBackups"
RELEASES = "https://github.com/ShimmerResearch/shimmer3-firmware/releases/download"
INFOMEM_SIZE, INFOMEM_CHUNK = 512, 128
SELF_SET_BITS = [(230, 0x01)]             # flagWriteCfgToSd: set by the firmware itself on config writes
TRIAL_CONFIG0, BT_DISABLE = 217, 0x08      # bluetoothDisable: ignored by 0.x, obeyed by 1.x
INFOMEM_LABELS = {0: "sampling rate", 1: "sampling rate", 3: "sensors", 4: "sensors", 5: "sensors",
                  6: "wide-range accel settings", 7: "gyro rate", 8: "gyro range / magnetometer rate and range",
                  9: "power / GSR range / pressure", 217: "SD-logging options", 230: "internal flags"}


@dataclass(frozen=True)
class Image:
    version: str           # as the Shimmer reports it, e.g. "1.1.9"
    tag: str               # Shimmer's release tag
    sha256: str
    published_sha: bool    # True: GitHub publishes this checksum; False: checksum of our own download
    note: str = ""

    @property
    def filename(self) -> str:
        return f"{self.tag}.txt"

    @property
    def url(self) -> str:
        return f"{RELEASES}/{self.tag}/{self.filename}"


KNOWN_IMAGES = [
    Image("1.1.9", "LogAndStream_Shimmer3_v1.01.009", "c5490697cb3aed75d9a1a9865b48419a392c2bcf624d3a15c552c9198588f1a4",
          True, "latest; tested here with ECG (BD44) and EDA+PPG (A187, A281) out of the base"),
    Image("1.1.2", "LogAndStream_Shimmer3_v1.01.002", "2072089295dfc1b31333a2f2cbfc9a5d67feed162e493a5488f1d24cef3af285", True),
    Image("1.0.5", "LogAndStream_Shimmer3_v1.00.005", "978a0a562c28c95fd2049d05870c7e87eb0cf1ef41dd8fd9ec487ef5c1d4b7bf", True),
    Image("1.0.4", "LogAndStream_Shimmer3_v1.00.004", "701e8e0015f6df2a11f6bc12b854d463c55ce6108b0ca1a353c9a87d09eb54f5", True),
    Image("1.0.3", "LogAndStream_Shimmer3_v1.00.003", "e89237f37c788749e3c51a9241b04b1f8315f86bc7d0206b364a7a3bfee10bb8", True),
    Image("0.16.3", "LogAndStream_Shimmer3_v0.16.003", "2c1e43d3f4f33f8410bd87ae2e0683605bc6054534f9c6fa36b403a6bc1f054f",
          False, "old: does not power a PPG probe"),
    Image("0.15.0", "LogAndStream_Shimmer3_v0.15.000", "e58f291e0b28b6f4faed7ed0646f151b62992004bcfea324978ad3aa2826ed7f",
          False, "old: does not power a PPG probe"),
]
RECOMMENDED = KNOWN_IMAGES[0]


def sha256_of(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def local_images(folder=FIRMWARE_DIR) -> list:
    """[(Image, path)] for known images present in the folder with the right checksum."""
    out = []
    for img in KNOWN_IMAGES:
        p = Path(folder) / img.filename
        if p.exists() and sha256_of(p) == img.sha256:
            out.append((img, p))
    return out


def download(img: Image, folder=FIRMWARE_DIR) -> Path:
    """Fetch an image from Shimmer's GitHub release and keep it only if its checksum matches."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    data = urllib.request.urlopen(img.url, timeout=60).read()
    if hashlib.sha256(data).hexdigest() != img.sha256:
        raise ValueError(f"{img.filename}: downloaded file does not match its checksum - not saved")
    path = folder / img.filename
    path.write_bytes(data)
    return path


def version_tuple(text):
    return tuple(int(x) for x in str(text).split()[-1].split("."))


def masked(im):
    b = bytearray(im)
    for off, m in SELF_SET_BITS:
        b[off] &= ~m & 0xFF
    return bytes(b)


@dataclass
class UpdateResult:
    mac: str
    before: str
    after: str
    backup_dir: Path
    seconds: float
    changed: list = field(default_factory=list)     # [(offset, label, before, after)] the new firmware changed
    notes: list = field(default_factory=list)


class _Dock:
    def __init__(self, port, attempts=30, delay=0.5):
        last = None
        for _ in range(attempts):
            try:
                self.ser = serial.Serial(port, 115200, timeout=1.0)
            except serial.SerialException as e:
                last = e
                time.sleep(delay)
                continue
            try:
                self.dock = ShimmerDock(self.ser)
                self.mac = ":".join(f"{b:02X}" for b in self.dock.get_mac_address())
                _hw, fw, ma, mi, rel = self.dock.get_firmware_version()
                self.version = f"{fw.name} {ma}.{mi}.{rel}"
                return
            except Exception as e:                         # noqa: BLE001 - a booting unit drops commands
                last = e
                self.ser.close()
                time.sleep(delay)
        raise RuntimeError(f"the docked Shimmer did not answer ({last})")

    def read_infomem(self):
        return b"".join(self.dock.get_infomem(off, INFOMEM_CHUNK)[:INFOMEM_CHUNK]
                        for off in range(0, INFOMEM_SIZE, INFOMEM_CHUNK))

    def write_infomem(self, data):
        for off in range(0, len(data), INFOMEM_CHUNK):
            chunk = data[off:off + INFOMEM_CHUNK]
            self.dock._write_packet(UART_SET, UART_COMP_SHIMMER, UART_PROP_INFOMEM,
                                    struct.pack("<BH", len(chunk), off) + chunk)
            self.dock._read_ack()

    def close(self):
        self.ser.close()


def _base_cmd(link, cmd):
    s = link._control()
    s.reset_input_buffer()
    s.write(cmd.encode())
    buf, end = b"", time.monotonic() + 3
    while time.monotonic() < end and b"\r\n" not in buf:
        buf += s.read(64)
    reply = buf.decode(errors="replace").strip()
    if not reply or reply.startswith("E"):
        raise RuntimeError(f"base answered {reply!r} to {cmd}")
    return reply


def update(link, slot: int, expected_mac: str, image_path, sha256: str, progress=lambda text: None,
           backup_root=BACKUP_DIR) -> UpdateResult:
    """Update the Shimmer in `slot` of the base behind `link` (a base.BaseLink)."""
    t0 = time.monotonic()
    image_path = Path(image_path)
    if sha256_of(image_path) != sha256.lower():
        raise ValueError(f"{image_path.name} does not match its checksum: nothing done")
    segs = bsl.read_ti_txt(image_path)
    if any(a < 0x5C00 for a, _ in segs):
        raise ValueError("the image writes below main flash (configuration/bootloader area): refused")
    dev = link.dev
    if not dev.control_port or len(dev.ports) != 4:
        raise RuntimeError("firmware update needs a Shimmer base (Base6U/Base15U)")
    bsl_port, uart_port = dev.ports[2], dev.ports[3]
    with link.lock:
        _base_cmd(link, "SDS,1$")                              # bootloader lines masked before anything else
        link.select(slot)
        progress("reading the Shimmer and saving its configuration…")
        d = _Dock(uart_port)
        try:
            if not d.mac.replace(":", "").upper().endswith(expected_mac.replace(":", "").upper()):
                raise RuntimeError(f"slot {slot} holds {d.mac}, not {expected_mac}: nothing done")
            before, infomem = d.version, d.read_infomem()
        finally:
            d.close()
        out = Path(backup_root) / f"{d.mac.replace(':', '')}_{dt.datetime.now():%Y%m%d-%H%M%S}"
        out.mkdir(parents=True, exist_ok=True)
        (out / "infomem_before.bin").write_bytes(infomem)
        log.info("%s %s: configuration saved to %s", d.mac, before, out)

        b = bsl.Bsl(bsl_port)                                  # opened while masked: no effect on the Shimmer
        erased = False
        try:
            _base_cmd(link, "SDS,0$")
            b.invoke()
            progress("erasing the old firmware…")
            b.mass_erase()
            erased = True
            b.change_baud(115200)
            b.unlock(b"\xff" * 32)
            b.program(segs, lambda done, total: progress(f"writing… {done * 100 // total}%"))
            progress("verified: the Shimmer holds exactly the new firmware")
            b.run_application()
        except Exception as e:
            if erased:
                raise RuntimeError(f"{e}. The Shimmer has no firmware now: leave it docked and run the update "
                                   f"again. Its configuration is saved in {out}") from e
            raise
        finally:
            try:
                _base_cmd(link, "SDS,1$")
            finally:
                b.close()

        progress("waiting for the new firmware…")
        time.sleep(2.5)
        link.select(slot)
        d = _Dock(uart_port)
        notes = []
        try:
            after = d.version
            (out / "infomem_new_defaults.bin").write_bytes(d.read_infomem())
            restore = bytearray(infomem)
            if version_tuple(before) < (1, 0, 0) <= version_tuple(after) and restore[TRIAL_CONFIG0] & BT_DISABLE:
                restore[TRIAL_CONFIG0] &= ~BT_DISABLE & 0xFF
                notes.append("cleared 'Bluetooth disabled' (ignored by the old firmware, it would switch Bluetooth "
                             "off under the new one)")
            progress("restoring its configuration…")
            d.write_infomem(bytes(restore))
            back = d.read_infomem()
            (out / "infomem_after.bin").write_bytes(back)
        finally:
            d.close()
        link.disconnect()
    want, got = masked(bytes(restore)), masked(back)
    changed = [(i, INFOMEM_LABELS.get(i, "other"), want[i], got[i]) for i in range(INFOMEM_SIZE) if want[i] != got[i]]
    log.info("%s: %s -> %s in %.0f s; %d byte(s) changed by the new firmware", expected_mac, before, after,
             time.monotonic() - t0, len(changed))
    return UpdateResult(expected_mac, before, after, out, time.monotonic() - t0, changed, notes)
