"""Live data page: real-time plots, signal visibility/recording selection, keypoints, plugins, recording."""
import datetime as dt
import logging

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QButtonGroup, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel,
                               QMessageBox, QPushButton, QRadioButton, QScrollArea, QToolButton, QVBoxLayout, QWidget)

from .. import clocks
from .. import keypoints as kpmod
from ..plugins.host import PluginHost
from ..recorder import VALUES_CAL, VALUES_RAW, Recorder
from ..session import ShimmerSession, State, stop_all
from .common import (CRITICAL, DEVICE_COLORS, FONT_BASE, FONT_L, FONT_M, FONT_S, FONT_XS, FONT_XXL, GOOD, ICON_BTN,
                     INK, MUTED, SURFACE, WARNING, AlertArea, Card, FlowLayout, fmt_duration, run_bg, set_if_changed,
                     set_role)
from .device_settings import DeviceInfoDialog
from .dialogs import CalibrationDialog, SaveRecordingDialog, TimingInfoDialog
from .branding import BrandHeader
from .icons import icon

log = logging.getLogger("shimmerlive.live")

BATTERY_WARN, BATTERY_CRIT = 20.0, 10.0
LOW_RATE_FRACTION = 0.85
DISK_WARN_BYTES = 1_000_000_000
PLOT_ROW_PX = 190
PANEL_WIDTH = 320
MARK_KEEP_S = 70.0
SIGNAL_DEFAULTS_VERSION = 2         # bump when the default eye/record choices change                  # keypoint lines are kept a little longer than the longest window


def display_unit(unit):
    return {"uS": "µS", None: "raw", "": ""}.get(unit, unit)


def plugin_key(binding, output) -> str:
    return f"plugin:{binding.cls.id}:{output.key}"


def sticky_range(cur, lo, hi, pad_frac=0.08):
    """Y range for data spanning lo..hi, or None to keep cur. Grows at once to fit new data,
    shrinks only when the data uses less than half of it, so the axis is not redrawn every frame."""
    span = hi - lo
    pad = span * pad_frac if span > 0 else max(abs(hi) * 0.01, 1e-6)
    want = (lo - pad, hi + pad)
    if cur is None or lo < cur[0] or hi > cur[1] or (want[1] - want[0]) < 0.5 * (cur[1] - cur[0]):
        return want
    return None


pg.setConfigOptions(antialias=True, background=SURFACE, foreground=MUTED)


class DeviceCard(QFrame):
    def __init__(self, session: ShimmerSession, page):
        super().__init__()
        self.s, self.page = session, page
        self.setStyleSheet("DeviceCard { background: white; border: 1px solid #dcdcd8; border-radius: 6px; }")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        sw = QLabel("●")
        sw.setStyleSheet(f"color: {session.color}; font-size: 22pt;")
        lay.addWidget(sw)
        col = QVBoxLayout()
        col.setSpacing(0)
        self.title = QLabel(f"<span style='font-size:{FONT_M}pt; font-weight:700'>{session.name}</span> "
                            f"<span style='color:{MUTED}; font-size:{FONT_S}pt'>{session.mac}</span>")
        self.line = QLabel()
        self.line2 = QLabel()
        self.line2.setStyleSheet(f"color: {MUTED}; font-size: {FONT_S}pt;")
        self.line3 = QLabel()
        self.line3.setStyleSheet(f"color: {MUTED}; font-size: {FONT_S}pt;")
        for w in (self.title, self.line, self.line2, self.line3):
            col.addWidget(w)
        lay.addLayout(col)
        gear = QPushButton("⚙")
        gear.setToolTip("Device info")
        gear.setFixedSize(ICON_BTN + 4, ICON_BTN + 4)
        gear.setStyleSheet(f"font-size: {FONT_M}pt; padding: 0; min-height: 0;")
        gear.clicked.connect(lambda: DeviceInfoDialog(self, self.s).exec())
        lay.addWidget(gear, 0, Qt.AlignTop)

    def refresh(self):
        s = self.s
        st = s.state
        if st == State.STREAMING:
            ok = s.measured_rate >= LOW_RATE_FRACTION * s.rate or s.seconds_in_state() < 5
            state = (f"<span style='color:{GOOD if ok else WARNING}; font-weight:600'>● streaming</span> "
                     f"{s.measured_rate:.0f}/{s.rate:.0f} Hz")
        elif st == State.LOST:
            state = (f"<span style='color:{CRITICAL}; font-weight:700'>● LOST {fmt_duration(s.seconds_in_state())}"
                     f"</span> reconnecting (attempt {s.attempt})")
        elif st == State.CONNECTING:
            state = f"<span style='color:#1f4e8c; font-weight:600'>● connecting</span> {s.detail}"
        elif st == State.FAILED:
            state = f"<span style='color:{CRITICAL}; font-weight:700'>● not connected</span>"
        else:
            state = f"<span style='color:{MUTED}'>● {st.value}</span>"
        batt = ""
        if s.battery_pct is not None:
            c = CRITICAL if s.battery_pct < BATTERY_CRIT else WARNING if s.battery_pct < BATTERY_WARN else INK
            batt = f" · battery <span style='color:{c}; font-weight:600'>{s.battery_pct:.0f}%</span>"
        set_if_changed(self.line, state + batt)
        rec = s.recorder
        recs = f" · recorded {rec.stats[s.mac].samples:,}" if rec and s.mac in rec.stats else ""
        set_if_changed(self.line2, f"{s.kind or 'Shimmer3'} · {s.firmware or '—'} · {s.packets:,} samples{recs}")
        set_if_changed(self.line3, self.page.plugin_status(s))


class SignalRow(QWidget):
    """👁 show in live view · ● record · label."""

    def __init__(self, label, visible, record, on_change):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self.eye = QToolButton()
        self.eye.setCheckable(True)
        self.eye.setChecked(visible)
        self.eye.setToolTip("Show or hide this signal in the live plots")
        self.rec = QToolButton()
        self.rec.setCheckable(True)
        self.rec.setChecked(record)
        self.rec.setToolTip("Record this signal or not (locked while a recording is running)")
        for b in (self.eye, self.rec):
            b.setAutoRaise(True)
            b.setFixedSize(ICON_BTN, ICON_BTN - 4)
            b.setIconSize(QSize(20, 20))
        lab = QLabel(label)
        lab.setWordWrap(True)
        lay.addWidget(self.eye)
        lay.addWidget(self.rec)
        lay.addWidget(lab, 1)
        self._paint()
        self.eye.toggled.connect(lambda _: (self._paint(), on_change()))
        self.rec.toggled.connect(lambda _: (self._paint(), on_change()))

    def _paint(self):
        self.eye.setIcon(icon("eye" if self.eye.isChecked() else "eye_off", INK if self.eye.isChecked() else "#9a9a95"))
        self.rec.setIcon(icon("rec_on" if self.rec.isChecked() else "rec_off",
                              CRITICAL if self.rec.isChecked() else "#9a9a95"))


class LivePage(QWidget):
    back = Signal()

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.sessions: list[ShimmerSession] = []
        self.host: PluginHost | None = None
        self.recorder: Recorder | None = None
        self.t0 = clocks.perf_ns()
        self.window_s = 10.0
        self._plots = []            # dicts: kind, session, binding, index, spec, plot, curve, value
        self._built_for = None
        self._panel_for = None
        self._bound = set()
        self._lost_seen = set()
        self._blink = False
        self.rows = {}              # (mac, signal key) -> SignalRow
        self.keypoints = []
        self.marks = []             # [t_plot, name, color, [(plot, line), ...]]
        if cfg.data.get("signal_defaults_version") != SIGNAL_DEFAULTS_VERSION:
            # Defaults changed (only ECG and EDA on): forget per-signal choices saved under the old ones.
            cfg.data["signals"] = {}
            cfg.data["signal_defaults_version"] = SIGNAL_DEFAULTS_VERSION
            cfg.save()

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)

        # ---- toolbar ----
        bar = QHBoxLayout()
        self.btn_back = QPushButton("◀  Devices")
        self.btn_back.setToolTip("Stop streaming, disconnect and go back to the device list")
        self.btn_back.clicked.connect(self._on_back)
        bar.addWidget(self.btn_back)
        bar.addSpacing(6)
        bar.addWidget(BrandHeader("Live data", compact=True))
        bar.addSpacing(16)
        self.btn_panel = set_role(QPushButton("Signals ◂"), "small")
        self.btn_panel.setCheckable(True)
        self.btn_panel.setChecked(True)
        self.btn_panel.setToolTip("Show/hide the signals panel (eye = show, dot = record)")
        self.btn_panel.toggled.connect(self._toggle_panel)
        bar.addWidget(self.btn_panel)
        self.win_combo = QComboBox()
        self.win_combo.setToolTip("How many seconds of signal the plots show")
        for sec in (5, 10, 20, 30, 60):
            self.win_combo.addItem(f"{sec} s window", sec)
        self.win_combo.setCurrentIndex(1)
        self.win_combo.currentIndexChanged.connect(lambda _: setattr(self, "window_s",
                                                                     float(self.win_combo.currentData())))
        bar.addWidget(self.win_combo)
        bar.addStretch(1)
        self.btn_info = set_role(QPushButton("ⓘ Timestamps"), "small")
        self.btn_info.setToolTip("Which system clocks are recorded and how to synchronise other systems")
        self.btn_info.clicked.connect(lambda: TimingInfoDialog(self).exec())
        bar.addWidget(self.btn_info)
        self.btn_folder = set_role(QPushButton("Folder…"), "small")
        self.btn_folder.clicked.connect(self._choose_folder)
        bar.addWidget(self.btn_folder)
        self.btn_rec = QPushButton("●  Start recording")
        self.btn_rec.setToolTip("Start saving the signals marked with the dot in the Signals panel. Keypoints can "
                                "be marked while recording.")
        set_role(self.btn_rec, "record")
        self.btn_rec.clicked.connect(self.start_recording)
        self.btn_stop = set_role(QPushButton("■  Stop recording"), "stop")
        self.btn_stop.setToolTip("Stop the recording: you then see the quality checks, add a name and notes, and save")
        self.btn_stop.clicked.connect(self.stop_recording)
        bar.addWidget(self.btn_rec)
        bar.addWidget(self.btn_stop)
        root.addLayout(bar)

        self.rec_label = QLabel()
        self.rec_label.setStyleSheet(f"color: {CRITICAL}; font-weight: 800; font-size: {FONT_L}pt;")
        self.rec_label.setVisible(False)
        self.kp_feedback = QLabel()
        self.kp_feedback.setStyleSheet(f"color: {INK}; font-size: {FONT_BASE}pt; font-weight: 600;")
        rec_row = QHBoxLayout()
        rec_row.addWidget(self.rec_label)
        rec_row.addSpacing(16)
        rec_row.addWidget(self.kp_feedback)
        rec_row.addStretch(1)
        root.addLayout(rec_row)

        # ---- keypoints bar (wraps to a second row on narrow windows) ----
        kp_card = Card("Keypoints", "While recording, press a button (or its number key 1–9) to mark this moment "
                                    "on every Shimmer.")
        kp_holder = QWidget()
        self.kp_bar = FlowLayout(kp_holder, spacing=8)
        kp_card.body.addWidget(kp_holder)
        root.addWidget(kp_card)
        self.kp_buttons = []
        self._shortcuts = []

        # ---- device cards ----
        self.cards_row = QHBoxLayout()
        self.cards_row.setSpacing(8)
        root.addLayout(self.cards_row)
        self.cards = []

        # ---- big recording-loss panel ----
        self.overlay = QFrame()
        ov = QVBoxLayout(self.overlay)
        self.overlay_title = QLabel()
        self.overlay_title.setStyleSheet(f"color: white; font-size: {FONT_XXL}pt; font-weight: 800;")
        self.overlay_title.setAlignment(Qt.AlignCenter)
        self.overlay_title.setWordWrap(True)          # never widen the window on small screens
        self.overlay_text = QLabel()
        self.overlay_text.setStyleSheet(f"color: white; font-size: {FONT_M}pt;")
        self.overlay_text.setAlignment(Qt.AlignCenter)
        self.overlay_text.setWordWrap(True)
        ov.addWidget(self.overlay_title)
        ov.addWidget(self.overlay_text)
        self.overlay.setVisible(False)
        root.addWidget(self.overlay)

        self.alerts = AlertArea()
        root.addWidget(self.alerts)

        # ---- signals panel + plots ----
        body = QHBoxLayout()
        self.panel = QWidget()
        self.panel_lay = QVBoxLayout(self.panel)
        self.panel_lay.setContentsMargins(0, 0, 6, 0)
        self.panel_scroll = QScrollArea()
        self.panel_scroll.setWidgetResizable(True)
        self.panel_scroll.setFrameShape(QFrame.NoFrame)
        self.panel_scroll.setWidget(self.panel)
        self.panel_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.left = QWidget()
        self.left.setFixedWidth(PANEL_WIDTH)
        left_lay = QVBoxLayout(self.left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(10)
        left_lay.addWidget(self._values_box())
        sig_card = Card("Signals", "Eye: show in the plots · Dot: record. The dots lock while recording.")
        sig_card.body.addWidget(self.panel_scroll, 1)
        left_lay.addWidget(sig_card, 1)
        body.addWidget(self.left)
        plots_col = QVBoxLayout()
        self.glw = pg.GraphicsLayoutWidget()
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setWidget(self.glw)
        plots_col.addWidget(self.scroll, 1)
        self.placeholder = QLabel("Connecting… plots appear as soon as a device streams.")
        self.placeholder.setAlignment(Qt.AlignCenter)
        self.placeholder.setStyleSheet(f"color: {MUTED}; font-size: {FONT_L}pt;")
        plots_col.addWidget(self.placeholder)
        body.addLayout(plots_col, 1)
        root.addLayout(body, 1)

        self.plot_timer = QTimer(self, interval=50, timeout=self._update_plots)    # 20 fps
        self.status_timer = QTimer(self, interval=250, timeout=self._update_status)
        self._update_rec_buttons()

    # ---------------- lifecycle ----------------
    def start(self, devices, plugin_entries=()):
        self.t0 = clocks.perf_ns()
        self.sessions = [ShimmerSession(mac, name, self.cfg, DEVICE_COLORS[i % len(DEVICE_COLORS)], self.t0)
                         for i, (mac, name) in enumerate(devices)]
        self.host = PluginHost(list(plugin_entries))
        self.cards = [DeviceCard(s, self) for s in self.sessions]
        for c in self.cards:
            self.cards_row.addWidget(c)
        self.cards_row.addStretch(1)
        self._built_for = self._panel_for = None
        self._bound = set()
        self._lost_seen.clear()
        self.rows = {}
        self.marks = []
        self.glw.clear()
        self._plots = []
        self._build_keypoint_bar()
        for s in self.sessions:
            s.start()
        self.plot_timer.start()
        self.status_timer.start()
        self._update_status()

    def shutdown(self, on_done):
        """Stop streaming and disconnect all devices (in the background)."""
        self.plot_timer.stop()
        self.status_timer.stop()
        host, self.host = self.host, None
        sessions, self.sessions = self.sessions, []

        def work():
            if host is not None:
                host.stop()
            stop_all(sessions)
        run_bg(work, lambda _: on_done(), name="stop-sessions")
        for lay in (self.cards_row, self.panel_lay):
            while lay.count():
                it = lay.takeAt(0)
                if it.widget():
                    it.widget().deleteLater()
        self.cards = []
        self.rows = {}
        self.glw.clear()
        self._plots = []
        self.marks = []
        self.alerts.sync({})
        self.overlay.setVisible(False)

    def stop_now(self, timeout=8.0):
        """Blocking stop, used when the app closes."""
        if self.host is not None:
            self.host.stop()
        stop_all(self.sessions, timeout=timeout)

    def _on_back(self):
        if self.recorder is not None:
            if QMessageBox.question(self, "Recording in progress",
                                    "A recording is running. Stop it and go back to the device list?") \
                    != QMessageBox.Yes:
                return
            self.stop_recording()
        self.back.emit()

    # ---------------- signal selection (eye / record) ----------------
    def _sig_state(self, mac, key, default_on=True) -> dict:
        return self.cfg.data.setdefault("signals", {}).setdefault(mac, {}).setdefault(
            key, {"visible": default_on, "record": default_on})

    def is_visible(self, mac, key, default_on=True):
        row = self.rows.get((mac, key))
        return row.eye.isChecked() if row else self._sig_state(mac, key, default_on)["visible"]

    def is_recorded(self, mac, key, default_on=True):
        row = self.rows.get((mac, key))
        return row.rec.isChecked() if row else self._sig_state(mac, key, default_on)["record"]

    def _signals_changed(self):
        for (mac, key), row in self.rows.items():
            st = self._sig_state(mac, key)
            st["visible"] = row.eye.isChecked()
            st["record"] = row.rec.isChecked()
        self.cfg.save()
        self._built_for = None                      # rebuild plots with the new visibility

    def _values_box(self):
        """Raw (default) or calibrated values in recordings; locked while recording."""
        box = Card("Values to record")
        v = box.body
        v.setSpacing(4)
        self.values_group = QButtonGroup(self)
        self.rb_raw = QRadioButton("Raw device values (default)")
        self.rb_raw.setToolTip("Save every value exactly as the Shimmer sent it (columns ending in _RAW). The formulas "
                               "to convert them are saved with the recording.")
        self.rb_cal = QRadioButton("Calibrated (mV, µS)")
        self.rb_cal.setToolTip("Save values already converted to physical units (columns ending in _CAL), like a "
                               "Consensys 'Calibrated' export")
        for i, rb in enumerate((self.rb_raw, self.rb_cal)):
            self.values_group.addButton(rb, i)
            v.addWidget(rb)
        (self.rb_cal if self.cfg.data.get("record_values") == VALUES_CAL else self.rb_raw).setChecked(True)
        self.values_group.idToggled.connect(self._values_changed)
        info = set_role(QPushButton("ⓘ How values are calibrated"), "small")
        info.setToolTip("Show the formulas that convert raw values to mV and µS")
        info.clicked.connect(lambda: CalibrationDialog(self).exec())
        v.addWidget(info, 0, Qt.AlignLeft)
        return box

    def _values_changed(self, _id, checked):
        if checked:
            self.cfg.data["record_values"] = VALUES_CAL if self.rb_cal.isChecked() else VALUES_RAW
            self.cfg.save()

    @property
    def record_values(self):
        return VALUES_CAL if self.rb_cal.isChecked() else VALUES_RAW

    def _toggle_panel(self, on):
        self.left.setVisible(on)
        self.btn_panel.setText("Signals ◂" if on else "Signals ▸")

    def _rebuild_panel(self):
        key = tuple((s.mac, s.specs is not None, tuple(b.cls.id for b in self._bindings(s))) for s in self.sessions)
        if key == self._panel_for:
            return
        self._panel_for = key
        while self.panel_lay.count():
            it = self.panel_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self.rows = {}
        for s in self.sessions:
            title = QLabel(f"<span style='color:{s.color}'>●</span> {s.name}")
            title.setStyleSheet(f"padding-top: 12px; font-size: {FONT_M}pt; font-weight: 700;")
            self.panel_lay.addWidget(title)
            if not s.specs:
                w = set_role(QLabel("waiting for connection…"), "muted")
                self.panel_lay.addWidget(w)
                continue
            for sp in s.specs:
                st = self._sig_state(s.mac, sp.key, sp.default_on)
                row = SignalRow(sp.label, st["visible"], st["record"], self._signals_changed)
                self.rows[(s.mac, sp.key)] = row
                self.panel_lay.addWidget(row)
            for b in self._bindings(s):
                for o in b.cls.outputs:
                    k = plugin_key(b, o)
                    st = self._sig_state(s.mac, k)
                    row = SignalRow(f"{b.cls.name}: {o.label}", st["visible"], st["record"], self._signals_changed)
                    self.rows[(s.mac, k)] = row
                    self.panel_lay.addWidget(row)
        self.panel_lay.addStretch(1)
        self._lock_recording_choices(self.recorder is not None)

    def _lock_recording_choices(self, locked):
        for row in self.rows.values():
            row.rec.setEnabled(not locked)
        for rb in (self.rb_raw, self.rb_cal):
            rb.setEnabled(not locked)

    def _bindings(self, s):
        return self.host.bindings_for(s) if self.host else []

    # ---------------- keypoints ----------------
    def _build_keypoint_bar(self):
        while self.kp_bar.count():
            it = self.kp_bar.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        for sc in self._shortcuts:
            sc.setEnabled(False)
            sc.deleteLater()
        self._shortcuts = []
        self.keypoints = kpmod.load(self.cfg)
        self.kp_buttons = []
        for i, k in enumerate(self.keypoints):
            b = set_role(QPushButton(f"{i + 1}   {k.name}" if i < 9 else k.name), "keypoint")
            b.setStyleSheet(f"QPushButton {{ border-left: 10px solid {k.color}; }}")
            b.setToolTip(f"Mark '{k.name}' now" + (f" (key {i + 1})" if i < 9 else "") + " — only while recording")
            b.clicked.connect(lambda _=False, kp=k: self.mark(kp))
            self.kp_bar.addWidget(b)
            self.kp_buttons.append(b)
            if i < 9:
                sc = QShortcut(QKeySequence(str(i + 1)), self, context=Qt.WidgetWithChildrenShortcut)
                sc.activated.connect(lambda kp=k: self.mark(kp))
                self._shortcuts.append(sc)
        self._update_rec_buttons()

    def mark(self, kp):
        if self.recorder is None:
            return
        ev = self.recorder.keypoint(kp)
        t_plot = (clocks.perf_ns() - self.t0) / 1e9
        mark = [t_plot, kp.name, kp.color, []]
        self.marks.append(mark)
        for i, entry in enumerate(self._plots):
            self._add_mark_line(mark, entry["plot"], label=(i == 0))
        when = dt.datetime.fromtimestamp(ev["unix_ns"] / 1e9).strftime("%H:%M:%S")
        self.kp_feedback.setText(f"<span style='color:{kp.color}'>■</span> '{kp.name}' marked at {when}")

    @staticmethod
    def _add_mark_line(mark, plot, label):
        line = pg.InfiniteLine(pos=mark[0], angle=90, movable=False, pen=pg.mkPen(mark[2], width=2),
                               label=mark[1] if label else None,
                               labelOpts={"position": 0.92, "color": mark[2], "fill": (255, 255, 255, 220),
                                          "movable": False})
        if label:
            line.label.setFont(QFont(QApplication.font().family(), int(round(FONT_S)), QFont.Bold))
        plot.addItem(line)
        mark[3].append((plot, line))

    def _prune_marks(self, now):
        keep = []
        for m in self.marks:
            if now - m[0] > MARK_KEEP_S:
                for plot, line in m[3]:
                    plot.removeItem(line)
            else:
                keep.append(m)
        self.marks = keep

    # ---------------- plots ----------------
    def _rebuild_plots(self):
        entries = []
        for s in self.sessions:
            if s.ring is None:
                continue
            for i, sp in enumerate(s.plot_specs):
                if self.is_visible(s.mac, sp.key, sp.default_on):
                    entries.append(("dev", s, None, i, sp))
            for b in self._bindings(s):
                for o in b.cls.outputs:
                    if self.is_visible(s.mac, plugin_key(b, o)):
                        entries.append(("plugin", s, b, o.key, o))
        key = tuple((k, s.mac, id(b), i) for k, s, b, i, _ in entries)
        if key == self._built_for:
            return
        self._built_for = key
        self.glw.clear()
        for m in self.marks:
            m[3].clear()
        self._plots = []
        for row, (kind, s, b, i, spec) in enumerate(entries):
            p = self.glw.addPlot(row=row, col=0)
            # No background grid: its semi-transparent full-area lines were ~2/3 of the
            # paint cost (26 -> 8 ms/frame). Axes with tick labels remain.
            p.setLabel("left", display_unit(spec.unit), **{"font-size": f"{FONT_S}pt"})
            for side in ("left", "bottom"):
                p.getAxis(side).setTickFont(QFont(QApplication.font().family(), int(round(FONT_XS))))
                # Auto SI prefix re-renders the axis label (HTML) on every range change: 20k+/15 s.
                p.getAxis(side).enableAutoSIPrefix(False)
            p.setMouseEnabled(x=False, y=True)
            # Y range is set by _update_plots (sticky_range). pyqtgraph's auto range ran during
            # painting, changed the range and so painted every frame twice.
            p.disableAutoRange()
            # All plots share the time window: only the bottom one shows the time axis.
            p.hideAxis("bottom")
            p.setMenuEnabled(False)
            p.hideButtons()
            # Plots are not X-linked: _update_plots sets every plot to the same window directly.
            # Linking made each scroll propagate to all other plots (n² range updates per frame).
            # Keep width 1: Qt strokes wider pens on dense signals ~30x slower (900 vs 29 ms/frame).
            if kind == "plugin" and spec.style == "points":
                curve = p.plot(pen=pg.mkPen(s.color, width=1), symbol="o", symbolSize=7,
                               symbolBrush=s.color, symbolPen=None)
            else:
                curve = p.plot(pen=pg.mkPen(s.color, width=1))
            # Peak downsampling keeps spikes visible while drawing ~1 point per pixel.
            curve.setClipToView(True)
            curve.setDownsampling(auto=True, method="peak")
            label = f"{b.cls.name}: {spec.label}" if kind == "plugin" else spec.label
            p.setTitle(f"<span style='color:{INK}'><b>{s.name}</b> — {label}</span>", size=f"{FONT_BASE}pt",
                       justify="left")
            # The live value floats in the title row's right corner; it is not part of the
            # layout, so updating it never triggers a relayout of the whole plot grid.
            value = pg.LabelItem(justify="right", color=INK, size=f"{FONT_BASE}pt")
            value.setParentItem(p)
            value.anchor(itemPos=(1, 0), parentPos=(1, 0), offset=(-8, 0))
            entry = {"kind": kind, "session": s, "binding": b, "index": i, "spec": spec, "plot": p,
                     "curve": curve, "value": value, "yr": None, "manual_y": False}
            # Zoomed with the mouse wheel: leave that plot's y range to the user.
            p.vb.sigRangeChangedManually.connect(lambda *_, e=entry: e.__setitem__("manual_y", True))
            self._plots.append(entry)
            for m in self.marks:
                self._add_mark_line(m, p, label=(row == 0))
        if self._plots:
            self._plots[-1]["plot"].showAxis("bottom")
            self._plots[-1]["plot"].setLabel("bottom", "time since live view opened (s)", **{"font-size": f"{FONT_S}pt"})
        self.glw.setMinimumHeight(PLOT_ROW_PX * max(1, len(self._plots)))
        self.placeholder.setVisible(not self._plots)
        self.scroll.setVisible(bool(self._plots))

    def _update_plots(self):
        self._rebuild_plots()
        now = (clocks.perf_ns() - self.t0) / 1e9
        x0 = now - self.window_s
        snaps = {}
        for e in self._plots:
            if e["kind"] == "dev":
                s = e["session"]
                if s.mac not in snaps:
                    t, y = s.ring.snapshot()
                    m = t >= x0 - 1.0
                    snaps[s.mac] = (t[m], y[:, m])
                t, y = snaps[s.mac]
                y = y[e["index"]]
                e["curve"].setData(t, y, connect="finite")
            else:
                t, y = e["binding"].rings[e["index"]].snapshot()
                m = t >= x0 - 30.0              # beat series are sparse: keep the previous points
                t, y = t[m], y[0, m]
                e["curve"].setData(t, y)
            e["plot"].setXRange(x0, now, padding=0)
            if not e["manual_y"]:
                v = y[(t >= x0) & np.isfinite(y)]
                if v.size:
                    r = sticky_range(e["yr"], float(v.min()), float(v.max()))
                    if r is not None:
                        e["yr"] = r
                        e["plot"].setYRange(*r, padding=0)
        self._prune_marks(now)

    # ---------------- status, alerts ----------------
    def plugin_status(self, s) -> str:
        if not self.host or not self.host.entries:
            return ""
        parts = []
        for entry in self.host.entries:
            b = next((x for x in self._bindings(s) if x.cls is entry.cls), None)
            if b is not None:
                if b.error:
                    parts.append(f"<span style='color:{CRITICAL}'>{entry.cls.name} ✖</span>")
                else:
                    first = entry.cls.outputs[0] if entry.cls.outputs else None
                    val = f" {b.last[first.key]:.0f} {first.unit}" if first and first.key in b.last else ""
                    parts.append(f"<span style='color:{GOOD}'>{entry.cls.name} ✔</span>{val}")
            elif (entry.cls.id, s.mac) in self.host.unmatched:
                parts.append(f"{entry.cls.name}: n/a ({self.host.unmatched[(entry.cls.id, s.mac)]})")
        return "Plugins: " + " · ".join(parts) if parts else ""

    def _update_status(self):
        if self.host is not None:
            for s in self.sessions:
                if s.specs and s.mac not in self._bound:
                    self._bound.add(s.mac)
                    self.host.bind(s)
        self._rebuild_panel()
        for c in self.cards:
            c.refresh()
        for e in self._plots:
            if e["kind"] == "dev":
                v = e["session"].last_values.get(e["spec"].key)
            else:
                v = e["binding"].last.get(e["index"])
            val = f"{v:.4g} {display_unit(e['spec'].unit)}" if isinstance(v, (int, float)) and v == v else "—"
            set_if_changed(e["value"], f"now {val}")
        self._update_alerts()
        self._update_overlay()
        self._update_rec_label()
        self._update_rec_buttons()

    def _update_alerts(self):
        a = {}
        now = clocks.perf_ns()
        for s in self.sessions:
            k = s.mac
            if s.state == State.LOST:
                if k not in self._lost_seen:
                    self._lost_seen.add(k)
                    QApplication.beep()
                a[f"lost-{k}"] = ("critical",
                                  f"{s.name}: CONNECTION LOST {fmt_duration(s.seconds_in_state())} ago — reconnect as "
                                  f"soon as possible: check it is on and bring it closer. Reconnecting automatically "
                                  f"(attempt {s.attempt}).", None)
            else:
                self._lost_seen.discard(k)
            if s.state == State.FAILED:
                a[f"failed-{k}"] = ("critical", f"{s.name}: could not connect — {s.detail}. Check it is on "
                                                f"(blue LED blinking), close by and not connected elsewhere.",
                                    ("Retry", s.retry, "Try to connect to this Shimmer again"))
            elif s.state == State.CONNECTING:
                a[f"conn-{k}"] = ("info", f"Connecting to {s.name}… {s.detail}", None)
            if s.state == State.STREAMING:
                if s.detail.startswith("reconnected") and s.seconds_in_state() < 10:
                    a[f"recon-{k}"] = ("info", f"{s.name}: {s.detail} — there is a gap in its data.", None)
                if s.seconds_in_state() > 5 and s.measured_rate < LOW_RATE_FRACTION * s.rate:
                    a[f"rate-{k}"] = ("warning", f"{s.name}: receiving only {s.measured_rate:.0f} of "
                                                 f"{s.rate:.0f} samples/s — weak Bluetooth link, move it closer.",
                                      None)
                sat = [sp.label.split(" (")[0] for sp in s.specs or []
                       if sp.key in s.saturated_since and (now - s.saturated_since[sp.key]) > 1e9]
                if sat:
                    a[f"sat-{k}"] = ("warning", f"{s.name}: {', '.join(sat)} at full scale — electrode/lead "
                                                f"off or not connected?", None)
                if s.no_contact_since is not None and (now - s.no_contact_since) > 2e9:
                    a[f"gsr-{k}"] = ("warning", f"{s.name}: GSR shows no skin contact (&lt; 0.05 µS) — "
                                                f"check the electrodes.", None)
            if s.battery_pct is not None and s.state in (State.STREAMING, State.LOST):
                if s.battery_pct < BATTERY_CRIT:
                    a[f"batt-{k}"] = ("critical", f"{s.name}: battery critically low ({s.battery_pct:.0f}%) — "
                                                  f"it may switch off soon.", None)
                elif s.battery_pct < BATTERY_WARN:
                    a[f"batt-{k}"] = ("warning", f"{s.name}: battery low ({s.battery_pct:.0f}%).", None)
            for b in self._bindings(s):
                if b.error:
                    a[f"plugin-{k}-{b.cls.id}"] = ("warning", f"Plugin {b.cls.name} on {s.name} stopped: {b.error}",
                                                   None)
        rec = self.recorder
        if rec is not None:
            if rec.write_error:
                a["rec-err"] = ("critical", f"Error writing the recording: {rec.write_error}", None)
            free = rec.free_disk_bytes()
            if 0 <= free < DISK_WARN_BYTES:
                a["disk"] = ("warning", f"Low disk space: {free / 1e9:.2f} GB left in {rec.folder}", None)
            missing = [s.name for s in self.sessions if s.specs and s.mac not in rec.stats]
            if missing:
                a["rec-missing"] = ("warning", f"Not part of this recording (connected after it started, or "
                                               f"nothing selected to record): {', '.join(missing)}", None)
        self.alerts.sync(a)

    def _update_overlay(self):
        rec = self.recorder
        lost = [s for s in self.sessions if s.state == State.LOST and s.mac in rec.stats] if rec else []
        if not lost:
            if not self.overlay.isHidden():
                self.overlay.setVisible(False)
            return
        self._blink = not self._blink
        bg = CRITICAL if self._blink else "#8e0000"
        self.overlay.setStyleSheet(f"QFrame {{ background: {bg}; border-radius: 8px; }}")   # blink
        worst = max(lost, key=lambda s: s.seconds_in_state())
        names = ", ".join(s.name for s in lost)
        set_if_changed(self.overlay_title, f"⚠  LOST CONNECTION — {names} — "
                                           f"{fmt_duration(worst.seconds_in_state())} and counting")
        ok = [s.name for s in self.sessions if s.state == State.STREAMING]
        set_if_changed(self.overlay_text,
                       f"No data from {names} is being recorded until it reconnects (trying automatically). "
                       f"Still recording: {', '.join(ok) or 'nothing'}. "
                       f"Check the device is on and close — or stop the recording if the experiment is "
                       f"compromised.")
        if self.overlay.isHidden():
            self.overlay.setVisible(True)

    # ---------------- recording ----------------
    def _update_rec_buttons(self):
        recording = self.recorder is not None
        can = any(s.specs for s in self.sessions)
        self.btn_rec.setEnabled(not recording and can)
        self.btn_stop.setEnabled(recording)
        self.btn_folder.setEnabled(not recording)
        self.btn_folder.setToolTip(f"Recordings are saved in {self.cfg.recordings_dir}")
        for b in self.kp_buttons:
            b.setEnabled(recording)

    def _update_rec_label(self):
        rec = self.recorder
        if rec is None:
            if not self.rec_label.isHidden():
                self.rec_label.setText("")
                self.rec_label.setVisible(False)
            return
        n = sum(st.samples for st in rec.stats.values())
        dot = "●" if int(rec.elapsed_s() * 2) % 2 == 0 else "○"
        set_if_changed(self.rec_label, f"{dot} REC {fmt_duration(rec.elapsed_s())}  ·  {n:,} samples  ·  "
                                       f"{len(rec.keypoints)} keypoint(s)")
        if self.rec_label.isHidden():
            self.rec_label.setVisible(True)

    def _choose_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Folder for recordings", str(self.cfg.recordings_dir))
        if d:
            self.cfg.recordings_dir = d

    def start_recording(self):
        ready = [s for s in self.sessions if s.specs]
        not_ready = [s for s in self.sessions if not s.specs]
        if not ready:
            return
        record_keys = {s.mac: {sp.key for sp in s.specs if self.is_recorded(s.mac, sp.key, sp.default_on)}
                       for s in ready}
        plugin_outputs = {}
        for s in ready:
            for b in self._bindings(s):
                if b.error is None:
                    plugin_outputs[b] = {o.key for o in b.cls.outputs if self.is_recorded(s.mac, plugin_key(b, o))}
        if not any(record_keys.values()) and not any(plugin_outputs.values()):
            QMessageBox.warning(self, "Start recording", "Nothing is selected for recording (the dot buttons in "
                                                         "the Signals panel).")
            return
        if not_ready:
            names = ", ".join(s.name for s in not_ready)
            if QMessageBox.question(self, "Start recording",
                                    f"{names} {'is' if len(not_ready) == 1 else 'are'} not connected and will NOT be "
                                    f"part of this recording.\n\nStart anyway?") != QMessageBox.Yes:
                return
        lost = [s.name for s in ready if s.state != State.STREAMING]
        if lost and QMessageBox.question(self, "Start recording",
                                         f"{', '.join(lost)} currently has no connection. Its data will be "
                                         f"missing until it reconnects.\n\nStart anyway?") != QMessageBox.Yes:
            return
        try:
            self.recorder = Recorder(self.cfg.recordings_dir, ready, record_keys, plugin_outputs, self.keypoints,
                                     values=self.record_values)
        except OSError as e:
            QMessageBox.critical(self, "Cannot record", f"Could not create the recording in "
                                                        f"{self.cfg.recordings_dir}:\n{e}")
            return
        if self.host is not None:
            self.host.recorder = self.recorder
        self._lock_recording_choices(True)
        self.kp_feedback.setText("")
        self._update_status()

    def stop_recording(self):
        rec, self.recorder = self.recorder, None
        if rec is None:
            return
        if self.host is not None:
            self.host.recorder = None
        folder = rec.stop()
        self._lock_recording_choices(False)
        self._update_status()
        SaveRecordingDialog(self, folder).exec()

    @property
    def recording(self):
        return self.recorder is not None
