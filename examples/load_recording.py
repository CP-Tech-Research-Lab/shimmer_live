#!/usr/bin/env python3
"""Load a Shimmer Live recording and align every sample to PC time.

    python load_recording.py path/to/recording.csv            # summary
    python load_recording.py path/to/recording.csv --plot     # also plot (needs matplotlib)
    python load_recording.py path/to/recording.csv --save     # write <device>_aligned.csv files

Or from your own code:

    from load_recording import load_recording
    devices = load_recording("recording.csv")     # {device name: DataFrame}
    ecg = devices["Shimmer3-8395"]
    ecg[["sample_unix_s", "EXG1_CH1_mV"]]

Why alignment is needed: host_unix_ns / shared_clock_* are the times the PC *received* each
packet. Bluetooth adds a variable delay (typically 10-100 ms, packets often arrive in bursts),
so these jitter. The Shimmer's own clock (shimmer_time_s) spaces samples exactly. For each
connection period this script fits a line from Shimmer time to PC time (the slope absorbs the
small drift between the two crystals) and places it on the lower edge of the arrival times,
because a packet can arrive late but never early. Added columns:

    period             connection period (increments after each link loss / reconnect)
    sample_unix_s      estimated sampling instant, PC wall clock (UTC, seconds since 1970)
    sample_shared_s    the same instant on the shared clock (same units as shared_clock_s)
    latency_ms         how late each packet arrived relative to that estimate

Only needs pandas and numpy. Licensed GPL-3.0 like the rest of the project.
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

TICKS_PER_S = 32768.0
COMMON = ["device", "mac", "host_unix_ns", "host_utc_iso", "shared_clock_ticks", "shared_clock_s",
          "host_perf_ns", "shimmer_ts_raw", "shimmer_ticks", "shimmer_time_s"]


def load_recording(csv_path) -> dict:
    """Read a recording; return {device name: DataFrame} with sample-time alignment added."""
    df = pd.read_csv(csv_path, dtype={"device": str, "mac": str, "host_utc_iso": str},
                     low_memory=False)
    out = {}
    for name, d in df.groupby("device", sort=False):
        d = d.dropna(axis=1, how="all")                       # other devices' channel columns
        d = d.sort_values("shimmer_ticks").reset_index(drop=True)
        d["host_unix_ns"] = d["host_unix_ns"].astype("int64")
        if "shared_clock_ticks" in d:          # absent in recordings from early versions
            d["shared_clock_ticks"] = d["shared_clock_ticks"].astype("int64")
        out[name] = align_to_host(d)
    return out


def split_periods(ticks: np.ndarray) -> np.ndarray:
    """Period number per sample: a new period starts where the Shimmer clock jumps (reconnect)."""
    if len(ticks) < 2:
        return np.zeros(len(ticks), dtype=int)
    step = np.diff(ticks)
    nominal = np.median(step)
    return np.concatenate([[0], np.cumsum(step > 2.5 * nominal)])


def _fit_lower_edge(t: np.ndarray, host: np.ndarray):
    """host ~= a*t + b, with b chosen so no packet arrives before its estimated sampling time."""
    if len(t) < 2 or np.ptp(t) == 0:
        return 1.0, float(np.min(host - t))
    a = np.polyfit(t, host, 1)[0]
    return a, float(np.min(host - a * t))


def align_to_host(d: pd.DataFrame) -> pd.DataFrame:
    d = d.copy()
    d["period"] = split_periods(d["shimmer_ticks"].to_numpy())
    t = d["shimmer_time_s"].to_numpy(dtype=float)
    # Work relative to the first sample: absolute epoch seconds lose precision in a float fit.
    u0 = int(d["host_unix_ns"].iloc[0])
    host = (d["host_unix_ns"].to_numpy() - u0) / 1e9
    has_shared = "shared_clock_ticks" in d
    if has_shared:
        s0 = int(d["shared_clock_ticks"].iloc[0])
        spc = float(d["shared_clock_s"].iloc[0]) / s0 if s0 else 1e-9   # seconds per shared-clock tick
        shared = (d["shared_clock_ticks"].to_numpy() - s0) * spc
    sample_host = np.empty(len(d))
    sample_shared = np.empty(len(d))
    for p in np.unique(d["period"]):
        m = (d["period"] == p).to_numpy()
        a, b = _fit_lower_edge(t[m], host[m])
        sample_host[m] = a * t[m] + b
        if has_shared:
            a, b = _fit_lower_edge(t[m], shared[m])
            sample_shared[m] = a * t[m] + b
    d["sample_unix_s"] = u0 / 1e9 + sample_host
    if has_shared:
        d["sample_shared_s"] = s0 * spc + sample_shared
    d["latency_ms"] = (host - sample_host) * 1000.0
    return d


def channel_columns(d: pd.DataFrame) -> list:
    """Calibrated signal columns (e.g. GSR_uS, EXG1_CH1_mV), falling back to raw ones."""
    cols = [c for c in d.columns if c not in COMMON and c not in ("period", "sample_unix_s", "sample_shared_s",
                                                                 "latency_ms") and not c.startswith("VBATT")
            and "STATUS" not in c]
    calibrated = [c for c in cols if not c.endswith("_raw")]
    return calibrated or cols


def summarise(path: Path, devices: dict) -> None:
    meta_path = path.with_name(path.name[:-4] + ".meta.json")
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        print(f"Recording {meta.get('recording_name') or meta['file']}: started {meta['start_host_utc_iso']}")
        for ev in meta.get("events", []):
            print(f"  {ev['host_utc_iso']}  {ev['device']}: {ev['event']} - {ev['text']}")
    for name, d in devices.items():
        span = d["sample_unix_s"].iloc[-1] - d["sample_unix_s"].iloc[0]
        step = np.median(np.diff(d["shimmer_ticks"])) if len(d) > 1 else float("nan")
        rate = TICKS_PER_S / step                             # from the Shimmer's own sample spacing
        covered = len(d) / rate
        print(f"\n{name} ({d['mac'].iloc[0]}): {len(d):,} samples at {rate:g} Hz = {covered:.1f} s of data "
              f"over {span:.1f} s, {d['period'].nunique()} connection period(s)")
        print(f"  Bluetooth latency: median {d['latency_ms'].median():.1f} ms, "
              f"95th pct {d['latency_ms'].quantile(0.95):.1f} ms, max {d['latency_ms'].max():.1f} ms")
        print(f"  channels: {', '.join(channel_columns(d))}")


def plot(devices: dict) -> None:
    import matplotlib.pyplot as plt
    rows = [(name, c) for name, d in devices.items() for c in channel_columns(d)]
    t0 = min(d["sample_unix_s"].iloc[0] for d in devices.values())
    fig, axes = plt.subplots(len(rows), 1, sharex=True, figsize=(11, 1.8 * len(rows) + 1), squeeze=False)
    for ax, (name, c) in zip(axes[:, 0], rows):
        d = devices[name]
        for _, part in d.groupby("period"):                   # no line across connection gaps
            ax.plot(part["sample_unix_s"] - t0, part[c], lw=0.8)
        ax.set_title(f"{name} - {c}", loc="left", fontsize=9)
    axes[-1, 0].set_xlabel("seconds since recording start")
    fig.tight_layout()
    plt.show()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("csv", type=Path)
    ap.add_argument("--plot", action="store_true", help="plot all channels (needs matplotlib)")
    ap.add_argument("--save", action="store_true", help="write one aligned CSV per device next to the input")
    args = ap.parse_args(argv)
    devices = load_recording(args.csv)
    summarise(args.csv, devices)
    if args.save:
        for name, d in devices.items():
            out = args.csv.with_name(f"{args.csv.stem}_{name}_aligned.csv")
            d.to_csv(out, index=False)
            print(f"wrote {out}")
    if args.plot:
        plot(devices)


if __name__ == "__main__":
    sys.exit(main())
