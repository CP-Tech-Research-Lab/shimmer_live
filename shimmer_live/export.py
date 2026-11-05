"""Recording, phase 2: turn a working folder (recorder.py) into the final ZIP.

<name>_shimmer_<start time>.zip
  <device>.csv             one per Shimmer, Consensys "Calibrated" layout:
                             "sep=,"  /  column names  /  units  /  data
                           <device>_Timestamp_Unix_CAL    ms    sample time, PC wall clock (UTC epoch)
                           <device>_Timestamp_Shared_CAL  s     sample time, shared clock (Unreal/QPC on Windows)
                           <device>_Timestamp_CAL         ms    sample time, Shimmer clock (from session start)
                           <device>_<channel>_RAW ...           recorded channels: raw device values (default)
                                                                or <channel>_CAL calibrated values; the
                                                                formulas are in info.json ("calibration")
                           <device>_Keypoint                    keypoint name at the nearest sample
  <plugin> - <device>.csv  plugin outputs, same three timestamps
  keypoints.csv            every keypoint with its time on every clock and its source (button / tcp:<port>)
  qc.csv                   quality checks
  info.json                notes, devices, plugins, connection events, clocks, app version
  notes.txt                the notes, as plain text

The two PC timestamps are *sampling* times, not arrival times: per connection period the
Shimmer clock is fitted to the PC clock along the lower edge of the packet arrival times
(see alignment.py), which removes Bluetooth latency and corrects clock drift.
"""
import csv
import io
import json
import re
import shutil
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from . import __version__, clocks, qc
from .alignment import PeriodFits, split_periods
from .conversions import CALIBRATION_DOC, battery_percent, calibrate_array
from .recorder import PARTIAL_SUFFIX, VALUES_CAL, VALUES_RAW

KEYPOINT_MAX_DISTANCE_MS = 1000.0


@dataclass
class DeviceData:
    meta: dict
    data: pd.DataFrame                 # export columns (without the device-name prefix)
    units: list
    stats: dict


@dataclass
class PluginData:
    meta: dict
    data: pd.DataFrame
    units: list


@dataclass
class Analysis:
    folder: Path                       # working folder
    session: dict
    stop: dict | None
    devices: list = field(default_factory=list)
    plugins: list = field(default_factory=list)
    keypoints: pd.DataFrame = None
    events: list = field(default_factory=list)
    qc: list = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    calibrated: dict = field(default_factory=dict)

    @property
    def recovered(self) -> bool:
        return self.stop is None


def unfinished(folder) -> list:
    """Working folders left by a recording that was never saved (app closed while recording)."""
    folder = Path(folder)
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.glob(f".*{PARTIAL_SUFFIX}") if (p / "session.json").exists())


def discard(partial: Path) -> None:
    shutil.rmtree(partial, ignore_errors=True)


def _read_events(folder: Path) -> list:
    path = folder / "events.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:                 # last line may be cut short by a crash
            pass
    return out


def analyze(folder) -> Analysis:
    folder = Path(folder)
    session = json.loads((folder / "session.json").read_text(encoding="utf-8"))
    stop_path = folder / "stop.json"
    stop = json.loads(stop_path.read_text(encoding="utf-8")) if stop_path.exists() else None
    a = Analysis(folder=folder, session=session, stop=stop, events=_read_events(folder))
    u0 = session["start_unix_ns"]
    s0 = session["start_shared_ticks"]
    spc = session["clocks"]["shared_clock"]["seconds_per_cycle"]
    kp_events = [e for e in a.events if e["type"] == "keypoint"]
    fits = {}
    raw_mode = session.get("values", VALUES_CAL) == VALUES_RAW      # older recordings were always calibrated
    a.calibrated = {}                  # (mac, export column) -> calibrated array, for quality control

    last_ns = u0
    for dm in session["devices"]:
        raw = pd.read_csv(folder / dm["file"])
        rate = dm["sampling_rate_hz"]
        cols = {}
        units = []
        if len(raw):
            ticks = raw["shimmer_ticks"].to_numpy(dtype=np.int64)
            t = ticks / clocks.TICKS_PER_S
            periods = split_periods(ticks)
            host = (raw["host_unix_ns"].to_numpy(dtype=np.int64) - u0) / 1e9
            shared = (raw["shared_ticks"].to_numpy(dtype=np.int64) - s0) * spc
            f_unix, f_shared = PeriodFits(t, host, periods), PeriodFits(t, shared, periods)
            fits[dm["mac"]] = (f_unix, f_shared, t)
            unix_ms = u0 / 1e6 + f_unix(t) * 1e3
            cols["Timestamp_Unix_CAL"] = unix_ms
            cols["Timestamp_Shared_CAL"] = s0 * spc + f_shared(t)
            cols["Timestamp_CAL"] = ticks / (clocks.TICKS_PER_S / 1000.0)
            last_ns = max(last_ns, int(raw["host_unix_ns"].iloc[-1]))
        else:
            unix_ms = np.empty(0)
            for c in ("Timestamp_Unix_CAL", "Timestamp_Shared_CAL", "Timestamp_CAL"):
                cols[c] = np.empty(0)
        units += ["ms", "s", "ms"]
        for ch in dm["channels"]:
            data = raw[ch["key"]].to_numpy() if len(raw) else np.empty(0)
            if raw_mode:
                col = column_name(ch, raw_mode)
                cols[col] = data
                units.append("no_units")
                cal = calibrate_array(ch.get("calibration"), data) if ch.get("calibration") else data
            else:
                col = ch["export_name"]
                cols[col] = data
                units.append(ch["export_unit"])
                cal = data
            a.calibrated[(dm["mac"], ch["export_name"])] = np.asarray(cal, dtype=float)
        names = {ch["export_name"] for ch in dm["channels"]}
        if not raw_mode and {"ECG_LL-RA_24BIT_CAL", "ECG_LA-RA_24BIT_CAL"} <= names:   # derived lead, as Consensys
            cols["ECG_LL-LA_24BIT_CAL"] = cols["ECG_LL-RA_24BIT_CAL"] - cols["ECG_LA-RA_24BIT_CAL"]
            units.append("mV")
        marks = np.full(len(unix_ms), "", dtype=object)
        for ev in kp_events:
            if len(unix_ms):
                kp_ms = ev["unix_ns"] / 1e6
                i = int(np.argmin(np.abs(unix_ms - kp_ms)))         # nearest sample
                if abs(unix_ms[i] - kp_ms) <= KEYPOINT_MAX_DISTANCE_MS:
                    marks[i] = f"{marks[i]};{ev['name']}" if marks[i] else ev["name"]
        cols["Keypoint"] = marks
        units.append("no_units")
        df = pd.DataFrame(cols)
        a.devices.append(DeviceData(meta=dm, data=df, units=units, stats={}))

    # ---- plugin outputs, mapped to PC time with the device's fits ----
    for pm in session["plugins"]:
        raw = pd.read_csv(folder / pm["file"])
        units = ["ms", "s", "ms"] + [o["unit"] or "no_units" for o in pm["outputs"]]
        if len(raw) and pm["mac"] in fits:
            wide = raw.pivot_table(index="shimmer_s", columns="output", values="value", aggfunc="last")
            t = wide.index.to_numpy(dtype=float)
            f_unix, f_shared, _ = fits[pm["mac"]]
            df = pd.DataFrame({"Timestamp_Unix_CAL": u0 / 1e6 + f_unix(t) * 1e3,
                               "Timestamp_Shared_CAL": s0 * spc + f_shared(t),
                               "Timestamp_CAL": t * 1000.0})
            for o in pm["outputs"]:
                df[o["key"]] = wide[o["key"]].to_numpy() if o["key"] in wide else np.nan
        else:
            df = pd.DataFrame(columns=["Timestamp_Unix_CAL", "Timestamp_Shared_CAL", "Timestamp_CAL"]
                              + [o["key"] for o in pm["outputs"]])
        a.plugins.append(PluginData(meta=pm, data=df, units=units))

    # ---- keypoints table ----
    rows = []
    for ev in kp_events:
        row = {"Keypoint": ev["name"], "Color": ev["color"], "Source": ev.get("source") or "button",
               "Timestamp_Unix_CAL": ev["unix_ns"] / 1e6,
               "Timestamp_Shared_CAL": ev["shared_ticks"] * spc}
        for d in a.devices:
            col = f"{d.meta['name']}_Timestamp_CAL"
            u = d.data["Timestamp_Unix_CAL"].to_numpy()
            row[col] = np.nan
            if len(u):
                i = int(np.argmin(np.abs(u - row["Timestamp_Unix_CAL"])))
                if abs(u[i] - row["Timestamp_Unix_CAL"]) <= KEYPOINT_MAX_DISTANCE_MS:
                    row[col] = d.data["Timestamp_CAL"].iloc[i] + (row["Timestamp_Unix_CAL"] - u[i])
        rows.append(row)
    a.keypoints = pd.DataFrame(rows)

    # ---- summary and quality control ----
    stop_ns = stop["stop_unix_ns"] if stop else last_ns
    duration = max(0.0, (stop_ns - u0) / 1e9)
    a.summary = {"duration_s": duration, "start_unix_ns": u0, "stop_unix_ns": stop_ns,
                 "start_utc": clocks.utc_iso(u0), "stop_utc": clocks.utc_iso(stop_ns),
                 "recovered": a.recovered, "write_error": stop.get("write_error") if stop else None}
    for d in a.devices:
        st = (stop or {}).get("devices", {}).get(d.meta["mac"], _stats_from_events(a.events, d.meta["mac"], stop_ns))
        # SD recordings: each Shimmer starts and stops at its own time, so the expected count comes
        # from its own recording span (written by sdlog.build_recording).
        expected = st.get("expected_samples") or int(round(d.meta["sampling_rate_hz"] * duration))
        d.stats = {**st, "samples": len(d.data), "expected_samples": expected,
                   "completeness_pct": 100.0 * len(d.data) / expected if expected else 0.0}
        name, rate = d.meta["name"], d.meta["sampling_rate_hz"]
        a.qc += qc.check_completeness(name, len(d.data), expected, st["disconnects"], st["downtime_s"],
                                      st["longest_gap_s"])
        for ch in d.meta["channels"]:
            x = a.calibrated[(d.meta["mac"], ch["export_name"])]
            if ch["kind"] == "exg":
                a.qc += qc.check_ecg(name, ch["export_name"], x, rate, ch.get("full_scale"))
            elif ch["kind"] == "gsr":
                a.qc += qc.check_eda(name, ch["export_name"], x, rate)
            elif ch["kind"] == "vbatt":
                a.qc += qc.check_battery(name, x[np.isfinite(x)], battery_percent)
    for p in a.plugins:
        if p.meta["id"] == "hrv" and "hr" in p.data:
            a.qc += qc.check_heart_rate(p.meta["device"], p.data["hr"].dropna().to_numpy(dtype=float))
    a.qc += qc.check_keypoints([e["name"] for e in kp_events])
    return a


def _timestamp_doc(session: dict) -> dict:
    shared = f"s, shared clock: {session['clocks']['shared_clock']['source']}"
    if session.get("source") == "sd":
        return {"Timestamp_Unix_CAL": "ms since 1970-01-01 UTC: the Shimmer's own clock, set to this PC's time at "
                                      "the start, corrected by the offset measured at start and stop",
                "Timestamp_Shared_CAL": shared + " (converted from the PC time above, not measured per sample)",
                "Timestamp_CAL": "ms, Shimmer clock (32768 Hz crystal), counted from the Shimmer's power-on"}
    return {"Timestamp_Unix_CAL": "ms since 1970-01-01 UTC, PC wall clock, sample time (latency removed)",
            "Timestamp_Shared_CAL": shared,
            "Timestamp_CAL": "ms, Shimmer clock (32768 Hz crystal), 0 = first sample of the live session"}


def column_name(ch: dict, raw_mode: bool) -> str:
    """Consensys naming: <signal>_CAL for calibrated values, <signal>_RAW for raw device values."""
    name = ch["export_name"]
    if raw_mode and name.endswith("_CAL"):
        return name[:-4] + "_RAW"
    return name


def calibration_info(a: "Analysis") -> list:
    """Per recorded channel: how its raw value converts to physical units (written to info.json)."""
    raw_mode = a.session.get("values", VALUES_CAL) == VALUES_RAW
    out = []
    for dm in a.session["devices"]:
        for ch in dm["channels"]:
            cal = ch.get("calibration") or {}
            out.append({"device": dm["name"], "column": f"{dm['name']}_{column_name(ch, raw_mode)}",
                        "calibrated_unit": ch["export_unit"], "parameters": cal,
                        "formula": CALIBRATION_DOC.get(cal.get("formula"))})
    return out


def _stats_from_events(events, mac, stop_ns):
    """Connection statistics from the event log (recovered recordings have no stop.json)."""
    n, down, longest, lost_at = 0, 0.0, 0.0, None
    for e in events:
        if e.get("mac") != mac:
            continue
        if e["type"] == "lost" and lost_at is None:
            n, lost_at = n + 1, e["unix_ns"]
        elif e["type"] == "reconnected" and lost_at is not None:
            gap = (e["unix_ns"] - lost_at) / 1e9
            down, longest, lost_at = down + gap, max(longest, gap), None
    if lost_at is not None:
        gap = (stop_ns - lost_at) / 1e9
        down, longest = down + gap, max(longest, gap)
    return {"disconnects": n, "downtime_s": down, "longest_gap_s": longest}


def _safe(name: str) -> str:
    return re.sub(r"[^\w\-. ]+", "_", name).strip(" ._") or "data"


def _write_table(zf, arcname, prefix, df: pd.DataFrame, units: list):
    """Consensys layout: "sep=," line, names row, units row, data."""
    names = [f"{prefix}_{c}" if prefix else c for c in df.columns]
    with zf.open(arcname, "w") as raw, io.TextIOWrapper(raw, encoding="utf-8", newline="") as f:
        f.write('"sep=,"\r\n')
        w = csv.writer(f)
        w.writerow(names)
        if units is not None:
            w.writerow(units)
        df.to_csv(f, header=False, index=False, lineterminator="\r\n")


def write_zip(a: Analysis, name: str = "", notes: str = "", out_folder=None) -> Path:
    out_folder = Path(out_folder) if out_folder else a.folder.parent
    clean = re.sub(r"[^\w\-.]+", "_", name.strip()).strip("._")
    base = f"{clean}_{a.session['base']}" if clean else a.session["base"]
    path = out_folder / f"{base}.zip"
    n = 2
    while path.exists():
        path = out_folder / f"{base} ({n}).zip"
        n += 1
    tmp = path.with_suffix(".zip.tmp")
    files = []
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for d in a.devices:
            if d.meta["channels"]:
                arc = f"{_safe(d.meta['name'])}.csv"
                _write_table(zf, arc, d.meta["name"], d.data, d.units)
                files.append(arc)
        for p in a.plugins:
            arc = f"{_safe(p.meta['name'])} - {_safe(p.meta['device'])}.csv"
            _write_table(zf, arc, f"{p.meta['device']}_{p.meta['id']}", p.data, p.units)
            files.append(arc)
        kp = a.keypoints if len(a.keypoints) else pd.DataFrame(
            columns=["Keypoint", "Color", "Source", "Timestamp_Unix_CAL", "Timestamp_Shared_CAL"])
        _write_table(zf, "keypoints.csv", "", kp, None)
        qc_df = pd.DataFrame([asdict(q) for q in a.qc], columns=["device", "signal", "check", "status", "value",
                                                                 "detail"])
        _write_table(zf, "qc.csv", "", qc_df, None)
        info = {
            "app": "Shimmer Live", "app_version": __version__, "recording_name": name, "notes": notes,
            "recorded_with_app_version": a.session.get("app_version"),
            "summary": a.summary, "files": files + ["keypoints.csv", "qc.csv", "notes.txt"],
            "values": a.session.get("values", VALUES_CAL),
            "calibration": calibration_info(a),
            "timestamps": _timestamp_doc(a.session),
            "sd_card": a.session.get("sd"),
            "devices": [{**{k: v for k, v in d.meta.items() if k != "file"}, "stats": d.stats} for d in a.devices],
            "plugins": [{k: v for k, v in p.meta.items() if k != "file"} for p in a.plugins],
            "keypoint_definitions": a.session.get("keypoint_definitions", []),
            "events": [{**e, "utc": clocks.utc_iso(e["unix_ns"])} for e in a.events],
            "clocks": a.session["clocks"],
        }
        raw_dir = a.folder / "sd_raw"                   # SD recordings: the Shimmers' original files
        if raw_dir.is_dir():
            for f in sorted(raw_dir.rglob("*")):
                if f.is_file():
                    arc = "sd_raw/" + f.relative_to(raw_dir).as_posix()
                    zf.write(f, arc)
                    info["files"].append(arc)
        zf.writestr("info.json", json.dumps(info, indent=2))
        zf.writestr("notes.txt", notes or "")
    with zipfile.ZipFile(tmp) as zf:
        bad = zf.testzip()
    if bad:
        raise OSError(f"ZIP verification failed at {bad}; working files kept in {a.folder}")
    tmp.replace(path)
    discard(a.folder)
    return path
