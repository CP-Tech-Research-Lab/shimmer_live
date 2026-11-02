import numpy as np
import pytest

from shimmer_live.plugins import registry
from shimmer_live.plugins.api import InputSpec, OutputSpec, Plugin, SignalInfo
from shimmer_live.plugins.builtin.hrv import HRV
from shimmer_live.plugins.host import PluginHost, signal_infos, wait_idle

from .conftest import ECG_CHANNELS, ECG_GAINS, GSR_CHANNELS, feed, make_session, reconnect

ECG_IN = [SignalInfo("d", "m", "EXG1_CH1", "ECG LL-RA", "mV", 256.0, "exg")]


def synthetic_ecg(fs=256.0, dur=90.0, polarity=1.0, seed=0):
    """ECG-like signal with known beat times, heart-rate variability, P/T waves, mains hum,
    baseline wander and noise."""
    rng = np.random.default_rng(seed)
    beats, tb = [], 0.5
    while tb < dur - 1:
        beats.append(tb)
        tb += 0.8 + 0.05 * np.sin(2 * np.pi * tb / 6) + rng.normal(0, 0.015)
    beats = np.array(beats)
    t = np.arange(int(dur * fs)) / fs
    ecg = np.zeros_like(t)
    for b in beats:
        ecg += 1.4 * np.exp(-0.5 * ((t - b) / 0.012) ** 2)
        ecg += 0.15 * np.exp(-0.5 * ((t - b + 0.18) / 0.03) ** 2)
        ecg += 0.3 * np.exp(-0.5 * ((t - b - 0.3) / 0.06) ** 2)
    ecg = polarity * ecg + 0.2 * np.sin(2 * np.pi * 50 * t) + 0.4 * np.sin(2 * np.pi * 0.2 * t)
    return t, ecg + rng.normal(0, 0.04, len(t)), beats


def run(plugin, t, x, chunk=26):
    out = {}
    for i in range(0, len(t), chunk):
        for k, (tt, yy) in plugin.process(t[i:i + chunk], x[i:i + chunk, None]).items():
            out.setdefault(k, []).extend(zip(tt, yy))
    return {k: np.array(v) for k, v in out.items()}


@pytest.mark.parametrize("polarity", [1.0, -1.0])
def test_hrv_detects_beats_accurately(polarity):
    t, ecg, beats = synthetic_ecg(polarity=polarity)
    p = HRV()
    p.start(ECG_IN)
    out = run(p, t, ecg)
    r_t = out["rr"][:, 0]
    nearest = beats[np.abs(beats[None, :] - r_t[:, None]).argmin(axis=1)]
    assert len(set(nearest)) == len(r_t)                         # no beat reported twice
    assert len(r_t) >= len(beats) - 3                            # (first beats are used for learning)
    assert np.abs(r_t - nearest).max() < 0.03                    # R peak within 30 ms
    rr_err = np.diff(r_t) * 1000 - np.diff(nearest) * 1000
    assert np.median(np.abs(rr_err)) < 3 and np.abs(rr_err).max() < 10
    true_rr = np.diff(beats) * 1000
    assert out["hr"][:, 1].mean() == pytest.approx(60000 / true_rr.mean(), rel=0.02)
    true_rmssd = np.sqrt(np.mean(np.diff(true_rr[-38:]) ** 2))
    assert out["rmssd"][-1, 1] == pytest.approx(true_rmssd, rel=0.15)


def test_hrv_restarts_after_gap():
    t, ecg, _ = synthetic_ecg(dur=40)
    p = HRV()
    p._inputs = ECG_IN
    p.start(ECG_IN)
    run(p, t[:5000], ecg[:5000])
    p.reset()
    out = run(p, t[6000:] + 20.0, ecg[6000:])                      # later data, after a jump in time
    assert len(out["hr"]) > 5
    assert np.all((out["hr"][:, 1] > 60) & (out["hr"][:, 1] < 90))


def test_match_prefers_lead_and_explains_mismatch(tmp_path, clock):
    ecg = make_session(tmp_path, ECG_CHANNELS, 256.0, ECG_GAINS)
    inputs, why = HRV.match(signal_infos(ecg))
    assert why == "" and inputs[0].key == "EXG1_CH1"
    gsr = make_session(tmp_path, GSR_CHANNELS, 32.0)
    inputs, why = HRV.match(signal_infos(gsr))
    assert inputs is None and why == "needs an ECG/EMG signal at ≥ 100 Hz"


GOOD_PLUGIN = '''
from shimmer_live.plugins.api import InputSpec, OutputSpec, Plugin

class Doubler(Plugin):
    id = "doubler"
    name = "Doubler"
    inputs = [InputSpec(kind="gsr")]
    outputs = [OutputSpec("x2", "Twice", "uS")]

    def start(self, inputs):
        pass

    def process(self, t, x):
        return {"x2": (t, x[:, 0] * 2)}
'''


def test_registry_loads_user_plugins_and_reports_broken_ones(tmp_path):
    (tmp_path / "good.py").write_text(GOOD_PLUGIN)
    (tmp_path / "broken.py").write_text("this is not python(")
    (tmp_path / "empty.py").write_text("x = 1\n")
    (tmp_path / "dup.py").write_text(GOOD_PLUGIN)
    entries, errors = registry.discover(tmp_path)
    ids = [e.cls.id for e in entries]
    assert "hrv" in ids and ids.count("doubler") == 1
    msgs = " | ".join(f"{src}: {m}" for src, m in errors)
    assert "broken.py" in msgs and "empty.py" in msgs and "already used" in msgs


class Doubler(Plugin):
    id = "doubler_test"
    name = "Doubler"
    inputs = [InputSpec(kind="gsr")]
    outputs = [OutputSpec("x2", "Twice", "uS")]

    def start(self, inputs):
        self.started = True

    def process(self, t, x):
        return {"x2": (t, x[:, 0] * 2)}


class Crasher(Doubler):
    id = "crasher_test"

    def process(self, t, x):
        raise ValueError("boom")


class Rec:
    def __init__(self):
        self.rows = []

    def put_plugin(self, binding, key, t, y):
        self.rows.append((binding.cls.id, key, t, y))


def test_host_runs_plugins_feeds_plots_and_recorder(tmp_path, clock):
    gsr = make_session(tmp_path, GSR_CHANNELS, 32.0)
    host = PluginHost([registry.PluginEntry(Doubler, "test"), registry.PluginEntry(Crasher, "test"),
                       registry.PluginEntry(HRV, "built-in")])
    host.recorder = rec = Rec()
    try:
        host.bind(gsr)
        assert ("hrv", gsr.mac) in host.unmatched                   # HRV needs ECG: not bound
        ts = feed(gsr, clock, GSR_CHANNELS, 32, start_ts=0, GSR_RAW=2048)
        wait_idle(host)
        b = {x.cls.id: x for x in host.bindings}
        t, y = b["doubler_test"].rings["x2"].snapshot()
        assert len(t) == 32 and y[0, -1] == pytest.approx(2 * gsr.last_values["GSR"])
        assert len(rec.rows) == 32 and rec.rows[0][:2] == ("doubler_test", "x2")
        assert b["crasher_test"].error == "ValueError: boom"        # isolated; others keep running
        clock.advance(3.0)
        reconnect(gsr)
        feed(gsr, clock, GSR_CHANNELS, 8, start_ts=ts + 3 * 32768, GSR_RAW=2048)
        wait_idle(host)
        assert len(b["doubler_test"].rings["x2"].snapshot()[0]) == 40
    finally:
        host.stop()
    assert gsr.taps == []


def test_example_plugin_template_loads_and_runs():
    """examples/plugins is what PLUGINS.md tells users to copy: it must keep working."""
    from pathlib import Path
    folder = Path(__file__).resolve().parents[1] / "examples" / "plugins"
    entries, errors = registry.discover(folder)
    assert errors == []
    tonic = next(e.cls for e in entries if e.cls.id == "eda_tonic")
    p = tonic()
    p.start([SignalInfo("d", "m", "GSR", "GSR", "uS", 32.0, "gsr")])
    t = np.arange(32 * 10) / 32
    x = 2.0 + 0.05 * np.sin(t)
    x[100] = 40.0                                         # a single artifact must not move the level
    out = {}
    for i in range(0, len(t), 3):
        for k, (tt, yy) in p.process(t[i:i + 3], x[i:i + 3, None]).items():
            out.setdefault(k, []).extend(yy)
    assert len(out["scl"]) == pytest.approx((10 - 4) * 4, abs=2)
    assert max(out["scl"]) < 2.1
