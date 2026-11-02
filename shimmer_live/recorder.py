"""CSV recording of all streaming Shimmers into one long-format file.

One row per received sample. Columns:
  device, mac            which Shimmer the row belongs to
  host_unix_ns           system wall clock (UTC epoch ns) when the packet was received
  host_utc_iso           the same instant, human readable (UTC)
  shared_clock_ticks     monotonic shared clock at reception (Windows: QueryPerformanceCounter)
  shared_clock_s         shared_clock_ticks * seconds-per-cycle (see clocks.py; Unreal-compatible)
  shimmer_ts_raw         the Shimmer's own 24-bit timestamp as sent (32768 ticks/s, wraps every 512 s)
  shimmer_ticks          shimmer_ts_raw unwrapped to a continuous count (0 = first sample of the session)
  shimmer_time_s         shimmer_ticks / 32768
  <CH>_raw, <CH>_<unit>  every channel the devices stream: raw value and calibrated value
Rows of a device leave the other devices' channel columns empty.

A sidecar <name>.meta.json holds device configuration, clock sources, connection
events (losses/reconnects with timestamps) and the summary shown at stop.
"""
import csv
import json
import logging
import os
import queue
import re
import shutil
import threading
from pathlib import Path

from . import clocks

log = logging.getLogger("shimmerlive.recorder")

COMMON_COLUMNS = ["device", "mac", "host_unix_ns", "host_utc_iso", "shared_clock_ticks", "shared_clock_s",
                  "shimmer_ts_raw", "shimmer_ticks", "shimmer_time_s"]


def _fmt(v) -> str:
    if isinstance(v, float):
        return "" if v != v else f"{v:.9g}"
    return str(v)


class DeviceStats:
    def __init__(self, session):
        self.name, self.mac, self.rate = session.name, session.mac, session.rate
        self.samples = 0
        self.first_ticks = self.last_ticks = None
        self.disconnects = 0
        self.downtime_s = 0.0
        self.longest_gap_s = 0.0
        self.lost_since_perf = None


class Recorder:
    def __init__(self, folder: Path, sessions):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.start_unix_ns = clocks.unix_ns()
        self.start_shared_ticks = clocks.shared_clock_ticks()
        self.start_perf_ns = clocks.perf_ns()
        self.base = "shimmer_" + clocks.local_stamp_for_filename(self.start_unix_ns)
        self.sessions = [s for s in sessions if s.specs]

        columns = list(COMMON_COLUMNS)
        for s in self.sessions:
            for spec in s.specs:
                for col in spec.columns:
                    if col not in columns:
                        columns.append(col)
        self.columns = columns
        idx = {c: i for i, c in enumerate(columns)}
        self._layout = {}
        for s in self.sessions:
            self._layout[s.mac] = [(spec.convert, idx[spec.columns[0]],
                                    idx[spec.columns[1]] if spec.convert else None) for spec in s.specs]
        self.stats = {s.mac: DeviceStats(s) for s in self.sessions}
        self.events = []
        self.write_error = None
        self.stop_unix_ns = None

        self.csv_path = self.folder / f"{self.base}.csv"
        self.meta_path = self.folder / f"{self.base}.meta.json"
        self._fh = open(self.csv_path, "w", newline="", encoding="utf-8")
        self._csv = csv.writer(self._fh)
        self._csv.writerow(columns)
        self._q = queue.SimpleQueue()
        self._running = True
        self._writer = threading.Thread(target=self._write_loop, daemon=True, name="csv-writer")
        self._writer.start()
        for s in self.sessions:
            if s.state.value == "lost":
                self.stats[s.mac].lost_since_perf = s.state_since_ns
                self.stats[s.mac].disconnects += 1
        self._write_meta()
        for s in self.sessions:
            s.recorder = self
        log.info("recording to %s", self.csv_path)

    # called from session reader threads
    def put(self, session, host_unix_ns, shared_ticks, ts_raw, ticks, raws):
        self._q.put((session.name, session.mac, host_unix_ns, shared_ticks, ts_raw, ticks, raws))

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
        self.events.append({"host_unix_ns": unix_ns, "host_utc_iso": clocks.utc_iso(unix_ns),
                            "device": session.name, "mac": session.mac, "event": kind, "text": text})

    def _write_loop(self):
        n = len(self.columns)
        last_flush = clocks.perf_ns()
        while self._running or not self._q.empty():
            try:
                item = self._q.get(timeout=0.25)
            except queue.Empty:
                item = None
            if item is not None:
                name, mac, hu, hs, ts, ticks, raws = item
                row = [""] * n
                row[0:9] = [name, mac, hu, clocks.utc_iso(hu), hs, repr(clocks.shared_clock_seconds(hs)),
                            ts, ticks, f"{ticks / clocks.TICKS_PER_S:.6f}"]
                for (conv, i_raw, i_cal), raw in zip(self._layout[mac], raws):
                    row[i_raw] = raw
                    if i_cal is not None:
                        row[i_cal] = _fmt(conv(raw))
                st = self.stats[mac]
                st.samples += 1
                if st.first_ticks is None:
                    st.first_ticks = ticks
                st.last_ticks = ticks
                try:
                    self._csv.writerow(row)
                except OSError as e:
                    self.write_error = str(e)
            now = clocks.perf_ns()
            if now - last_flush > 5e8:
                try:
                    self._fh.flush()
                except OSError as e:
                    self.write_error = str(e)
                last_flush = now

    def elapsed_s(self) -> float:
        return (clocks.perf_ns() - self.start_perf_ns) / 1e9

    def free_disk_bytes(self) -> int:
        try:
            return shutil.disk_usage(self.folder).free
        except OSError:
            return -1

    def stop(self) -> dict:
        stop_perf = clocks.perf_ns()
        self.stop_unix_ns = clocks.unix_ns()
        for s in self.sessions:
            s.recorder = None
        self._running = False
        self._writer.join(timeout=10)
        self._fh.close()
        duration = (stop_perf - self.start_perf_ns) / 1e9
        for st in self.stats.values():
            if st.lost_since_perf is not None:      # still lost at stop
                gap = (stop_perf - max(st.lost_since_perf, self.start_perf_ns)) / 1e9
                st.downtime_s += gap
                st.longest_gap_s = max(st.longest_gap_s, gap)
        self.summary = {
            "duration_s": duration,
            "start_utc": clocks.utc_iso(self.start_unix_ns),
            "stop_utc": clocks.utc_iso(self.stop_unix_ns),
            "file_bytes": self.csv_path.stat().st_size,
            "write_error": self.write_error,
            "devices": [{
                "name": st.name, "mac": st.mac, "rate_hz": st.rate, "samples": st.samples,
                "expected_samples": int(round(st.rate * duration)),
                "completeness_pct": 100.0 * st.samples / max(1.0, st.rate * duration),
                "shimmer_span_s": ((st.last_ticks - st.first_ticks) / clocks.TICKS_PER_S
                                   if st.first_ticks is not None else 0.0),
                "disconnects": st.disconnects, "downtime_s": st.downtime_s,
                "longest_gap_s": st.longest_gap_s,
            } for st in self.stats.values()],
        }
        self._write_meta()
        log.info("recording stopped: %s", self.csv_path)
        return self.summary

    def _write_meta(self):
        meta = {
            "file": self.csv_path.name,
            "start_host_unix_ns": self.start_unix_ns,
            "start_host_utc_iso": clocks.utc_iso(self.start_unix_ns),
            "start_shared_clock_ticks": self.start_shared_ticks,
            "start_shared_clock_s": clocks.shared_clock_seconds(self.start_shared_ticks),
            "stop_host_unix_ns": self.stop_unix_ns,
            "clocks": clocks.clock_report(),
            "columns": self.columns,
            "notes": [
                "host_* and shared_clock_* timestamps are taken when a packet is received by this PC; "
                "Bluetooth adds a variable latency (typically 10-100 ms, packets may arrive in bursts). "
                "For precise alignment, fit shimmer_time_s against host time (linear regression) per device.",
                "shimmer_ticks continue across reconnects; wraps of the 24-bit counter during a "
                "gap are estimated from the host clock.",
                "For ECG devices the first 50 ms after each (re)start of streaming are discarded "
                "(ADS1292R start-up zeros).",
            ],
            "devices": [{
                "name": s.name, "mac": s.mac, "kind": s.kind, "firmware": s.firmware,
                "sampling_rate_hz": s.rate, "link": s.link_desc,
                "channels": [{"channel": sp.chan.name, "columns": sp.columns, "label": sp.label,
                              "unit": sp.unit} for sp in s.specs],
            } for s in self.sessions],
            "events": self.events,
            "summary": getattr(self, "summary", None),
        }
        tmp = self.meta_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        os.replace(tmp, self.meta_path)

    def rename(self, user_name: str) -> Path:
        """Prefix the files with a user-chosen name; the start timestamp is always kept."""
        clean = re.sub(r"[^\w\-.]+", "_", user_name.strip()).strip("._")
        if not clean:
            return self.csv_path
        base = f"{clean}_{self.base}"
        new_csv = self.folder / f"{base}.csv"
        new_meta = self.folder / f"{base}.meta.json"
        os.replace(self.csv_path, new_csv)
        meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
        meta["file"] = new_csv.name
        meta["recording_name"] = user_name
        new_meta.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        self.meta_path.unlink()
        self.csv_path, self.meta_path, self.base = new_csv, new_meta, base
        return new_csv
