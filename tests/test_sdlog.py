"""Recording on the Shimmers' SD cards: names, clock correction, start/stop logic, import to a recording."""
import shutil
from pathlib import Path

import numpy as np
import pytest

from shimmer_live import clocks, export, sdlog
from shimmer_live.config import Config

DATA = Path(__file__).parent / "data" / "sd_ecg_8395_000.bin"     # 30 s logged by a real Shimmer3 (no electrodes)
MAC = "00:06:66:B9:83:95"


def test_session_name_is_safe_for_the_card():
    assert sdlog.clean_name(" P 12 / ECG ") == "P_12_ECG"
    assert len(sdlog.clean_name("a" * 40)) == sdlog.NAME_MAX
    assert sdlog.clean_name("///") == ""


def test_clock_offsets_are_removed_linearly():
    rtc = np.array([1000.0, 1050.0, 1100.0])
    start = {"offset_ms": -60.0, "pc_unix": 1000.0}
    stop = {"offset_ms": 40.0, "pc_unix": 1100.0}
    assert np.allclose(sdlog.pc_times(rtc, start, stop), rtc - np.array([-0.06, -0.01, 0.04]))
    assert np.allclose(sdlog.pc_times(rtc, start, None), rtc + 0.06)        # no stop measurement: constant
    assert np.allclose(sdlog.pc_times(rtc, None, None), rtc)


class FakeConn:
    """A Shimmer over Bluetooth: logging starts only out of the base (as verified on hardware)."""
    shimmer = {}

    def __init__(self, mac, cfg, attempts=0):
        self.st = FakeConn.shimmer
        if self.st.get("unreachable"):
            raise ConnectionError("did not answer")
        dev = self

        class Dev:
            def get_firmware_version(self):
                from types import SimpleNamespace
                return SimpleNamespace(name=dev.st.get("fw", "LogAndStream")), SimpleNamespace(major=1, minor=0, rel=5)

            def get_battery_state(self, _):
                return 90.0

            def get_inquiry(self):
                from types import SimpleNamespace
                return 256.0, 0, [] if dev.st.get("no_sensors") else [SimpleNamespace(name="EXG_ADS1292R_1_CH1_24BIT")]

            def _process_and_wait(self, cmd):
                dev.st["rtc_set"] = True

            def set_experiment_id(self, name):
                dev.st["exp"] = name

            def set_config_time(self, t):
                dev.st["config_time"] = t

            def start_logging(self):
                dev.st["logging"] = not dev.st.get("docked") and not dev.st.get("broken")

            def stop_logging(self):
                dev.st["logging"] = False
        self.dev = Dev()

    def status(self):
        return {b: bool(self.st.get(b, b == "sd_present")) for b in sdlog.STATUS_BITS}

    def offset(self, rounds=0):
        return {"offset_ms": -55.0, "round_trip_ms": 30.0, "pc_unix": 1.0}

    def close(self):
        pass


@pytest.fixture
def shimmer(monkeypatch):
    monkeypatch.setattr(sdlog, "Connection", FakeConn)
    monkeypatch.setattr(sdlog.time, "sleep", lambda s: None)
    FakeConn.shimmer = {}
    return FakeConn.shimmer


def test_start_sets_clock_and_name_then_confirms(shimmer, tmp_path):
    r = sdlog.start_device(MAC, Config(tmp_path / "c.json"), "P_12", 1793714116)
    assert shimmer["logging"] and shimmer["rtc_set"] and shimmer["exp"] == "P_12"
    assert shimmer["config_time"] == 1793714116 and r["start_offset"]["offset_ms"] == -55.0


def test_sd_start_does_not_proceed_if_ppg_power_preparation_fails(shimmer, tmp_path, monkeypatch):
    def failed_power(*args):
        raise RuntimeError("Could not verify PPG 3 V power ON")

    monkeypatch.setattr(sdlog, "prepare_ppg", failed_power)
    with pytest.raises(RuntimeError, match="PPG 3 V power"):
        sdlog.start_device(MAC, Config(tmp_path / "c.json"), "P_12", 1)
    assert not shimmer.get("logging")
    assert not shimmer.get("rtc_set")


@pytest.mark.parametrize("state, message", [
    ({"docked": True}, "in the base"),
    ({"logging": True}, "already recording"),
    ({"sd_present": False}, "no SD card"),
    ({"fw": "SDLog"}, "LogAndStream"),
    ({"broken": True}, "does not report recording"),
    ({"no_sensors": True}, "no sensors enabled"),
])
def test_start_refuses_with_a_reason(shimmer, tmp_path, state, message):
    shimmer.update(state)
    with pytest.raises(RuntimeError, match=message):
        sdlog.start_device(MAC, Config(tmp_path / "c.json"), "P_12", 1)


def test_stop_reports_whether_it_was_still_recording(shimmer, tmp_path):
    shimmer["logging"] = True
    r = sdlog.stop_device(MAC, Config(tmp_path / "c.json"))
    assert r["was_recording"] and not shimmer["logging"]
    assert not sdlog.stop_device(MAC, Config(tmp_path / "c.json"))["was_recording"]


def test_clock_read_failure_cannot_prevent_confirmed_stop(shimmer, tmp_path, monkeypatch):
    shimmer["logging"] = True

    def failed_clock(self, rounds=0):
        assert not self.st["logging"], "stop must precede optional clock reads"
        raise TimeoutError("RTC did not answer")

    monkeypatch.setattr(FakeConn, "offset", failed_clock)
    result = sdlog.stop_device(MAC, Config(tmp_path / "c.json"))
    assert not shimmer["logging"]
    assert result["was_recording"] and result["stop_offset"] is None
    assert "RTC did not answer" in result["clock_warning"]


def test_stop_is_not_confirmed_when_device_keeps_logging(shimmer, tmp_path, monkeypatch):
    shimmer["logging"] = True
    original_init = FakeConn.__init__

    def stuck_device(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.dev.stop_logging = lambda: None

    monkeypatch.setattr(FakeConn, "__init__", stuck_device)
    with pytest.raises(RuntimeError, match="still reports recording"):
        sdlog.stop_device(MAC, Config(tmp_path / "c.json"))


def test_card_layout_and_empty_file(tmp_path):
    run = tmp_path / "data" / "P_12_1793714116" / "ecg_8395-000"
    run.mkdir(parents=True)
    shutil.copy(DATA, run / "000")
    (run / "001").write_bytes(b"")                               # docked before it was stopped
    files = sdlog.session_files(tmp_path, "P_12", 1793714116)
    assert [f.name for f in files] == ["000", "001"]
    d = sdlog.read_files(files)
    assert len(d.ticks) == 7442 and d.rate_hz == 256.0 and d.gains[(0, 0)] == 4
    assert any("is empty" in p for p in d.problems)


def test_sd_import_becomes_a_normal_recording(tmp_path):
    d = sdlog.read_files([DATA])
    cfg = Config(tmp_path / "c.json")
    s = sdlog.new_session(cfg, "P_12", [(MAC, "Shimmer3-8395")], "raw", [{"name": "rest", "color": "#1aa"}])
    t0 = float(d.rtc_unix[0])
    s["devices"][MAC].update(state=sdlog.STOPPED, firmware="LogAndStream 1.0.5",
                             start={"start_offset": {"offset_ms": -60.0, "pc_unix": t0}},
                             stop={"stop_offset": {"offset_ms": -70.0, "pc_unix": t0 + 30}})
    s["keypoints"].append({"type": "keypoint", "name": "rest", "color": "#1aa", "unix_ns": int((t0 + 10) * 1e9),
                           "shared_ticks": 0})
    folder = sdlog.build_recording(s, {MAC: ("Shimmer3-8395", d, [DATA])}, tmp_path, "raw")
    a = export.analyze(folder)
    dev = a.devices[0]
    assert dev.stats["samples"] == 7442 and dev.stats["disconnects"] == 0
    unix_ms = dev.data["Timestamp_Unix_CAL"].to_numpy()
    assert abs(unix_ms[0] - (t0 + 0.060) * 1000) < 1.0                # start offset removed
    assert np.allclose(np.diff(unix_ms), 1000 / 256, atol=0.05)          # sample spacing kept
    assert "ECG_LL-RA_24BIT_RAW" in dev.data and (dev.data["Keypoint"] == "rest").sum() == 1
    z = export.write_zip(a, "sd", "", tmp_path)
    import zipfile
    names = zipfile.ZipFile(z).namelist()
    assert any(n.startswith("sd_raw/Shimmer3-8395/") for n in names) and "Shimmer3-8395.csv" in names


def test_session_survives_in_the_settings(tmp_path):
    cfg = Config(tmp_path / "c.json")
    sdlog.new_session(cfg, "P_12", [(MAC, "Shimmer3-8395")], "raw", [])
    cfg.data[sdlog.SESSION_KEY]["devices"][MAC]["state"] = sdlog.RECORDING
    cfg.save()
    again = Config(tmp_path / "c.json")
    assert sdlog.recording_macs(again) == {MAC}
    from types import SimpleNamespace
    sdlog.add_keypoint(again, SimpleNamespace(name="rest", color="#1aa"))
    assert Config(tmp_path / "c.json").data[sdlog.SESSION_KEY]["keypoints"][0]["name"] == "rest"


def test_start_sent_but_not_confirmed_is_never_failed(shimmer, tmp_path, monkeypatch):
    """Seen with 5 real Shimmers: start_logging worked, the confirming reconnect timed out, and the
    app called them 'did not start' while they were recording."""
    real = FakeConn.__init__
    calls = {"n": 0}

    def flaky(self, mac, cfg, attempts=0):
        calls["n"] += 1
        if calls["n"] > 1:                                 # every reconnect after the start times out
            raise ConnectionError("did not answer over Bluetooth (initialize got no reply within 10 s)")
        real(self, mac, cfg, attempts)
    monkeypatch.setattr(FakeConn, "__init__", flaky)
    with pytest.raises(sdlog.StartUnconfirmed) as e:
        sdlog.start_device(MAC, Config(tmp_path / "c.json"), "P_12", 1)
    assert shimmer["logging"] and e.value.result["start_offset"]["offset_ms"] == -55.0
