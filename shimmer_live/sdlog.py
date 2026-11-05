"""Recording on the Shimmers' own SD cards (no Bluetooth link while recording, so no limit of 7).

Verified on real Shimmer3 (LogAndStream 1.0.5) and a Base6U:
  * start/stop and status work over Bluetooth; after start_logging the same connection stops
    answering, so the status is confirmed on a fresh connection.
  * a docked Shimmer will not log, and docking a logging Shimmer ends the recording WITHOUT
    closing the file (0 bytes): always stop before docking.
  * files: <card>/data/<session name>_<config time>/<device name>-NNN/000, 001, ...
    (one -NNN folder per start); read with pyshimmer's reader.
  * the base shows a docked Shimmer's card to the PC with SDC,NN$ (reply C,NN; ~4 s).

Timing: at start each Shimmer's clock (RTC) is set to this PC's time.time(), with sub-second
precision (pyshimmer's own command drops the fraction). The offset Shimmer - PC is measured at
start and at stop (best of several round trips); at import, sample times are corrected along
the line between the two, which removes the crystal drift.

Session state lives in cfg["sd_session"], so closing the app does not lose it.
"""
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from pyshimmer import ShimmerBluetooth
from pyshimmer.bluetooth.bt_commands import ShimmerCommand
from pyshimmer.bluetooth.bt_const import SET_RWC_COMMAND
from pyshimmer.reader.shimmer_reader import ShimmerReader

from . import clocks
from .expansion_power import prepare_ppg
from .session import timed as _timed
from .transport import open_link, quiesce

log = logging.getLogger("shimmerlive.sd")

SESSION_KEY = "sd_session"
NAME_MAX = 12                  # the Shimmer keeps a short experiment id; existing card folders use <= 12
CONNECT_ATTEMPTS = 6           # a Shimmer only listens for connections part of the time
VERIFY_ATTEMPTS = 4
OFFSET_ROUNDS = 7
SD_GAP_S = 1.0                 # a gap in an SD log this long counts as an interruption
SD_APPEAR_TIMEOUT_S = 20.0
STATUS_BITS = ("docked", "sensing", "rtc_set", "logging", "streaming", "sd_present", "sd_error", "red_led")
RECORDING, FAILED, STOPPED, IMPORTED = "recording", "failed", "stopped", "imported"
UNCONFIRMED = "unconfirmed"     # start was sent but not confirmed: treat as possibly recording
MAY_RECORD = ("starting", RECORDING, UNCONFIRMED)


class StartUnconfirmed(RuntimeError):
    """The start command was sent but the Shimmer could not be asked afterwards: it may be recording."""

    def __init__(self, message, result):
        super().__init__(message)
        self.result = result


class PreciseRtc(ShimmerCommand):
    """Set the RTC to this PC's time with sub-second precision (time read at the moment of sending)."""

    def send(self, ser):
        ser.write_command(SET_RWC_COMMAND, "<Q", round(time.time() * clocks.TICKS_PER_S))


def clean_name(name: str) -> str:
    """Session name as stored on the Shimmer and used for the card's folder: letters, digits, _ and -."""
    return re.sub(r"[^A-Za-z0-9_-]+", "_", name.strip())[:NAME_MAX].strip("_")


# ------------------------------------------------------------------ Bluetooth
class Connection:
    def __init__(self, mac, cfg, attempts=CONNECT_ATTEMPTS):
        # Connecting and the first reply are both retried: a Shimmer that has just started logging
        # can take >10 s to answer (seen on D1A4, LogAndStream 0.16), and the next try usually works.
        last = None
        for i in range(attempts):
            link = None
            try:
                link = open_link(mac, cfg)
                quiesce(link)
                dev = ShimmerBluetooth(link)
                _timed(dev.initialize, 10.0)
                self.link, self.dev = link, dev
                return
            except Exception as e:                    # noqa: BLE001 - retried, last error re-raised
                last = e
                if link is not None:
                    try:
                        link.cancel_read()
                    finally:
                        link.close()
                time.sleep(1.5)
        raise ConnectionError(f"did not answer over Bluetooth ({last})")

    def status(self) -> dict:
        return dict(zip(STATUS_BITS, _timed(self.dev.get_status, 5.0)))

    def offset(self, rounds=OFFSET_ROUNDS) -> dict:
        """Shimmer clock - PC clock, from the fastest of several round trips."""
        best = None
        for _ in range(rounds):
            a = time.time()
            rtc = _timed(self.dev.get_rtc, 5.0)
            b = time.time()
            if best is None or b - a < best[1]:
                best = ((rtc - (a + b) / 2) * 1000.0, b - a, (a + b) / 2)
        return {"offset_ms": round(best[0], 2), "round_trip_ms": round(best[1] * 1000.0, 1), "pc_unix": best[2]}

    def close(self):
        try:
            _timed(self.dev.shutdown, 3.0)
        except Exception:                             # noqa: BLE001
            pass
        self.link.close()


def _firmware(conn) -> str:
    fw_type, v = _timed(conn.dev.get_firmware_version, 5.0)
    return f"{fw_type.name} {v.major}.{v.minor}.{v.rel}"


def start_device(mac, cfg, session_name, config_time, progress=lambda text: None) -> dict:
    """Set clock and session name, start SD logging, confirm on a fresh connection."""
    progress("connecting…")
    conn = Connection(mac, cfg)
    try:
        fw = _firmware(conn)
        if not fw.startswith("LogAndStream"):
            raise RuntimeError(f"runs {fw}: SD recording from this app needs LogAndStream firmware")
        st = conn.status()
        if st["docked"]:
            raise RuntimeError("is in the base: a docked Shimmer does not record. Take it out and try again")
        if not st["sd_present"]:
            raise RuntimeError("has no SD card")
        if st["sd_error"]:
            raise RuntimeError("reports an SD card error")
        if st["logging"]:
            raise RuntimeError("is already recording to its SD card (stop it first)")
        _rate, _, chans = _timed(conn.dev.get_inquiry, 5.0)
        if not [c for c in chans if c.name != "TIMESTAMP"]:
            raise RuntimeError("has no sensors enabled, so it would record nothing. Turn some on in ⚙ Settings")
        prepare_ppg(conn.dev, cfg, mac, chans, _timed)
        battery = None
        try:
            battery = _timed(conn.dev.get_battery_state, 5.0, True)
        except Exception:                             # noqa: BLE001 - battery is informative only
            pass
        progress("setting its clock…")
        _timed(conn.dev._process_and_wait, 5.0, PreciseRtc())
        offset = conn.offset()
        _timed(conn.dev.set_experiment_id, 5.0, session_name)
        _timed(conn.dev.set_config_time, 5.0, int(config_time))
        progress("starting the recording…")
        sent_at = time.time()
        _timed(conn.dev.start_logging, 5.0)
    except Exception as e:
        if "sent_at" in locals():                    # start was sent: it may well be recording
            raise StartUnconfirmed(f"start sent, but no answer afterwards ({e})",
                                   {"firmware": fw, "start_offset": offset, "started_unix": sent_at}) from e
        raise
    finally:
        conn.close()
    result = {"firmware": fw, "battery_pct": battery, "start_offset": offset, "started_unix": sent_at}
    progress("checking it is recording…")
    last = None
    for i in range(VERIFY_ATTEMPTS):
        time.sleep(1.5)
        try:
            conn = Connection(mac, cfg, attempts=2)
            try:
                st = conn.status()
            finally:
                conn.close()
        except Exception as e:                        # noqa: BLE001 - retried; never decides "failed"
            last = e
            continue
        if st["logging"]:
            return result
        last = None
    if last is not None:
        raise StartUnconfirmed(f"started, but could not confirm it ({last}): press Check status", result)
    raise RuntimeError("was told to start, but does not report recording")


def check_device(mac, cfg) -> dict:
    conn = Connection(mac, cfg, attempts=3)
    try:
        return {"status": conn.status(), "offset": conn.offset(3), "checked_unix": time.time()}
    finally:
        conn.close()


def stop_device(mac, cfg, progress=lambda text: None) -> dict:
    progress("connecting…")
    conn = Connection(mac, cfg)
    try:
        st = conn.status()
        was_recording = st["logging"]
        if was_recording:
            progress("stopping…")
            _timed(conn.dev.stop_logging, 5.0)
            time.sleep(1.5)
            st = conn.status()
            if st["logging"]:
                raise RuntimeError("still reports recording after the stop command")
        result = {"was_recording": was_recording, "stop_offset": None,
                  "stopped_unix": time.time(), "docked": st["docked"]}
        # Stopping takes priority over drift measurement. A failed RTC read must
        # never prevent the stop command or turn a confirmed stop into a failure.
        progress("recording stopped; measuring clock offset…")
        try:
            result["stop_offset"] = conn.offset()
        except Exception as e:
            result["clock_warning"] = f"Stopped, but could not measure the final clock offset: {e}"
            log.warning("%s: %s", mac, result["clock_warning"])
        return result
    finally:
        conn.close()


# ------------------------------------------------------------------ the SD card, through the base
def _linux_card_partition():
    """/dev/sdXN of the base's built-in card reader (USB model 'HS-SD/MMC', vendor Shimmer), or None."""
    for blk in sorted(Path("/sys/block").glob("sd*")):
        try:
            model = (blk / "device" / "model").read_text().strip()
            vendor = (blk / "device" / "vendor").read_text().strip()
        except OSError:
            continue
        if "HS-SD" not in model and "shimm" not in vendor.lower():
            continue
        parts = sorted(p.name for p in blk.glob(f"{blk.name}[0-9]*"))
        if parts:
            return f"/dev/{parts[0]}"
        if int((blk / "size").read_text().strip() or 0) > 0:
            return f"/dev/{blk.name}"                 # card without a partition table
    return None


def _linux_mountpoint(devnode):
    for line in Path("/proc/mounts").read_text().splitlines():
        parts = line.split()
        if parts and parts[0] == devnode:
            return Path(parts[1].replace("\\040", " "))
    return None


def _windows_removable_drives() -> set:
    import ctypes
    out, mask = set(), ctypes.windll.kernel32.GetLogicalDrives()
    for i in range(26):
        if mask & (1 << i):
            root = f"{chr(65 + i)}:\\"
            if ctypes.windll.kernel32.GetDriveTypeW(root) == 2:      # DRIVE_REMOVABLE
                out.add(root)
    return out


class DockedCard:
    """The docked Shimmer's SD card, shown to this PC by the base and mounted read-only-in-practice.

    with DockedCard(link, slot) as root:  ...read root / "data"...
    """

    def __init__(self, link, slot):
        self.link, self.slot = link, slot
        self.root = None
        self._unmount = None

    def __enter__(self) -> Path:
        before = _windows_removable_drives() if sys.platform == "win32" else set()
        self.link.select_sd(self.slot)
        end = time.monotonic() + SD_APPEAR_TIMEOUT_S
        while time.monotonic() < end:
            time.sleep(1.0)
            root = self._find(before)
            if root is not None:
                self.root = root
                return root
        self.link.disconnect()
        raise TimeoutError("the Shimmer's SD card did not appear on this computer")

    def _find(self, before):
        if sys.platform == "win32":
            # Windows mounts the card itself and gives it a drive letter.
            for root in sorted(_windows_removable_drives() - before) or sorted(_windows_removable_drives()):
                if (Path(root) / "data").is_dir() or (Path(root) / "sdlog.cfg").exists():
                    return Path(root)
            return None
        dev = _linux_card_partition()
        if dev is None:
            return None
        mp = _linux_mountpoint(dev)
        if mp is None:
            r = subprocess.run(["udisksctl", "mount", "--no-user-interaction", "-b", dev],
                               capture_output=True, text=True)
            if r.returncode != 0:
                log.info("mount %s: %s", dev, r.stderr.strip())
                return None
            mp = _linux_mountpoint(dev)
            self._unmount = dev
        return mp

    def __exit__(self, *exc):
        if self._unmount:
            subprocess.run(["udisksctl", "unmount", "--no-user-interaction", "-b", self._unmount],
                           capture_output=True, text=True)
        try:
            self.link.disconnect()
        except Exception:                             # noqa: BLE001
            pass


def session_files(root: Path, session_name: str, config_time: int) -> list:
    """The binary files of one session on a card, in recording order."""
    folder = Path(root) / "data" / f"{session_name}_{int(config_time)}"
    if not folder.is_dir():
        return []
    out = []
    for run in sorted(p for p in folder.iterdir() if p.is_dir()):
        out += sorted((f for f in run.iterdir() if f.is_file() and f.name.isdigit()), key=lambda f: int(f.name))
    return out


def copy_files(files, dest: Path) -> list:
    """Copy the card's files (keeping <run>/<file>) so the card is free again quickly."""
    out = []
    for f in files:
        target = dest / f.parent.name / f.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, target)
        out.append(target)
    return out


# ------------------------------------------------------------------ reading the files
@dataclass
class SdData:
    rate_hz: float
    chans: list                       # EChannelType, without TIMESTAMP
    gains: dict                       # {(chip, ch): gain}
    ticks: np.ndarray                 # Shimmer clock, ticks since its power-on (int64)
    rtc_unix: np.ndarray              # Shimmer RTC time of each sample, s (float64)
    values: dict                      # EChannelType -> raw values
    files: list = field(default_factory=list)
    problems: list = field(default_factory=list)


def read_files(paths) -> SdData:
    parts, problems = [], []
    for p in paths:
        if os.path.getsize(p) == 0:
            problems.append(f"{Path(p).parent.name}/{Path(p).name} is empty (docked before it was stopped?)")
            continue
        try:
            with open(p, "rb") as fh:
                r = ShimmerReader(fh, post_process=False)
                r.load_file_data()
                br = r._bin_reader
                gains = {}
                for chip in (0, 1):
                    reg = r.get_exg_reg(chip)
                    gains[(chip, 0)], gains[(chip, 1)] = reg.ch1_gain, reg.ch2_gain
                ts = np.asarray(r.timestamp, dtype=float)
                diff = br.global_clock_diff if br.has_global_clock else 0
                parts.append({"rate": clocks.TICKS_PER_S / br.sample_rate, "chans": list(r.channels),
                              "gains": gains, "rtc": ts, "has_rtc": br.has_global_clock,
                              "ticks": np.round(ts * clocks.TICKS_PER_S).astype(np.int64) - int(diff),
                              "values": {c: np.asarray(r[c]) for c in r.channels}, "file": str(p)})
        except Exception as e:                        # noqa: BLE001 - one bad file must not lose the others
            problems.append(f"{Path(p).parent.name}/{Path(p).name} could not be read: {e}")
    if not parts:
        raise ValueError("; ".join(problems) or "no recording found")
    first = parts[0]
    same = [q for q in parts if q["chans"] == first["chans"]]
    if len(same) < len(parts):
        problems.append("some files used a different sensor configuration and were left out")
    if not first["has_rtc"]:
        problems.append("the Shimmer's clock was not set: times are relative, not PC times")
    return SdData(rate_hz=first["rate"], chans=first["chans"], gains=first["gains"],
                  ticks=np.concatenate([q["ticks"] for q in same]),
                  rtc_unix=np.concatenate([q["rtc"] for q in same]),
                  values={c: np.concatenate([q["values"][c] for q in same]) for c in first["chans"]},
                  files=[q["file"] for q in same], problems=problems)


def pc_times(rtc_unix: np.ndarray, start_offset: dict | None, stop_offset: dict | None) -> np.ndarray:
    """Shimmer RTC times -> PC times, removing the offset measured at start and stop (linear drift)."""
    if not start_offset:
        return rtc_unix
    o1, t1 = start_offset["offset_ms"] / 1000.0, start_offset["pc_unix"]
    if not stop_offset or stop_offset["pc_unix"] - t1 < 1.0:
        return rtc_unix - o1
    o2, t2 = stop_offset["offset_ms"] / 1000.0, stop_offset["pc_unix"]
    return rtc_unix - (o1 + (o2 - o1) * (rtc_unix - t1) / (t2 - t1))


# ------------------------------------------------------------------ into a normal recording folder
def build_recording(session: dict, loaded: dict, out_folder, values: str) -> Path:
    """Write a recorder-style working folder (see recorder.py) from SD data, so that the usual
    Save dialog (analysis, quality checks, ZIP) handles SD recordings exactly like live ones.

    loaded: {mac: (name, SdData, [copied raw files])}."""
    import json

    import pandas as pd

    from . import __version__
    from .conversions import build_specs, calibrate_array, device_kind
    from .recorder import PARTIAL_SUFFIX, VALUES_RAW, _full_scale, _json_dump

    out_folder = Path(out_folder)
    t_first = min(float(pc_times(d.rtc_unix[:1], *_offsets(session, mac))[0]) for mac, (_, d, _) in loaded.items())
    t_last = max(float(pc_times(d.rtc_unix[-1:], *_offsets(session, mac))[0]) for mac, (_, d, _) in loaded.items())
    u0 = int(t_first * 1e9)
    s0, spc = session["start_shared_ticks"], session["clocks"]["shared_clock"]["seconds_per_cycle"]
    u_ref = session["start_unix_ns"]
    base = "shimmer_sd_" + clocks.local_stamp_for_filename(u0)
    folder = out_folder / f".{base}{PARTIAL_SUFFIX}"
    folder.mkdir(parents=True, exist_ok=False)
    devices_meta, stats = [], {}
    for n, (mac, (name, d, raw_files)) in enumerate(loaded.items()):
        specs = build_specs(d.chans, d.gains)
        pc = pc_times(d.rtc_unix, *_offsets(session, mac))
        host_ns = np.round(pc * 1e9).astype(np.int64)
        # shared clock: from the (unix, shared) pair read together when the session started
        shared = np.round(s0 + (host_ns - u_ref) / 1e9 / spc).astype(np.int64)
        table = {"shimmer_ticks": d.ticks, "host_unix_ns": host_ns, "shared_ticks": shared}
        for sp in specs:
            raw = d.values[sp.chan]
            table[sp.key] = raw if values == VALUES_RAW or not sp.calibration else calibrate_array(sp.calibration, raw)
        pd.DataFrame(table).to_csv(folder / f"device_{n}.csv", index=False, float_format="%.9g")
        dev_s = session["devices"][mac]
        devices_meta.append({
            "file": f"device_{n}.csv", "name": name, "mac": mac, "kind": device_kind(d.chans),
            "firmware": dev_s.get("firmware", ""), "sampling_rate_hz": d.rate_hz, "link": "SD card",
            "channels": [{"key": sp.key, "label": sp.label, "unit": sp.unit, "export_name": sp.export_name,
                          "export_unit": sp.export_unit, "kind": sp.kind, "full_scale": _full_scale(sp),
                          "calibration": sp.calibration} for sp in specs],
        })
        gaps = np.diff(d.ticks) / clocks.TICKS_PER_S if len(d.ticks) > 1 else np.empty(0)
        # Short gaps (10-120 ms) are normal in SD logs: seen once a minute on every unit, and when a host
        # connects to a logging Shimmer on firmware older than 1.01.009. Only >= 1 s counts as an interruption.
        big = gaps[gaps >= SD_GAP_S]
        span = (d.ticks[-1] - d.ticks[0]) / clocks.TICKS_PER_S if len(d.ticks) > 1 else 0.0
        stats[mac] = {"disconnects": int(len(big)), "downtime_s": float(big.sum()),
                      "longest_gap_s": float(gaps.max()) if len(gaps) else 0.0,
                      "short_gaps": int(((gaps > 3.0 / d.rate_hz) & (gaps < SD_GAP_S)).sum()),
                      "expected_samples": int(round(span * d.rate_hz)) + 1}
        for f in raw_files:                          # original files go into the ZIP too
            target = folder / "sd_raw" / name / Path(f).parent.name / Path(f).name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, target)
    _json_dump(folder / "session.json", {
        "app_version": __version__, "base": base, "values": values, "source": "sd",
        "start_unix_ns": u0, "start_shared_ticks": int(round(s0 + (u0 - u_ref) / 1e9 / spc)),
        "clocks": session["clocks"], "devices": devices_meta, "plugins": [],
        "keypoint_definitions": session.get("keypoint_definitions", []),
        "sd": {"name": session["name"], "config_time": session["config_time"],
               "devices": {m: {k: v for k, v in dv.items() if k in ("name", "start", "stop", "problems")}
                           for m, dv in session["devices"].items()},
               "timing": "Sample times come from each Shimmer's clock, set to this PC's time at start; the "
                         "offset Shimmer - PC measured at start and stop is removed linearly (drift)."},
    })
    with open(folder / "events.jsonl", "w", encoding="utf-8") as fh:
        for ev in session.get("keypoints", []):
            fh.write(json.dumps(ev) + "\n")
        for m, dv in session["devices"].items():
            for p in dv.get("problems", []):
                fh.write(json.dumps({"type": "sd_problem", "device": dv["name"], "mac": m, "unix_ns": u0,
                                     "text": p}) + "\n")
    _json_dump(folder / "stop.json", {"stop_unix_ns": int(t_last * 1e9), "stop_shared_ticks": None,
                                      "write_error": None, "devices": stats})
    return folder


def _offsets(session, mac):
    dv = session["devices"].get(mac, {})
    return (dv.get("start") or {}).get("start_offset"), (dv.get("stop") or {}).get("stop_offset")


# ------------------------------------------------------------------ session bookkeeping
def new_session(cfg, name: str, macs_names: list, values: str, keypoint_definitions: list) -> dict:
    s = {"name": name, "config_time": int(time.time()), "start_unix_ns": clocks.unix_ns(),
         "start_shared_ticks": clocks.shared_clock_ticks(), "clocks": clocks.clock_report(),
         "values": values, "keypoint_definitions": keypoint_definitions, "keypoints": [], "stopped": False,
         "devices": {mac: {"name": nm, "state": "starting"} for mac, nm in macs_names}}
    cfg.data[SESSION_KEY] = s
    cfg.save()
    return s


def active(cfg) -> dict | None:
    return cfg.data.get(SESSION_KEY)


def recording_macs(cfg) -> set:
    s = active(cfg)
    if not s:
        return set()
    return {m for m, d in s["devices"].items() if d.get("state") in MAY_RECORD}


def add_keypoint(cfg, kp, at=None, source="") -> dict:
    """at: kp_server.now() taken when a TCP message arrived (default: now)."""
    ev = {"type": "keypoint", "name": kp.name, "color": kp.color,
          "unix_ns": at["unix_ns"] if at else clocks.unix_ns(),
          "shared_ticks": at["shared_ticks"] if at else clocks.shared_clock_ticks()}
    if source:
        ev["source"] = source
    cfg.data[SESSION_KEY]["keypoints"].append(ev)
    cfg.save()
    return ev


def end_session(cfg) -> None:
    cfg.data.pop(SESSION_KEY, None)
    cfg.save()
