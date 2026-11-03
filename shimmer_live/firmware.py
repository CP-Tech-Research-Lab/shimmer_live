"""Is a Shimmer's firmware current? Used only to show a flag; this app never flashes firmware.

The latest version is fixed in the app (checked by hand against Shimmer's GitHub releases), so it
can be out of date: the flag says "latest known to this app", never "latest".
Source: https://github.com/ShimmerResearch/shimmer3-firmware/releases
Shimmer's tag LogAndStream_Shimmer3_v1.01.009 is reported by the device as 1.1.9.
"""
import re

LATEST_KNOWN = (1, 1, 9)
LATEST_TAG = "LogAndStream_Shimmer3_v1.01.009"
CHECKED_ON = "2026-11-03"
RELEASES_URL = "https://github.com/ShimmerResearch/shimmer3-firmware/releases"

CURRENT, NEWER_EXISTS, OLD, NOT_STREAMING, UNKNOWN = "current", "newer", "old", "sdlog", "unknown"

HOW_TO_UPDATE = ("Update with Shimmer's Consensys software (dock the Shimmer, Firmware tab). Update one Shimmer "
                 "first and test it with this app before updating the others.")


def parse(fw: str):
    """'LogAndStream 1.0.5' -> ('LogAndStream', (1, 0, 5)); None if not recognised."""
    m = re.match(r"\s*(\w+)\s+(\d+)\.(\d+)\.(\d+)", fw or "")
    if not m:
        return None
    return m.group(1), (int(m.group(2)), int(m.group(3)), int(m.group(4)))


def fmt(v) -> str:
    return ".".join(str(x) for x in v)


def assess(fw: str) -> tuple[str, str, str]:
    """(level, short text, explanation) for a firmware string."""
    p = parse(fw)
    if p is None:
        return UNKNOWN, "—", "Firmware not read yet: test the connection or dock the Shimmer."
    kind, v = p
    latest = (f"Latest version known to this app: {fmt(LATEST_KNOWN)} ({LATEST_TAG}, checked {CHECKED_ON}; "
              f"see {RELEASES_URL}).")
    if kind != "LogAndStream":
        return NOT_STREAMING, f"{kind} {fmt(v)} ⚠", (f"{kind} firmware cannot stream over Bluetooth. Install "
                                                     f"LogAndStream to use it with this app.\n{latest}\n{HOW_TO_UPDATE}")
    if v < (1, 0, 0):
        return OLD, f"{fmt(v)} ⬆", (f"Old firmware (0.x series, 2018–2023). Versions 1.0 and later fix Bluetooth "
                                    f"lock-ups when SD logging starts or stops while connected, Bluetooth throughput, "
                                    f"SD-card handover to the PC and the GSR auto-range.\n{latest}\n{HOW_TO_UPDATE}")
    if v < LATEST_KNOWN:
        return NEWER_EXISTS, f"{fmt(v)} ↑", (f"A newer version exists ({fmt(LATEST_KNOWN)}). Not urgent: this "
                                            f"version works with this app.\n{latest}\n{HOW_TO_UPDATE}")
    return CURRENT, fmt(v), f"Up to date. {latest}"
