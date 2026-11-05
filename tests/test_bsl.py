"""MSP430 bootloader client against a simulated chip (TI SLAU319 behaviour, incl. the
mass erase a wrong password triggers)."""
import struct
from pathlib import Path

import pytest

from shimmer_live import bsl


class FakeChip:
    """A 5xx BSL: flash + InfoMem, locked until the right password; wrong password = mass erase."""

    def __init__(self, image, infomem=b"\x11" * 512):
        self.flash = {}
        for a, d in image:
            for i, x in enumerate(d):
                self.flash[a + i] = x
        self.infomem = bytearray(infomem)
        self.unlocked, self.out, self.baudrate, self.timeout = False, b"", 9600, 1
        self.dtr = self.rts = False
        self.log = []

    # serial API
    def write(self, frame):
        assert frame[0] == 0x80
        n = struct.unpack("<H", frame[1:3])[0]
        core, crc = frame[3:3 + n], struct.unpack("<H", frame[3 + n:5 + n])[0]
        assert crc == bsl.crc_ccitt(core)
        self.log.append(core[0])
        self.out += b"\x00"                                     # ACK
        self._do(core[0], core[1:])

    def read(self, n):
        data, self.out = self.out[:n], self.out[n:]
        return data

    def reset_input_buffer(self):
        self.out = b""

    def close(self):
        pass

    # chip
    def _reply(self, payload):
        self.out += bytes([0x80]) + struct.pack("<H", len(payload)) + payload + struct.pack("<H", bsl.crc_ccitt(payload))

    def _vectors(self):
        return bytes(self.flash.get(a, 0xFF) for a in bsl.VECTORS)

    def _do(self, cmd, data):
        if cmd == bsl.MASS_ERASE:
            self.flash.clear()
            self.infomem[:] = b"\xff" * 512                     # B-D wiped too
            return self._reply(bytes([bsl.MESSAGE, 0]))
        if cmd == bsl.RX_PASSWORD:
            if data == self._vectors():
                self.unlocked = True
                return self._reply(bytes([bsl.MESSAGE, 0]))
            self._do(bsl.MASS_ERASE, b"")
            self.out = self.out[:-7]                              # erase happens silently
            return self._reply(bytes([bsl.MESSAGE, 5]))
        if cmd == bsl.CHANGE_BAUD:
            return
        if not self.unlocked:
            return self._reply(bytes([bsl.MESSAGE, 4]))
        addr = int.from_bytes(data[:3], "little")
        if cmd == bsl.RX_DATA_BLOCK:
            for i, x in enumerate(data[3:]):
                self.flash[addr + i] = x
            return self._reply(bytes([bsl.MESSAGE, 0]))
        if cmd in (bsl.CRC_CHECK, bsl.TX_DATA_BLOCK):
            n = struct.unpack("<H", data[3:5])[0]
            mem = bytes(self.flash.get(addr + i, 0xFF) for i in range(n))
            out = struct.pack("<H", bsl.crc_ccitt(mem)) if cmd == bsl.CRC_CHECK else mem
            return self._reply(bytes([bsl.DATA]) + out)
        if cmd == bsl.TX_BSL_VERSION:
            return self._reply(bytes([bsl.DATA, 0, 4, 1, 0]))


OLD = [(0x5C00, bytes(range(256)) * 4), (0xFFE0, bytes(range(32)))]
NEW = [(0x5C00, bytes(reversed(range(256))) * 5), (0xFFE0, bytes(range(32, 64)))]


def session(chip):
    s = bsl.Bsl("x", serial_factory=lambda *a, **k: chip)
    s.ser = chip
    return s


def test_crc_and_ti_txt(tmp_path):
    assert bsl.crc_ccitt(b"123456789") == 0x29B1            # CRC-CCITT (0xFFFF) check value
    p = tmp_path / "img.txt"
    p.write_text("@5c00\nAF 14 B1 00\n@ffe0\n01 02\nq\n")
    assert bsl.read_ti_txt(p) == [(0x5C00, b"\xaf\x14\xb1\x00"), (0xFFE0, b"\x01\x02")]


def test_locked_bootloader_only_says_locked():
    chip = FakeChip(OLD)
    with pytest.raises(bsl.BslError) as e:
        session(chip).version()
    assert e.value.status == 4 and chip.flash                # nothing erased


def test_right_old_password_reads_the_firmware_out():
    chip = FakeChip(OLD)
    s = session(chip)
    s.unlock(bsl.password_of(OLD))
    assert s.read(0x5C00, 1024) == OLD[0][1] and chip.infomem == bytearray(b"\x11" * 512)


def test_wrong_password_erases_everything_as_the_real_chip_does():
    chip = FakeChip(OLD)
    with pytest.raises(bsl.BslError) as e:
        session(chip).unlock(bsl.password_of(NEW))
    assert e.value.status == 5 and not chip.flash and chip.infomem == bytearray(b"\xff" * 512)


def test_update_erases_writes_and_verifies():
    chip = FakeChip(OLD)
    s = session(chip)
    s.mass_erase()
    s.unlock(b"\xff" * 32)                                    # erased chip: password is all 0xFF
    s.program(NEW)
    assert bytes(chip.flash[0x5C00 + i] for i in range(len(NEW[0][1]))) == NEW[0][1]
    assert bsl.RX_PASSWORD in chip.log and chip.log.count(bsl.RX_PASSWORD) == 1


def test_a_bad_write_is_caught_by_the_crc_check():
    chip = FakeChip(OLD)
    s = session(chip)
    s.mass_erase()
    s.unlock(b"\xff" * 32)
    real = chip._do

    def flaky(cmd, data):
        if cmd == bsl.RX_DATA_BLOCK and int.from_bytes(data[:3], "little") == 0x5C00:
            data = data[:3] + b"\x00" + data[4:]                # one byte lands wrong
        real(cmd, data)
    chip._do = flaky
    with pytest.raises(bsl.BslError, match="verify failed"):
        s.program(NEW)


def test_flash_tool_refuses_an_image_that_does_not_match_its_checksum(tmp_path):
    import subprocess
    import sys
    img = tmp_path / "fw.txt"
    img.write_text("@5c00\n00\nq\n")
    root = Path(__file__).parent.parent
    r = subprocess.run([sys.executable, str(root / "tools" / "fw_flash.py"), "1", "F66C", str(img), "0" * 64],
                       capture_output=True, text=True)
    assert r.returncode != 0 and "does not match the published SHA-256" in r.stderr
