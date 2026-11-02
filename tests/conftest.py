import sys
from pathlib import Path

import pytest
from pyshimmer import EChannelType as E

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shimmer_live import clocks  # noqa: E402
from shimmer_live.config import Config  # noqa: E402
from shimmer_live.session import ShimmerSession, State  # noqa: E402

GSR_CHANNELS = [E.VBATT, E.GSR_RAW]
ECG_CHANNELS = [E.VBATT, E.EXG_ADS1292R_1_STATUS, E.EXG_ADS1292R_1_CH1_24BIT, E.EXG_ADS1292R_1_CH2_24BIT,
                E.EXG_ADS1292R_2_STATUS, E.EXG_ADS1292R_2_CH1_24BIT, E.EXG_ADS1292R_2_CH2_24BIT]
ECG_GAINS = {(0, 0): 4, (0, 1): 4, (1, 0): 4, (1, 1): 4}


class FakeClock:
    """Controllable host clock, so packet timing and reconnect gaps are exact in tests."""

    def __init__(self):
        self.ns = 5_000_000_000

    def advance(self, seconds):
        self.ns += int(round(seconds * 1e9))


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(clocks, "perf_ns", lambda: c.ns)
    monkeypatch.setattr(clocks, "unix_ns", lambda: 1_700_000_000_000_000_000 + c.ns)
    monkeypatch.setattr(clocks, "shared_clock_ticks", lambda: c.ns)
    return c


class Capture:
    """Stands in for a Recorder: collects what a session hands over."""

    def __init__(self):
        self.rows, self.events = [], []

    def put(self, session, host_unix_ns, shared_ticks, ticks, vals):
        self.rows.append((shared_ticks, ticks, vals))

    def event(self, session, kind, unix_ns, perf_ns, text):
        self.events.append(kind)


def make_session(tmp_path, chans, rate, gains=None, name="sim", mac="AA:BB:CC:DD:EE:FF"):
    s = ShimmerSession(mac, name, Config(tmp_path / "cfg.json"), "#000000", clocks.perf_ns())
    s.configure(rate, chans, gains or {})
    s.state = State.STREAMING
    return s


def packet(chans, ts, **values):
    """A decoded pyshimmer packet: {channel: value}, with defaults for unspecified channels."""
    p = {E.TIMESTAMP: ts % clocks.TS_WRAP}
    for c in chans:
        p[c] = values.get(c.name, 0)
    return p


def feed(session, clock, chans, n, start_ts, **values):
    """Deliver n packets at the session's rate, advancing the host clock with each."""
    step = int(round(clocks.TICKS_PER_S / session.rate))
    for k in range(n):
        clock.advance(1.0 / session.rate)
        session._on_packet(packet(chans, start_ts + k * step, **values))
    return start_ts + n * step


def reconnect(session):
    """What _open() does on a new connection: the next packet starts a new streaming period."""
    session._prev_ts = None
