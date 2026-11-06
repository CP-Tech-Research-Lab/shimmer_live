"""Plugin interface for Shimmer Live.

A plugin turns live signals from one Shimmer into derived signals (heart rate from ECG, tonic
level from EDA, ...). Derived signals are plotted in the live view next to the device's own
signals and written to the recording like any other signal.

Write a plugin by subclassing `Plugin`:

    from shimmer_live.plugins.api import InputSpec, OutputSpec, Plugin

    class MovingAverage(Plugin):
        id = "moving_average"                       # unique, stable (used in settings/files)
        name = "EDA 1 s average"
        description = "Mean skin conductance over the last second."
        inputs = [InputSpec(kind="gsr")]            # what the plugin needs from a device
        outputs = [OutputSpec("mean", "EDA 1 s mean", "uS")]

        def start(self, inputs):
            self.rate = inputs[0].rate
            self.buf = []

        def process(self, t, x):
            out_t, out_y = [], []
            for ti, xi in zip(t, x[:, 0]):
                self.buf = (self.buf + [xi])[-int(self.rate):]
                out_t.append(ti)
                out_y.append(sum(self.buf) / len(self.buf))
            return {"mean": (out_t, out_y)}

How the app uses a plugin
-------------------------
1. Enabled plugins are matched against each streaming device: `match()` picks, for every
   InputSpec, a signal of that device (default: first signal of the right kind, preferring
   InputSpec.preferred keys, with rate >= min_rate). No match = the plugin is not run for
   that device, and the live view says why.
2. One plugin instance is created per matched device, then `start(inputs)` is called once.
3. `process(t, x)` is called on a worker thread (never the UI or Bluetooth thread) every
   ~100 ms with the new samples:
       t  float array (n,)            sample times in seconds, Shimmer clock (exact spacing)
       x  float array (n, n_inputs)   calibrated values, columns in `inputs` order
   It returns {output key: (times, values)} for whatever it produced in this call (may be
   empty). Output times must be in the same Shimmer-clock seconds as `t`; they can lag the
   input (e.g. a beat detected 300 ms later) but must not be in the future.
4. `reset()` is called after a connection loss: the next `t` jumps forward over the gap.
5. `stop()` is called once when the live view closes.

Exceptions raised by a plugin are caught and shown in the live view; that plugin instance is
then disabled, the rest of the app keeps running.

Installing your own plugin: put the .py file in the plugins folder shown on the Plugins page.
Any `Plugin` subclass with an `id` found there is loaded at start-up.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

import numpy as np


@dataclass(frozen=True)
class InputSpec:
    """A signal the plugin needs from one device.

    kind:        "exg" (ECG/EMG), "gsr" (EDA), "vbatt", or "other" (any raw channel)
    min_rate:    minimum sampling rate in Hz
    preferred:   channel keys to try first, e.g. ("EXG1_CH1",) for ECG lead LL-RA
    """
    kind: str
    min_rate: float = 0.0
    preferred: tuple = ()
    description: str = ""


@dataclass(frozen=True)
class OutputSpec:
    """A derived signal the plugin produces.

    style: "line" for regularly sampled outputs, "points" for event-based outputs
    (one value per heartbeat, per detected peak, ...).
    """
    key: str
    label: str
    unit: str = ""
    style: str = "line"


@dataclass(frozen=True)
class SignalInfo:
    """Description of a device signal handed to a plugin."""
    device: str             # device name, e.g. "Shimmer3-8395"
    mac: str
    key: str                # channel key, e.g. "EXG1_CH1", "GSR"
    label: str              # e.g. "ECG LL-RA (gain 4)"
    unit: str               # e.g. "mV", "uS"
    rate: float             # Hz
    kind: str               # "exg", "gsr", "vbatt", "other"
    device_label: str = ""  # the label given to the Shimmer in the app, e.g. the participant ("alberto")


KIND_NAMES = {"exg": "ECG/EMG", "gsr": "EDA/GSR", "vbatt": "battery", "other": "raw"}


class AppContext:
    """Handed to start_app(): what the app-wide part of a plugin can do with the app."""

    def __init__(self, mark, keypoint_names):
        self._mark, self._names = mark, keypoint_names

    def mark(self, keypoint: str, when: float | None = None, source: str = "") -> str:
        """Mark a keypoint (its name or 1-based number), now or at `when` (seconds on the shared clock,
        see Plugin.host_time). Any thread. Returns "OK <name> <unix ns>" or "ERR <reason>" (e.g. when
        nothing is recording). `source` is saved with it (keypoints.csv Source column)."""
        return self._mark(keypoint, when, source)

    def keypoint_names(self) -> list:
        return self._names()


class Plugin(ABC):
    id: ClassVar[str] = ""
    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    version: ClassVar[str] = "1.0"
    inputs: ClassVar[list] = []
    outputs: ClassVar[list] = []
    # Optional parts (all off by default, so plugins written before them are unaffected):
    per_device: ClassVar[bool] = True    # False: no per-Shimmer part (start/process are never called)
    app_wide: ClassVar[bool] = False     # True: start_app/on_keypoint/stop_app run while the plugin is ticked
    _host_offset_s = 0.0                 # set by the app before each process() (see host_time)

    @classmethod
    def match(cls, signals: list) -> tuple[list | None, str]:
        """Choose one device signal per InputSpec. Returns (inputs, "") or (None, reason)."""
        chosen = []
        for spec in cls.inputs:
            cands = [s for s in signals if s.kind == spec.kind and s.rate >= spec.min_rate and s not in chosen]
            if not cands:
                need = f"{KIND_NAMES.get(spec.kind, spec.kind)} signal" + (
                    f" at ≥ {spec.min_rate:g} Hz" if spec.min_rate else "")
                return None, f"needs an {need}" if need[0] in "AEIOU" else f"needs a {need}"
            cands.sort(key=lambda s: spec.preferred.index(s.key) if s.key in spec.preferred else len(spec.preferred))
            chosen.append(cands[0])
        return chosen, ""

    @abstractmethod
    def start(self, inputs: list) -> None:
        """Called once with the matched SignalInfo list, before the first process()."""

    @abstractmethod
    def process(self, t: np.ndarray, x: np.ndarray) -> dict:
        """New samples in, {output key: (times, values)} out. See the module docstring."""

    def reset(self) -> None:
        """Connection lost; samples after this jump over a gap. Default: restart."""
        self.start(self._inputs)

    def stop(self) -> None:
        """Live view closing."""

    def host_time(self, t):
        """Shimmer-clock seconds (the `t` of process()) -> seconds on this PC's shared monotonic clock: the
        clock of the recordings' shared timestamps (QueryPerformanceCounter on Windows, CLOCK_MONOTONIC on
        Linux), which is also time.perf_counter() and LSL's local_clock()."""
        return np.asarray(t, dtype=float) + self._host_offset_s

    # ---- app-wide part (app_wide = True): runs whenever the plugin is ticked, whatever page is open,
    # including during SD recordings. One instance for the whole app, separate from the per-Shimmer ones.
    def start_app(self, app: AppContext) -> None:
        """Plugin ticked (or app started with it ticked)."""

    def on_keypoint(self, event: dict) -> None:
        """A keypoint was marked (button, key, TCP, a plugin...), during a live or an SD recording. UI thread:
        keep it quick. event: name, color, unix_ns, time (shared-clock seconds), source ("" for a button)."""

    def stop_app(self) -> None:
        """Plugin unticked, or the app is closing."""


def output_label(plugin_cls, spec: OutputSpec) -> str:
    return f"{plugin_cls.name}: {spec.label}"
