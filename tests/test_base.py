"""Shimmer Base protocol (simulated base) and the docked-setup flow (simulated Bluetooth)."""
from types import SimpleNamespace

import pytest

from shimmer_live import base, dock_setup
from shimmer_live.base import FT4232H_PID, FTDI_VID, BaseDevice, BaseLink, SlotInfo, _group_ftdi_ports
from shimmer_live.bt.base import BtDevice, PairingError
from shimmer_live.config import Config


class FakeBaseSerial:
    """Answers SDx$ commands like a Base6U (replies verified on real hardware)."""

    def __init__(self, occupied="110100"):
        self.occupied, self.active, self.out, self.log = occupied, None, b"", []

    def reset_input_buffer(self):
        self.out = b""

    def write(self, data):
        cmd = data.decode()
        self.log.append(cmd)
        if cmd == "SDQ$":
            reply = f"Q,{self.occupied}"
        elif cmd == "SDV$":
            reply = "V,2,1,0,1,0"
        elif cmd == "SDD$":
            self.active, reply = None, "D"
        elif cmd.startswith("SDP,") and cmd.endswith("$") and 1 <= int(cmd[4:6]) <= len(self.occupied):
            self.active, reply = int(cmd[4:6]), f"P,{cmd[4:6]}"
        else:
            reply = "E"
        self.out += reply.encode() + b"\r\n"

    def read(self, n):
        data, self.out = self.out[:n], self.out[n:]
        return data

    def close(self):
        pass


@pytest.fixture
def link(monkeypatch):
    fake = FakeBaseSerial()
    monkeypatch.setattr(base.serial, "Serial", lambda *a, **k: fake)
    monkeypatch.setattr(base, "SETTLE_S", 0)
    lk = BaseLink(BaseDevice("Base6U", "SR3VRR8A", [], "/dev/ctl", "/dev/shimmer", 6))
    lk.fake = fake
    return lk


def test_base_commands(link):
    assert link.occupancy() == [True, True, False, True, False, False]
    link.select(4)
    assert link.fake.active == 4 and link.fake.log[-1] == "SDP,04$"
    link.disconnect()
    assert link.fake.active is None
    with pytest.raises(IOError):
        link.command("SDX$")                                  # the base answers E to unknown commands


def test_read_all_routes_each_occupied_slot(link, monkeypatch):
    routed = []

    def fake_with_shimmer(fn, tries=3):
        routed.append(link.fake.active)
        fw_type = SimpleNamespace(name="SDLog" if link.fake.active == 4 else "LogAndStream")
        dock = SimpleNamespace(get_mac_address=lambda: (0, 6, 0x66, 0xB9, 0x80, link.fake.active),
                               get_firmware_version=lambda: (3, fw_type, 1, 0, 5))
        return fn(dock)
    monkeypatch.setattr(link, "_with_shimmer", fake_with_shimmer)
    infos = link.read_all()
    assert routed == [1, 2, 4]                                # empty slots are never routed
    assert [i.occupied for i in infos] == [True, True, False, True, False, False]
    assert infos[0].mac == "00:06:66:B9:80:01" and infos[0].name == "Shimmer3-8001"
    assert infos[0].can_stream and not infos[3].can_stream and infos[3].firmware == "SDLog 1.0.5"
    assert link.fake.active is None                           # left disconnected, as found


def port(sn, dev, loc=None, product="Shimmer Base6U Rev2 08Aug15"):
    return SimpleNamespace(vid=FTDI_VID, pid=FT4232H_PID, serial_number=sn, location=loc, device=dev,
                           manufacturer="Shimmer", product=product, description=product)


def test_ports_grouped_per_base_on_linux_and_windows(monkeypatch):
    linux = [port("SR3VRR8A", f"/dev/ttyUSB{i}", f"3-2.3:1.{i}") for i in range(4)]
    windows = [port("SR3VRR8A" + c, f"COM{7 + i}", product=None) for i, c in enumerate("ABCD")]
    assert _group_ftdi_ports(linux) == {(FT4232H_PID, "SR3VRR8A"): [f"/dev/ttyUSB{i}" for i in range(4)]}
    monkeypatch.setattr(base.sys, "platform", "win32")       # Windows' FTDI driver reports no product name
    assert _group_ftdi_ports(windows) == {(FT4232H_PID, "SR3VRR8A"): ["COM7", "COM8", "COM9", "COM10"]}
    two_bases = linux + [port("XY12345B", f"/dev/ttyUSB{4 + i}") for i in range(4)]
    assert len(_group_ftdi_ports(two_bases)) == 2


def test_unrelated_ftdi_equipment_is_not_probed(monkeypatch):
    monkeypatch.setattr(base.sys, "platform", "linux")
    other = SimpleNamespace(vid=FTDI_VID, pid=FT4232H_PID, serial_number="FT123", location=None, device="/dev/ttyUSB9",
                            manufacturer="FTDI", product="Quad RS232-HS", description="Quad RS232-HS")
    assert _group_ftdi_ports([other]) == {}


# ---------------------------------------------------------------- docked setup flow

MAC = "00:06:66:B9:86:84"


class FakeLink:
    def __init__(self):
        self.clock_set = []

    def set_clock(self, slot):
        self.clock_set.append(slot)

    def disconnect(self):
        pass


class FakeBackend:
    def __init__(self, visible=True, paired=False, pin_ok=("1234",)):
        self.visible, self.paired, self.pin_ok = visible, paired, pin_ok
        self.pins, self.forgotten = [], 0

    def find(self, addresses, timeout=20.0):
        return {MAC: BtDevice(MAC, "Shimmer3-8684", self.paired)} if self.visible else {}

    def pair(self, dev, pin_provider):
        pin = pin_provider(dev.name)
        self.pins.append(pin)
        if pin not in self.pin_ok:
            raise PairingError("Authentication failed - wrong PIN.", "", retryable=True)
        self.paired = True

    def forget(self, dev):
        self.forgotten += 1
        self.paired = False


@pytest.fixture
def probe(monkeypatch):
    calls = {"results": []}

    def fake_probe(mac, name, cfg, attempts=3, progress=None):
        r = calls["results"].pop(0) if calls["results"] else {"ok": True}
        return {"ok": True, "kind": "ECG/ExG", "rate_hz": 256.0, "firmware": "LogAndStream 1.0.5",
                "battery_pct": 90.0, **r}
    monkeypatch.setattr(dock_setup, "probe_device", fake_probe)
    return calls


def slot(**kw):
    return SlotInfo(**{"slot": 1, "occupied": True, "mac": MAC, "firmware": "LogAndStream 0.11.0",
                       "can_stream": True, **kw})


def test_unpaired_shimmer_is_paired_with_default_pin_and_selected(tmp_path, probe):
    cfg, link, bt = Config(tmp_path / "c.json"), FakeLink(), FakeBackend()
    r = dock_setup.setup_slot(link, slot(), bt, cfg, ask_user=lambda n: pytest.fail("must not ask"))
    assert r.status == "ready" and "newly paired" in r.message
    assert bt.pins == ["1234"] and link.clock_set == [1]
    assert MAC in cfg.data["selected"]


def test_user_asked_only_when_default_pin_refused(tmp_path, probe):
    bt = FakeBackend(pin_ok=("0000",))
    r = dock_setup.setup_slot(FakeLink(), slot(), bt, Config(tmp_path / "c.json"), ask_user=lambda n: "0000")
    assert r.status == "ready" and bt.pins == ["1234", "0000"]


def test_stale_pairing_is_replaced_only_after_repeated_failure(tmp_path, probe):
    probe["results"] = [{"ok": False, "error": "device did not answer"}]
    bt = FakeBackend(paired=True)
    r = dock_setup.setup_slot(FakeLink(), slot(), bt, Config(tmp_path / "c.json"), ask_user=lambda n: None)
    assert r.status == "ready" and r.details["repaired"] and bt.forgotten == 1


def test_working_pairing_is_left_alone(tmp_path, probe):
    bt = FakeBackend(paired=True)
    r = dock_setup.setup_slot(FakeLink(), slot(), bt, Config(tmp_path / "c.json"), ask_user=lambda n: None)
    assert r.status == "ready" and bt.forgotten == 0 and bt.pins == []


@pytest.mark.parametrize("info, backend, expected", [
    (slot(firmware="SDLog 0.22.0", can_stream=False), FakeBackend(), "cannot stream over Bluetooth"),
    (slot(mac="", error="docked Shimmer did not answer"), FakeBackend(), "push it back into the slot"),
    (slot(), FakeBackend(visible=False), "not visible over Bluetooth"),
])
def test_problems_are_explained(tmp_path, probe, info, backend, expected):
    link = FakeLink()
    r = dock_setup.setup_slot(link, info, backend, Config(tmp_path / "c.json"), ask_user=lambda n: None)
    assert r.status == "problem" and expected in r.message
    assert link.clock_set == []                                # nothing written to a Shimmer that failed


def test_empty_slots_are_skipped(tmp_path, probe):
    res = dock_setup.setup_all(FakeLink(), [SlotInfo(1, False), slot(slot=2)], FakeBackend(),
                               Config(tmp_path / "c.json"), ask_user=lambda n: None)
    assert [r.slot for r in res] == [2]
