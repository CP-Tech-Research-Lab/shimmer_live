"""One streaming Shimmer: connect (with retries), stream, detect link loss, reconnect.

All Bluetooth work runs on a per-device manager thread; pyshimmer's own reader thread
delivers packets to _on_packet(). The UI only reads attributes / snapshots.
"""
import logging
import threading
import time
from collections import deque
from enum import Enum

import numpy as np
from pyshimmer import EChannelType, ShimmerBluetooth

from . import clocks
from .conversions import (GSR_NO_CONTACT_US, battery_percent, build_specs, device_kind,
                          exg_saturated)
from .transport import DeviceUnreachable, open_link, quiesce

log = logging.getLogger("shimmerlive.session")

INITIAL_ATTEMPTS = 5
RETRY_PAUSE_S = 2.0
STARTUP_DISCARD_S = 0.05        # ADS1292R emits zeros for its first sample(s) after start
BUFFER_SECONDS = 65             # longest plot window + margin
PLOT_RATE_HZ = 128              # display only: ECG 256 Hz is averaged in pairs (ECG content <~40 Hz);
                                # slower signals (GSR 32 Hz) are plotted as-is. Recording keeps every sample.


class State(str, Enum):
    CONNECTING = "connecting"
    STREAMING = "streaming"
    LOST = "lost"
    FAILED = "failed"
    STOPPED = "stopped"


def timed(fn, timeout, *args):
    """Run fn on a daemon thread: pyshimmer commands wait forever on a dead link."""
    box = {}

    def run():
        try:
            box["v"] = fn(*args)
        except BaseException as e:      # noqa: BLE001 - re-raised below
            box["e"] = e

    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        raise TimeoutError(f"{getattr(fn, '__name__', 'command')} got no reply within {timeout:.0f} s")
    if "e" in box:
        raise box["e"]
    return box.get("v")


def friendly_error(e: BaseException) -> str:
    if isinstance(e, DeviceUnreachable):
        return "device did not answer (off, out of range, or connected to another program)"
    if isinstance(e, TimeoutError):
        return f"device connected but stopped answering ({e})"
    if isinstance(e, PermissionError):
        return "permission denied opening Bluetooth link"
    return str(e) or type(e).__name__


class Ring:
    def __init__(self, n_series: int, capacity: int):
        self.t = np.full(capacity, np.nan)
        self.y = np.full((n_series, capacity), np.nan)
        self.cap, self.idx, self.count = capacity, 0, 0
        self.lock = threading.Lock()

    def push(self, t, values):
        with self.lock:
            self.t[self.idx] = t
            self.y[:, self.idx] = values
            self.idx = (self.idx + 1) % self.cap
            self.count = min(self.count + 1, self.cap)

    def snapshot(self):
        with self.lock:
            if self.count < self.cap:
                return self.t[:self.count].copy(), self.y[:, :self.count].copy()
            order = np.r_[self.idx:self.cap, 0:self.idx]
            return self.t[order], self.y[:, order]


class ShimmerSession:
    def __init__(self, mac: str, name: str, cfg, color: str, time_base_ns: int):
        self.mac, self.name, self.cfg, self.color = mac.upper(), name, cfg, color
        self.time_base_ns = time_base_ns
        self.state = State.CONNECTING
        self.state_since_ns = clocks.perf_ns()
        self.detail = "waiting"
        self.attempt = 0
        self.link_desc = ""
        # filled on first successful connect
        self.specs = None
        self.plot_specs = []
        self.rate = 0.0
        self.firmware = ""
        self.kind = ""
        self.battery_pct = None
        self.battery_mv = None
        self.battery_from_stream = False
        self.ring = None
        # health
        self.packets = 0
        self.measured_rate = 0.0
        self.disconnects = 0
        self.downtime_s = 0.0
        self.last_values = {}
        self.saturated_since = {}       # spec.key -> perf ns since when saturated
        self.no_contact_since = None
        self.events = deque(maxlen=200)
        self.recorder = None
        self.taps = []                  # plugin inputs (see plugins.host.Tap)
        # internals
        self._stop = threading.Event()
        self._dev = None
        self._link = None
        self._thread = None
        self._last_rx_ns = 0
        self._prev_ts = None
        self._ticks = 0
        self._period_start_ticks = 0
        self._plot_offset_s = 0.0
        self._gap_from_ns = None
        self._rate_hist = deque(maxlen=20)
        self._last_ts = 0
        self._last_pkt_ns = 0
        self._last_plot_t = 0.0
        self.plot_decim = 1
        self._acc = None                # running sum for display block-averaging
        self._acc_n = 0
        self._clock_note = None

    # ---------- lifecycle ----------
    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"session-{self.name}")
        self._thread.start()

    def _run(self):
        try:
            self._manage()
        finally:
            # The manager thread owns the link: if stop() arrived mid-connect, the connect may
            # still have succeeded, so disconnect here rather than in stop().
            if self._stop.is_set():
                self._disconnect()

    def retry(self):
        if self.state == State.FAILED and not (self._thread and self._thread.is_alive()):
            self.start()

    def stop(self, timeout: float = 30.0):
        """Stop streaming and disconnect; blocks until the manager thread has finished."""
        self._stop.set()
        th = self._thread
        if th is not None and th.is_alive():
            th.join(timeout)
            if th.is_alive():                    # stuck in a Bluetooth call: best effort
                self._disconnect()
        self._set_state(State.STOPPED, "disconnected")

    def _disconnect(self):
        if self._dev is not None:
            try:
                timed(self._dev.stop_streaming, 3.0)
            except Exception as e:
                log.info("%s: stop_streaming: %s", self.name, e)
        self._teardown()

    def _set_state(self, state, detail=""):
        if state != self.state:
            self.state_since_ns = clocks.perf_ns()
        self.state, self.detail = state, detail

    def event(self, kind, text):
        ns = clocks.unix_ns()
        self.events.append((ns, kind, text))
        log.info("%s: %s", self.name, text)
        rec = self.recorder
        if rec is not None:
            rec.event(self, kind, ns, clocks.perf_ns(), text)

    # ---------- connection ----------
    def _open(self, first: bool):
        self.detail = "opening Bluetooth link"
        link = open_link(self.mac, self.cfg)
        self.link_desc = getattr(link, "description", "")
        try:
            self.detail = "handshake"
            quiesce(link)
            dev = ShimmerBluetooth(link)
            timed(dev.initialize, 10.0)
            rate, _, chans = timed(dev.get_inquiry, 5.0)
            if first:
                fw_type, fw_ver = timed(dev.get_firmware_version, 5.0)
                self.firmware = f"{fw_type.name} {fw_ver.major}.{fw_ver.minor}.{fw_ver.rel}"
                gains = {}
                for chip in {0, 1}:
                    if any(c.name.startswith(f"EXG_ADS1292R_{chip + 1}") for c in chans):
                        reg = timed(dev.get_exg_register, 5.0, chip)
                        gains[(chip, 0)], gains[(chip, 1)] = reg.ch1_gain, reg.ch2_gain
                self.configure(rate, chans, gains)
                try:
                    self.battery_pct = timed(dev.get_battery_state, 5.0, True)
                except Exception as e:
                    log.info("%s: battery query failed: %s", self.name, e)
                self.cfg.update_device(self.mac, name=self.name, last_check={
                    "ok": True, "when": clocks.utc_iso(clocks.unix_ns()), "firmware": self.firmware,
                    "rate_hz": rate, "kind": self.kind, "channels": [c.name for c in chans],
                    "battery_pct": self.battery_pct})
            elif [s.chan for s in self.specs] != [c for c in chans if c != EChannelType.TIMESTAMP]:
                raise RuntimeError("device configuration changed while disconnected - reopen live view")
            dev.add_stream_callback(self._on_packet)
            self._prev_ts = None
            self._last_rx_ns = clocks.perf_ns()
            timed(dev.start_streaming, 5.0)
        except BaseException:
            try:
                link.cancel_read()
            finally:
                link.close()
            raise
        self._dev, self._link = dev, link

    def configure(self, rate, chans, exg_gains):
        """Set up channel decoding and the plot buffer for the device's stream layout."""
        self.specs = build_specs(chans, exg_gains)
        self.plot_specs = [s for s in self.specs if s.plot]
        self.rate = rate
        self.kind = device_kind(chans)
        self.battery_from_stream = EChannelType.VBATT in chans
        self.plot_decim = max(1, round(rate / PLOT_RATE_HZ))
        self.ring = Ring(len(self.plot_specs), int(rate / self.plot_decim * BUFFER_SECONDS) + 64)

    def _teardown(self):
        dev, link = self._dev, self._link
        self._dev = self._link = None
        if dev is not None:
            try:
                timed(dev.shutdown, 3.0)
            except Exception:
                pass
        if link is not None:
            link.close()

    def _manage(self):
        first = self.specs is None
        # initial connection: several attempts - a Shimmer only listens for pages 12.5 % of the time
        self._set_state(State.CONNECTING, "connecting")
        for self.attempt in range(1, INITIAL_ATTEMPTS + 1):
            if self._stop.is_set():
                return
            try:
                self._open(first)
                break
            except Exception as e:
                msg = friendly_error(e)
                log.warning("%s: attempt %d/%d failed: %s", self.name, self.attempt, INITIAL_ATTEMPTS, msg)
                self.detail = f"attempt {self.attempt}/{INITIAL_ATTEMPTS} failed: {msg}"
                if self._stop.wait(RETRY_PAUSE_S):
                    return
        else:
            self._set_state(State.FAILED, self.detail)
            self.event("failed", f"could not connect: {self.detail}")
            return
        self._set_state(State.STREAMING, "streaming")
        self.event("connected", f"streaming at {self.rate:.0f} Hz via {self.link_desc}")
        self._watch()

    def _watch(self):
        stall_s = max(3.0, 10.0 / max(self.rate, 1.0))
        while not self._stop.wait(0.5):
            now = clocks.perf_ns()
            self._rate_hist.append((now, self.packets))
            t0, p0 = self._rate_hist[0]
            if now - t0 > 1e9:
                self.measured_rate = (self.packets - p0) / ((now - t0) / 1e9)
            silent = (now - self._last_rx_ns) / 1e9
            if silent < stall_s:
                continue
            # ---- link lost ----
            self.disconnects += 1
            self._gap_from_ns = self._last_rx_ns
            self._set_state(State.LOST, "no data - reconnecting")
            self.state_since_ns = self._last_rx_ns
            self.event("lost", f"connection lost (no data for {silent:.1f} s)")
            self._teardown()
            self._rate_hist.clear()
            self.measured_rate = 0.0
            self.attempt = 0
            while not self._stop.is_set():
                self.attempt += 1
                try:
                    self._open(first=False)
                    break
                except Exception as e:
                    self.detail = f"reconnect attempt {self.attempt} failed: {friendly_error(e)}"
                    log.warning("%s: %s", self.name, self.detail)
                    self._stop.wait(RETRY_PAUSE_S)
            if self._stop.is_set():
                return
            gap = (clocks.perf_ns() - self._gap_from_ns) / 1e9
            self.downtime_s += gap
            self._set_state(State.STREAMING, f"reconnected after {gap:.1f} s")
            self.event("reconnected", f"reconnected after {gap:.1f} s without data")
            for _ in range(10):
                if self._clock_note or self._stop.wait(0.3):
                    break
            if self._clock_note:
                self.event("clock", self._clock_note)
                self._clock_note = None

    # ---------- data path (pyshimmer reader thread) ----------
    def _on_packet(self, pkt):
        hu, hs, hp = clocks.unix_ns(), clocks.shared_clock_ticks(), clocks.perf_ns()
        ts = pkt[EChannelType.TIMESTAMP]
        if self._prev_ts is None:
            if self.packets == 0:
                ticks = 0
            else:
                # reconnect: if the device clock kept running, count the 24-bit wraps
                # during the gap from the host clock. Some firmware restarts the counter
                # when streaming restarts - then fall back to the host-measured gap.
                delta = (ts - self._last_ts) % clocks.TS_WRAP
                gap_ticks = (hp - self._last_pkt_ns) / 1e9 * clocks.TICKS_PER_S
                wraps = max(0, round((gap_ticks - delta) / clocks.TS_WRAP))
                ticks = self._ticks + delta + wraps * clocks.TS_WRAP
                mismatch_s = abs(delta + wraps * clocks.TS_WRAP - gap_ticks) / clocks.TICKS_PER_S
                if mismatch_s > 1.0:
                    ticks = self._ticks + int(round(gap_ticks))
                    self._clock_note = (f"device clock discontinuity after reconnect (off by "
                                        f"{mismatch_s:.1f} s); shimmer_ticks advanced by host-measured gap")
            self._period_start_ticks = ticks
            self._plot_offset_s = (hp - self.time_base_ns) / 1e9 - ticks / clocks.TICKS_PER_S
            self._acc_n = 0                                   # never average across a gap
            if self.packets and self.ring is not None:        # NaN breaks the plotted line at the gap
                self.ring.push(self._last_plot_t, [np.nan] * len(self.plot_specs))
            if self.packets:
                for tap in self.taps:
                    tap.gap()
        else:
            ticks = self._ticks + (ts - self._prev_ts) % clocks.TS_WRAP
        self._prev_ts = ts
        self._ticks = ticks
        self._last_rx_ns = hp
        if (ticks - self._period_start_ticks) / clocks.TICKS_PER_S < STARTUP_DISCARD_S and "ECG" in self.kind:
            return
        self._last_ts = ts
        self._last_pkt_ns = hp
        self.packets += 1

        raws = [pkt[s.chan] for s in self.specs]
        vals = []
        plot_vals = []
        for s, raw in zip(self.specs, raws):
            val = s.convert(raw) if s.convert else raw
            vals.append(val)
            self.last_values[s.key] = val
            if s.kind == "vbatt":
                self.battery_mv = val if self.battery_mv is None else 0.98 * self.battery_mv + 0.02 * val
                self.battery_pct = battery_percent(self.battery_mv)
            elif s.kind == "exg":
                if exg_saturated(raw):
                    self.saturated_since.setdefault(s.key, hp)
                else:
                    self.saturated_since.pop(s.key, None)
            elif s.kind == "gsr":
                if not (val >= GSR_NO_CONTACT_US):          # also catches NaN (open circuit)
                    if self.no_contact_since is None:
                        self.no_contact_since = hp
                else:
                    self.no_contact_since = None
            if s.plot:
                plot_vals.append(val)
        if self.ring is not None:
            # Average blocks of plot_decim samples (a simple low-pass, so mains hum does not
            # alias into slow fake waves); plotted at the block's centre time.
            if self._acc_n == 0:
                self._acc = np.array(plot_vals, dtype=float)
                self._acc_t0 = ticks
            else:
                self._acc += plot_vals
            self._acc_n += 1
            if self._acc_n >= self.plot_decim:
                t_plot = self._plot_offset_s + (self._acc_t0 + ticks) / 2 / clocks.TICKS_PER_S
                self.ring.push(t_plot, self._acc / self._acc_n)
                self._last_plot_t = t_plot
                self._acc_n = 0
        for tap in self.taps:
            tap.push(ticks / clocks.TICKS_PER_S, self._plot_offset_s, [vals[i] for i in tap.indices])
        rec = self.recorder
        if rec is not None:
            rec.put(self, hu, hs, ticks, vals)

    # ---------- helpers for the UI ----------
    def seconds_in_state(self) -> float:
        return (clocks.perf_ns() - self.state_since_ns) / 1e9


def probe_device(mac: str, name: str, cfg, attempts: int = 3, progress=None) -> dict:
    """Connect without streaming and report firmware, sampling rate, channels and battery.

    Several attempts are made because a Shimmer only listens for connections part of the
    time. The result is stored as the device's last_check in the config."""
    last_err = None
    for i in range(1, attempts + 1):
        if progress:
            progress(f"attempt {i}/{attempts}")
        try:
            link = open_link(mac, cfg)
        except Exception as e:
            last_err = e
            time.sleep(RETRY_PAUSE_S)
            continue
        try:
            quiesce(link)
            dev = ShimmerBluetooth(link)
            timed(dev.initialize, 10.0)
            fw_type, fw_ver = timed(dev.get_firmware_version, 5.0)
            rate, _, chans = timed(dev.get_inquiry, 5.0)
            batt = timed(dev.get_battery_state, 5.0, True)
            try:
                dev_name = timed(dev.get_device_name, 5.0)
            except Exception:
                dev_name = ""
            result = {"ok": True, "when": clocks.utc_iso(clocks.unix_ns()),
                      "firmware": f"{fw_type.name} {fw_ver.major}.{fw_ver.minor}.{fw_ver.rel}",
                      "rate_hz": rate, "kind": device_kind(chans), "channels": [c.name for c in chans],
                      "battery_pct": batt, "device_name": dev_name,
                      "link": getattr(link, "description", "")}
            try:
                timed(dev.shutdown, 3.0)
            except Exception:
                pass
            cfg.update_device(mac, name=name, last_check=result)
            return result
        except Exception as e:
            last_err = e
            link.cancel_read()
            time.sleep(RETRY_PAUSE_S)
        finally:
            link.close()
    result = {"ok": False, "when": clocks.utc_iso(clocks.unix_ns()), "error": friendly_error(last_err)}
    cfg.update_device(mac, name=name, last_check=result)
    return result


def stop_all(sessions, timeout: float = 30.0):
    """Stop several sessions in parallel (each stop can take seconds on a dead link)."""
    threads = [threading.Thread(target=s.stop, args=(timeout,), daemon=True) for s in sessions]
    for th in threads:
        th.start()
    deadline = time.monotonic() + timeout
    for th in threads:
        th.join(max(0.0, deadline - time.monotonic()))
