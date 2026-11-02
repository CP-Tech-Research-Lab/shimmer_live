"""Timestamp unwrapping, reconnect handling, start-up discard and plot decimation."""
import numpy as np
import pytest

from shimmer_live import clocks
from shimmer_live.session import STARTUP_DISCARD_S

from .conftest import ECG_CHANNELS, ECG_GAINS, GSR_CHANNELS, Capture, feed, make_session, packet, reconnect

GSR_STEP = 1024        # 32 Hz in 32768 Hz ticks
ECG_STEP = 128         # 256 Hz


def ticks_of(cap):
    return np.array([t for _, t, _ in cap.rows])


def test_ticks_unwrap_across_24bit_wrap(tmp_path, clock):
    s = make_session(tmp_path, GSR_CHANNELS, 32.0)
    s.recorder = cap = Capture()
    feed(s, clock, GSR_CHANNELS, 40, start_ts=clocks.TS_WRAP - 20 * GSR_STEP)
    t = ticks_of(cap)
    assert t[0] == 0
    assert np.all(np.diff(t) == GSR_STEP)               # no jump where the 24-bit counter wrapped


def test_reconnect_with_running_device_clock_counts_wraps(tmp_path, clock):
    s = make_session(tmp_path, GSR_CHANNELS, 32.0)
    s.recorder = cap = Capture()
    ts = feed(s, clock, GSR_CHANNELS, 10, start_ts=1000)
    gap_s = 600.0                                         # longer than one 512 s wrap period
    clock.advance(gap_s)
    reconnect(s)
    feed(s, clock, GSR_CHANNELS, 10, start_ts=ts + int(gap_s * clocks.TICKS_PER_S))
    t = ticks_of(cap)
    jump = t[10] - t[9]
    assert jump == pytest.approx((gap_s + 1 / 32) * clocks.TICKS_PER_S, abs=GSR_STEP)
    assert s._clock_note is None


def test_reconnect_with_reset_device_clock_uses_host_gap(tmp_path, clock):
    s = make_session(tmp_path, GSR_CHANNELS, 32.0)
    s.recorder = cap = Capture()
    feed(s, clock, GSR_CHANNELS, 10, start_ts=1_000_000)
    clock.advance(15.0)
    reconnect(s)
    feed(s, clock, GSR_CHANNELS, 10, start_ts=472_538)    # counter restarted, as 8395 did
    t = ticks_of(cap)
    assert np.all(np.diff(t) > 0)
    assert (t[10] - t[9]) / clocks.TICKS_PER_S == pytest.approx(15.0 + 1 / 32, abs=0.05)
    assert "discontinuity" in s._clock_note


def test_ecg_startup_samples_discarded_after_each_start(tmp_path, clock):
    s = make_session(tmp_path, ECG_CHANNELS, 256.0, ECG_GAINS)
    s.recorder = cap = Capture()
    ts = feed(s, clock, ECG_CHANNELS, 256, start_ts=0)
    discarded = int(np.ceil(STARTUP_DISCARD_S * 256))
    assert len(cap.rows) == 256 - discarded
    clock.advance(5.0)
    reconnect(s)
    feed(s, clock, ECG_CHANNELS, 256, start_ts=ts + 5 * 32768)
    assert len(cap.rows) == 2 * (256 - discarded)


def test_gsr_samples_never_discarded(tmp_path, clock):
    s = make_session(tmp_path, GSR_CHANNELS, 32.0)
    s.recorder = cap = Capture()
    feed(s, clock, GSR_CHANNELS, 32, start_ts=0)
    assert len(cap.rows) == 32


def test_plot_is_decimated_but_recording_is_not(tmp_path, clock):
    s = make_session(tmp_path, ECG_CHANNELS, 256.0, ECG_GAINS)
    assert s.plot_decim == 2
    s.recorder = cap = Capture()
    feed(s, clock, ECG_CHANNELS, 512, start_ts=0, EXG_ADS1292R_1_CH1_24BIT=1000)
    t, y = s.ring.snapshot()
    assert len(cap.rows) == 512 - 13                      # every sample after start-up is recorded
    assert len(t) == (512 - 13) // 2                      # plot holds pair averages
    ch1 = [i for i, sp in enumerate(s.plot_specs) if sp.key == "EXG1_CH1"][0]
    assert y[ch1, -1] == pytest.approx(1000 * 2420 / 4 / (2 ** 23 - 1))


def test_plot_averages_pairs(tmp_path, clock):
    s = make_session(tmp_path, ECG_CHANNELS, 256.0, ECG_GAINS)
    ch1 = [i for i, sp in enumerate(s.plot_specs) if sp.key == "EXG1_CH1"][0]
    ts = feed(s, clock, ECG_CHANNELS, 20, start_ts=0)     # stream past the start-up discard window
    for raw in (100, 300, 100, 300):
        clock.advance(1 / 256)
        s._on_packet(packet(ECG_CHANNELS, ts, EXG_ADS1292R_1_CH1_24BIT=raw))
        ts += ECG_STEP
    _, y = s.ring.snapshot()
    assert y[ch1, -1] == pytest.approx(200 * 2420 / 4 / (2 ** 23 - 1))


def test_gsr_not_decimated(tmp_path, clock):
    s = make_session(tmp_path, GSR_CHANNELS, 32.0)
    assert s.plot_decim == 1
    feed(s, clock, GSR_CHANNELS, 32, start_ts=0)
    t, _ = s.ring.snapshot()
    assert len(t) == 32


def test_gap_breaks_plotted_line(tmp_path, clock):
    s = make_session(tmp_path, GSR_CHANNELS, 32.0)
    ts = feed(s, clock, GSR_CHANNELS, 10, start_ts=0, GSR_RAW=2048)
    clock.advance(5.0)
    reconnect(s)
    feed(s, clock, GSR_CHANNELS, 10, start_ts=ts + 5 * 32768, GSR_RAW=2048)
    _, y = s.ring.snapshot()
    assert np.isnan(y[0]).sum() == 1


def test_health_flags(tmp_path, clock):
    s = make_session(tmp_path, ECG_CHANNELS, 256.0, ECG_GAINS)
    feed(s, clock, ECG_CHANNELS, 100, start_ts=0, VBATT=2800, EXG_ADS1292R_2_CH2_24BIT=-(2 ** 23))
    assert "EXG2_CH2" in s.saturated_since and "EXG1_CH1" not in s.saturated_since
    assert s.battery_pct is not None and 0 < s.battery_pct <= 100

    g = make_session(tmp_path, GSR_CHANNELS, 32.0)
    feed(g, clock, GSR_CHANNELS, 10, start_ts=0, GSR_RAW=49855)        # open circuit
    assert g.no_contact_since is not None
    feed(g, clock, GSR_CHANNELS, 10, start_ts=10 * GSR_STEP, GSR_RAW=2048)   # ~50 uS
    assert g.no_contact_since is None
