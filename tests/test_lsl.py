"""LSL plugin, over real LSL on this computer: Shimmer data out, keypoints out, markers in."""
import sys
import time

import numpy as np
import pytest

pylsl = pytest.importorskip("pylsl")

from shimmer_live import clocks  # noqa: E402
from shimmer_live.plugins.api import SignalInfo  # noqa: E402
from shimmer_live.plugins.builtin import lsl as lslmod  # noqa: E402

INPUTS = [SignalInfo("Shimmer3-A281", "40:84:32:56:A2:81", "GSR", "GSR skin conductance", "uS", 256.0, "gsr",
                     "alberto"),
          SignalInfo("Shimmer3-A281", "40:84:32:56:A2:81", "INT_ADC13", "PPG", "mV", 256.0, "other", "alberto")]


def inlet_for(**prop):
    (key, value), = prop.items()
    found = pylsl.resolve_byprop(key, value, 1, 5.0)
    assert found, f"LSL stream {prop} not found"
    inlet = pylsl.StreamInlet(found[0])
    inlet.open_stream(timeout=5)
    return inlet, found[0]


def wait(cond, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end and not cond():
        time.sleep(0.05)
    return cond()


def test_each_shimmer_becomes_a_stream_with_its_label_channels_and_sample_times():
    p = lslmod.LSL()
    p.start(INPUTS)
    try:
        inlet, info = inlet_for(source_id="shimmer-40843256A281")
        assert info.name() == "Shimmer3-A281 (alberto)" and info.type() == "GSR"
        assert info.channel_count() == 2 and info.nominal_srate() == 256.0
        full = inlet.info(timeout=5)
        ch = full.desc().child("channels").child("channel")
        assert ch.child_value("label") == "GSR" and ch.child_value("unit") == "uS"
        p._host_offset_s = 1000.0                                  # Shimmer t + 1000 s = shared clock
        t = np.arange(256) / 256.0
        x = np.column_stack([2 + t, 500 + t])
        p.process(t, x)
        got_x, got_t = [], []
        end = time.monotonic() + 8
        while len(got_x) < 256 and time.monotonic() < end:
            chunk, stamps = inlet.pull_chunk(timeout=0.2)
            got_x.extend(chunk)
            got_t.extend(stamps)
        assert len(got_x) == 256
        assert np.allclose(np.array(got_x), x, atol=1e-4)
        expected = t + 1000.0 + lslmod.lsl_minus_shared(pylsl)
        assert np.allclose(np.array(got_t) + inlet.time_correction(), expected, atol=2e-3)
    finally:
        p.stop()


class FakeApp:
    def __init__(self):
        self.marked = []

    def mark(self, keypoint, when=None, source=""):
        self.marked.append((keypoint, when, source))
        return "OK " + keypoint if keypoint in ("task start", "3") else "ERR unknown keypoint"

    def keypoint_names(self):
        return ["rest start", "rest end", "task start"]


def test_keypoints_go_out_as_markers_and_lsl_ones_are_not_echoed(monkeypatch):
    monkeypatch.setattr(lslmod, "RESOLVE_EVERY_S", 0.3)
    p = lslmod.LSL()
    p.start_app(FakeApp())
    try:
        inlet, info = inlet_for(name=lslmod.MARKER_STREAM)
        assert info.type() == "Markers"
        now = clocks.shared_clock_seconds(clocks.shared_clock_ticks())
        p.on_keypoint({"name": "lsl echo", "time": now, "source": "lsl:PsychoPy"})   # skipped
        p.on_keypoint({"name": "task start", "time": now - 0.25, "source": ""})
        sample, ts = inlet.pull_sample(timeout=5)
        assert sample == ["task start"]
        assert ts + inlet.time_correction() == pytest.approx(now - 0.25 + lslmod.lsl_minus_shared(pylsl), abs=2e-3)
        assert inlet.pull_sample(timeout=0.5)[0] is None                              # nothing else
    finally:
        p.stop_app()


def test_markers_from_other_programs_become_keypoints_at_their_own_time(monkeypatch):
    monkeypatch.setattr(lslmod, "RESOLVE_EVERY_S", 0.3)
    app = FakeApp()
    p = lslmod.LSL()
    p.start_app(app)
    other = pylsl.StreamOutlet(pylsl.StreamInfo("PsychoPy", "Markers", 1, pylsl.IRREGULAR_RATE, pylsl.cf_string,
                                                "psychopy-test"))
    try:
        assert wait(lambda: other.have_consumers(), 10), "Shimmer Live did not subscribe to the markers"
        sent_at = pylsl.local_clock() - 0.1
        other.push_sample(["task start"], sent_at)
        other.push_sample(["jump"], sent_at)
        assert wait(lambda: len(app.marked) >= 2)
        (k1, w1, s1), (k2, _, _) = app.marked[:2]
        assert (k1, s1, k2) == ("task start", "lsl:PsychoPy", "jump")
        assert w1 == pytest.approx(sent_at - lslmod.lsl_minus_shared(pylsl), abs=2e-3)   # the marker's time
        assert not any(s.endswith(lslmod.MARKER_STREAM) for _, _, s in app.marked)      # never our own stream
    finally:
        p.stop_app()
        del other


def test_missing_pylsl_is_reported_as_this_plugins_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "pylsl", None)
    with pytest.raises(RuntimeError, match="pylsl is not installed"):
        lslmod.LSL().start(INPUTS)
