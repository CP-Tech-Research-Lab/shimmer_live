"""Channel descriptions and raw -> physical unit conversions for Shimmer3 streams."""
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

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
    export_name: str = ""       # Consensys-style column name, e.g. "ECG_LL-RA_24BIT_CAL"
    export_unit: str = "no_units"
    calibration: dict = field(default_factory=dict)   # {"formula": "exg"|"gsr"|"vbatt", ...} or {} if none

    @property
    def columns(self) -> list[str]:
        cols = [f"{self.key}_raw"]
        if self.convert is not None:
            cols.append(f"{self.key}_{self.unit}")
        return cols


# Shimmer3 ECG lead naming of the four ExG channels, as in Consensys exports:
# (label, chip, channel-on-chip, Consensys column name).
EXG_CHANNELS = {
    EChannelType.EXG_ADS1292R_1_CH1_24BIT: ("ECG LL-RA", 0, 0, "ECG_LL-RA_24BIT_CAL"),
    EChannelType.EXG_ADS1292R_1_CH2_24BIT: ("ECG LA-RA", 0, 1, "ECG_LA-RA_24BIT_CAL"),
    EChannelType.EXG_ADS1292R_2_CH1_24BIT: ("ECG RESP", 1, 0, "ECG_RESP_24BIT_CAL"),
    EChannelType.EXG_ADS1292R_2_CH2_24BIT: ("ECG Vx-RL", 1, 1, "ECG_Vx-RL_24BIT_CAL"),
}
STATUS_CHANNELS = {EChannelType.EXG_ADS1292R_1_STATUS, EChannelType.EXG_ADS1292R_2_STATUS}


def build_specs(chans, exg_gains: dict) -> list[ChannelSpec]:
    """exg_gains: {(chip, ch): gain} read from the ADS1292R registers."""
    specs = []
    for c in chans:
        if c == EChannelType.TIMESTAMP:
            continue
        if c == EChannelType.GSR_RAW:
            specs.append(ChannelSpec(c, "GSR", "GSR skin conductance", "uS", gsr_raw_to_us, kind="gsr",
                                     export_name="GSR_Skin_Conductance_CAL", export_unit="uS",
                                     calibration={"formula": "gsr"}))
        elif c == EChannelType.VBATT:
            specs.append(ChannelSpec(c, "VBATT", "Battery voltage", "mV", vbatt_raw_to_mv, plot=False, kind="vbatt",
                                     export_name="Battery_CAL", export_unit="mV", calibration={"formula": "vbatt"}))
        elif c in EXG_CHANNELS:
            label, chip, ch, export = EXG_CHANNELS[c]
            gain = exg_gains.get((chip, ch), 1)
            key = f"EXG{chip + 1}_CH{ch + 1}"
            specs.append(ChannelSpec(c, key, f"{label} (gain {gain})", "mV", exg_to_mv(gain), kind="exg",
                                     export_name=export, export_unit="mV",
                                     calibration={"formula": "exg", "gain": gain}))
        elif c in STATUS_CHANNELS:
            n = 1 if c == EChannelType.EXG_ADS1292R_1_STATUS else 2
            specs.append(ChannelSpec(c, c.name.replace("EXG_ADS1292R_", "EXG"), f"ECG/EMG status {n}", None, None,
                                     plot=False, export_name=f"ECG_EMG_Status{n}_CAL"))
        else:
            # Channels this app has no calibration for are still recorded and plotted raw.
            specs.append(ChannelSpec(c, c.name, f"{c.name} (raw)", None, None, export_name=f"{c.name}_RAW"))
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



# ---------------------------------------------------------------------------------------------
# How raw values become calibrated values. These are the nominal Shimmer3 formulas (as used by
# Consensys/pyshimmer for these sensors); the same text is written into every recording.
# ---------------------------------------------------------------------------------------------
CALIBRATION_DOC = {
    "exg": ("ECG/ExG (ADS1292R, 24-bit signed): mV = raw x (2420 / gain) / (2^23 - 1). "
            "2420 mV is the chip's reference voltage; the gain (1, 2, 3, 4, 6, 8 or 12) is read from the chip's "
            "registers when the device connects. raw = +/-8388607 means the channel is saturated (lead off)."),
    "gsr": ("GSR+ (16-bit word): bits 15-14 = range, selecting the feedback resistor Rf "
            "(0: 40.2 kOhm, 1: 287 kOhm, 2: 1 MOhm, 3: 3.3 MOhm); bits 11-0 = 12-bit ADC value. "
            "V = ADC x 3.0 / 4095; skin resistance (kOhm) = Rf / (V / 0.5 - 1); "
            "skin conductance (uS) = 1000 / resistance. V <= 0.5 V means no skin contact (written as empty)."),
    "vbatt": "Battery (12-bit ADC): mV = raw x 3000 / 4095 x 1.988 (on-board voltage divider).",
    None: "No calibration: the value is stored as sent by the device (e.g. ECG/EMG status bytes).",
}


def calibrate_array(calibration: dict, raw: np.ndarray) -> np.ndarray:
    """Vectorised raw -> calibrated conversion for a recorded column (see CALIBRATION_DOC)."""
    raw = np.asarray(raw, dtype=float)
    formula = (calibration or {}).get("formula")
    if formula == "exg":
        return raw * (EXG_VREF_MV / calibration["gain"] / EXG_FULL_SCALE)
    if formula == "vbatt":
        return raw * 3000.0 / 4095.0 * 1.988
    if formula == "gsr":
        r = np.nan_to_num(raw).astype(np.int64)
        rf = np.asarray(GSR_RF_KOHM)[(r >> 14) & 0x03]
        ratio = ((r & 0x0FFF) * 3.0 / 4095.0) / 0.5 - 1.0
        with np.errstate(divide="ignore", invalid="ignore"):
            out = np.where(ratio > 0, 1000.0 * ratio / rf, np.nan)
        return np.where(np.isfinite(raw), out, np.nan)
    return raw
