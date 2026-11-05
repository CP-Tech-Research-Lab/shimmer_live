"""Labels (e.g. the participant wearing a Shimmer): shown next to the name, kept with recordings."""
import json
import os
import sys
import zipfile
from pathlib import Path

import pytest

from shimmer_live import export, labels, sdlog
from shimmer_live.bt.base import BtDevice
from shimmer_live.config import Config

sys.path.insert(0, str(Path(__file__).parent))
from test_sdlog import DATA, MAC  # noqa: E402


def test_label_text_is_tidied_and_formatted(tmp_path):
    cfg = Config(tmp_path / "c.json")
    assert labels.set_label(cfg, "AA", "  alberto   rossi ") == "alberto rossi"
    assert labels.get(cfg, "AA") == "alberto rossi" and labels.get(cfg, "") == ""
    assert labels.plain("Shimmer3-A281", "alberto") == "Shimmer3-A281 (alberto)"
    assert labels.plain("Shimmer3-A281", "") == "Shimmer3-A281"
    assert "(a&lt;b)" in labels.rich("S", "a<b")
    assert len(labels.set_label(cfg, "AA", "x" * 100)) == labels.MAX_LEN


def test_label_is_shown_and_edited_on_the_shimmers_page(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    widgets = pytest.importorskip("PySide6.QtWidgets")
    widgets.QApplication.instance() or widgets.QApplication([])
    from shimmer_live.ui.common import init_bridge
    from shimmer_live.ui import devices_page as dpm
    init_bridge()
    page = dpm.DevicesPage(Config(tmp_path / "c.json"), None, "test")
    page.paired = [BtDevice("40:84:32:56:A2:81", "Shimmer3-A281", True)]
    page.base.state = "none"
    page._render_all()
    row = next(i for i, r in enumerate(page.rows) if not r.header)
    assert not page.table.item(row, 1).data(dpm.LABEL_ROLE)
    monkeypatch.setattr(dpm.QInputDialog, "getText", lambda *a, **k: ("alberto", True))
    page._edit_label(row, 1)
    assert labels.get(page.cfg, "40:84:32:56:A2:81") == "alberto"
    assert page.table.item(row, 1).data(dpm.LABEL_ROLE) == "alberto"
    assert page.table.item(row, 1).text() == "Shimmer3-A281"          # the name itself is unchanged
    page._presence_timer.stop()


def test_label_goes_into_the_recording(tmp_path):
    cfg = Config(tmp_path / "c.json")
    labels.set_label(cfg, MAC, "alberto")
    d = sdlog.read_files([DATA])
    s = sdlog.new_session(cfg, "P_1", [(MAC, "Shimmer3-8395")], "raw", [])
    labels.set_label(cfg, MAC, "someone else")                        # changed later: the recording keeps alberto
    t0 = float(d.rtc_unix[0])
    s["devices"][MAC].update(state=sdlog.STOPPED, start={"start_offset": {"offset_ms": 0.0, "pc_unix": t0}})
    folder = sdlog.build_recording(s, {MAC: ("Shimmer3-8395", d, [DATA])}, tmp_path, "raw")
    z = zipfile.ZipFile(export.write_zip(export.analyze(folder), "x", "", tmp_path))
    assert json.loads(z.read("info.json"))["devices"][0]["label"] == "alberto"
    text = z.read("devices.csv").decode()
    assert "Label" in text and "alberto" in text and "Shimmer3-8395" in text
    sys.path.insert(0, str(Path(__file__).parents[1] / "examples"))
    import load_recording
    rec = load_recording.load_recording(z.filename)
    assert list(rec["device_list"]["Label"]) == ["alberto"] and "Shimmer3-8395" in rec["devices"]
