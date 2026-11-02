import math

import pytest
from pyshimmer import EChannelType as E

from shimmer_live.conversions import (EXG_FULL_SCALE, battery_percent, build_specs, device_kind, exg_saturated,
                                      exg_to_mv, gsr_raw_to_us, vbatt_raw_to_mv)


def test_gsr_high_range_matches_device_reading():
    # 49855 = range 3 (3300 kOhm), ADC 703: the open-circuit value 401F streamed in testing.
    assert 49855 >> 14 == 3
    expected = 1000 * ((703 * 3.0 / 4095) / 0.5 - 1) / 3300
    assert gsr_raw_to_us(49855) == pytest.approx(expected)
    assert gsr_raw_to_us(49855) == pytest.approx(0.009102, rel=1e-3)


def test_gsr_low_range():
    # range 0 (40.2 kOhm), ADC 2048 -> ~1.5 V -> ratio ~2 -> ~49.8 uS
    assert gsr_raw_to_us(2048) == pytest.approx(1000 * ((2048 * 3.0 / 4095) / 0.5 - 1) / 40.2)


def test_gsr_below_reference_is_nan():
    assert math.isnan(gsr_raw_to_us((3 << 14) | 600))     # 0.44 V < 0.5 V reference


def test_exg_full_scale_is_vref_over_gain():
    for gain in (1, 4, 12):
        assert exg_to_mv(gain)(EXG_FULL_SCALE) == pytest.approx(2420.0 / gain)
        assert exg_to_mv(gain)(-EXG_FULL_SCALE) == pytest.approx(-2420.0 / gain)


def test_exg_saturation_both_rails():
    assert exg_saturated(EXG_FULL_SCALE)
    assert exg_saturated(-(EXG_FULL_SCALE + 1))         # -8388608, the value a lead-off channel reads
    assert not exg_saturated(1000)


def test_battery():
    assert vbatt_raw_to_mv(2800) == pytest.approx(2800 * 3000 / 4095 * 1.988)
    assert battery_percent(4200) == 100
    assert battery_percent(3200) == 0
    assert 0 < battery_percent(3800) < 100


def test_build_specs_columns_and_kinds():
    specs = build_specs([E.TIMESTAMP, E.VBATT, E.GSR_RAW, E.EXG_ADS1292R_1_STATUS, E.EXG_ADS1292R_1_CH1_24BIT],
                        {(0, 0): 4})
    by_key = {s.key: s for s in specs}
    assert "TIMESTAMP" not in by_key
    assert by_key["GSR"].columns == ["GSR_raw", "GSR_uS"]
    assert by_key["EXG1_CH1"].columns == ["EXG1_CH1_raw", "EXG1_CH1_mV"]
    assert by_key["EXG1_STATUS"].columns == ["EXG1_STATUS_raw"] and not by_key["EXG1_STATUS"].plot
    assert not by_key["VBATT"].plot


def test_device_kind():
    assert device_kind([E.VBATT, E.GSR_RAW]) == "GSR+"
    assert device_kind([E.EXG_ADS1292R_1_CH1_24BIT]) == "ECG/ExG"
    assert device_kind([E.VBATT]) == "Shimmer3"
