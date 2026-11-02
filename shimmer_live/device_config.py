"""Read and change a Shimmer3's on-device configuration over Bluetooth.

What can be changed (LogAndStream firmware): sampling rate, enabled sensors, ECG/ExG
channel gains (ADS1292R registers), GSR range, device name, and the on-board clock.
Changes are written while the device is connected but not streaming, then read back
and verified.
"""
import logging
import time
from dataclasses import dataclass, field

from pyshimmer import ShimmerBluetooth
from pyshimmer.bluetooth.bt_commands import ResponseCommand, ShimmerCommand
from pyshimmer.dev.channels import ChDataTypeAssignment, ESensorGroup, SensorChannelAssignment
from pyshimmer.dev.exg import ExGRegister

from .session import timed
from .transport import open_link, quiesce

log = logging.getLogger("shimmerlive.config")

# The Shimmer samples at its 32768 Hz clock divided by a whole number, so any requested rate is
# rounded to 32768 / round(32768 / rate). The presets below are exact (power-of-two dividers etc.).
DEVICE_CLOCK_HZ = 32768
RATE_PRESETS = [1024.0, 512.0, 256.0, 204.8, 128.0, 102.4, 51.2, 32.0, 10.24]
RATE_MIN_HZ, RATE_MAX_HZ = 1.0, 2048.0


def achievable_rate(requested_hz: float) -> tuple[int, float]:
    """(divider, actual rate in Hz) the device will use for a requested rate (same rounding as pyshimmer)."""
    divider = max(1, min(65535, round(DEVICE_CLOCK_HZ / requested_hz)))
    return divider, DEVICE_CLOCK_HZ / divider

GAIN_CODES = {gain: code for code, gain in ExGRegister.GAIN_MAP.items()}           # gain -> bits 6:4
ADS_RATES = sorted((sps, code) for code, sps in ExGRegister.DATA_RATE_MAP.items() if sps > 0)

# Shimmer3 GSR+ ranges (protocol: SET 0x21 <range>, GET 0x23 -> response 0x22 <range>).
GSR_RANGES = {0: "8 kΩ – 63 kΩ", 1: "63 kΩ – 220 kΩ", 2: "220 kΩ – 680 kΩ", 3: "680 kΩ – 4.7 MΩ", 4: "Auto"}
SET_GSR_RANGE, GSR_RANGE_RESPONSE, GET_GSR_RANGE = 0x21, 0x22, 0x23

NAME_MAX_LEN = 12

# Bytes/s above which an RN42 link (115200 baud UART) starts dropping packets.
BT_WARN_BYTES_S, BT_LIMIT_BYTES_S = 8_000, 11_000

# Sensors present on every Shimmer3 main board.
BASE_SENSORS = [
    (ESensorGroup.ACCEL_LN, "Low-noise accelerometer"),
    (ESensorGroup.ACCEL_WR, "Wide-range accelerometer"),
    (ESensorGroup.GYRO, "Gyroscope"),
    (ESensorGroup.MAG, "Magnetometer"),
    (ESensorGroup.PRESSURE, "Pressure / temperature"),
    (ESensorGroup.BATTERY, "Battery voltage"),
]
# Sensors that need an expansion board (GSR+, ECG/EMG/ExG, Bridge Amplifier, PROTO3).
EXPANSION_SENSORS = [
    (ESensorGroup.GSR, "GSR (GSR+ board)"),
    (ESensorGroup.EXG1_24BIT, "ExG chip 1, 24-bit (ECG/EMG board)"),
    (ESensorGroup.EXG2_24BIT, "ExG chip 2, 24-bit (ECG/EMG board)"),
    (ESensorGroup.EXG1_16BIT, "ExG chip 1, 16-bit"),
    (ESensorGroup.EXG2_16BIT, "ExG chip 2, 16-bit"),
    (ESensorGroup.STRAIN, "Strain gauge (Bridge Amplifier+)"),
    (ESensorGroup.CH_A1, "Internal ADC A1"),
    (ESensorGroup.CH_A12, "Internal ADC A12"),
    (ESensorGroup.CH_A13, "Internal ADC A13"),
    (ESensorGroup.CH_A14, "Internal ADC A14"),
    (ESensorGroup.CH_A6, "External ADC A6"),
    (ESensorGroup.CH_A7, "External ADC A7"),
    (ESensorGroup.CH_A15, "External ADC A15"),
]
# Pairs that share hardware and cannot be enabled together.
CONFLICTS = [
    (ESensorGroup.GSR, ESensorGroup.CH_A1),
    (ESensorGroup.STRAIN, ESensorGroup.CH_A13),
    (ESensorGroup.STRAIN, ESensorGroup.CH_A14),
    (ESensorGroup.EXG1_24BIT, ESensorGroup.EXG1_16BIT),
    (ESensorGroup.EXG2_24BIT, ESensorGroup.EXG2_16BIT),
]
EXG_CHIP_SENSORS = {0: (ESensorGroup.EXG1_24BIT, ESensorGroup.EXG1_16BIT),
                    1: (ESensorGroup.EXG2_24BIT, ESensorGroup.EXG2_16BIT)}


class _GetGsrRange(ResponseCommand):
    def __init__(self):
        super().__init__(GSR_RANGE_RESPONSE)

    def send(self, ser):
        ser.write_command(GET_GSR_RANGE)

    def receive(self, ser):
        return ser.read_response(GSR_RANGE_RESPONSE, arg_format="<B")


class _SetGsrRange(ShimmerCommand):
    def __init__(self, rng: int):
        self._rng = rng

    def send(self, ser):
        ser.write_command(SET_GSR_RANGE, "<B", self._rng)


@dataclass
class DeviceSettings:
    name: str
    sampling_rate: float
    sensors: frozenset
    exg_regs: dict = field(default_factory=dict)      # chip -> 10 register bytes
    gsr_range: int | None = None

    def exg_gain(self, chip: int, ch: int) -> int:
        return ExGRegister(self.exg_regs[chip]).get_ch_gain(ch)


@dataclass
class DeviceInfo:
    mac: str
    firmware: str
    link: str
    battery_pct: float | None
    experiment_id: str
    settings: DeviceSettings


def enabled_sensors(chans) -> frozenset:
    """Sensor groups whose channels are all in the streamed channel list."""
    chans = set(chans)
    return frozenset(s for s, cs in SensorChannelAssignment.items() if cs and set(cs) <= chans)


def packet_bytes(sensors) -> int:
    """Size of one streamed data packet: packet type + 3-byte timestamp + channel data."""
    n = 1 + 3
    for s in sensors:
        n += sum(ChDataTypeAssignment[c].size for c in SensorChannelAssignment.get(s, []))
    return n


def bluetooth_load(sensors, rate) -> float:
    return packet_bytes(sensors) * rate


def exg_rate_code(sampling_rate: float) -> int:
    """Smallest ADS1292R data rate at or above the Shimmer sampling rate."""
    for sps, code in ADS_RATES:
        if sps >= sampling_rate:
            return code
    return ADS_RATES[-1][1]


def with_exg_changes(regs: bytes, sampling_rate: float | None = None, gains: dict | None = None) -> bytes:
    """Return ADS1292R register bytes with the data rate and/or channel gains replaced."""
    b = bytearray(regs)
    if sampling_rate is not None:
        b[0] = (b[0] & ~0x07) | exg_rate_code(sampling_rate)              # CONFIG1 bits 2:0
    for ch, gain in (gains or {}).items():
        off = 3 + ch                                                      # CH1SET / CH2SET
        b[off] = (b[off] & ~0x70) | (GAIN_CODES[gain] << 4)               # bits 6:4
    return bytes(b)


class DeviceConfigurator:
    """Holds a non-streaming connection to one Shimmer while its settings dialog is open."""

    def __init__(self, mac: str, cfg):
        self.mac, self.cfg = mac.upper(), cfg
        self._link = self._dev = None

    def connect(self, attempts: int = 3, progress=None):
        last = None
        for i in range(1, attempts + 1):
            if progress:
                progress(f"connecting (attempt {i}/{attempts})…")
            try:
                link = open_link(self.mac, self.cfg)
            except Exception as e:
                last = e
                time.sleep(2.0)
                continue
            try:
                quiesce(link)
                dev = ShimmerBluetooth(link)
                timed(dev.initialize, 10.0)
                self._link, self._dev = link, dev
                return
            except Exception as e:
                last = e
                link.cancel_read()
                link.close()
                time.sleep(2.0)
        raise ConnectionError(f"could not connect: {last}")

    def close(self):
        if self._dev is not None:
            try:
                timed(self._dev.shutdown, 3.0)
            except Exception:
                pass
        if self._link is not None:
            self._link.close()
        self._dev = self._link = None

    def _cmd(self, cmd, timeout=5.0):
        # pyshimmer has no public API for commands it does not wrap (GSR range).
        return timed(self._dev._process_and_wait, timeout, cmd)

    def read(self) -> DeviceInfo:
        d = self._dev
        fw_type, fw_ver = timed(d.get_firmware_version, 5.0)
        rate, _, chans = timed(d.get_inquiry, 5.0)
        sensors = enabled_sensors(chans)
        name = timed(d.get_device_name, 5.0)
        try:
            exp_id = timed(d.get_experiment_id, 5.0)
        except Exception:
            exp_id = ""
        try:
            batt = timed(d.get_battery_state, 5.0, True)
        except Exception:
            batt = None
        regs = {}
        for chip, group in EXG_CHIP_SENSORS.items():
            if sensors & set(group):
                regs[chip] = timed(d.get_exg_register, 5.0, chip).binary
        gsr = None
        if ESensorGroup.GSR in sensors:
            try:
                gsr = self._cmd(_GetGsrRange(), 3.0)
            except Exception as e:
                log.info("GSR range not readable: %s", e)
        settings = DeviceSettings(name=name, sampling_rate=rate, sensors=sensors, exg_regs=regs, gsr_range=gsr)
        return DeviceInfo(mac=self.mac, firmware=f"{fw_type.name} {fw_ver.major}.{fw_ver.minor}.{fw_ver.rel}",
                          link=getattr(self._link, "description", ""), battery_pct=batt,
                          experiment_id=exp_id, settings=settings)

    def apply(self, old: DeviceSettings, new: DeviceSettings) -> list[str]:
        """Write the differences, read back, and return a list of mismatches (empty = verified)."""
        d = self._dev
        if new.sensors != old.sensors:
            log.info("%s: sensors -> %s", self.mac, sorted(s.name for s in new.sensors))
            timed(d.set_sensors, 5.0, list(new.sensors))
        if new.sampling_rate != old.sampling_rate:
            log.info("%s: sampling rate -> %s Hz", self.mac, new.sampling_rate)
            timed(d.set_sampling_rate, 5.0, new.sampling_rate)
        for chip, regs in new.exg_regs.items():
            if regs != old.exg_regs.get(chip):
                log.info("%s: ExG chip %d registers -> %s", self.mac, chip + 1, regs.hex())
                timed(d.set_exg_register, 5.0, chip, 0, regs)
        if new.gsr_range is not None and new.gsr_range != old.gsr_range:
            log.info("%s: GSR range -> %s", self.mac, GSR_RANGES[new.gsr_range])
            self._cmd(_SetGsrRange(new.gsr_range))
        if new.name != old.name:
            log.info("%s: name -> %s", self.mac, new.name)
            timed(d.set_device_name, 5.0, new.name)

        got = self.read().settings
        problems = []
        if got.sensors != new.sensors:
            problems.append("enabled sensors")
        if abs(got.sampling_rate - new.sampling_rate) > 0.01:
            problems.append(f"sampling rate (device reports {got.sampling_rate:g} Hz)")
        for chip, regs in new.exg_regs.items():
            if got.exg_regs.get(chip, regs) != regs:
                problems.append(f"ExG chip {chip + 1} registers")
        if new.gsr_range is not None and got.gsr_range is not None and got.gsr_range != new.gsr_range:
            problems.append("GSR range")
        if got.name != new.name:
            problems.append("device name")
        return problems

    def set_clock_to_pc(self):
        """Set the Shimmer's real-time clock (clears the 'RTC not set' LED pattern)."""
        timed(self._dev.set_rtc, 5.0, time.time())

