"""Quality checks run on a recording before it is saved.

These are heuristics that flag likely problems (electrode off, mains interference, missing
data, implausible values); they are not a clinical assessment. Every threshold is a named
constant below so it can be reviewed and tuned.
"""
from dataclasses import dataclass

import numpy as np
from scipy.signal import butter, sosfiltfilt, welch

OK, INFO, WARNING, FAIL = "OK", "INFO", "WARNING", "FAIL"

COMPLETENESS_WARN, COMPLETENESS_FAIL = 98.0, 90.0          # % of expected samples
ECG_SATURATION_FAIL = 1.0                                   # % of samples at full scale
ECG_FLAT_STD_MV = 0.005
ECG_MIN_P2P_MV, ECG_MAX_P2P_MV = 0.1, 8.0                   # robust peak-to-peak after 0.5 Hz high-pass
MAINS_RATIO_WARN = 0.5                                      # 48-52/58-62 Hz power vs 5-40 Hz power
HR_MIN_BPM, HR_MAX_BPM = 40.0, 180.0
EDA_NO_CONTACT_US = 0.05
EDA_NO_CONTACT_FAIL = 5.0                                   # % of samples
EDA_MAX_US = 60.0
EDA_JUMP_US_PER_S = 5.0                                     # faster changes are artifacts
EDA_JUMP_WARN = 1.0                                         # % of samples
EDA_FLAT_STD_US = 0.002
BATTERY_WARN_PCT = 10.0


@dataclass
class QCItem:
    device: str
    signal: str
    check: str
    status: str
    value: str
    detail: str


def _pct(mask) -> float:
    return 100.0 * float(np.mean(mask)) if len(mask) else 0.0


def check_completeness(device, samples, expected, disconnects, downtime_s, longest_gap_s):
    out = []
    pct = 100.0 * samples / expected if expected else 0.0
    status = OK if pct >= COMPLETENESS_WARN else WARNING if pct >= COMPLETENESS_FAIL else FAIL
    out.append(QCItem(device, "all", "Completeness", status, f"{pct:.1f} %",
                      f"{samples:,} of {expected:,} expected samples"))
    if disconnects:
        out.append(QCItem(device, "all", "Connection", WARNING, f"{disconnects} loss(es)",
                          f"{downtime_s:.1f} s without data in total, longest gap {longest_gap_s:.1f} s"))
    else:
        out.append(QCItem(device, "all", "Connection", OK, "no losses", ""))
    return out


def check_ecg(device, name, x: np.ndarray, rate: float, full_scale: float | None):
    out = []
    x = x[np.isfinite(x)]
    if len(x) < rate * 2:
        return [QCItem(device, name, "ECG data", WARNING, f"{len(x)} samples", "too short to assess")]
    if full_scale:
        sat = _pct(np.abs(x) >= 0.999 * full_scale)
        status = OK if sat == 0 else WARNING if sat < ECG_SATURATION_FAIL else FAIL
        out.append(QCItem(device, name, "Saturation", status, f"{sat:.2f} %",
                          "" if status == OK else "signal at full scale: electrode/lead off or disconnected"))
    if np.std(x) < ECG_FLAT_STD_MV:
        out.append(QCItem(device, name, "Flat signal", FAIL, f"std {np.std(x):.4f} mV", "no ECG activity recorded"))
        return out
    hp = sosfiltfilt(butter(2, 0.5, btype="highpass", fs=rate, output="sos"), x)
    p2p = float(np.percentile(hp, 99.5) - np.percentile(hp, 0.5))
    if p2p < ECG_MIN_P2P_MV:
        out.append(QCItem(device, name, "Amplitude", WARNING, f"{p2p:.2f} mV",
                          "very small ECG: check electrode placement and contact"))
    elif p2p > ECG_MAX_P2P_MV:
        out.append(QCItem(device, name, "Amplitude", WARNING, f"{p2p:.1f} mV",
                          "very large amplitude: movement artifacts or poor contact"))
    else:
        out.append(QCItem(device, name, "Amplitude", OK, f"{p2p:.2f} mV", ""))
    if rate >= 128:
        f, p = welch(hp, fs=rate, nperseg=min(len(hp), int(rate * 4)))
        band = lambda lo, hi: float(np.trapezoid(p[(f >= lo) & (f <= hi)], f[(f >= lo) & (f <= hi)]))
        mains = band(48, 52) + (band(58, 62) if rate > 124 else 0.0)
        ratio = mains / max(band(5, 40), 1e-12)
        status = OK if ratio < MAINS_RATIO_WARN else WARNING
        out.append(QCItem(device, name, "Mains interference", status, f"{ratio:.2f}",
                          "" if status == OK else "strong 50/60 Hz noise: check electrode contact and cables"))
    return out


def check_eda(device, name, x: np.ndarray, rate: float):
    out = []
    finite = np.isfinite(x)
    no_contact = _pct(~finite | (np.nan_to_num(x) < EDA_NO_CONTACT_US))
    status = OK if no_contact == 0 else WARNING if no_contact < EDA_NO_CONTACT_FAIL else FAIL
    out.append(QCItem(device, name, "Skin contact", status, f"{no_contact:.1f} % without contact",
                      "" if status == OK else f"conductance below {EDA_NO_CONTACT_US} µS: electrodes not on skin"))
    x = x[finite]
    if len(x) < 2:
        return out
    high = _pct(x > EDA_MAX_US)
    out.append(QCItem(device, name, "Range", OK if high == 0 else WARNING, f"{np.min(x):.3g}–{np.max(x):.3g} µS",
                      "" if high == 0 else f"{high:.1f} % above {EDA_MAX_US} µS (implausible)"))
    jumps = _pct(np.abs(np.diff(x)) * rate > EDA_JUMP_US_PER_S)
    out.append(QCItem(device, name, "Artifacts", OK if jumps < EDA_JUMP_WARN else WARNING,
                      f"{jumps:.2f} % abrupt changes",
                      "" if jumps < EDA_JUMP_WARN else f"changes faster than {EDA_JUMP_US_PER_S} µS/s: "
                                                        f"movement or electrode artifacts"))
    if np.std(x) < EDA_FLAT_STD_US and no_contact < 100:
        out.append(QCItem(device, name, "Flat signal", WARNING, f"std {np.std(x):.4f} µS",
                          "no variation at all: check the sensor"))
    return out


def check_heart_rate(device, hr: np.ndarray):
    if len(hr) == 0:
        return [QCItem(device, "HRV", "Heart rate", WARNING, "no beats", "no heartbeats detected")]
    med = float(np.median(hr))
    out_of_range = _pct((hr < HR_MIN_BPM) | (hr > HR_MAX_BPM))
    status = OK if HR_MIN_BPM <= med <= HR_MAX_BPM and out_of_range < 5 else WARNING
    return [QCItem(device, "HRV", "Heart rate", status, f"median {med:.0f} bpm",
                   "" if status == OK else f"{out_of_range:.1f} % of beats outside {HR_MIN_BPM:.0f}–{HR_MAX_BPM:.0f} bpm")]


def check_battery(device, battery_mv: np.ndarray, to_percent):
    if len(battery_mv) == 0:
        return []
    low = to_percent(float(np.min(battery_mv)))
    return [QCItem(device, "Battery", "Battery", OK if low >= BATTERY_WARN_PCT else WARNING, f"min {low:.0f} %",
                   "" if low >= BATTERY_WARN_PCT else "battery ran low during the recording")]


START_END_PAIRS = [("rest start", "rest end"), ("experiment start", "experiment end"), ("task start", "task end")]


def check_keypoints(names: list):
    out = []
    for start, end in START_END_PAIRS:
        a, b = names.count(start), names.count(end)
        if a or b:
            out.append(QCItem("recording", "Keypoints", f"{start} / {end}", OK if a == b else WARNING,
                              f"{a} / {b}", "" if a == b else "starts and ends do not match"))
    n = names.count("discard")
    if n:
        out.append(QCItem("recording", "Keypoints", "discard", INFO, str(n), "segments marked as discard"))
    return out
