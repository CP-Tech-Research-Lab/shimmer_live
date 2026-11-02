"""The examples/load_recording.py loader reads real recorder output and recovers sample times."""
import importlib.util
import random
from pathlib import Path

import numpy as np
import pytest

from shimmer_live import clocks
from shimmer_live.recorder import Recorder

from .conftest import GSR_CHANNELS, make_session, packet, reconnect

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("load_recording", ROOT / "examples" / "load_recording.py")
loader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(loader)

UNIX_BASE = 1_700_000_000_000_000_000
DRIFT = 30e-6                       # Shimmer crystal 30 ppm fast relative to the PC


def test_loader_recovers_sampling_times(tmp_path, clock, monkeypatch):
    rng = random.Random(1)
    # Packets reach the PC 0-80 ms after they were sampled; the true instant is clock.ns.
    monkeypatch.setattr(clocks, "unix_ns", lambda: UNIX_BASE + clock.ns + int(rng.uniform(0, 0.08) * 1e9))
    truth = []
    s = make_session(tmp_path, GSR_CHANNELS, 32.0, name="gsr")
    rec = Recorder(tmp_path / "rec", [s])

    def stream(n, ts):
        for _ in range(n):
            clock.advance((1 / 32) / (1 + DRIFT))
            truth.append((UNIX_BASE + clock.ns) / 1e9)
            s._on_packet(packet(GSR_CHANNELS, ts, GSR_RAW=2048))
            ts += 1024
        return ts

    ts = stream(400, 0)
    clock.advance(7.0)                                       # link lost for 7 s
    reconnect(s)
    stream(400, ts + 7 * 32768)
    rec.stop()

    devices = loader.load_recording(rec.csv_path)
    d = devices["gsr"]
    assert len(d) == 800
    assert sorted(d["period"].unique()) == [0, 1]
    err_ms = (d["sample_unix_s"].to_numpy() - np.array(truth)) * 1000
    assert np.abs(err_ms).max() < 2.0                         # despite 0-80 ms arrival jitter
    assert d["latency_ms"].min() >= -1e-6                     # nothing estimated to arrive early
    assert loader.channel_columns(d) == ["GSR_uS"]


def test_loader_command_line(tmp_path, clock, capsys):
    s = make_session(tmp_path, GSR_CHANNELS, 32.0, name="gsr")
    rec = Recorder(tmp_path / "rec", [s])
    for k in range(64):
        clock.advance(1 / 32)
        s._on_packet(packet(GSR_CHANNELS, k * 1024, GSR_RAW=2048))
    rec.stop()
    loader.main([str(rec.csv_path), "--save"])
    out = capsys.readouterr().out
    assert "gsr" in out and "64 samples" in out
    assert (rec.csv_path.parent / f"{rec.csv_path.stem}_gsr_aligned.csv").exists()


def test_split_periods():
    ticks = np.array([0, 1024, 2048, 3072, 400_000, 401_024])
    assert loader.split_periods(ticks).tolist() == [0, 0, 0, 0, 1, 1]
    assert loader.split_periods(np.array([5])).tolist() == [0]


@pytest.mark.parametrize("n", [1, 2])
def test_fit_handles_tiny_periods(n):
    t = np.arange(n) / 32
    a, b = loader._fit_lower_edge(t, t + 0.05)
    assert np.allclose(a * t + b, t + 0.05)
