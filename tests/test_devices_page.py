"""The Shimmers list never states something it does not know yet (e.g. 'not docked' before the base is read)."""
import os

import pytest

from shimmer_live.base import SlotInfo
from shimmer_live.bt.base import BtDevice
from shimmer_live.config import Config


@pytest.fixture
def page(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    widgets = pytest.importorskip("PySide6.QtWidgets")
    from shimmer_live.ui.common import init_bridge
    from shimmer_live.ui.devices_page import DevicesPage
    _app = widgets.QApplication.instance() or widgets.QApplication([])
    init_bridge()
    p = DevicesPage(Config(tmp_path / "cfg.json"), None, "no Bluetooth in test")
    yield p
    p.deleteLater()


def cells(page):
    t = page.table
    return {t.item(r, 1).text(): (t.item(r, 2).text(), t.item(r, 3).text(), t.item(r, 4).text())
            for r in range(t.rowCount())}


def test_empty_while_looking_then_explains(page):
    assert page.base.state == "checking"
    page._render_all()
    assert page.empty.isVisibleTo(page) and "Looking for Shimmers" in page.empty.text()
    page.base.state = "none"
    page._render_all()
    assert "No Shimmers yet" in page.empty.text() and not page.table.isVisibleTo(page)


def test_where_is_unknown_until_the_base_has_been_read(page):
    page.paired = [BtDevice("D8:47:8F:04:BB:C9", "Shimmer3-BBC9", True)]
    page._render_all()
    assert cells(page)["Shimmer3-BBC9"][0] == "checking…"          # base not looked at yet
    page.base.state, page.base.reading = "base", True
    page.base.slots = [SlotInfo(1, True)]                           # slot 1 occupied, not read yet
    page._render_all()
    c = cells(page)
    assert c["Shimmer3-BBC9"][0] == "checking…"                     # it might still be the one in slot 1
    assert c["…"] == ("base slot 1", "not paired", "reading it over USB…")
    page.base.slots = [SlotInfo(1, True, "00:06:66:B9:86:84", "LogAndStream 0.11.0", True)]
    page.base.reading = False
    page._render_all()
    c = cells(page)
    assert c["Shimmer3-BBC9"][0] == "not docked"                    # now it is known
    assert c["Shimmer3-8684"][:2] == ("base slot 1", "not paired")
    assert "Set up docked Shimmers" in c["Shimmer3-8684"][2]


def test_docked_and_paired_shimmer_appears_once(page):
    page.paired = [BtDevice("00:06:66:B9:86:84", "Shimmer3-8684", True)]
    page.base.state = "base"
    page.base.slots = [SlotInfo(1, True, "00:06:66:B9:86:84", "LogAndStream 0.11.0", True),
                       SlotInfo(2, True, "60:8A:10:40:FD:15", "SDLog 0.22.0", False)]
    page._render_all()
    c = cells(page)
    assert list(c) == ["Shimmer3-8684", "Shimmer3-FD15"]
    assert c["Shimmer3-8684"][:2] == ("base slot 1", "paired")
    assert "cannot stream" in c["Shimmer3-FD15"][2]


def test_setup_runs_only_on_ticked_docked_shimmers(page, monkeypatch):
    from PySide6.QtCore import Qt
    page.backend = object()
    page.base.state, page.base.link = "base", object()
    page.base.slots = [SlotInfo(1, True, "00:06:66:B9:86:84", "LogAndStream 0.11.0", True),
                       SlotInfo(2, True, "00:06:66:B8:D1:A4", "LogAndStream 0.16.0", True),
                       SlotInfo(3, True, "60:8A:10:40:FD:15", "SDLog 0.22.0", False)]
    page._render_all()
    t = page.table
    assert not page.btn_setup.isEnabled()                            # nothing ticked yet
    assert t.item(0, 0).flags() & Qt.ItemIsUserCheckable              # unpaired but docked: tickable
    assert not t.item(2, 0).flags() & Qt.ItemIsUserCheckable          # SDLog: cannot stream, not tickable
    started = []
    monkeypatch.setattr(page.base, "setup_all", lambda slots=None: started.append(slots))
    t.item(1, 0).setCheckState(Qt.Checked)                           # tick D1A4 only
    assert page.btn_setup.isEnabled() and page.btn_setup.text().endswith("(1)")
    assert page.checked_devices() == []                              # not paired: not offered to live view
    page.btn_setup.click()
    assert started == [[2]]


def test_refresh_starts_over_from_checking(page, monkeypatch):
    page.cfg.data["devices"] = {"00:06:66:B9:86:84": {"name": "Shimmer3-8684"}}   # no Bluetooth in tests:
    page.paired = [BtDevice("00:06:66:B9:86:84", "Shimmer3-8684", True)]           # paired list comes from here
    page.base.state = "base"
    page.base.slots = [SlotInfo(1, True, "00:06:66:B9:86:84", "LogAndStream 0.11.0", True)]
    page.base.occupancy = [True]
    page._render_all()
    assert cells(page)["Shimmer3-8684"][0] == "base slot 1"
    polled = []
    monkeypatch.setattr(page.base, "_poll", lambda: polled.append(1))
    page.btn_refresh.click()
    assert polled and page.base.state == "checking" and page.base.occupancy is None
    assert cells(page)["Shimmer3-8684"][0] == "checking…"
    assert "looking for a Shimmer base" in page.pill_base.text()


def test_shimmers_recording_on_sd_are_flagged_and_locked(page):
    from PySide6.QtCore import Qt
    from shimmer_live import sdlog
    mac = "00:06:66:B9:83:95"
    page.cfg.data["selected"] = [mac]
    page.paired = [BtDevice(mac, "Shimmer3-8395", True)]
    sdlog.new_session(page.cfg, "P_12", [(mac, "Shimmer3-8395")], "raw", [])
    page.cfg.data[sdlog.SESSION_KEY]["devices"][mac]["state"] = sdlog.RECORDING
    page.base.state = "none"
    page._render_all()
    assert "recording on its SD card" in cells(page)["Shimmer3-8395"][2]
    assert not page.table.item(0, 0).flags() & Qt.ItemIsUserCheckable     # not for live view meanwhile
    assert page.checked_devices() == [] and "in progress" in page.btn_sd.text()
