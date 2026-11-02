"""Runs enabled plugins on live device data, on a worker thread of its own."""
import logging
import threading
import time
from collections import deque

import numpy as np

from ..session import Ring
from .api import SignalInfo

log = logging.getLogger("shimmerlive.plugins")

PROCESS_INTERVAL_S = 0.1
OUTPUT_BUFFER = 20000
GAP = object()


class Tap:
    """Receives samples from a session's reader thread (deque appends are thread-safe)."""

    def __init__(self, indices):
        self.indices = indices          # positions in session.specs of the plugin's inputs
        self.queue = deque()

    def push(self, t_shimmer, plot_offset, values):
        self.queue.append((t_shimmer, plot_offset, values))

    def gap(self):
        self.queue.append(GAP)


class Binding:
    """One plugin instance running on one device."""

    def __init__(self, entry, session, inputs):
        self.cls = entry.cls
        self.session = session
        self.inputs = inputs
        self.plugin = entry.cls()
        self.tap = Tap([next(i for i, s in enumerate(session.specs) if s.key == inp.key) for inp in inputs])
        self.rings = {o.key: Ring(1, OUTPUT_BUFFER) for o in self.cls.outputs}
        self.last = {}
        self.error = None
        self.plot_offset = 0.0

    @property
    def name(self):
        return f"{self.cls.name} · {self.session.name}"


def signal_infos(session) -> list:
    return [SignalInfo(session.name, session.mac, s.key, s.label, s.unit or "", session.rate, s.kind)
            for s in session.specs if s.convert is not None or s.kind == "other"]


class PluginHost:
    def __init__(self, entries):
        self.entries = entries
        self.bindings: list[Binding] = []
        self.unmatched = {}             # (plugin id, mac) -> reason
        self.recorder = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="plugin-host")
        self._thread.start()

    def bind(self, session):
        """Called once a session knows its channels: start every enabled plugin that matches."""
        infos = signal_infos(session)
        for entry in self.entries:
            inputs, reason = entry.cls.match(infos)
            if inputs is None:
                self.unmatched[(entry.cls.id, session.mac)] = reason
                continue
            b = Binding(entry, session, inputs)
            try:
                b.plugin._inputs = inputs
                b.plugin.start(inputs)
            except Exception as e:
                b.error = f"start failed: {type(e).__name__}: {e}"
                log.warning("%s: %s", b.name, b.error)
            with self._lock:
                self.bindings.append(b)
            session.taps.append(b.tap)
            log.info("plugin %s running on %s (inputs: %s)", entry.cls.name, session.name,
                     ", ".join(i.key for i in inputs))

    def bindings_for(self, session):
        return [b for b in self.bindings if b.session is session]

    def _run(self):
        while not self._stop.wait(PROCESS_INTERVAL_S):
            with self._lock:
                bindings = list(self.bindings)
            for b in bindings:
                if b.error is None:
                    self._step(b)

    def _step(self, b: Binding):
        items = []
        while b.tap.queue:
            items.append(b.tap.queue.popleft())
        if not items:
            return
        # split at gaps: process the samples before a gap, reset, continue
        chunk = []
        for it in items + [None]:
            if it is GAP or it is None:
                if chunk:
                    self._process(b, chunk)
                    chunk = []
                if it is GAP:
                    try:
                        b.plugin.reset()
                    except Exception as e:
                        b.error = f"reset failed: {type(e).__name__}: {e}"
                        return
            else:
                chunk.append(it)

    def _process(self, b: Binding, chunk):
        t = np.fromiter((c[0] for c in chunk), float, len(chunk))
        x = np.array([c[2] for c in chunk], dtype=float).reshape(len(chunk), -1)
        b.plot_offset = chunk[-1][1]
        try:
            result = b.plugin.process(t, x) or {}
        except Exception as e:
            b.error = f"{type(e).__name__}: {e}"
            log.warning("plugin %s stopped: %s", b.name, b.error)
            return
        rec = self.recorder
        for key, (ts, ys) in result.items():
            ring = b.rings.get(key)
            if ring is None:
                continue
            for tt, yy in zip(ts, ys):
                ring.push(float(tt) + b.plot_offset, [float(yy)])
                b.last[key] = float(yy)
                if rec is not None:
                    rec.put_plugin(b, key, float(tt), float(yy))

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2)
        for b in self.bindings:
            try:
                b.plugin.stop()
            except Exception:
                pass
            if b.tap in b.session.taps:
                b.session.taps.remove(b.tap)
        self.bindings = []


def wait_idle(host, timeout=1.0):
    """Test helper: wait until every tap queue has been processed."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and any(b.tap.queue for b in host.bindings):
        time.sleep(0.02)
    time.sleep(PROCESS_INTERVAL_S * 1.5)
