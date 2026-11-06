"""Plugin interface additions: host time, device label, per_device=False, the app-wide part."""
import threading

import numpy as np
import pytest

from shimmer_live import clocks
from shimmer_live import keypoints as kpmod
from shimmer_live.config import Config
from shimmer_live.plugins import registry
from shimmer_live.plugins.api import Plugin
from shimmer_live.plugins.app import AppPlugins, at_shared
from shimmer_live.plugins.host import PluginHost, signal_infos, wait_idle

from .conftest import GSR_CHANNELS, feed, make_session


class HostTimes(Plugin):
    id = "host_times_test"
    name = "Host times"

    @classmethod
    def match(cls, signals):
        return list(signals), ""                     # every channel of the Shimmer

    def start(self, inputs):
        self.inputs, self.seen = inputs, []

    def process(self, t, x):
        self.seen.extend(self.host_time(t))
        return {}


class AppOnly(Plugin):
    id = "app_only_test"
    name = "App only"
    per_device = False
    app_wide = True
    events = []

    def start(self, inputs):
        raise AssertionError("never called: no per-Shimmer part")

    def process(self, t, x):
        raise AssertionError("never called")

    def start_app(self, app):
        self.app = app
        AppOnly.instance = self

    def on_keypoint(self, event):
        AppOnly.events.append(event)

    def stop_app(self):
        AppOnly.stopped = True


class Broken(AppOnly):
    id = "broken_test"

    def on_keypoint(self, event):
        raise RuntimeError("boom")


def test_host_time_is_when_each_sample_arrived_and_label_is_passed(tmp_path, clock):
    s = make_session(tmp_path, GSR_CHANNELS, 32.0)
    s.label = "alberto"
    host = PluginHost([registry.PluginEntry(HostTimes, "test"), registry.PluginEntry(AppOnly, "test")])
    try:
        host.bind(s)
        assert [b.cls for b in host.bindings] == [HostTimes]           # app-only plugin: no per-Shimmer part
        arrivals = []
        for k in range(32):
            feed(s, clock, GSR_CHANNELS, 1, start_ts=k * 1024)
            arrivals.append(clock.ns / 1e9)
        wait_idle(host)
        b = host.bindings[0]
        assert {i.device_label for i in b.plugin.inputs} == {"alberto"}
        assert len(b.plugin.inputs) == len(signal_infos(s)) > 1
        assert np.allclose(b.plugin.seen, arrivals, atol=1e-6)         # = the shared clock of the recordings
    finally:
        host.stop()


def test_app_wide_part_hears_keypoints_and_can_mark(tmp_path):
    cfg = Config(tmp_path / "c.json")
    marked = []
    app = AppPlugins(cfg, lambda target, at, source: (marked.append((target, at, source)), "OK x 1")[1],
                     lambda fn: fn())
    try:
        entries = [registry.PluginEntry(AppOnly, "test"), registry.PluginEntry(Broken, "test")]
        AppOnly.events, AppOnly.stopped = [], False
        app.sync(entries)
        assert set(app.running) == {"app_only_test", "broken_test"}
        kpmod.notify({"name": "task start", "color": "#f00", "unix_ns": 5, "shared_ticks": 2_000_000_000,
                      "source": "tcp:4005"})
        ev = AppOnly.events[-1]
        assert ev["name"] == "task start" and ev["source"] == "tcp:4005"
        assert ev["time"] == pytest.approx(clocks.shared_clock_seconds(2_000_000_000))
        assert "broken_test" not in app.running and "boom" in app.errors["broken_test"]   # isolated
        # marking from the plugin, from another thread, at a moment in the past
        when = clocks.shared_clock_seconds(clocks.shared_clock_ticks()) - 0.5
        out = []
        th = threading.Thread(target=lambda: out.append(AppOnly.instance.app.mark("2", when, "lsl:PsychoPy")))
        th.start()
        th.join(5)
        assert out == ["OK x 1"]
        target, at, source = marked[-1]
        assert target == "2" and source == "lsl:PsychoPy"
        assert clocks.shared_clock_seconds(at["shared_ticks"]) == pytest.approx(when, abs=1e-3)
        assert AppOnly.instance.app.keypoint_names()[0] == "rest start"
        app.sync([])                                                    # unticked: stopped
        assert app.running == {} and AppOnly.stopped
    finally:
        app.stop_all()
    kpmod.notify({"name": "x", "unix_ns": 1, "shared_ticks": 1})        # no longer listening: no error


def test_at_shared_moves_every_clock_together():
    now = at_shared(None)
    past = at_shared(clocks.shared_clock_seconds(now["shared_ticks"]) - 1.0)
    assert 0.9e9 < now["unix_ns"] - past["unix_ns"] < 1.1e9
    assert 0.9e9 < now["perf_ns"] - past["perf_ns"] < 1.1e9
