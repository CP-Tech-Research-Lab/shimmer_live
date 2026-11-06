import math

import pytest
from pyshimmer import EChannelType as E

import numpy as np

from shimmer_live.conversions import (CALIBRATION_DOC, adc_raw_to_mv, EXG_FULL_SCALE, battery_percent, build_specs, calibrate_array,
                                      device_kind, exg_saturated, exg_to_mv, gsr_raw_to_us, vbatt_raw_to_mv)


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
    assert by_key["EXG1_STATUS"].columns == ["EXG1_STATUS_raw"]
    # only ECG and EDA are shown and recorded by default
    assert by_key["GSR"].default_on and by_key["EXG1_CH1"].default_on
    assert not by_key["VBATT"].default_on and not by_key["EXG1_STATUS"].default_on


def test_device_kind():
    assert device_kind([E.VBATT, E.GSR_RAW]) == "GSR+"
    assert device_kind([E.EXG_ADS1292R_1_CH1_24BIT]) == "ECG/ExG"
    assert device_kind([E.VBATT]) == "Shimmer3"


def test_vectorised_calibration_matches_live_conversion():
    """Recordings in raw mode are calibrated later with calibrate_array: it must agree with the live path."""
    gsr = np.array([49855, 2048, (1 << 14) | 1500, (2 << 14) | 3000, (3 << 14) | 600])
    expected = [gsr_raw_to_us(int(r)) for r in gsr]
    np.testing.assert_allclose(calibrate_array({"formula": "gsr"}, gsr), expected, equal_nan=True)
    exg = np.array([0, 300_000, -(2 ** 23), EXG_FULL_SCALE])
    np.testing.assert_allclose(calibrate_array({"formula": "exg", "gain": 6}, exg), [exg_to_mv(6)(int(r)) for r in exg])
    np.testing.assert_allclose(calibrate_array({"formula": "vbatt"}, [2800]), [vbatt_raw_to_mv(2800)])
    assert calibrate_array({}, [5])[0] == 5
    assert set(CALIBRATION_DOC) == {"exg", "gsr", "vbatt", "adc", None}
    np.testing.assert_allclose(calibrate_array({"formula": "adc"}, [1704]), [adc_raw_to_mv(1704)])


def test_specs_carry_calibration_parameters():
    specs = {s.key: s for s in build_specs([E.GSR_RAW, E.EXG_ADS1292R_1_CH1_24BIT, E.EXG_ADS1292R_1_STATUS],
                                           {(0, 0): 12})}
    assert specs["EXG1_CH1"].calibration == {"formula": "exg", "gain": 12}
    assert specs["GSR"].calibration == {"formula": "gsr"}
    assert specs["EXG1_STATUS"].calibration == {}


def test_imu_channels_off_by_default():
    specs = build_specs([E.ACCEL_LN_X, E.GYRO_MPU9150_Y, E.MAG_LSM303DLHC_Z, E.GSR_RAW], {})
    assert {s.key: s.default_on for s in specs} == {"ACCEL_LN_X": False, "GYRO_MPU9150_Y": False,
                                                    "MAG_LSM303DLHC_Z": False, "GSR": True}


def test_same_numbers_as_consensys():
    """Values copied from a Consensys export of Shimmer3-A281 (GSR+ with PPG), 4 Nov 2026."""
    assert adc_raw_to_mv(1704) == pytest.approx(1248.3516483516482, abs=1e-9)          # PPG_A13_CAL, mV
    us = gsr_raw_to_us((2 << 14) | 2799)                                                # range 2, ADC 2799
    assert us == pytest.approx(3.1010989010989007, abs=1e-12)                          # GSR_Skin_Conductance_CAL
    assert 1000 / us == pytest.approx(322.46633593196316, abs=1e-9)                     # GSR_Skin_Resistance_CAL


def test_adc_channels_are_mv_and_named_like_consensys():
    gsr_plus = {s.key: s for s in build_specs([E.GSR_RAW, E.INTERNAL_ADC_13], {})}
    ppg = gsr_plus["INTERNAL_ADC_13"]
    assert (ppg.export_name, ppg.unit, ppg.label, ppg.calibration) == ("PPG_A13_CAL", "mV", "PPG (A13)",
                                                                       {"formula": "adc"})
    assert ppg.convert(4095) == pytest.approx(3000.0)
    other = {s.key: s for s in build_specs([E.INTERNAL_ADC_13, E.EXTERNAL_ADC_7], {})}
    assert other["INTERNAL_ADC_13"].export_name == "Int_ADC_A13_CAL"
    assert other["EXTERNAL_ADC_7"].export_name == "Ext_ADC_A7_CAL"
