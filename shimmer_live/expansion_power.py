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


def rewrite_ppg_power(dev, timed):
    """Rewrite OFF then ON while idle; never cut power during existing acquisition.

    Readback verifies commands, but never decides whether to skip a write.
    If the OFF step fails, still attempt to restore and verify ON.
    """
    status = timed(dev.get_status, 5.0)
    acquiring = status[1] or status[3] or status[4]

    def write_and_verify(enabled):
        timed(dev._process_and_wait, 5.0, SetExpansionPower(enabled))
        if timed(dev._process_and_wait, 5.0, GetExpansionPower()) is not enabled:
            raise RuntimeError(f"Could not verify PPG 3 V power {'ON' if enabled else 'OFF'}")

    try:
        if not acquiring:
            write_and_verify(False)
    finally:
        write_and_verify(True)
    log.info("PPG power rewritten and verified: %s", "ON (acquisition active)" if acquiring else "OFF then ON")


def prepare_ppg(dev, cfg, mac, channels, timed):
    if cfg.data.get("ppg_inputs", {}).get(mac.upper()):
        if EChannelType.INTERNAL_ADC_13 not in channels:
            raise RuntimeError("PPG is selected but A13 is disabled; enable PPG in device Settings")
        rewrite_ppg_power(dev, timed)
