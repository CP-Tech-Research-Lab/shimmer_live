"""Channel descriptions and raw -> physical unit conversions for Shimmer3 streams."""
from dataclasses import dataclass
from typing import Callable, Optional

from pyshimmer import EChannelType
from pyshimmer.util import battery_voltage_to_percent

# Shimmer3 GSR+ feedback resistors (kOhm) selected by the 2 range bits.
GSR_RF_KOHM = (40.2, 287.0, 1000.0, 3300.0)
EXG_FULL_SCALE = 2 ** 23 - 1
EXG_VREF_MV = 2420.0
GSR_NO_CONTACT_US = 0.05        # below this the electrodes are almost certainly not on skin


def gsr_raw_to_us(raw: int) -> float:
    """GSR+ raw word -> skin conductance (uS). Shimmer3 GSR+ formula, 0.5 V reference."""
    rf = GSR_RF_KOHM[(raw >> 14) & 0x03]
    ratio = ((raw & 0x0FFF) * 3.0 / 4095.0) / 0.5 - 1.0
    if ratio <= 0:
        return float("nan")
    return 1000.0 * ratio / rf


def vbatt_raw_to_mv(raw: int) -> float:
    return raw * 3000.0 / 4095.0 * 1.988


def battery_percent(mv: float) -> float:
    return float(battery_voltage_to_percent(mv / 1000.0))


def exg_to_mv(gain: int) -> Callable[[int], float]:
    k = EXG_VREF_MV / gain / EXG_FULL_SCALE
    return lambda raw: raw * k


def exg_saturated(raw: int) -> bool:
    return raw >= EXG_FULL_SCALE or raw <= -EXG_FULL_SCALE


@dataclass
class ChannelSpec:
    chan: EChannelType
    key: str                    # CSV column stem, e.g. "GSR" -> GSR_raw, GSR_uS
    label: str                  # human label for plots
    unit: Optional[str]         # physical unit, None when only raw is known
    convert: Optional[Callable[[int], float]]
    plot: bool = True
    kind: str = "other"         # gsr | exg | vbatt | other

    @property
    def columns(self) -> list[str]:
        cols = [f"{self.key}_raw"]
        if self.convert is not None:
            cols.append(f"{self.key}_{self.unit}")
        return cols


# Standard Shimmer3 ECG lead naming of the four ExG channels: (label, chip, channel-on-chip).
EXG_CHANNELS = {
    EChannelType.EXG_ADS1292R_1_CH1_24BIT: ("ECG LL-RA", 0, 0),
    EChannelType.EXG_ADS1292R_1_CH2_24BIT: ("ECG LA-RA", 0, 1),
    EChannelType.EXG_ADS1292R_2_CH1_24BIT: ("ExG2 CH1", 1, 0),
    EChannelType.EXG_ADS1292R_2_CH2_24BIT: ("ECG Vx-RL", 1, 1),
}
STATUS_CHANNELS = {EChannelType.EXG_ADS1292R_1_STATUS, EChannelType.EXG_ADS1292R_2_STATUS}


def build_specs(chans, exg_gains: dict) -> list[ChannelSpec]:
    """exg_gains: {(chip, ch): gain} read from the ADS1292R registers."""
    specs = []
    for c in chans:
        if c == EChannelType.TIMESTAMP:
            continue
        if c == EChannelType.GSR_RAW:
            specs.append(ChannelSpec(c, "GSR", "GSR skin conductance", "uS", gsr_raw_to_us, kind="gsr"))
        elif c == EChannelType.VBATT:
            specs.append(ChannelSpec(c, "VBATT", "Battery voltage", "mV", vbatt_raw_to_mv, plot=False, kind="vbatt"))
        elif c in EXG_CHANNELS:
            label, chip, ch = EXG_CHANNELS[c]
            gain = exg_gains.get((chip, ch), 1)
            key = f"EXG{chip + 1}_CH{ch + 1}"
            specs.append(ChannelSpec(c, key, f"{label} (gain {gain})", "mV", exg_to_mv(gain), kind="exg"))
        elif c in STATUS_CHANNELS:
            specs.append(ChannelSpec(c, c.name.replace("EXG_ADS1292R_", "EXG"), c.name, None, None, plot=False))
        else:
            # Channels this app has no calibration for are still recorded and plotted raw.
            specs.append(ChannelSpec(c, c.name, f"{c.name} (raw)", None, None))
    return specs


def device_kind(chans) -> str:
    names = {c.name for c in chans}
    kinds = []
    if "GSR_RAW" in names:
        kinds.append("GSR+")
    if any(n.startswith("EXG") for n in names):
        kinds.append("ECG/ExG")
    if any(n.startswith(("ACCEL", "GYRO", "MAG")) for n in names):
        kinds.append("IMU")
    return " + ".join(kinds) or "Shimmer3"

