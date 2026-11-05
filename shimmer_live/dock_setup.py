"""One-click setup of Shimmers docked in a base: clock, pairing, connection test.

Per docked Shimmer:
  1. firmware must be LogAndStream (SDLog cannot stream over Bluetooth)
  2. find it over Bluetooth by the address read from the dock, pair if needed
     (the Shimmer PIN 1234 is tried automatically; the user is asked only if it is refused)
  3. test the connection (several attempts); preserve existing pairings on failure because
     an unreachable or busy radio does not prove that its pairing is stale
  4. set its clock to this PC's time (over USB)
Each Shimmer ends with one plain-language result line.
"""
import logging
import time
from dataclasses import dataclass, field

from .bt.base import PairingError
from .session import probe_device

log = logging.getLogger("shimmerlive.docksetup")

SHIMMER_PIN = "1234"
PAIR_ATTEMPTS = 4
PAIR_BACKOFF_S = 3.0      # 3, 6, 9 s: 0.11 firmware listens only part of the time (seen on 5042, 8341)
FIND_TIMEOUT_S = 20.0
TEST_ATTEMPTS = 3            # a single failed test is not proof of a stale pairing
READY, PROBLEM, SKIPPED = "ready", "problem", "skipped"


@dataclass
class SetupResult:
    slot: int
    mac: str = ""
    name: str = ""
    status: str = SKIPPED
    message: str = ""
    details: dict = field(default_factory=dict)
    seconds: float = 0.0


def auto_pin(ask_user):
    """PIN provider: answer the standard Shimmer PIN first, ask the user only if it was refused."""
    state = {"tries": 0}

    def provide(device_name):
        state["tries"] += 1
        return SHIMMER_PIN if state["tries"] == 1 else ask_user(device_name)
    return provide


def _pair(backend, dev, ask_user, progress):
    provide = auto_pin(ask_user)
    for attempt in range(1, PAIR_ATTEMPTS + 1):
        progress(f"pairing (attempt {attempt}/{PAIR_ATTEMPTS})…")
        try:
            backend.pair(dev, provide)
            return
        except PairingError as e:
            if not e.retryable or attempt == PAIR_ATTEMPTS:
                raise
            time.sleep(PAIR_BACKOFF_S * attempt)


def setup_slot(link, info, backend, cfg, ask_user, progress=lambda text: None) -> SetupResult:
    t0 = time.monotonic()
    res = SetupResult(info.slot, info.mac, info.name)

    def done(status, message, **details):
        res.status, res.message, res.seconds = status, message, time.monotonic() - t0
        res.details.update(details)
        log.info("slot %d %s: %s", info.slot, res.name or "-", message)
        return res

    if not info.occupied:
        return done(SKIPPED, "empty slot")
    if info.error or not info.mac:
        return done(PROBLEM, f"could not read this Shimmer over USB ({info.error or 'no answer'}). "
                             f"Take it out and push it back into the slot.")
    if not info.can_stream:
        return done(PROBLEM, f"runs {info.firmware} firmware, which cannot stream over Bluetooth. "
                             f"Install LogAndStream with Consensys (Firmware tab).")

    progress("looking for it over Bluetooth…")
    dev = backend.find([info.mac], FIND_TIMEOUT_S).get(info.mac)
    if dev is None:
        return done(PROBLEM, "not visible over Bluetooth. Check its switch is ON and that no other "
                             "computer or program is connected to it.")
    was_paired = dev.paired
    if not was_paired:
        try:
            _pair(backend, dev, ask_user, progress)
        except PairingError as e:
            return done(PROBLEM, f"pairing failed: {e}")

    progress("testing the connection…")
    result = probe_device(info.mac, dev.name or info.name, cfg, attempts=TEST_ATTEMPTS)
    if not result["ok"]:
        return done(PROBLEM, f"paired, but the connection test failed: {result.get('error')}. "
                             "Pairing kept; it is checked again automatically when it is switched on nearby (Ready).")
    progress("setting its clock…")
    clock_ok = True
    try:
        link.set_clock(info.slot)                            # over USB, after the Bluetooth test
    except Exception as e:                                   # noqa: BLE001 - not fatal for streaming
        clock_ok = False
        log.info("slot %d: clock not set: %s", info.slot, e)
    finally:
        link.disconnect()
    cfg.data.setdefault("selected", [])
    if info.mac not in cfg.data["selected"]:
        cfg.data["selected"].append(info.mac)
    cfg.save()
    batt = result.get("battery_pct")
    summary = (f"ready: {result['kind']}, {result['rate_hz']:g} Hz, {result['firmware']}"
               + (f", battery {batt:.0f}%" if batt is not None else "")
               + ("" if was_paired else " (newly paired)"))
    return done(READY, summary, newly_paired=not was_paired, repaired=False, clock_set=clock_ok, test=result)


def setup_all(link, infos, backend, cfg, ask_user, on_progress=lambda slot, text: None,
              on_result=lambda res: None) -> list:
    results = []
    for info in infos:
        if not info.occupied:
            continue
        res = setup_slot(link, info, backend, cfg, ask_user, lambda text, s=info.slot: on_progress(s, text))
        on_result(res)
        results.append(res)
    return results
