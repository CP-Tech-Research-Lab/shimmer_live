"""MSP430F5xx UART bootloader (BSL) client, for updating a docked Shimmer3's firmware.

Written from TI's "MSP430 Flash Device Bootloader (BSL) User's Guide" (SLAU319, 5xx/6xx UART
protocol). The Shimmer-specific parts follow Shimmer's own test rig (ShimmerResearch/
log-and-stream-common, Test/hil/rig): in a Base6U the slot's BSL lines are on FTDI channel C,
masked by default and unmasked with SDS,0$; DTR drives RST and RTS drives TEST, both active low.

Safety facts that shape this module:
  * The BSL needs a password (the 32 bytes at 0xFFE0-0xFFFF, the interrupt vectors) for
    everything except MASS_ERASE and RX_PASSWORD. A WRONG password makes the chip mass-erase
    itself: main flash AND information memory B-D, where the Shimmer keeps its configuration
    and calibration. So: back InfoMem up first; send a password only right after a mass erase
    (it is then all 0xFF) or, deliberately, the vectors of a known official image.
  * The BSL itself lives in protected memory and is never erased: a failed update can be
    repeated.

Packet:   0x80 lenL lenH <cmd + data> crcL crcH   (CRC-CCITT, seed 0xFFFF, over cmd + data)
Answer:   one byte, 0x00 = ACK; commands with a reply then send 0x80 lenL lenH <payload> crc,
          payload[0] = 0x3A (data) or 0x3B (message: payload[1] = status, 0 = OK)
"""
import struct
import time
from pathlib import Path

import serial

ACK, HEADER = 0x00, 0x80
RX_DATA_BLOCK, RX_PASSWORD, MASS_ERASE, CRC_CHECK = 0x10, 0x11, 0x15, 0x16
TX_DATA_BLOCK, TX_BSL_VERSION, CHANGE_BAUD = 0x18, 0x19, 0x52
DATA, MESSAGE = 0x3A, 0x3B
BAUD_CODES = {9600: 0x02, 19200: 0x03, 38400: 0x04, 57600: 0x05, 115200: 0x06}
BLOCK = 240                       # data bytes per write/read packet
STATUS = {0x00: "ok", 0x01: "flash write check failed", 0x02: "flash fail bit set",
          0x03: "voltage change during program", 0x04: "BSL locked (no password yet)",
          0x05: "password error (the chip mass-erases itself)", 0x06: "byte write forbidden",
          0x07: "unknown command", 0x08: "packet too long"}
VECTORS = range(0xFFE0, 0x10000)  # the password


class BslError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def crc_ccitt(data: bytes, crc: int = 0xFFFF) -> int:
    for b in data:
        x = ((crc >> 8) ^ b) & 0xFF
        x ^= x >> 4
        crc = ((crc << 8) ^ (x << 12) ^ (x << 5) ^ x) & 0xFFFF
    return crc


def read_ti_txt(path) -> list:
    """[(address, bytes)] from a TI-TXT image ('@addr' lines, hex bytes, 'q')."""
    segs = []
    for line in Path(path).read_text(encoding="ascii").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("@"):
            segs.append((int(line[1:], 16), bytearray()))
        elif line.lower() == "q":
            break
        elif not segs:
            raise BslError(f"{path}: data before the first @address")
        else:
            segs[-1][1].extend(int(t, 16) for t in line.split())
    return [(a, bytes(d)) for a, d in segs if d]


def password_of(segs) -> bytes:
    """The BSL password a chip running this image has: its vectors, unprogrammed bytes = 0xFF."""
    mem = {}
    for a, d in segs:
        for i, x in enumerate(d):
            mem[a + i] = x
    return bytes(mem.get(a, 0xFF) for a in VECTORS)


class Bsl:
    """One session with the bootloader of the slot routed to `port` (lines must be unmasked)."""

    def __init__(self, port, timeout_s=2.0, serial_factory=serial.Serial):
        self.ser = serial_factory(port, 9600, bytesize=8, parity=serial.PARITY_EVEN, stopbits=1,
                                  timeout=timeout_s)

    def close(self):
        self.ser.close()

    # ---- reset lines: RST on DTR, TEST on RTS; True = asserted = low ----
    def _pins(self, rst_low, test_low, ms):
        self.ser.dtr, self.ser.rts = rst_low, test_low
        time.sleep(ms / 1000.0)

    def invoke(self):
        """Reset into the BSL: TEST pulses while RST is low, then RST is released (5xx sequence)."""
        for rst_low, test_low, ms in ((True, True, 250), (True, True, 10), (True, False, 10), (True, True, 10),
                                      (True, False, 10), (True, True, 10), (False, True, 10)):
            self._pins(rst_low, test_low, ms)
        time.sleep(0.35)
        self.ser.reset_input_buffer()
        self.ser.baudrate = 9600
        time.sleep(0.35)

    def run_application(self):
        """Reset into the application: RST low then high with TEST held low."""
        self._pins(True, True, 250)
        self._pins(False, True, 360)

    # ---- packets ----
    def _send(self, core: bytes):
        self.ser.write(bytes([HEADER]) + struct.pack("<H", len(core)) + core + struct.pack("<H", crc_ccitt(core)))
        a = self.ser.read(1)
        if not a:
            raise BslError(f"no answer to command {core[0]:#04x} (bootloader not entered?)")
        if a[0] != ACK:
            raise BslError(f"command {core[0]:#04x} refused with {a[0]:#04x}")

    def _reply(self) -> bytes:
        head = self.ser.read(3)
        if len(head) != 3 or head[0] != HEADER:
            raise BslError(f"bad reply header {head.hex()}")
        n = struct.unpack("<H", head[1:])[0]
        body = self.ser.read(n + 2)
        if len(body) != n + 2 or struct.unpack("<H", body[n:])[0] != crc_ccitt(body[:n]):
            raise BslError("reply cut short or corrupted")
        return body[:n]

    def _message(self, core: bytes):
        self._send(core)
        r = self._reply()
        if r[0] != MESSAGE:
            raise BslError(f"command {core[0]:#04x}: unexpected reply {r.hex()}")
        if r[1] != 0:
            raise BslError(f"command {core[0]:#04x}: {STATUS.get(r[1], hex(r[1]))}", status=r[1])

    def _data(self, core: bytes) -> bytes:
        self._send(core)
        r = self._reply()
        if r[0] == MESSAGE:
            raise BslError(f"command {core[0]:#04x}: {STATUS.get(r[1], hex(r[1]))}", status=r[1])
        if r[0] != DATA:
            raise BslError(f"command {core[0]:#04x}: unexpected reply {r.hex()}")
        return r[1:]

    # ---- commands ----
    def version(self) -> bytes:
        return self._data(bytes([TX_BSL_VERSION]))

    def mass_erase(self):
        self._message(bytes([MASS_ERASE]))

    def unlock(self, password: bytes):
        """WARNING: a wrong password mass-erases the chip (firmware and configuration)."""
        if len(password) != 32:
            raise ValueError("the BSL password is 32 bytes")
        self._message(bytes([RX_PASSWORD]) + password)

    def change_baud(self, baud):
        self._send(bytes([CHANGE_BAUD, BAUD_CODES[baud]]))
        time.sleep(0.05)
        self.ser.baudrate = baud

    def write(self, addr, data):
        self._message(bytes([RX_DATA_BLOCK]) + addr.to_bytes(3, "little") + data)

    def read(self, addr, length) -> bytes:
        out = b""
        while length > 0:
            n = min(length, BLOCK)
            out += self._data(bytes([TX_DATA_BLOCK]) + addr.to_bytes(3, "little") + struct.pack("<H", n))
            addr, length = addr + n, length - n
        return out

    def crc(self, addr, length) -> int:
        return struct.unpack("<H", self._data(bytes([CRC_CHECK]) + addr.to_bytes(3, "little")
                                              + struct.pack("<H", length))[:2])[0]

    # ---- whole operations ----
    def program(self, segs, progress=lambda done, total: None):
        """Write every segment, then verify each one with the chip's own CRC (byte for byte)."""
        total, done = sum(len(d) for _, d in segs), 0
        for addr, data in segs:
            for off in range(0, len(data), BLOCK):
                self.write(addr + off, data[off:off + BLOCK])
                done += len(data[off:off + BLOCK])
                progress(done, total)
        for addr, data in segs:
            for off in range(0, len(data), 0x8000):            # CRC_CHECK takes a 16-bit length
                chunk = data[off:off + 0x8000]
                got = self.crc(addr + off, len(chunk))
                if got != crc_ccitt(chunk):
                    raise BslError(f"verify failed at {addr + off:#07x}: chip CRC {got:#06x}, "
                                   f"image {crc_ccitt(chunk):#06x}")
