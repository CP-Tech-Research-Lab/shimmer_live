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
