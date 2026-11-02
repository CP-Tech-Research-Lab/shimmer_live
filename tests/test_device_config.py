from pyshimmer import EChannelType as E
from pyshimmer.dev.channels import ESensorGroup as S
from pyshimmer.dev.exg import ExGRegister

from shimmer_live.device_config import achievable_rate, bluetooth_load, enabled_sensors, exg_rate_code, packet_bytes, with_exg_changes

from .conftest import ECG_CHANNELS, GSR_CHANNELS

# Typical Shimmer3 ECG configuration: 500 SPS, gain 4 on both channels.
ECG_REGS = bytes([0x02, 0xA0, 0x10, 0x40, 0x40, 0x2D, 0x00, 0x00, 0x02, 0x03])


def test_enabled_sensors_from_streamed_channels():
    assert enabled_sensors([E.TIMESTAMP] + GSR_CHANNELS) == {S.BATTERY, S.GSR}
    assert enabled_sensors([E.TIMESTAMP] + ECG_CHANNELS) == {S.BATTERY, S.EXG1_24BIT, S.EXG2_24BIT}


def test_packet_size_and_load():
    ecg = {S.BATTERY, S.EXG1_24BIT, S.EXG2_24BIT}
    assert packet_bytes(ecg) == 20                      # 1 type + 3 ts + 2 vbatt + 2 x (1 status + 2 x 3)
    assert packet_bytes({S.BATTERY, S.GSR}) == 8
    assert bluetooth_load(ecg, 512) == 20 * 512


def test_exg_data_rate_is_smallest_at_or_above_sampling_rate():
    rate = ExGRegister.DATA_RATE_MAP
    assert rate[exg_rate_code(125)] == 125
    assert rate[exg_rate_code(256)] == 500
    assert rate[exg_rate_code(512)] == 1000
    assert rate[exg_rate_code(1024)] == 2000
    assert rate[exg_rate_code(100_000)] == 8000


def test_exg_register_edit_touches_only_rate_and_gain_bits():
    new = with_exg_changes(ECG_REGS, sampling_rate=1024, gains={0: 12, 1: 1})
    r = ExGRegister(new)
    assert (r.data_rate, r.ch1_gain, r.ch2_gain) == (2000, 12, 1)
    assert new[0] & ~0x07 == ECG_REGS[0] & ~0x07          # rest of CONFIG1 unchanged
    assert new[3] & ~0x70 == ECG_REGS[3] & ~0x70          # mux / power-down bits unchanged
    assert new[4] & ~0x70 == ECG_REGS[4] & ~0x70
    assert new[1:3] == ECG_REGS[1:3] and new[5:] == ECG_REGS[5:]


def test_exg_register_untouched_without_changes():
    assert with_exg_changes(ECG_REGS) == ECG_REGS
    assert with_exg_changes(ECG_REGS, gains={0: 4, 1: 4}) == ECG_REGS


def test_custom_rates_round_like_the_device():
    from pyshimmer.dev.base import dr2sr, sr2dr
    for requested in (1.0, 10.0, 32.0, 100.0, 256.0, 300.0, 333.3, 512.0, 1000.0, 2048.0):
        divider, actual = achievable_rate(requested)
        assert divider == sr2dr(requested) and actual == dr2sr(divider)       # same rounding as pyshimmer
    assert achievable_rate(300.0) == (109, 32768 / 109)
    assert achievable_rate(256.0) == (128, 256.0)
