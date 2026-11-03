#!/usr/bin/env python3
"""Load a Shimmer Live recording (.zip) with pandas.

    python load_recording.py recording.zip             # summary
    python load_recording.py recording.zip --plot      # plot every channel with keypoints (needs matplotlib)
    python load_recording.py recording.zip --extract   # unzip next to the file

From your own code:

    from load_recording import load_recording
    rec = load_recording("recording.zip")
    ecg = rec["devices"]["Shimmer3-8395"]                    # DataFrame, one row per sample
    ecg["Shimmer3-8395_Timestamp_Unix_CAL"]                  # ms since 1970 (UTC), sample time
    ecg["Shimmer3-8395_ECG_LL-RA_24BIT_CAL"]                 # mV
    rec["keypoints"], rec["qc"], rec["info"]["notes"]
    rec["plugins"]["HRV - Shimmer3-8395"]                    # heart rate, RR, RMSSD per beat

Each device file uses the Consensys "Calibrated" CSV layout ("sep=," line, column names, units,
data), so existing Consensys scripts can read the files inside the ZIP too. Every table has three
timestamps: <device>_Timestamp_Unix_CAL (ms, PC wall clock), _Timestamp_Shared_CAL (s, shared
clock: Unreal/QueryPerformanceCounter on Windows) and _Timestamp_CAL (ms, Shimmer clock). The
PC times are sampling times: Bluetooth latency was already removed when the recording was saved.

Only needs pandas. Licensed GPL-3.0 like the rest of the project.
"""
import argparse
import csv
import io
import json
import sys
import zipfile
from pathlib import Path

import pandas as pd

TABLES = {"keypoints.csv", "qc.csv"}


def _lines(data: bytes) -> list:
    lines = data.decode("utf-8").splitlines()
    return lines[1:] if lines and lines[0].strip('"') == "sep=," else lines


def read_consensys_csv(data: bytes) -> tuple:
    """Return (DataFrame, units) for a Consensys-layout CSV: "sep=," / names / units / data."""
    lines = _lines(data)
    names = next(csv.reader([lines[0]]))
    units = next(csv.reader([lines[1]]))
    text = {n: str for n in names if n.endswith("_Keypoint")}       # mostly empty: keep it text
    df = pd.read_csv(io.StringIO("\n".join(lines[2:])), header=None, names=names, dtype=text, low_memory=False)
    return df, units


def read_plain_csv(data: bytes) -> pd.DataFrame:
    lines = _lines(data)
    return pd.read_csv(io.StringIO("\n".join(lines))) if lines else pd.DataFrame()


def load_recording(path) -> dict:
    """{"devices": {name: df}, "plugins": {name: df}, "units": {file: [...]}, "keypoints": df, "qc": df,
    "info": dict, "notes": str}"""
    out = {"devices": {}, "plugins": {}, "units": {}}
    with zipfile.ZipFile(path) as zf:
        info = json.loads(zf.read("info.json"))
        out["info"] = info
        out["notes"] = zf.read("notes.txt").decode("utf-8") if "notes.txt" in zf.namelist() else ""
        device_names = [d["name"] for d in info["devices"]]
        for member in zf.namelist():
            if member in ("info.json", "notes.txt") or not member.endswith(".csv"):
                continue                     # e.g. sd_raw/: the Shimmers' original SD files (binary)
            if member in TABLES:
                out[member[:-4]] = read_plain_csv(zf.read(member))
                continue
            df, units = read_consensys_csv(zf.read(member))
            out["units"][member] = units
            prefix = df.columns[0].rsplit("_Timestamp_Unix_CAL", 1)[0]
            if prefix in device_names:
                out["devices"][prefix] = df
            else:
                out["plugins"][member[:-4]] = df
    return out


def summarise(rec: dict) -> None:
    info = rec["info"]
    s = info["summary"]
    print(f"Recording: {info.get('recording_name') or '(no name)'}   started {s['start_utc']}   "
          f"duration {s['duration_s']:.1f} s   app {info.get('app_version')}")
    if rec["notes"]:
        print("Notes:", rec["notes"].replace("\n", "\n       "))
    for d in info["devices"]:
        st = d["stats"]
        print(f"\n{d['name']} ({d['mac']}, {d['kind']}, {d['sampling_rate_hz']:g} Hz): {st['samples']:,} samples, "
              f"{st['completeness_pct']:.1f} % complete, {st['disconnects']} connection loss(es)")
        df = rec["devices"].get(d["name"])
        if df is not None:
            chans = [c[len(d["name"]) + 1:] for c in df.columns
                     if "Timestamp" not in c and not c.endswith("_Keypoint")]
            print("  channels:", ", ".join(chans))
    for name, df in rec["plugins"].items():
        print(f"\nplugin {name}: {len(df)} values")
    kp = rec.get("keypoints")
    if kp is not None and len(kp):
        print("\nkeypoints:", ", ".join(kp["Keypoint"]))
    qc = rec.get("qc")
    if qc is not None and len(qc):
        bad = qc[qc["status"].isin(["WARNING", "FAIL"])]
        print(f"\nquality checks: {len(qc) - len(bad)} OK/INFO, {len(bad)} warning(s)/failure(s)")
        for _, r in bad.iterrows():
            print(f"  {r['status']:7s} {r['device']} {r['signal']} {r['check']}: {r['value']} {r['detail']}")


def plot(rec: dict) -> None:
    import matplotlib.pyplot as plt
    series = []
    for df in list(rec["devices"].values()) + list(rec["plugins"].values()):
        tcol = df.columns[0]
        for c in df.columns:
            if "Timestamp" in c or c.endswith("_Keypoint") or "Status" in c or df[c].dtype == object:
                continue
            series.append((tcol, c, df))
    if not series:
        print("nothing to plot")
        return
    t0 = min(df[t].min() for t, _, df in series)
    fig, axes = plt.subplots(len(series), 1, sharex=True, figsize=(11, 1.7 * len(series) + 1), squeeze=False)
    kp = rec.get("keypoints")
    for ax, (tcol, c, df) in zip(axes[:, 0], series):
        ax.plot((df[tcol] - t0) / 1000, df[c], "o-" if len(df) < 2000 else "-", lw=0.8, ms=2)
        ax.set_title(c, loc="left", fontsize=9)
        if kp is not None and len(kp):
            for _, k in kp.iterrows():
                ax.axvline((k["Timestamp_Unix_CAL"] - t0) / 1000, color=k["Color"], lw=1.2)
    axes[-1, 0].set_xlabel("seconds since recording start")
    fig.tight_layout()
    plt.show()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("zip", type=Path)
    ap.add_argument("--plot", action="store_true", help="plot all channels with keypoints (needs matplotlib)")
    ap.add_argument("--extract", action="store_true", help="unzip the recording next to the file")
    args = ap.parse_args(argv)
    rec = load_recording(args.zip)
    summarise(rec)
    if args.extract:
        out = args.zip.with_suffix("")
        with zipfile.ZipFile(args.zip) as zf:
            zf.extractall(out)
        print(f"\nextracted to {out}")
    if args.plot:
        plot(rec)


if __name__ == "__main__":
    sys.exit(main())
