"""examples/load_recording.py reads the ZIPs the app writes."""
import importlib.util
from pathlib import Path

from shimmer_live import export
from shimmer_live.recorder import Recorder

from .conftest import ECG_CHANNELS, ECG_GAINS, GSR_CHANNELS, make_session
from .test_recording import ALL_ECG, ALL_GSR, REST, FakeBinding, stream

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("load_recording", ROOT / "examples" / "load_recording.py")
loader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(loader)


def make_zip(tmp_path, clock):
    gsr = make_session(tmp_path, GSR_CHANNELS, 32.0, name="Shimmer3-401F", mac="00:00:00:00:00:01")
    ecg = make_session(tmp_path, ECG_CHANNELS, 256.0, ECG_GAINS, name="Shimmer3-8395", mac="00:00:00:00:00:02")
    b = FakeBinding(ecg)
    rec = Recorder(tmp_path / "rec", [gsr, ecg], {gsr.mac: ALL_GSR, ecg.mac: ALL_ECG}, {b: {"hr", "rr"}}, [REST])
    vals = {"Shimmer3-401F": {"GSR_RAW": 2048}, "Shimmer3-8395": {"EXG_ADS1292R_1_CH1_24BIT": 300_000}}
    stream([gsr, ecg], clock, 2.0, {}, vals)
    rec.keypoint(REST)
    stream([gsr, ecg], clock, 1.0, {}, vals)
    rec.put_plugin(b, "hr", 2.5, 70.0)
    rec.put_plugin(b, "rr", 2.5, 857.0)
    return export.write_zip(export.analyze(rec.stop()), "demo", "all fine")


def test_loader_reads_zip(tmp_path, clock):
    rec = loader.load_recording(make_zip(tmp_path, clock))
    assert set(rec["devices"]) == {"Shimmer3-401F", "Shimmer3-8395"}
    gsr = rec["devices"]["Shimmer3-401F"]
    assert list(gsr.columns[:3]) == ["Shimmer3-401F_Timestamp_Unix_CAL", "Shimmer3-401F_Timestamp_Shared_CAL",
                                     "Shimmer3-401F_Timestamp_CAL"]
    assert len(gsr) == 96 and gsr["Shimmer3-401F_GSR_Skin_Conductance_RAW"].iloc[0] == 2048    # raw by default
    assert (gsr["Shimmer3-401F_Keypoint"] == "rest start").sum() == 1
    assert rec["units"]["Shimmer3-8395.csv"][:3] == ["ms", "s", "ms"]
    hrv = rec["plugins"]["HRV - Shimmer3-8395"]
    assert hrv["Shimmer3-8395_hrv_hr"].tolist() == [70.0]
    assert rec["notes"] == "all fine" and rec["info"]["recording_name"] == "demo"
    assert list(rec["keypoints"]["Keypoint"]) == ["rest start"]
    assert {"device", "check", "status"} <= set(rec["qc"].columns)


def test_loader_command_line(tmp_path, clock, capsys):
    path = make_zip(tmp_path, clock)
    loader.main([str(path), "--extract"])
    out = capsys.readouterr().out
    assert "Shimmer3-8395" in out and "keypoints: rest start" in out and "Notes: all fine" in out
    assert (path.with_suffix("") / "Shimmer3-401F.csv").exists()


def test_loader_reads_an_sd_recording(tmp_path):
    """SD recordings carry the Shimmers' binary files in sd_raw/: the loader must skip them."""
    from tests.test_sdlog import DATA, MAC
    from shimmer_live import export, sdlog
    from shimmer_live.config import Config
    d = sdlog.read_files([DATA])
    s = sdlog.new_session(Config(tmp_path / "c.json"), "P_12", [(MAC, "Shimmer3-8395")], "raw", [])
    s["devices"][MAC]["state"] = sdlog.STOPPED
    z = export.write_zip(export.analyze(sdlog.build_recording(s, {MAC: ("Shimmer3-8395", d, [DATA])}, tmp_path,
                                                              "raw")), "sd", "", tmp_path)
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent / "examples"))
    from load_recording import load_recording
    rec = load_recording(z)
    assert len(rec["devices"]["Shimmer3-8395"]) == 7442
