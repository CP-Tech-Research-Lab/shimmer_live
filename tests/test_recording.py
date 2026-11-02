"""Recorder (working files) + export (analysis, quality control, final ZIP)."""
import csv
import io
import json
import random
import zipfile

import numpy as np
import pandas as pd
import pytest

from shimmer_live import clocks, export
from shimmer_live.keypoints import Keypoint
from shimmer_live.plugins.api import OutputSpec, SignalInfo
from shimmer_live.recorder import Recorder
from shimmer_live.session import State

from .conftest import ECG_CHANNELS, ECG_GAINS, GSR_CHANNELS, make_session, packet, reconnect

UNIX_BASE = 1_700_000_000_000_000_000
REST = Keypoint("rest start", "#1baf7a", True)
TASK = Keypoint("task start", "#eb6834", True)


class FakePlugin:
    id, name, version = "hrv", "HRV", "1.0"
    outputs = [OutputSpec("hr", "Heart rate", "bpm"), OutputSpec("rr", "RR interval", "ms")]


class FakeBinding:
    cls = FakePlugin

    def __init__(self, session):
        self.session = session
        self.inputs = [SignalInfo(session.name, session.mac, "EXG1_CH1", "ECG", "mV", 256.0, "exg")]


@pytest.fixture
def latency(clock, monkeypatch):
    """Packets reach the PC 0-60 ms after sampling; the true sampling instant is clock.ns."""
    rng = random.Random(3)
    monkeypatch.setattr(clocks, "unix_ns", lambda: UNIX_BASE + clock.ns + int(rng.uniform(0, 0.06) * 1e9))
    monkeypatch.setattr(clocks, "shared_clock_ticks", lambda: clock.ns + int(rng.uniform(0, 0.06) * 1e9))
    return rng


def stream(sessions, clock, seconds, truth, values=None):
    """Deliver each session's packets at its own rate, interleaved in time order, and record
    every sample's true sampling instant (the fake clock) in truth[name]."""
    start = clock.ns
    end = start + int(seconds * 1e9)
    due = {s.mac: start + int(round(1e9 / s.rate)) for s in sessions}
    while True:
        s = min(sessions, key=lambda x: due[x.mac])
        if due[s.mac] > end:
            break
        clock.ns = due[s.mac]
        ts = getattr(s, "_sim_ts", 0)
        chans = GSR_CHANNELS if s.kind == "GSR+" else ECG_CHANNELS
        s._on_packet(packet(chans, ts, **(values or {}).get(s.name, {})))
        truth.setdefault(s.name, []).append(UNIX_BASE + clock.ns)
        s._sim_ts = ts + int(round(clocks.TICKS_PER_S / s.rate))
        due[s.mac] += int(round(1e9 / s.rate))


@pytest.fixture
def pair(tmp_path, clock):
    gsr = make_session(tmp_path, GSR_CHANNELS, 32.0, name="gsr", mac="00:00:00:00:00:01")
    ecg = make_session(tmp_path, ECG_CHANNELS, 256.0, ECG_GAINS, name="ecg", mac="00:00:00:00:00:02")
    return gsr, ecg


ALL_GSR = {"VBATT", "GSR"}
ALL_ECG = {"VBATT", "EXG1_STATUS", "EXG1_CH1", "EXG1_CH2", "EXG2_STATUS", "EXG2_CH1", "EXG2_CH2"}


def read_member(zf, name):
    text = zf.read(name).decode("utf-8")
    rows = list(csv.reader(io.StringIO(text)))
    return rows[0], rows[1], rows[2], rows[3:]


def test_full_pipeline(tmp_path, clock, latency, pair):
    gsr, ecg = pair
    binding = FakeBinding(ecg)
    rec = Recorder(tmp_path / "rec", [gsr, ecg],
                   {gsr.mac: ALL_GSR, ecg.mac: ALL_ECG - {"EXG2_CH2"}},     # Vx-RL not recorded
                   {binding: {"hr"}}, [REST, TASK])
    truth = {}
    values = {"gsr": {"GSR_RAW": 2048, "VBATT": 2800},
              "ecg": {"EXG_ADS1292R_1_CH1_24BIT": 300_000, "EXG_ADS1292R_1_CH2_24BIT": 100_000}}
    stream([gsr, ecg], clock, 3.0, truth, values)
    kp_true_ns = UNIX_BASE + clock.ns
    rec.keypoint(REST)
    stream([gsr, ecg], clock, 2.0, truth, values)
    rec.put_plugin(binding, "hr", 4.0, 72.0)
    rec.put_plugin(binding, "rr", 4.0, 833.0)                  # not selected: must not be written
    folder = rec.stop()

    a = export.analyze(folder)
    d = {x.meta["name"]: x for x in a.devices}
    assert set(d) == {"gsr", "ecg"}
    e = d["ecg"].data
    assert "ECG_Vx-RL_24BIT_CAL" not in e and "ECG_LL-RA_24BIT_CAL" in e
    assert np.allclose(e["ECG_LL-LA_24BIT_CAL"], e["ECG_LL-RA_24BIT_CAL"] - e["ECG_LA-RA_24BIT_CAL"])
    assert list(d["gsr"].data.columns[:3]) == ["Timestamp_Unix_CAL", "Timestamp_Shared_CAL", "Timestamp_CAL"]
    assert d["gsr"].units[:3] == ["ms", "s", "ms"]

    # aligned PC time recovers the true sampling time despite 0-60 ms arrival jitter
    for name in ("gsr", "ecg"):
        true_ms = np.array(truth[name][-len(d[name].data):]) / 1e6
        err = d[name].data["Timestamp_Unix_CAL"].to_numpy() - true_ms
        assert np.abs(err).max() < 3.0, name

    # keypoint marked on the nearest sample of every device
    for name in ("gsr", "ecg"):
        df = d[name].data
        row = df[df["Keypoint"] == "rest start"]
        assert len(row) == 1
        assert abs(row["Timestamp_Unix_CAL"].iloc[0] - kp_true_ns / 1e6) < 1000 / (32 if name == "gsr" else 256) + 61
    assert list(a.keypoints["Keypoint"]) == ["rest start"]

    hr = a.plugins[0].data
    assert list(hr.columns) == ["Timestamp_Unix_CAL", "Timestamp_Shared_CAL", "Timestamp_CAL", "hr"]
    assert hr["hr"].tolist() == [72.0]

    checks = {(q.device, q.check): q.status for q in a.qc}
    assert checks[("gsr", "Completeness")] == "OK"
    assert checks[("gsr", "Skin contact")] == "OK"
    assert ("recording", "rest start / rest end") in checks          # unmatched start flagged
    assert checks[("recording", "rest start / rest end")] == "WARNING"

    path = export.write_zip(a, "subject 03", "electrode fell off at the end\nline 2")
    assert path.name.startswith("subject_03_shimmer_") and path.suffix == ".zip"
    assert not folder.exists()                                       # working files removed
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        assert {"gsr.csv", "ecg.csv", "HRV - ecg.csv", "keypoints.csv", "qc.csv", "info.json",
                "notes.txt"} <= names
        sep, header, units, rows = read_member(zf, "ecg.csv")
        assert sep == ["sep=,"]
        assert header[:3] == ["ecg_Timestamp_Unix_CAL", "ecg_Timestamp_Shared_CAL", "ecg_Timestamp_CAL"]
        assert "ecg_ECG_LL-RA_24BIT_CAL" in header and units[header.index("ecg_ECG_LL-RA_24BIT_CAL")] == "mV"
        assert len(rows) == len(d["ecg"].data)
        info = json.loads(zf.read("info.json"))
        assert info["notes"].startswith("electrode fell off") and info["recording_name"] == "subject 03"
        assert zf.read("notes.txt").decode() == "electrode fell off at the end\nline 2"
        assert info["app_version"]


def test_connection_loss_stats_and_qc(tmp_path, clock, latency, pair):
    gsr, _ = pair
    rec = Recorder(tmp_path / "rec", [gsr], {gsr.mac: ALL_GSR}, {}, [])
    truth = {}
    stream([gsr], clock, 2.0, truth, {"gsr": {"GSR_RAW": 2048}})
    gsr.state, gsr.state_since_ns = State.LOST, clock.ns
    gsr.event("lost", "simulated")
    clock.advance(4.0)
    reconnect(gsr)
    gsr._sim_ts += int(4.0 * clocks.TICKS_PER_S)
    gsr.event("reconnected", "simulated")
    stream([gsr], clock, 2.0, truth, {"gsr": {"GSR_RAW": 2048}})
    a = export.analyze(rec.stop())
    st = a.devices[0].stats
    assert st["disconnects"] == 1 and st["downtime_s"] == pytest.approx(4.0)
    assert a.devices[0].stats["completeness_pct"] < 80
    q = {x.check: x for x in a.qc}
    assert q["Connection"].status == "WARNING"
    assert q["Completeness"].status == "FAIL"
    # periods are aligned separately; samples after the gap are still placed correctly
    true_ms = np.array(truth["gsr"]) / 1e6
    assert np.abs(a.devices[0].data["Timestamp_Unix_CAL"].to_numpy() - true_ms).max() < 3.0


def test_recovery_of_unsaved_recording(tmp_path, clock, latency, pair):
    gsr, _ = pair
    rec = Recorder(tmp_path / "rec", [gsr], {gsr.mac: ALL_GSR}, {}, [REST])
    stream([gsr], clock, 2.0, {}, {"gsr": {"GSR_RAW": 2048}})
    rec.keypoint(REST)
    rec._running = False                       # simulate a crash: no stop(), no stop.json
    rec._writer.join()
    for fh, *_ in rec._dev.values():
        fh.close()
    found = export.unfinished(tmp_path / "rec")
    assert found == [rec.dir]
    a = export.analyze(found[0])
    assert a.recovered and len(a.devices[0].data) == 64
    path = export.write_zip(a, "", "(recovered)")
    assert path.exists() and export.unfinished(tmp_path / "rec") == []


def test_eda_quality_checks():
    from shimmer_live import qc
    rate = 32.0
    good = 2.0 + 0.1 * np.sin(np.arange(640) / 40)
    assert {q.check: q.status for q in qc.check_eda("d", "GSR", good, rate)}["Skin contact"] == "OK"
    off = good.copy()
    off[:100] = 0.01
    assert {q.check: q.status for q in qc.check_eda("d", "GSR", off, rate)}["Skin contact"] == "FAIL"
    jumpy = good.copy()
    jumpy[::20] += 3.0
    assert {q.check: q.status for q in qc.check_eda("d", "GSR", jumpy, rate)}["Artifacts"] == "WARNING"


def test_ecg_quality_checks():
    from shimmer_live import qc
    rate = 256.0
    t = np.arange(int(rate * 20)) / rate
    ecg = sum(1.2 * np.exp(-0.5 * ((t - b) / 0.012) ** 2) for b in np.arange(0.5, 20, 0.8))
    res = {q.check: q.status for q in qc.check_ecg("d", "ECG", ecg, rate, 605.0)}
    assert res["Saturation"] == "OK" and res["Amplitude"] == "OK" and res["Mains interference"] == "OK"
    noisy = ecg + 1.5 * np.sin(2 * np.pi * 50 * t)
    assert {q.check: q.status for q in qc.check_ecg("d", "ECG", noisy, rate, 605.0)}["Mains interference"] == "WARNING"
    lead_off = np.full_like(t, -605.0)
    res = {q.check: q.status for q in qc.check_ecg("d", "ECG", lead_off, rate, 605.0)}
    assert res["Saturation"] == "FAIL" and res["Flat signal"] == "FAIL"
