"""The before/after firmware check flags what changed and tolerates person-dependent signals."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
import fw_check  # noqa: E402


def report(**over):
    r = {"settings": {"firmware": "LogAndStream 0.15.0", "rate_hz": 50.3, "channels": ["GSR_RAW", "INTERNAL_ADC_13"],
                      "exg_gains": {}, "expansion_power_3v": True, "device_name": "Shimmer_F66C"},
         "stream": {"completeness_pct": 100.0, "measured_rate_hz": 50.33},
         "signals": {"GSR_RAW": {"label": "GSR", "unit": "uS", "mean": 2.0, "std": 0.2, "no_contact_pct": 0.0},
                     "INTERNAL_ADC_13": {"label": "A13", "unit": "mV", "mean": 900.0, "std": 5.0}}}
    for k, v in over.items():
        r[k].update(v)
    return r


def run(tmp_path, a, b):
    pa, pb = tmp_path / "a.json", tmp_path / "b.json"
    pa.write_text(json.dumps(a))
    pb.write_text(json.dumps(b))
    return fw_check.compare(pa, pb)


def test_new_firmware_same_behaviour_passes(tmp_path):
    b = report(settings={"firmware": "LogAndStream 1.1.9"})
    b["signals"]["GSR_RAW"]["mean"] = 2.6                 # fingers differ a bit: still fine
    assert run(tmp_path, report(), b)


def test_lost_settings_or_samples_fail(tmp_path):
    assert not run(tmp_path, report(), report(settings={"expansion_power_3v": False}))
    assert not run(tmp_path, report(), report(stream={"completeness_pct": 95.0}))
    assert not run(tmp_path, report(), report(settings={"channels": ["GSR_RAW"]}))
