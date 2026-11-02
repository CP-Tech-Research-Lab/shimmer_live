"""System timestamps used in recordings, and a description of where they come from.

Every received packet is stamped with two host clocks:

* host_unix_ns        - time.time_ns(): wall clock, nanoseconds since 1970-01-01 UTC.
                        Comparable across machines if they are NTP/PTP-synchronised.
* shared_clock_ticks  - the monotonic "shared clock" other software on this PC can read
  shared_clock_s        to sync with a recording (shared_clock_s = ticks * seconds_per_cycle).
                        Windows: QueryPerformanceCounter / QueryPerformanceFrequency, computed
                        exactly like Unreal Engine's
                            FPlatformTime::Cycles64() * FPlatformTime::GetSecondsPerCycle64()
                        Other platforms: clock_gettime(CLOCK_MONOTONIC) in nanoseconds.

clock_report() lists the OS function behind each clock on the running system.
"""
import datetime as _dt
import platform
import sys
import time

TICKS_PER_S = 32768.0          # Shimmer3 RTC crystal behind the packet TIMESTAMP field
TS_BITS = 24
TS_WRAP = 1 << TS_BITS          # 24-bit counter wraps every 512 s

if sys.platform == "win32":
    import ctypes

    _kernel32 = ctypes.WinDLL("kernel32")
    _freq = ctypes.c_int64()
    _kernel32.QueryPerformanceFrequency(ctypes.byref(_freq))
    SHARED_CLOCK_SECONDS_PER_CYCLE = 1.0 / _freq.value
    SHARED_CLOCK_SOURCE = ("QueryPerformanceCounter() * (1.0 / QueryPerformanceFrequency()) - same as "
                           "Unreal FPlatformTime::Cycles64() * FPlatformTime::GetSecondsPerCycle64()")

    def shared_clock_ticks() -> int:
        v = ctypes.c_int64()                # per call: packets are stamped from several threads
        _kernel32.QueryPerformanceCounter(ctypes.byref(v))
        return v.value
else:
    SHARED_CLOCK_SECONDS_PER_CYCLE = 1e-9
    SHARED_CLOCK_SOURCE = "clock_gettime(CLOCK_MONOTONIC), nanoseconds"

    def shared_clock_ticks() -> int:
        return time.clock_gettime_ns(time.CLOCK_MONOTONIC)


def shared_clock_seconds(ticks: int) -> float:
    """double(ticks) * seconds-per-cycle: bit-identical to the Unreal computation on Windows."""
    return float(ticks) * SHARED_CLOCK_SECONDS_PER_CYCLE


def unix_ns() -> int:
    return time.time_ns()


def perf_ns() -> int:
    """Monotonic clock for durations inside the app (not written to recordings)."""
    return time.perf_counter_ns()


def utc_iso(ns: int) -> str:
    """ISO-8601 UTC with microseconds, e.g. 2026-11-02T09:41:03.123456Z."""
    return _dt.datetime.fromtimestamp(ns / 1e9, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def local_stamp_for_filename(ns: int) -> str:
    """Local start time, filename-safe on Windows too (no ':'), with UTC offset."""
    d = _dt.datetime.fromtimestamp(ns / 1e9).astimezone()
    return d.strftime("%Y-%m-%d_%H-%M-%S.") + f"{d.microsecond // 1000:03d}" + d.strftime("_UTC%z")


def clock_report() -> dict:
    i = time.get_clock_info("time")
    return {
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "host_unix_ns": {"python_call": "time.time_ns()", "implementation": i.implementation,
                         "resolution_s": i.resolution, "monotonic": i.monotonic, "adjustable": i.adjustable},
        "shared_clock": {"source": SHARED_CLOCK_SOURCE,
                         "seconds_per_cycle": SHARED_CLOCK_SECONDS_PER_CYCLE,
                         "monotonic": True, "adjustable": False},
        "shimmer_clock": {"ticks_per_second": TICKS_PER_S, "counter_bits": TS_BITS,
                          "wraps_every_s": TS_WRAP / TICKS_PER_S},
    }
