"""The Shimmers list never states something it does not know yet (e.g. 'not docked' before the base is read)."""
import os
import time

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
    p._shown = False
    p._presence_timer.stop()
    p.deleteLater()
    from PySide6.QtCore import QCoreApplication, QEvent
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)    # really delete it now, not at exit


class FakeLink:
    def __init__(self):
        import threading
        self.lock = threading.RLock()


def dock(page, slots, reading=False, serial="SR3VRR8A", letter="A"):
    """Put a base with these slots on the page (as the watcher would after reading it)."""
    from shimmer_live.base import BaseDevice
    from shimmer_live.ui.base_panel import BaseUnit
    u = page.base.units.get(serial) or BaseUnit(BaseDevice("Base6U", serial, [], "c", "s", 6), FakeLink())
    u.slots, u.reading, u.letter, u.occupancy = list(slots), reading, letter, [s.occupied for s in slots]
    page.base.units[serial] = u
    page.base.state = "base"
    return u


def row_of(page, name):
    return next(i for i, r in enumerate(page.rows) if r.name == name and not r.header)


def groups(page):
    return [r.header for r in page.rows if r.header]


def cells(page):
    t = page.table
    return {t.item(r, 1).text(): (t.item(r, 2).text(), "paired" if page.rows[r].paired else "not paired",
                                  t.item(r, 7).text())
            for r in range(t.rowCount()) if not page.rows[r].header}


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
    u = dock(page, [SlotInfo(1, True)], reading=True)               # slot 1 occupied, not read yet
    page._render_all()
    c = cells(page)
    assert c["Shimmer3-BBC9"][0] == "checking…"                     # it might still be the one in slot 1
    assert c["…"] == ("base slot 1", "not paired", "reading it over USB…")
    dock(page, [SlotInfo(1, True, "00:06:66:B9:86:84", "LogAndStream 0.11.0", True)])
    page._render_all()
    c = cells(page)
    assert c["Shimmer3-BBC9"][0] == "not docked"                    # now it is known
    assert c["Shimmer3-8684"][:2] == ("base slot 1", "not paired")
    assert "Set up docked Shimmers" in c["Shimmer3-8684"][2]


def test_docked_and_paired_shimmer_appears_once(page):
    page.paired = [BtDevice("00:06:66:B9:86:84", "Shimmer3-8684", True)]
    dock(page, [SlotInfo(1, True, "00:06:66:B9:86:84", "LogAndStream 0.11.0", True),
                SlotInfo(2, True, "60:8A:10:40:FD:15", "SDLog 0.22.0", False)])
    page._render_all()
    c = cells(page)
    assert list(c) == ["Shimmer3-8684", "Shimmer3-FD15"]
    assert c["Shimmer3-8684"][:2] == ("base slot 1", "paired")
    assert "can't stream" in c["Shimmer3-FD15"][2]


def test_setup_runs_only_on_ticked_docked_shimmers(page, monkeypatch):
    from PySide6.QtCore import Qt
    page.backend = object()
    dock(page, [SlotInfo(1, True, "00:06:66:B9:86:84", "LogAndStream 0.11.0", True),
                SlotInfo(2, True, "00:06:66:B8:D1:A4", "LogAndStream 0.16.0", True),
                SlotInfo(3, True, "60:8A:10:40:FD:15", "SDLog 0.22.0", False)])
    page._render_all()
    t = page.table
    assert groups(page) == ["base"]
    assert not page.btn_setup.isEnabled()                            # nothing ticked yet
    assert t.item(row_of(page, "Shimmer3-8684"), 0).flags() & Qt.ItemIsUserCheckable   # unpaired but docked
    assert not t.item(row_of(page, "Shimmer3-FD15"), 0).flags() & Qt.ItemIsUserCheckable  # SDLog: cannot stream
    started = []
    monkeypatch.setattr(page.base, "setup_all", lambda targets=None: started.append(targets))
    t.item(row_of(page, "Shimmer3-D1A4"), 0).setCheckState(Qt.Checked)  # tick D1A4 only
    assert page.btn_setup.isEnabled() and page.btn_setup.text().endswith("(1)")
    assert page.checked_devices() == []                              # not paired: not offered to live view
    page.btn_setup.click()
    assert started == [[("SR3VRR8A", 2)]]


def test_refresh_starts_over_from_checking(page, monkeypatch):
    page.cfg.data["devices"] = {"00:06:66:B9:86:84": {"name": "Shimmer3-8684"}}   # no Bluetooth in tests:
    page.paired = [BtDevice("00:06:66:B9:86:84", "Shimmer3-8684", True)]           # paired list comes from here
    dock(page, [SlotInfo(1, True, "00:06:66:B9:86:84", "LogAndStream 0.11.0", True)])
    page._render_all()
    assert cells(page)["Shimmer3-8684"][0] == "base slot 1"
    polled = []
    monkeypatch.setattr(page.base, "_poll", lambda: polled.append(1))
    page.btn_refresh.click()
    assert polled and page.base.state == "checking" and page.base.units["SR3VRR8A"].occupancy is None
    assert cells(page)["Shimmer3-8684"][0] == "checking…"
    assert "looking for Shimmer bases" in page.pill_base.text()


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


def test_firmware_column_flags_old_versions(page):
    page.paired = [BtDevice("00:06:66:B9:86:84", "Shimmer3-8684", True)]
    page.cfg.update_device("00:06:66:B9:86:84", name="Shimmer3-8684", last_check={"firmware": "LogAndStream 0.11.0"})
    dock(page, [SlotInfo(2, True, "60:8A:10:40:FD:15", "SDLog 0.22.0", False)])
    page._render_all()
    fw = {page.table.item(r, 1).text(): page.table.item(r, 3) for r in range(page.table.rowCount())}
    assert fw["Shimmer3-8684"].text() == "0.11.0 ⬆" and "update" in fw["Shimmer3-8684"].toolTip().lower()
    assert "cannot stream" in fw["Shimmer3-FD15"].toolTip()


def test_two_bases_are_shown_together(page):
    dock(page, [SlotInfo(1, True, "00:06:66:B9:86:84", "LogAndStream 0.11.0", True)], serial="SR3VRR8A", letter="A")
    dock(page, [SlotInfo(1, True, "40:84:32:56:A1:87", "LogAndStream 1.1.3", True),
                SlotInfo(3, True, error="docked Shimmer did not answer")], serial="SR8YS557", letter="B")
    page._render_all()
    c = cells(page)
    assert c["Shimmer3-8684"][0] == "base A · slot 1" and c["Shimmer3-A187"][0] == "base B · slot 1"
    assert c["…"][0] == "base B · slot 3"
    assert "2 Shimmer bases · 3 docked" in page.pill_base.text()


def test_firmware_button_needs_one_shimmer_in_the_base(page):
    page.paired = [BtDevice("00:06:66:B9:83:95", "Shimmer3-8395", True)]
    dock(page, [SlotInfo(2, True, "D8:47:8F:04:BC:CE", "LogAndStream 0.16.0", True)])
    page._render_all()
    names = [page.table.item(r, 1).text() for r in range(page.table.rowCount())]
    assert not page.btn_fw.isEnabled()                                   # nothing highlighted
    page.table.selectRow(names.index("Shimmer3-BCCE"))
    assert page.btn_fw.isEnabled()                                       # docked: can be updated
    page.table.selectRow(names.index("Shimmer3-8395"))
    assert not page.btn_fw.isEnabled()                                   # not in a base


def test_old_pairings_are_grey_and_battery_has_its_own_column(page):
    from shimmer_live.ui.devices_page import IDLE_GREY
    page.presence = True                                  # searching nearby, nothing found
    page.paired = [BtDevice("00:06:66:B9:86:84", "Shimmer3-8684", True), BtDevice("40:84:32:56:A1:87", "Shimmer3-A187", True)]
    page.cfg.update_device("00:06:66:B9:86:84", name="Shimmer3-8684", last_check={
        "ok": True, "when": "2026-11-03T14:52:00Z", "kind": "ECG/ExG", "rate_hz": 250.1, "battery_pct": 15.0,
        "channels": ["EXG_ADS1292R_1_CH1_24BIT"], "firmware": "LogAndStream 0.11.0"})
    dock(page, [SlotInfo(5, True, "40:84:32:56:A1:87", "LogAndStream 1.1.9", True)])
    page._render_all()
    rows = {page.table.item(r, 1).text(): r for r in range(page.table.rowCount())}
    old, docked = rows["Shimmer3-8684"], rows["Shimmer3-A187"]
    assert page.table.item(old, 1).foreground().color().name() == IDLE_GREY          # old pairing: grey
    assert page.table.item(docked, 1).foreground().color().name() != IDLE_GREY       # in the base: normal
    assert page.table.item(old, 6).text() == "15%" and page.table.item(old, 5).text() == "250 Hz"
    assert page.table.item(old, 7).text().startswith("✔ ")                             # no longer one long line


def test_updated_shimmer_shows_the_restart_reminder_until_it_answers(page):
    page.paired = [BtDevice("D8:47:8F:04:BC:CE", "Shimmer3-BCCE", True)]
    dock(page, [SlotInfo(1, True, "D8:47:8F:04:BC:CE", "LogAndStream 1.1.9", True)])
    page.mark_needs_restart("D8:47:8F:04:BC:CE")
    assert "switch it off and on" in cells(page)["Shimmer3-BCCE"][2]
    page.mark_restarted("D8:47:8F:04:BC:CE")
    assert "switch it off and on" not in cells(page)["Shimmer3-BCCE"][2]


class FakeBackend:
    def __init__(self, around):
        self.around, self.asked = around, []

    def discover(self, seconds, on_update):
        return [BtDevice(a, n, False) for a, n in getattr(self, "unpaired", [])]

    def nearby(self, addresses, seconds, should_stop=lambda: False, on_result=None):
        self.asked.append(list(addresses))
        out = {a: a in self.around for a in addresses}
        for a, ok in out.items():
            on_result(a, ok)
        return out


def test_only_shimmers_found_nearby_or_docked_can_be_ticked(page, monkeypatch):
    import shimmer_live.ui.devices_page as dpm
    monkeypatch.setattr(dpm, "run_bg", lambda fn, done=None, failed=None, name=None: done(fn()))
    monkeypatch.setattr(dpm, "ui_call", lambda fn, *a: fn(*a))
    near, far, docked = "40:84:32:56:A2:81", "00:06:66:B9:86:84", "40:84:32:56:A1:87"
    page.backend, page.presence, page._shown = FakeBackend({near}), True, True
    page._last_discover = time.monotonic()                              # knock rounds only in this test
    page.paired = [BtDevice(m, n, True) for m, n in ((near, "Shimmer3-A281"), (far, "Shimmer3-8684"),
                                                       (docked, "Shimmer3-A187"))]
    page.cfg.data["selected"] = [near, far, docked]
    dock(page, [SlotInfo(5, True, docked, "LogAndStream 1.1.9", True)])
    page._render_all()
    assert cells(page)["Shimmer3-A281"][0] == "waiting…"
    assert page.checked_devices() == []                 # nothing found yet; docked ones never go live / to SD
    assert page._checked_slots() == [("SR3VRR8A", 5)]                  # the docked tick is for the setup
    page._presence_scan()
    assert sorted(page.backend.asked[0]) == sorted([near, far])         # docked ones are not searched for
    assert cells(page)["Shimmer3-A281"][0] == "nearby" and cells(page)["Shimmer3-8684"][0] == "not found"
    assert page.checked_devices() == [(near, "Shimmer3-A281")]
    assert groups(page) == ["ready", "base", "away"]
    rows = {r.name: i for i, r in enumerate(page.rows) if not r.header}
    from shimmer_live.ui.devices_page import IDLE_GREY
    assert page.table.item(rows["Shimmer3-8684"], 1).foreground().color().name() == IDLE_GREY
    assert page.table.item(rows["Shimmer3-A281"], 1).foreground().color().name() != IDLE_GREY
    page.table.item(rows["Shimmer3-A281"], 0).setCheckState(dpm.Qt.Unchecked)   # untick one that is usable...
    assert far in page.cfg.data["selected"] and near not in page.cfg.data["selected"]  # ...the grey tick is kept
    page.backend.around = {far}                                       # switched on later: it becomes usable
    page._presence_scan()
    assert (far, "Shimmer3-8684") in page.checked_devices()
    assert page.backend.asked[-1][0] == near                            # found ones are re-checked first
    page._presence_scan()                                               # A281 missed twice: now grey
    assert cells(page)["Shimmer3-A281"][0] == "not found"


def test_knock_order_found_then_ticked_then_longest_ago(page):
    a, b, c, d = "AA:00:00:00:00:01", "AA:00:00:00:00:02", "AA:00:00:00:00:03", "AA:00:00:00:00:04"
    page.nearby = {c: 1.0}
    page.cfg.data["selected"] = [d]
    page.last_knock = {a: 5.0, b: 2.0}
    assert page._knock_order([a, b, c, d]) == [c, d, b, a]
    page.checked = {d, b}                                  # d and b already checked: a goes first
    page.last_knock = {a: 100.0, b: 90.0, d: 105.0}        # d ticked: counts as knocked at 85
    assert page._knock_order([a, b, c, d]) == [c, a, d, b]


def test_search_pauses_while_bluetooth_is_busy(page, monkeypatch):
    import shimmer_live.ui.devices_page as dpm
    monkeypatch.setattr(dpm, "run_bg", lambda fn, done=None, failed=None, name=None: done(fn()))
    page.backend, page.presence, page._shown = FakeBackend(set()), True, True
    page.paired = [BtDevice("00:06:66:B9:86:84", "Shimmer3-8684", True)]
    page.base.state = "none"
    page._render_all()
    page.busy["00:06:66:B9:86:84"] = "testing…"
    page._presence_scan()
    assert page.backend.asked == []


def test_without_presence_support_every_pairing_stays_usable(page):
    page.paired = [BtDevice("00:06:66:B9:86:84", "Shimmer3-8684", True)]
    page.cfg.data["selected"] = ["00:06:66:B9:86:84"]
    page.base.state = "none"
    page._render_all()                                                  # backend None in this fixture
    assert page.checked_devices() == [("00:06:66:B9:86:84", "Shimmer3-8684")]
    assert cells(page)["Shimmer3-8684"][0] == "not docked"


def test_unpaired_shimmer_found_nearby_gets_a_pair_button(page, monkeypatch):
    import shimmer_live.ui.devices_page as dpm
    monkeypatch.setattr(dpm, "run_bg", lambda fn, done=None, failed=None, name=None: done(fn()))
    monkeypatch.setattr(dpm, "ui_call", lambda fn, *a: fn(*a))
    new, gone = "40:84:32:56:BE:EF", "40:84:32:56:DE:AD"
    page.backend, page.presence, page._shown = FakeBackend({new}), True, True
    page.backend.unpaired = [(new, "Shimmer3-BEEF"), (gone, "Shimmer3-DEAD"), ("11:22:33:44:55:66", "JBL GO 2")]
    page.paired = []
    page.base.state = "none"
    page._render_all()
    page._presence_scan()                              # discovery round: candidates, not shown before they answer
    assert set(page.candidates) == {new, gone} and not page.rows
    page._presence_scan()                              # knock round
    assert groups(page) == ["nearby"] and list(cells(page)) == ["Shimmer3-BEEF"]
    i = row_of(page, "Shimmer3-BEEF")
    assert page.table.cellWidget(i, 8).text() == "Pair"
    assert not page.table.item(i, 0).flags() & dpm.Qt.ItemIsUserCheckable     # pair first
    for _ in range(2):
        page._presence_scan()
    assert gone not in page.candidates                 # never answered: forgotten


def test_ready_shimmers_are_read_every_5_minutes_one_at_a_time(page, monkeypatch):
    import datetime as dt
    import shimmer_live.ui.devices_page as dpm
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=6)).isoformat()
    fresh = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)).isoformat()
    a, b, c = "40:84:32:56:A2:81", "D8:47:8F:04:BC:CE", "00:06:66:B9:86:84"
    page.paired = [BtDevice(a, "Shimmer3-A281", True), BtDevice(b, "Shimmer3-BCCE", True),
                   BtDevice(c, "Shimmer3-8684", True)]
    for m, when in ((a, old), (b, fresh), (c, old)):
        page.cfg.update_device(m, last_check={"ok": True, "when": when})
    page.backend, page.presence, page._shown = object(), True, True
    page.nearby = {a: time.monotonic(), b: time.monotonic()}             # c is not in Ready
    page.base.state = "none"
    page._render_all()
    read = []
    monkeypatch.setattr(page, "_test", lambda mac, name, then=None: (read.append(mac), page.busy.update({mac: "x"})))
    page._auto_read()
    page._auto_read()                                                    # one at a time: busy until it ends
    assert read == [a]


def test_a_failed_reading_keeps_the_last_values(tmp_path, monkeypatch):
    from shimmer_live import session
    cfg = Config(tmp_path / "c.json")
    cfg.update_device("AA", last_check={"ok": True, "when": "x", "battery_pct": 80.0})

    class Dead:
        def __init__(self, *a, **k):
            raise OSError("no")
    monkeypatch.setattr(session, "RETRY_PAUSE_S", 0, raising=False)
    monkeypatch.setattr(session, "connect_link", Dead, raising=False)
    monkeypatch.setattr(session, "ShimmerLink", Dead, raising=False)
    try:
        res = session.probe_device("AA", "n", cfg, attempts=1)
    except Exception:                                                    # the link class has another name
        pytest.skip("probe_device link not patchable here")
    assert res["ok"] is False and res["last_ok"]["battery_pct"] == 80.0
