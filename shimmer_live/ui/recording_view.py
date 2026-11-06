"""Look through a finished recording (after Stop recording, or an SD import): every recorded signal on one
common time axis, a window of 5 s to 10 min moved with one scrollbar, keypoints, late starts and gaps.

Light on CPU whatever the length: the whole recording stays in the arrays the save dialog already has; each
redraw cuts out the visible window (binary search) and reduces it to ~2 points per pixel (min/max per
bucket, so spikes stay visible) before handing it to pyqtgraph."""
import datetime as dt
import math

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QFrame, QHBoxLayout, QLabel, QPushButton,
                               QScrollArea, QScrollBar, QVBoxLayout)

from .. import labels
from .common import DEVICE_COLORS, FONT_BASE, FONT_S, FONT_XS, INK, fit_to_screen, set_role
from .live_page import PlotsView

WINDOWS = [("5 s", 5), ("10 s", 10), ("30 s", 30), ("1 min", 60), ("5 min", 300), ("10 min", 600)]
DEFAULT_WINDOW_S = 60
MAX_POINTS = 4000                 # per signal per redraw (about 2 per pixel on a wide screen)
GAP_SAMPLES = 3                   # a jump of more than this many sample periods breaks the line
SHADE_GAP_S = 0.5                 # gaps at least this long are shaded
PLOT_ROW_PX = 170
STEP_S_PER_TICK = 0.1             # scrollbar resolution


def fmt_clock(seconds: float, spacing: float = 1.0) -> str:
    """Seconds since the start as m:ss / h:mm:ss (with tenths when ticks are closer than a second)."""
    sign = "-" if seconds < 0 else ""
    s = abs(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    sec_txt = f"{sec:04.1f}" if spacing < 1 else f"{int(round(sec)) % 60:02d}"
    if spacing >= 1 and round(sec) == 60:          # 59.6 s rounds up: carry into the minutes
        m, sec_txt = m + 1, "00"
    return f"{sign}{int(h)}:{int(m):02d}:{sec_txt}" if h else f"{sign}{int(m)}:{sec_txt}"


NICE_STEPS_S = [0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200]


class TimeAxis(pg.AxisItem):
    """Time since the start as m:ss / h:mm:ss, with ticks on round seconds / minutes."""

    def tickSpacing(self, minVal, maxVal, size):
        span = maxVal - minVal
        if span <= 0 or size <= 0:
            return super().tickSpacing(minVal, maxVal, size)
        want = span / max(2.0, size / 110.0)               # about one label every 110 px
        major = next((s for s in NICE_STEPS_S if s >= want), NICE_STEPS_S[-1])
        minor = next((s for s in reversed(NICE_STEPS_S) if s <= major / 4), major / 5)
        return [(major, 0), (minor, 0)]

    def tickStrings(self, values, scale, spacing):
        return [fmt_clock(v, spacing) for v in values]


def with_gaps(x, y, period):
    """Insert NaN where consecutive samples are more than GAP_SAMPLES periods apart (the line breaks there).
    Returns (x, y, gaps) with gaps = [(start, end)] in seconds."""
    if len(x) < 2:
        return x, y, []
    dx = np.diff(x)
    idx = np.nonzero(dx > GAP_SAMPLES * period)[0]
    gaps = [(float(x[i]), float(x[i + 1])) for i in idx]
    if len(idx):
        x = np.insert(x, idx + 1, (x[idx] + x[idx + 1]) / 2)
        y = np.insert(y.astype(float), idx + 1, np.nan)
    return x, y, gaps


def decimate(x, y, max_points=MAX_POINTS):
    """At most ~max_points points: min and max of each bucket, so peaks and spikes stay visible."""
    n = len(x)
    if n <= max_points:
        return x, y
    k = int(math.ceil(n / (max_points / 2)))
    m = (n // k) * k
    yb = y[:m].reshape(-1, k)
    xb = x[:m].reshape(-1, k)
    with np.errstate(all="ignore"):
        finite = np.isfinite(yb)
        lo = np.where(finite, yb, np.inf).min(axis=1)
        hi = np.where(finite, yb, -np.inf).max(axis=1)
    empty = ~finite.any(axis=1)
    lo[empty] = np.nan                             # an all-gap bucket keeps the line broken
    hi[empty] = np.nan
    # a bucket that contains a gap (a NaN) ends with a NaN too, so the line never bridges the gap
    brk = np.where(finite.all(axis=1), hi, np.nan)
    xs = np.repeat(xb[:, 0], 3)
    ys = np.column_stack([lo, hi, brk]).ravel()
    if m < n:
        xs, ys = np.concatenate([xs, x[m:]]), np.concatenate([ys, y[m:]])
    return xs, ys


class Track:
    """One plotted signal: its full arrays (seconds since start, values) and its plot."""

    def __init__(self, title, unit, color, x, y, period, points=False):
        self.title, self.unit, self.color, self.points = title, unit, color, points
        self.x, self.y, self.gaps = with_gaps(x, y, period) if period else (x, y, [])
        self.plot = self.curve = None
        self.yr = None


def tracks_from(a):
    """Every recorded signal of an export.Analysis, on one time axis: seconds since the recording start."""
    t0_ms = a.summary["start_unix_ns"] / 1e6
    out, coverage = [], []
    for n, d in enumerate(a.devices):
        color = DEVICE_COLORS[n % len(DEVICE_COLORS)]
        who = labels.rich(d.meta["name"], d.meta.get("label", ""))
        x = (d.data["Timestamp_Unix_CAL"].to_numpy(dtype=float) - t0_ms) / 1000.0
        rate = float(d.meta.get("sampling_rate_hz") or 0)
        period = 1.0 / rate if rate else 0
        _, _, gaps = with_gaps(x, np.zeros(len(x)), period) if period else (None, None, [])
        coverage.append((labels.plain(d.meta["name"], d.meta.get("label", "")), color,
                         (float(x[0]), float(x[-1])) if len(x) else None, gaps))
        for ch in d.meta["channels"]:
            if ch["kind"] == "other" and not ch.get("unit"):
                continue                             # status bytes: not a signal to look at
            y = a.calibrated.get((d.meta["mac"], ch["export_name"]))
            if y is None or len(y) != len(x):
                continue
            unit = ch.get("unit") or ""
            out.append(Track(f"<b>{who}</b> — {ch['label']}", unit, color, x, np.asarray(y, dtype=float), period))
    for p in a.plugins:
        if "Timestamp_Unix_CAL" not in p.data:
            continue
        x = (p.data["Timestamp_Unix_CAL"].to_numpy(dtype=float) - t0_ms) / 1000.0
        dev = next((n for n, d in enumerate(a.devices) if d.meta["name"] == p.meta.get("device")), 0)
        color = DEVICE_COLORS[dev % len(DEVICE_COLORS)]
        units = dict(zip(p.data.columns, p.units)) if p.units else {}
        for col in p.data.columns:
            if "Timestamp" in col or col == "Keypoint":
                continue
            y = p.data[col].to_numpy(dtype=float)
            keep = np.isfinite(y)
            out.append(Track(f"<b>{p.meta.get('device', '')}</b> — {p.meta['name']}: {col}",
                             units.get(col, ""), color, x[keep], y[keep], 0, points=True))
    marks = []
    if a.keypoints is not None and len(a.keypoints):
        for _, k in a.keypoints.iterrows():
            marks.append(((float(k["Timestamp_Unix_CAL"]) - t0_ms) / 1000.0, str(k["Keypoint"]), str(k["Color"])))
    return out, coverage, marks


class RecordingView(QDialog):
    def __init__(self, parent, analysis, title=""):
        super().__init__(parent)
        self.a = analysis
        self.setWindowTitle(f"Signals — {title}" if title else "Recording — signals")
        self.setModal(False)
        self.setAttribute(Qt.WA_DeleteOnClose)        # closed = memory freed (a long recording is large)
        fit_to_screen(self, 1440, 900)
        self.tracks, self.coverage, self.marks = tracks_from(analysis)
        ends = [t.x[-1] for t in self.tracks if len(t.x)] + [c[2][1] for c in self.coverage if c[2]]
        self.duration = max([analysis.summary.get("duration_s", 0.0)] + ends)
        self.window_s = DEFAULT_WINDOW_S
        self.start_s = 0.0

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        # ---- top bar ----
        top = QHBoxLayout()
        start = dt.datetime.fromtimestamp(analysis.summary["start_unix_ns"] / 1e9)
        head = QLabel(f"<b>Started {start:%Y-%m-%d %H:%M:%S}</b> · duration {fmt_clock(self.duration)} · "
                      f"{len(self.tracks)} signal(s) · time axis = time since the recording started (all Shimmers "
                      f"on the same clock) · values calibrated")
        head.setTextFormat(Qt.RichText)
        top.addWidget(head, 1)
        top.addWidget(QLabel("Show:"))
        self.window_combo = QComboBox()
        for text, sec in WINDOWS:
            self.window_combo.addItem(text, sec)
        self.window_combo.setCurrentIndex([s for _, s in WINDOWS].index(DEFAULT_WINDOW_S))
        self.window_combo.setToolTip("How much of the recording is shown at once")
        self.window_combo.currentIndexChanged.connect(self._window_changed)
        top.addWidget(self.window_combo)
        self.btn_prev = QPushButton("◀")
        self.btn_next = QPushButton("▶")
        for b, d, tip in ((self.btn_prev, -1, "Previous window (Page Up)"), (self.btn_next, 1, "Next window (Page Down)")):
            b.setToolTip(tip)
            b.setFixedWidth(44)
            b.clicked.connect(lambda _=False, d=d: self._step(d * self.window_s))
            top.addWidget(b)
        lay.addLayout(top)

        # ---- overview: the whole recording, where each Shimmer has data, keypoints, the visible window ----
        self.overview = pg.PlotWidget(axisItems={"bottom": TimeAxis("bottom")})
        self.overview.setFixedHeight(48 + 24 * max(1, len(self.coverage)))
        self.overview.setMouseEnabled(x=False, y=False)
        self.overview.setMenuEnabled(False)
        self.overview.hideButtons()
        self.overview.getAxis("left").setWidth(0)
        self.overview.getAxis("left").setStyle(showValues=False)
        self.overview.getAxis("bottom").setTickFont(QFont(QApplication.font().family(), int(round(FONT_XS))))
        self.overview.setToolTip("The whole recording: one bar per Shimmer where it has data (breaks = gaps), "
                                 "keypoints as lines. Click to jump; the shaded box is what is shown below.")
        n_dev = max(1, len(self.coverage))
        for row, (name, color, span, gaps) in enumerate(self.coverage):
            yv = n_dev - row
            if span:
                pieces, cur = [], span[0]
                for g0, g1 in gaps:
                    pieces.append((cur, g0))
                    cur = g1
                pieces.append((cur, span[1]))
                xs, ys = [], []
                for p0, p1 in pieces:
                    xs += [p0, p1, np.nan]
                    ys += [yv, yv, np.nan]
                self.overview.plot(xs, ys, pen=pg.mkPen(color, width=7), connect="finite")
            txt = pg.TextItem(name, color=INK, anchor=(0, 0.5))
            txt.setFont(QFont(QApplication.font().family(), int(round(FONT_XS))))
            txt.setPos(0, yv + 0.45)
            self.overview.addItem(txt)
        for x, name, color in self.marks:
            self.overview.addItem(pg.InfiniteLine(pos=x, angle=90, pen=pg.mkPen(color, width=1)))
        self.overview.setXRange(0, max(self.duration, 1.0), padding=0.01)
        self.overview.setYRange(0.3, n_dev + 0.9, padding=0)
        self.region = pg.LinearRegionItem(values=(0, self.window_s), movable=False,
                                          brush=pg.mkBrush(31, 78, 140, 40), pen=pg.mkPen(31, 78, 140, 120))
        self.overview.addItem(self.region)
        self.overview.scene().sigMouseClicked.connect(self._overview_clicked)
        lay.addWidget(self.overview)

        # ---- the signals, one time scrollbar for all ----
        self.glw = PlotsView()
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setWidget(self.glw)
        lay.addWidget(self.scroll, 1)
        self.time_bar = QScrollBar(Qt.Horizontal)
        self.time_bar.setToolTip("Move through the recording (all signals together). Arrow keys: a tenth of the "
                                 "window; Page Up/Down: a whole window.")
        self.time_bar.valueChanged.connect(self._bar_moved)
        lay.addWidget(self.time_bar)
        self.position = set_role(QLabel(), "muted")
        lay.addWidget(self.position)
        if not self.tracks:
            lay.insertWidget(2, QLabel("Nothing was recorded in this recording."))
        self._build_plots()
        self._redraw_timer = QTimer(self, singleShot=True, interval=15, timeout=self._redraw)
        for key, step in ((Qt.Key_Left, -0.1), (Qt.Key_Right, 0.1), (Qt.Key_PageUp, -1), (Qt.Key_PageDown, 1)):
            QShortcut(QKeySequence(key), self, activated=lambda s=step: self._step(s * self.window_s))
        self._configure_bar()
        self._redraw()

    # ---------------- building ----------------
    def _build_plots(self):
        for i, tr in enumerate(self.tracks):
            p = self.glw.addPlot(row=i, col=0, axisItems={"bottom": TimeAxis("bottom")})
            p.setMouseEnabled(x=False, y=False)
            p.setMenuEnabled(False)
            p.hideButtons()
            p.disableAutoRange()
            p.setLabel("left", tr.unit, **{"font-size": f"{FONT_S}pt"})
            for side in ("left", "bottom"):
                p.getAxis(side).setTickFont(QFont(QApplication.font().family(), int(round(FONT_XS))))
                p.getAxis(side).enableAutoSIPrefix(False)
            p.setTitle(f"<span style='color:{INK}'>{tr.title}</span>", size=f"{FONT_BASE}pt", justify="left")
            if tr.points:
                tr.curve = p.plot(pen=pg.mkPen(tr.color, width=1), symbol="o", symbolSize=5, symbolBrush=tr.color,
                                  symbolPen=None)
            else:
                tr.curve = p.plot(pen=pg.mkPen(tr.color, width=1))
            for g0, g1 in tr.gaps:
                if g1 - g0 >= SHADE_GAP_S:
                    shade = pg.LinearRegionItem(values=(g0, g1), movable=False, brush=pg.mkBrush(200, 60, 60, 35),
                                                pen=pg.mkPen(None))
                    shade.setToolTip(f"no data for {g1 - g0:.1f} s")
                    p.addItem(shade)
            for x, name, color in self.marks:
                line = pg.InfiniteLine(pos=x, angle=90, movable=False, pen=pg.mkPen(color, width=2),
                                       label=name if i == 0 or len(self.tracks) < 4 else None,
                                       labelOpts={"position": 0.9, "color": color, "fill": (255, 255, 255, 220)})
                p.addItem(line)
            tr.plot = p
        self.glw.setMinimumHeight(PLOT_ROW_PX * max(1, len(self.tracks)))

    def closeEvent(self, ev):
        """Take the plots down first (Python-side references too), then let Qt delete the window."""
        self._redraw_timer.stop()
        self.glw.clear()
        self.overview.clear()
        for tr in self.tracks:
            tr.plot = tr.curve = None
        self.tracks = []
        super().closeEvent(ev)

    # ---------------- moving ----------------
    def _configure_bar(self):
        span = max(0.0, self.duration - self.window_s)
        self.time_bar.blockSignals(True)
        self.time_bar.setRange(0, int(math.ceil(span / STEP_S_PER_TICK)))
        self.time_bar.setPageStep(max(1, int(self.window_s / STEP_S_PER_TICK)))
        self.time_bar.setSingleStep(max(1, int(self.window_s / 10 / STEP_S_PER_TICK)))
        self.start_s = min(self.start_s, span)
        self.time_bar.setValue(int(round(self.start_s / STEP_S_PER_TICK)))
        self.time_bar.blockSignals(False)
        self.time_bar.setEnabled(span > 0)

    def _window_changed(self):
        centre = self.start_s + self.window_s / 2
        at_start = self.start_s <= 0
        self.window_s = self.window_combo.currentData()
        # keep the same moment in the middle; at the very start, stay at the start
        self.start_s = 0.0 if at_start else max(0.0, centre - self.window_s / 2)
        self._configure_bar()
        self._redraw_timer.start()

    def _bar_moved(self, value):
        self.start_s = value * STEP_S_PER_TICK
        self._redraw_timer.start()                      # coalesce a fast drag into few redraws

    def _step(self, seconds):
        self.time_bar.setValue(int(round((self.start_s + seconds) / STEP_S_PER_TICK)))

    def _overview_clicked(self, ev):
        vb = self.overview.getPlotItem().vb
        x = vb.mapSceneToView(ev.scenePos()).x()
        self.time_bar.setValue(int(round((x - self.window_s / 2) / STEP_S_PER_TICK)))

    def _redraw(self):
        if not self.tracks and not self.isVisible():
            return
        a, b = self.start_s, self.start_s + self.window_s
        self.region.setRegion((a, b))
        for tr in self.tracks:
            if tr.plot is None:
                continue
            i0 = max(0, int(np.searchsorted(tr.x, a, "left")) - 1)
            i1 = min(len(tr.x), int(np.searchsorted(tr.x, b, "right")) + 1)
            x, y = decimate(tr.x[i0:i1], tr.y[i0:i1])
            tr.curve.setData(x, y, connect="finite")
            tr.plot.setXRange(a, b, padding=0)
            v = y[np.isfinite(y)]
            if v.size:
                lo, hi = float(v.min()), float(v.max())
                pad = (hi - lo) * 0.08 if hi > lo else max(abs(hi) * 0.01, 1e-6)
                r = (lo - pad, hi + pad)
                if r != tr.yr:
                    tr.yr = r
                    tr.plot.setYRange(*r, padding=0)
        wall = dt.datetime.fromtimestamp(self.a.summary["start_unix_ns"] / 1e9 + a)
        self.position.setText(f"Showing {fmt_clock(a)} – {fmt_clock(b)} of {fmt_clock(self.duration)}"
                              f"   (clock time {wall:%H:%M:%S})")
