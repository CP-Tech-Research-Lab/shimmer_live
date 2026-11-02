import csv
import json

import pytest

from shimmer_live import clocks
from shimmer_live.recorder import COMMON_COLUMNS, Recorder
from shimmer_live.session import State

from .conftest import ECG_CHANNELS, ECG_GAINS, GSR_CHANNELS, feed, make_session


@pytest.fixture
def sessions(tmp_path, clock):
    gsr = make_session(tmp_path, GSR_CHANNELS, 32.0, name="gsr", mac="00:00:00:00:00:01")
    ecg = make_session(tmp_path, ECG_CHANNELS, 256.0, ECG_GAINS, name="ecg", mac="00:00:00:00:00:02")
    return gsr, ecg


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_csv_layout_and_values(tmp_path, clock, sessions):
    gsr, ecg = sessions
    rec = Recorder(tmp_path / "rec", [gsr, ecg])
    feed(gsr, clock, GSR_CHANNELS, 32, start_ts=0, GSR_RAW=49855, VBATT=2800)
    feed(ecg, clock, ECG_CHANNELS, 64, start_ts=0, EXG_ADS1292R_1_CH1_24BIT=2 ** 22)
    rec.stop()
    rows = read_csv(rec.csv_path)
    assert list(rows[0])[:len(COMMON_COLUMNS)] == COMMON_COLUMNS
    g = [r for r in rows if r["device"] == "gsr"]
    e = [r for r in rows if r["device"] == "ecg"]
    assert len(g) == 32 and len(e) == 64 - 13
    assert float(g[0]["GSR_uS"]) == pytest.approx(0.009102, rel=1e-3)
    assert g[0]["EXG1_CH1_mV"] == ""                       # other devices' columns stay empty
    assert float(e[0]["EXG1_CH1_mV"]) == pytest.approx(2 ** 22 * 2420 / 4 / (2 ** 23 - 1))
    r = g[0]
    assert float(r["shared_clock_s"]) == clocks.shared_clock_seconds(int(r["shared_clock_ticks"]))
    assert float(r["shimmer_time_s"]) == int(r["shimmer_ticks"]) / clocks.TICKS_PER_S


def test_summary_counts_loss_and_downtime(tmp_path, clock, sessions):
    gsr, ecg = sessions
    rec = Recorder(tmp_path / "rec", [gsr, ecg])
    feed(gsr, clock, GSR_CHANNELS, 32, start_ts=0)
    gsr.state, gsr.state_since_ns = State.LOST, clock.ns
    gsr.event("lost", "simulated")
    clock.advance(4.0)
    gsr.event("reconnected", "simulated")
    summary = rec.stop()
    d = {x["name"]: x for x in summary["devices"]}
    assert d["gsr"]["disconnects"] == 1
    assert d["gsr"]["downtime_s"] == pytest.approx(4.0)
    assert d["gsr"]["samples"] == 32
    assert d["ecg"]["disconnects"] == 0
    meta = json.loads(rec.meta_path.read_text(encoding="utf-8"))
    assert [ev["event"] for ev in meta["events"]] == ["lost", "reconnected"]
    assert meta["clocks"]["shared_clock"]["seconds_per_cycle"] == clocks.SHARED_CLOCK_SECONDS_PER_CYCLE


def test_rename_keeps_timestamp_and_sanitises(tmp_path, clock, sessions):
    rec = Recorder(tmp_path / "rec", list(sessions))
    rec.stop()
    stamp = rec.base
    new = rec.rename("subject 03/baseline: run*1")
    assert new.name == f"subject_03_baseline_run_1_{stamp}.csv"
    assert new.exists() and rec.meta_path.exists()
    assert not (tmp_path / "rec" / f"{stamp}.csv").exists()
    meta = json.loads(rec.meta_path.read_text(encoding="utf-8"))
    assert meta["file"] == new.name and meta["recording_name"] == "subject 03/baseline: run*1"


def test_filename_has_start_time_and_no_colons(tmp_path, clock, sessions):
    rec = Recorder(tmp_path / "rec", list(sessions))
    rec.stop()
    assert rec.csv_path.name.startswith("shimmer_") and "_UTC" in rec.csv_path.name
    assert ":" not in rec.csv_path.name
