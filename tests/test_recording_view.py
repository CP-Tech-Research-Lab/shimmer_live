"""Recording viewer: reduction keeps peaks and gaps, time labels, opens on a real recording and scrolls."""
import os
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent))


def view_mod():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    widgets = pytest.importorskip("PySide6.QtWidgets")
    widgets.QApplication.instance() or widgets.QApplication([])
    from shimmer_live.ui import recording_view
    return recording_view


def test_reduction_keeps_spikes_and_never_bridges_a_gap():
    rv = view_mod()
    x = np.arange(100_000) / 512.0
    y = np.zeros_like(x)
    y[54_321] = 9.0                                         # one spike
    y[77_777] = -4.0
    x2, y2, gaps = rv.with_gaps(np.delete(x, np.s_[30_000:31_000]), np.delete(y, np.s_[30_000:31_000]), 1 / 512)
    assert len(gaps) == 1 and gaps[0][1] - gaps[0][0] == pytest.approx(1001 / 512)
    xs, ys = rv.decimate(x2, y2, 4000)
    assert len(xs) <= 6100 and np.nanmax(ys) == 9.0 and np.nanmin(ys) == -4.0
    g0, g1 = gaps[0]
    # every pair of consecutive finite points is closer than the gap: the line never crosses it
    f = np.isfinite(ys)
    seg = np.diff(xs)[f[:-1] & f[1:]]
    assert seg.max() < g1 - g0


def test_time_labels():
    rv = view_mod()
    assert rv.fmt_clock(0) == "0:00" and rv.fmt_clock(75) == "1:15" and rv.fmt_clock(3725) == "1:02:05"
    assert rv.fmt_clock(12.34, spacing=0.5) == "0:12.3" and rv.fmt_clock(59.7) == "1:00"


def test_viewer_opens_on_a_real_recording_and_scrolls(tmp_path):
    rv = view_mod()
    from shimmer_live import export, sdlog
    from shimmer_live.config import Config
    from test_sdlog import DATA, MAC
    d = sdlog.read_files([DATA])
    cfg = Config(tmp_path / "c.json")
    s = sdlog.new_session(cfg, "P", [(MAC, "Shimmer3-8395")], "raw", [])
    t0 = float(d.rtc_unix[0])
    s["devices"][MAC].update(state=sdlog.STOPPED, start={"start_offset": {"offset_ms": 0.0, "pc_unix": t0}})
    s["keypoints"].append({"type": "keypoint", "name": "rest", "color": "#1aa", "unix_ns": int((t0 + 10) * 1e9),
                           "shared_ticks": 0})
    a = export.analyze(sdlog.build_recording(s, {MAC: ("Shimmer3-8395", d, [DATA])}, tmp_path, "raw"))
    v = rv.RecordingView(None, a, "P")
    try:
        names = [t.title for t in v.tracks]
        assert any("ECG LL-RA" in n for n in names) and not any("status" in n.lower() for n in names)
        assert v.marks and v.marks[0][1] == "rest" and 9 < v.marks[0][0] < 11
        assert v.window_s == 60 and v.duration > 25
        v.window_combo.setCurrentIndex(0)                    # 5 s
        v._step(10)
        v._redraw()
        assert v.start_s == pytest.approx(10, abs=0.1)
        x0, x1 = v.tracks[0].plot.viewRange()[0]
        assert (x0, x1) == pytest.approx((v.start_s, v.start_s + 5))
        assert "0:10" in v.position.text()
    finally:
        from PySide6.QtCore import QCoreApplication, QEvent
        v.close()                                            # deletes itself (WA_DeleteOnClose)
        QCoreApplication.sendPostedEvents(v, QEvent.DeferredDelete)
    assert v.tracks == []                                    # plots released on close


def test_save_dialog_has_the_view_button(tmp_path):
    view_mod()
    from shimmer_live.ui import dialogs
    assert hasattr(dialogs.SaveRecordingDialog, "_view")
