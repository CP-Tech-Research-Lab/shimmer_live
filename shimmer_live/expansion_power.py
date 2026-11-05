"""Shimmer3 expansion supply commands and explicit PPG power preparation."""
import logging

from pyshimmer import EChannelType
from pyshimmer.bluetooth.bt_commands import ResponseCommand, ShimmerCommand

SET_EXP_POWER, EXP_POWER_RESPONSE, GET_EXP_POWER = 0x5E, 0x5F, 0x60
log = logging.getLogger("shimmerlive.power")


class GetExpansionPower(ResponseCommand):
    def __init__(self):
        super().__init__(EXP_POWER_RESPONSE)

    def send(self, ser):
        ser.write_command(GET_EXP_POWER)

    def receive(self, ser):
        value = ser.read_response(EXP_POWER_RESPONSE, arg_format="<B")
        if value not in (0, 1):
            raise ValueError(f"Invalid expansion power response: {value}")
        return bool(value)


class SetExpansionPower(ShimmerCommand):
    def __init__(self, enabled):
        self.enabled = bool(enabled)

    def send(self, ser):
        ser.write_command(SET_EXP_POWER, "<B", int(self.enabled))


def ensure_ppg_power(dev, timed):
    """Make sure the Shimmer's 3 V expansion power setting is ON; write it only if it is OFF.

    The firmware itself switches the probe on when streaming/SD logging starts and off when it
    stops, as long as this setting is ON: the probe LED lights only during acquisition. Nothing
    is written when the setting is already ON (every write is a configuration save on the Shimmer).
    Verified on Shimmer3 GSR+ (SR48-4-0, LogAndStream 1.1.9), out of the base.
    Returns True if it had to be written."""
    try:
        current = timed(dev._process_and_wait, 5.0, GetExpansionPower())
    except Exception as e:                                    # noqa: BLE001 - unknown: write ON and verify
        log.info("PPG power setting not readable (%s): writing ON", e)
        current = None
    if current is True:
        return False
    timed(dev._process_and_wait, 5.0, SetExpansionPower(True))
    if timed(dev._process_and_wait, 5.0, GetExpansionPower()) is not True:
        raise RuntimeError("Could not verify PPG 3 V power ON")
    log.info("PPG 3 V expansion power setting switched ON")
    return True


def prepare_ppg(dev, cfg, mac, channels, timed) -> dict:
    """Before streaming/SD logging with a PPG probe: A13 must be on and the power setting ON.

    Returns {"docked": True} when the Shimmer is in a base or dock: there the probe loses power a
    second after acquisition starts (LED off, A13 reads 0), so PPG only works out of the base."""
    if not cfg.data.get("ppg_inputs", {}).get(mac.upper()):
        return {}
    if EChannelType.INTERNAL_ADC_13 not in channels:
        raise RuntimeError("PPG is selected but A13 is disabled; enable PPG in device Settings")
    ensure_ppg_power(dev, timed)
    docked = False
    try:
        docked = bool(timed(dev.get_status, 5.0)[0])
    except Exception as e:                                    # noqa: BLE001 - informative only
        log.info("%s: status not readable: %s", mac, e)
    if docked:
        log.warning("%s: PPG does not work while the Shimmer is in the base: take it out", mac)
    return {"docked": docked}
