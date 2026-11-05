"""Recording, phase 1: stream everything to crash-safe working files while recording.

Working folder  <recordings>/.shimmer_<start time>.partial/
  session.json        devices, recorded channels, plugin outputs, keypoint definitions, clocks
  device_<n>.csv      shimmer_ticks, host_unix_ns, shared_ticks, then one column per recorded channel:
                      raw device values (default) or calibrated values, see VALUES_RAW/VALUES_CAL
  plugin_<n>.csv      shimmer_s, output, value     (one row per plugin output value)
  events.jsonl        keypoints, connection losses/reconnects, clock notes (flushed per line)
  stop.json           written at Stop (absent if the app closed while recording)

Phase 2 (export.py) turns this folder into the final ZIP, with aligned timestamps, quality
checks and notes, and deletes it. If the app closes while recording, the folder survives and
is offered for recovery at the next start.
"""
import csv
import json
import logging
import os
import queue
import shutil
import threading
from pathlib import Path

from . import __version__, clocks
from .conversions import EXG_FULL_SCALE

log = logging.getLogger("shimmerlive.recorder")

PARTIAL_SUFFIX = ".partial"
VALUES_RAW, VALUES_CAL = "raw", "calibrated"
FLUSH_INTERVAL_NS = 500_000_000


class DeviceStats:
    def __init__(self, session):
        self.name, self.mac, self.rate = session.name, session.mac, session.rate
        self.samples = 0
        self.disconnects = 0
        self.downtime_s = 0.0
        self.longest_gap_s = 0.0
        self.lost_since_perf = None


def _json_dump(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _fmt(v) -> str:
    if isinstance(v, float):
        return "" if v != v else f"{v:.9g}"
    return str(v)


def _full_scale(spec):
    """Calibrated value of a saturated sample (ExG), used by quality control."""
    if spec.kind == "exg" and spec.convert is not None:
        return abs(spec.convert(EXG_FULL_SCALE))
    return None


class Recorder:
    def __init__(self, folder, sessions, record_keys: dict, plugin_outputs: dict, keypoints: list,
                 values: str = VALUES_RAW):
        """record_keys: {mac: set of channel keys}; plugin_outputs: {Binding: set of output keys};
        values: VALUES_RAW (device values, default) or VALUES_CAL (physical units)."""
        if values not in (VALUES_RAW, VALUES_CAL):
            raise ValueError(f"values must be {VALUES_RAW!r} or {VALUES_CAL!r}")
        self.values = values
        self.folder = Path(folder)
        self.start_unix_ns = clocks.unix_ns()
        self.start_shared_ticks = clocks.shared_clock_ticks()
        self.start_perf_ns = clocks.perf_ns()
        self.base = "shimmer_" + clocks.local_stamp_for_filename(self.start_unix_ns)
        self.dir = self.folder / f".{self.base}{PARTIAL_SUFFIX}"
        self.dir.mkdir(parents=True, exist_ok=False)

        plugin_items = [(b, set(k)) for b, k in plugin_outputs.items() if k]
        self._plugin = {b: (j, keys) for j, (b, keys) in enumerate(plugin_items)}
        macs_with_plugins = {b.session.mac for b, _ in plugin_items}
        self.sessions = [s for s in sessions if s.specs and (record_keys.get(s.mac) or s.mac in macs_with_plugins)]
        self._dev = {}
        devices_meta = []
        for n, s in enumerate(self.sessions):
            keys = record_keys.get(s.mac, set())
            idx = [i for i, sp in enumerate(s.specs) if sp.key in keys]
            fh = open(self.dir / f"device_{n}.csv", "w", newline="", encoding="utf-8")
            w = csv.writer(fh)
            w.writerow(["shimmer_ticks", "host_unix_ns", "shared_ticks"] + [s.specs[i].key for i in idx])
            self._dev[s.mac] = (fh, w, idx)
            devices_meta.append({
                "file": f"device_{n}.csv", "name": s.name, "mac": s.mac, "kind": s.kind, "firmware": s.firmware,
                "sampling_rate_hz": s.rate, "link": s.link_desc,
                "channels": [{"key": s.specs[i].key, "label": s.specs[i].label, "unit": s.specs[i].unit,
                              "export_name": s.specs[i].export_name, "export_unit": s.specs[i].export_unit,
                              "kind": s.specs[i].kind, "full_scale": _full_scale(s.specs[i]),
                              "calibration": s.specs[i].calibration} for i in idx],
            })
        plugins_meta = []
        self._pfiles = {}
        for b, (j, keys) in self._plugin.items():
            fh = open(self.dir / f"plugin_{j}.csv", "w", newline="", encoding="utf-8")
            w = csv.writer(fh)
            w.writerow(["shimmer_s", "output", "value"])
            self._pfiles[j] = (fh, w)
            plugins_meta.append({
                "file": f"plugin_{j}.csv", "id": b.cls.id, "name": b.cls.name, "version": b.cls.version,
                "device": b.session.name, "mac": b.session.mac, "inputs": [i.key for i in b.inputs],
                "outputs": [{"key": o.key, "label": o.label, "unit": o.unit} for o in b.cls.outputs if o.key in keys],
            })
        self.stats = {s.mac: DeviceStats(s) for s in self.sessions}
        self.write_error = None
        self.keypoints = []

        _json_dump(self.dir / "session.json", {
            "app_version": __version__, "base": self.base, "values": self.values,
            "start_unix_ns": self.start_unix_ns, "start_shared_ticks": self.start_shared_ticks,
            "clocks": clocks.clock_report(), "devices": devices_meta, "plugins": plugins_meta,
            "keypoint_definitions": [{"name": k.name, "color": k.color} for k in keypoints],
        })
        self._events_fh = open(self.dir / "events.jsonl", "a", encoding="utf-8")
        self._events_lock = threading.Lock()
        self._q = queue.SimpleQueue()
        self._running = True
        self._writer = threading.Thread(target=self._write_loop, daemon=True, name="recorder")
        self._writer.start()
        for s in self.sessions:
            if s.state.value == "lost":
                self.stats[s.mac].lost_since_perf = s.state_since_ns
                self.stats[s.mac].disconnects += 1
        for s in self.sessions:
            s.recorder = self
        log.info("recording to %s", self.dir)

    # ---------- called from session / plugin threads ----------
    def put(self, session, host_unix_ns, shared_ticks, ticks, vals, raws):
        self._q.put((0, session.mac, ticks, host_unix_ns, shared_ticks, raws if self.values == VALUES_RAW else vals))

    def put_plugin(self, binding, key, t_shimmer, value):
        entry = self._plugin.get(binding)
        if entry is not None and key in entry[1]:
            self._q.put((1, entry[0], t_shimmer, key, value))

    def _log_event(self, ev: dict):
        with self._events_lock:
            if self._events_fh.closed:
                return
            self._events_fh.write(json.dumps(ev) + "\n")
            self._events_fh.flush()

    def event(self, session, kind, unix_ns, perf_ns, text):
        st = self.stats.get(session.mac)
        if st is not None:
            if kind == "lost" and st.lost_since_perf is None:
                st.lost_since_perf = session.state_since_ns
                st.disconnects += 1
            elif kind == "reconnected" and st.lost_since_perf is not None:
                gap = (perf_ns - max(st.lost_since_perf, self.start_perf_ns)) / 1e9
                st.downtime_s += gap
                st.longest_gap_s = max(st.longest_gap_s, gap)
                st.lost_since_perf = None
        self._log_event({"type": kind, "device": session.name, "mac": session.mac, "unix_ns": unix_ns,
                         "text": text})

    def keypoint(self, kp, at=None, source="") -> dict:
        """Mark a keypoint now, or at `at` (kp_server.now() taken when a TCP message arrived). UI thread."""
        ev = {"type": "keypoint", "name": kp.name, "color": kp.color,
              "unix_ns": at["unix_ns"] if at else clocks.unix_ns(),
              "shared_ticks": at["shared_ticks"] if at else clocks.shared_clock_ticks()}
        if source:
            ev["source"] = source
        self.keypoints.append(ev)
        self._log_event(ev)
        return ev

    # ---------- writer thread ----------
    def _write_loop(self):
        last_flush = clocks.perf_ns()
        while self._running or not self._q.empty():
            try:
                item = self._q.get(timeout=0.25)
            except queue.Empty:
                item = None
            try:
                if item is not None and item[0] == 0:
                    _, mac, ticks, hu, hs, vals = item
                    fh, w, idx = self._dev[mac]
                    w.writerow([ticks, hu, hs] + [_fmt(vals[i]) for i in idx])
                    self.stats[mac].samples += 1
                elif item is not None:
                    _, j, t, key, value = item
                    self._pfiles[j][1].writerow([repr(t), key, _fmt(value)])
                now = clocks.perf_ns()
                if now - last_flush > FLUSH_INTERVAL_NS:
                    for fh, *_ in list(self._dev.values()) + list(self._pfiles.values()):
                        fh.flush()
                    last_flush = now
            except OSError as e:
                self.write_error = str(e)

    def elapsed_s(self) -> float:
        return (clocks.perf_ns() - self.start_perf_ns) / 1e9

    def free_disk_bytes(self) -> int:
        try:
            return shutil.disk_usage(self.folder).free
        except OSError:
            return -1

    def stop(self) -> Path:
        """Stop writing; returns the working folder for export.analyze()."""
        stop_perf = clocks.perf_ns()
        stop_unix, stop_shared = clocks.unix_ns(), clocks.shared_clock_ticks()
        for s in self.sessions:
            s.recorder = None
        self._running = False
        self._writer.join(timeout=10)
        for fh, *_ in list(self._dev.values()) + list(self._pfiles.values()):
            fh.close()
        with self._events_lock:
            self._events_fh.close()
        for st in self.stats.values():
            if st.lost_since_perf is not None:      # still lost at stop
                gap = (stop_perf - max(st.lost_since_perf, self.start_perf_ns)) / 1e9
                st.downtime_s += gap
                st.longest_gap_s = max(st.longest_gap_s, gap)
        _json_dump(self.dir / "stop.json", {
            "stop_unix_ns": stop_unix, "stop_shared_ticks": stop_shared, "write_error": self.write_error,
            "devices": {st.mac: {"disconnects": st.disconnects, "downtime_s": st.downtime_s,
                                 "longest_gap_s": st.longest_gap_s} for st in self.stats.values()},
        })
        log.info("recording stopped: %s", self.dir)
        return self.dir
