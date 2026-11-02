import re
import sys

import pytest

from shimmer_live import clocks


def test_shared_clock_seconds_is_ticks_times_seconds_per_cycle():
    t = clocks.shared_clock_ticks()
    assert clocks.shared_clock_seconds(t) == float(t) * clocks.SHARED_CLOCK_SECONDS_PER_CYCLE


def test_shared_clock_is_monotonic():
    ticks = [clocks.shared_clock_ticks() for _ in range(2000)]
    assert all(b >= a for a, b in zip(ticks, ticks[1:]))
    assert ticks[-1] > ticks[0]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows performance counter")
def test_windows_shared_clock_matches_unreal_definition():
    import ctypes
    freq = ctypes.c_int64()
    ctypes.WinDLL("kernel32").QueryPerformanceFrequency(ctypes.byref(freq))
    assert clocks.SHARED_CLOCK_SECONDS_PER_CYCLE == 1.0 / freq.value
    assert "QueryPerformanceCounter" in clocks.SHARED_CLOCK_SOURCE


def test_utc_iso_format():
    assert clocks.utc_iso(1_700_000_000_123_456_000) == "2023-11-14T22:13:20.123456Z"


def test_filename_stamp_is_windows_safe():
    s = clocks.local_stamp_for_filename(1_700_000_000_123_456_000)
    assert re.fullmatch(r"\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d\.123_UTC[+-]\d{4}", s)


def test_clock_report_describes_both_host_clocks():
    rep = clocks.clock_report()
    assert rep["host_unix_ns"]["implementation"]
    assert rep["shared_clock"]["seconds_per_cycle"] == clocks.SHARED_CLOCK_SECONDS_PER_CYCLE
