"""Before/after check for a firmware update: does the Shimmer still record the same way?

    python tools/fw_check.py record <MAC> <label> [seconds] [--ppg]   e.g. record 60:8A:10:40:F6:6C before
                                       --ppg: make sure the PPG probe's 3 V power setting is ON, as the app does
    python tools/fw_check.py compare <before.json> <after.json>

`record` connects over Bluetooth (pairing first if needed, PIN 1234), reads the Shimmer's
settings without changing anything, streams `seconds` of live data (default 60) and writes a
report (JSON) to ~/Documents/ShimmerFirmwareChecks/. `compare` prints, per item, SAME,
APPROX (values that depend on the person, e.g. EDA on fingers) or DIFFERENT.

Use the same set-up for both runs: same sensors attached the same way, Shimmer lying still.
"""
import datetime as dt
import json
import pathlib
import sys
import threading
import time

import numpy as np
from pyshimmer import EChannelType

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shimmer_live import clocks, sdlog  # noqa: E402
from shimmer_live.config import Config  # noqa: E402
from shimmer_live.conversions import build_specs, calibrate_array  # noqa: E402
from shimmer_live.session import timed  # noqa: E402

OUT = pathlib.Path.home() / "Documents" / "ShimmerFirmwareChecks"
LOST_FACTOR = 1.5                       # a gap longer than 1.5 sample periods = lost samples


def _ensure_paired(mac, cfg):
    from shimmer_live import dock_setup
    from shimmer_live.bt import get_backend
    be = get_backend()
    if any(d.address == mac and d.paired for d in be.list_paired()):
        return
    print("not paired yet: looking for it over Bluetooth and pairing (PIN 1234)…")
    dev = be.find([mac], 20.0).get(mac)
    if dev is None:
        sys.exit("not visible over Bluetooth: is it switched on?")
    dock_setup._pair(be, dev, lambda name: None, print)


def _settings(conn):
    """Everything about the Shimmer's setup that an update must not change (read-only)."""
    from shimmer_live.expansion_power import GetExpansionPower
    fw_type, v = timed(conn.dev.get_firmware_version, 5.0)
    rate, _, chans = timed(conn.dev.get_inquiry, 5.0)
    gains = {}
    for chip in (0, 1):
        if any(c.name.startswith(f"EXG_ADS1292R_{chip + 1}") for c in chans):
            reg = timed(conn.dev.get_exg_register, 5.0, chip)
            gains[f"{chip},0"], gains[f"{chip},1"] = reg.ch1_gain, reg.ch2_gain
    try:
        exp_power = bool(timed(conn.dev._process_and_wait, 5.0, GetExpansionPower()))
    except Exception:                                          # noqa: BLE001 - older firmware may not answer
        exp_power = None
    return {"firmware": f"{fw_type.name} {v.major}.{v.minor}.{v.rel}", "rate_hz": rate,
            "channels": [c.name for c in chans], "exg_gains": gains, "expansion_power_3v": exp_power,
            "device_name": timed(conn.dev.get_device_name, 5.0)}, chans


def record(mac, label, seconds=60.0, ppg=False):
    mac = mac.upper()
    cfg = Config()
    _ensure_paired(mac, cfg)
    print("connecting…")
    conn = sdlog.Connection(mac, cfg)
    try:
        settings, chans = _settings(conn)
        if ppg:                                   # as the app does for a PPG probe: 3 V power setting ON
            from shimmer_live.expansion_power import ensure_ppg_power
            ensure_ppg_power(conn.dev, timed)
            settings["ppg_power_rewritten"] = True
        print(f"{settings['firmware']} · {settings['rate_hz']:g} Hz · {', '.join(settings['channels'])}"
              f" · 3 V expansion power: {settings['expansion_power_3v']}")
        gains = {tuple(int(x) for x in k.split(",")): g for k, g in settings["exg_gains"].items()}
        specs = build_specs(chans, gains)
        rows, lock = [], threading.Lock()

        def on_packet(pkt):
            with lock:
                rows.append((pkt[EChannelType.TIMESTAMP], clocks.perf_ns(), [pkt[s.chan] for s in specs]))
        conn.dev.add_stream_callback(on_packet)
        timed(conn.dev.start_streaming, 5.0)
        t0 = time.monotonic()
        while time.monotonic() - t0 < seconds:
            time.sleep(5)
            print(f"  {time.monotonic() - t0:4.0f} s · {len(rows)} samples")
        timed(conn.dev.stop_streaming, 5.0)
    finally:
        conn.close()
    report = {"label": label, "mac": mac, "when": dt.datetime.now().isoformat(timespec="seconds"),
              "seconds": seconds, "settings": settings, "stream": _stream_stats(rows, settings["rate_hz"]),
              "signals": _signal_stats(rows, specs)}
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"fwcheck_{mac.replace(':', '')}_{label}_{dt.datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps(report, indent=2))
    print(f"\nreport: {path}")
    _print_report(report)
    return path


def _stream_stats(rows, rate):
    ts = np.array([r[0] for r in rows], dtype=np.int64)
    ticks = np.concatenate([[0], np.cumsum(np.diff(ts) % clocks.TS_WRAP)]) if len(ts) else ts
    period = clocks.TICKS_PER_S / rate
    gaps = np.diff(ticks) / period if len(ticks) > 1 else np.empty(0)
    lost = int(np.round(gaps[gaps > LOST_FACTOR] - 1).sum()) if len(gaps) else 0
    span = ticks[-1] / clocks.TICKS_PER_S if len(ticks) else 0.0
    host = np.array([r[1] for r in rows], dtype=np.int64)
    return {"samples": len(rows), "lost_samples": lost,
            "completeness_pct": round(100.0 * len(rows) / (len(rows) + lost), 3) if rows else 0.0,
            "measured_rate_hz": round((len(rows) - 1) / span, 3) if span else 0.0,
            "pc_rate_hz": round((len(rows) - 1) / ((host[-1] - host[0]) / 1e9), 3) if len(host) > 1 else 0.0}


def _signal_stats(rows, specs):
    out = {}
    for i, s in enumerate(specs):
        raw = np.array([r[2][i] for r in rows], dtype=float)
        val = calibrate_array(s.calibration, raw) if s.calibration else raw
        if s.chan == EChannelType.INTERNAL_ADC_13:            # PPG on GSR+: 12-bit ADC, 3 V reference
            val, unit = raw * 3000.0 / 4095.0, "mV"
        else:
            unit = s.unit or "raw"
        ok = val[np.isfinite(val)]
        entry = {"label": s.label, "unit": unit, "raw_mean": float(raw.mean()) if len(raw) else None,
                 "raw_std": float(raw.std()) if len(raw) else None}
        if len(ok):
            entry.update(mean=float(ok.mean()), std=float(ok.std()), min=float(ok.min()), max=float(ok.max()),
                         p5=float(np.percentile(ok, 5)), p95=float(np.percentile(ok, 95)))
        if s.kind == "gsr":
            entry["no_contact_pct"] = round(100.0 * (1 - len(ok) / len(val)), 2) if len(val) else None
            entry["range_codes"] = sorted({int(x) >> 14 & 3 for x in raw})
        out[s.chan.name] = entry
    return out


def _print_report(r):
    s = r["stream"]
    print(f"stream: {s['samples']} samples, lost {s['lost_samples']} ({s['completeness_pct']}% complete), "
          f"Shimmer clock rate {s['measured_rate_hz']} Hz, PC-measured {s['pc_rate_hz']} Hz")
    for name, e in r["signals"].items():
        if "mean" in e:
            extra = f", no contact {e['no_contact_pct']}%" if "no_contact_pct" in e else ""
            print(f"  {e['label']:<28} mean {e['mean']:10.3f} {e['unit']:<4} std {e['std']:8.3f}  "
                  f"[{e['min']:.3f} … {e['max']:.3f}]{extra}")
        else:
            print(f"  {e['label']:<28} no valid values")


# ---------------------------------------------------------------- compare
# Signals that depend on the person or the room: only the order of magnitude is checked.
PERSON_DEPENDENT = {"GSR_RAW", "INTERNAL_ADC_13"}


def compare(a_path, b_path):
    a, b = (json.loads(pathlib.Path(p).read_text()) for p in (a_path, b_path))
    rows = []

    def add(item, va, vb, verdict):
        rows.append((item, va, vb, verdict))

    sa, sb = a["settings"], b["settings"]
    add("firmware", sa["firmware"], sb["firmware"], "changed (expected)" if sa["firmware"] != sb["firmware"] else "SAME")
    for key in ("rate_hz", "channels", "exg_gains", "expansion_power_3v", "device_name"):
        add(key, sa.get(key), sb.get(key), "SAME" if sa.get(key) == sb.get(key) else "DIFFERENT")
    ca, cb = a["stream"]["completeness_pct"], b["stream"]["completeness_pct"]
    add("samples received (%)", ca, cb, "SAME" if min(ca, cb) >= 99.0 else "DIFFERENT")
    ra, rb = a["stream"]["measured_rate_hz"], b["stream"]["measured_rate_hz"]
    add("sampling rate kept (Hz)", ra, rb, "SAME" if abs(ra - rb) <= 0.001 * ra else "DIFFERENT")
    for name in sorted(set(a["signals"]) | set(b["signals"])):
        ea, eb = a["signals"].get(name), b["signals"].get(name)
        if ea is None or eb is None or "mean" not in ea or "mean" not in eb:
            add(name, ea and ea.get("mean"), eb and eb.get("mean"), "DIFFERENT (missing)")
            continue
        ma, mb = ea["mean"], eb["mean"]
        ratio = mb / ma if ma else float("inf")
        if name in PERSON_DEPENDENT:
            verdict = "APPROX ok" if 0.5 <= ratio <= 2.0 else "APPROX check"
        else:
            verdict = "SAME" if abs(mb - ma) <= max(3 * max(ea["std"], eb["std"]), 0.02 * abs(ma)) else "DIFFERENT"
        add(f"{ea['label']} mean ({ea['unit']})", round(ma, 3), round(mb, 3), verdict)
        if "no_contact_pct" in ea:
            add(f"{ea['label']} no-contact %", ea["no_contact_pct"], eb["no_contact_pct"],
                "SAME" if abs(ea["no_contact_pct"] - eb["no_contact_pct"]) < 5 else "APPROX check")
    w = max(len(r[0]) for r in rows)
    print(f"{'item':<{w}}  {'before':<28} {'after':<28} verdict")
    for item, va, vb, verdict in rows:
        print(f"{item:<{w}}  {str(va)[:28]:<28} {str(vb)[:28]:<28} {verdict}")
    bad = [r for r in rows if r[3].startswith("DIFFERENT") or r[3] == "APPROX check"]
    print("\nRESULT:", "everything matches" if not bad else f"{len(bad)} item(s) to look at")
    return not bad


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "record":
        args = [a for a in sys.argv[2:] if a != "--ppg"]
        record(args[0], args[1], float(args[2]) if len(args) > 2 else 60.0, ppg="--ppg" in sys.argv)
    elif len(sys.argv) == 4 and sys.argv[1] == "compare":
        sys.exit(0 if compare(sys.argv[2], sys.argv[3]) else 1)
    else:
        sys.exit(__doc__)
