"""Live data page: real-time plots, status, alerts and recording."""
import logging

import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QApplication, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel,
                               QMessageBox, QPushButton, QScrollArea, QVBoxLayout, QWidget)

from .. import clocks
from ..recorder import Recorder
from ..session import ShimmerSession, State, stop_all
from .common import (CRITICAL, DEVICE_COLORS, GOOD, INK, MUTED, SURFACE, WARNING, AlertArea,
                     fmt_duration, run_bg, set_if_changed, set_role)
from .device_settings import DeviceInfoDialog
from .dialogs import RecordingSummaryDialog, TimingInfoDialog

log = logging.getLogger("shimmerlive.live")

BATTERY_WARN, BATTERY_CRIT = 20.0, 10.0
LOW_RATE_FRACTION = 0.85
DISK_WARN_BYTES = 1_000_000_000
PLOT_ROW_PX = 170


def display_unit(unit):
    return {"uS": "µS", None: "raw"}.get(unit, unit)


pg.setConfigOptions(antialias=True, background=SURFACE, foreground=MUTED)


class DeviceCard(QFrame):
    def __init__(self, session: ShimmerSession):
        super().__init__()
        self.s = session
        self.setStyleSheet("DeviceCard { background: white; border: 1px solid #dcdcd8; border-radius: 6px; }")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 6, 12, 6)
        sw = QLabel("●")
        sw.setStyleSheet(f"color: {session.color}; font-size: 18pt;")
        lay.addWidget(sw)
        col = QVBoxLayout()
        col.setSpacing(0)
        self.title = QLabel(f"<b>{session.name}</b> <span style='color:{MUTED}'>{session.mac}</span>")
        self.line = QLabel()
        self.line2 = QLabel()
        self.line2.setStyleSheet(f"color: {MUTED};")
        for w in (self.title, self.line, self.line2):
            col.addWidget(w)
        lay.addLayout(col)
        gear = QPushButton("⚙")
        gear.setToolTip("Device info")
        gear.setFixedWidth(34)
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


class LivePage(QWidget):
    back = Signal()

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.sessions: list[ShimmerSession] = []
        self.recorder: Recorder | None = None
        self.t0 = clocks.perf_ns()
        self.window_s = 10.0
        self._plots = []            # (session, series index, PlotItem, curve, spec, value label)
        self._built_for = None
        self._lost_seen = set()
        self._blink = False

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)

        # ---- toolbar ----
        bar = QHBoxLayout()
        self.btn_back = QPushButton("◀  Devices")
        self.btn_back.clicked.connect(self._on_back)
        bar.addWidget(self.btn_back)
        t = QLabel("Live data")
        t.setStyleSheet("font-size: 15pt; font-weight: 700; padding-left: 8px;")
        bar.addWidget(t)
        bar.addSpacing(16)
        bar.addWidget(QLabel("Window:"))
        self.win_combo = QComboBox()
        for s in (5, 10, 20, 30, 60):
            self.win_combo.addItem(f"{s} s", s)
        self.win_combo.setCurrentIndex(1)
        self.win_combo.currentIndexChanged.connect(lambda _: setattr(self, "window_s",
                                                                     float(self.win_combo.currentData())))
        bar.addWidget(self.win_combo)
        bar.addStretch(1)
        self.btn_info = QPushButton("ⓘ Timestamps")
        self.btn_info.setToolTip("Which system clocks are recorded and how to synchronise other systems")
        self.btn_info.clicked.connect(lambda: TimingInfoDialog(self).exec())
        bar.addWidget(self.btn_info)
        self.btn_folder = QPushButton("Folder…")
        self.btn_folder.clicked.connect(self._choose_folder)
        bar.addWidget(self.btn_folder)
        self.btn_rec = QPushButton("●  Start recording")
        set_role(self.btn_rec, "record")
        self.btn_rec.clicked.connect(self.start_recording)
        self.btn_stop = QPushButton("■  Stop recording")
        self.btn_stop.clicked.connect(self.stop_recording)
        bar.addWidget(self.btn_rec)
        bar.addWidget(self.btn_stop)
        root.addLayout(bar)

        self.rec_label = QLabel()
        self.rec_label.setStyleSheet(f"color: {CRITICAL}; font-weight: 700; font-size: 11pt;")
        root.addWidget(self.rec_label)

        # ---- device cards ----
        self.cards_row = QHBoxLayout()
        self.cards_row.setSpacing(8)
        root.addLayout(self.cards_row)
        self.cards = []

        # ---- big recording-loss panel ----
        self.overlay = QFrame()
        ov = QVBoxLayout(self.overlay)
        self.overlay_title = QLabel()
        self.overlay_title.setStyleSheet("color: white; font-size: 22pt; font-weight: 800;")
        self.overlay_title.setAlignment(Qt.AlignCenter)
        self.overlay_text = QLabel()
        self.overlay_text.setStyleSheet("color: white; font-size: 11pt;")
        self.overlay_text.setAlignment(Qt.AlignCenter)
        self.overlay_text.setWordWrap(True)
        ov.addWidget(self.overlay_title)
        ov.addWidget(self.overlay_text)
        self.overlay.setVisible(False)
        root.addWidget(self.overlay)

        # ---- alerts ----
        self.alerts = AlertArea()
        root.addWidget(self.alerts)

        # ---- plots ----
        self.glw = pg.GraphicsLayoutWidget()
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setWidget(self.glw)
        root.addWidget(self.scroll, 1)
        self.placeholder = QLabel("Connecting… plots appear as soon as a device streams.")
        self.placeholder.setAlignment(Qt.AlignCenter)
        self.placeholder.setStyleSheet(f"color: {MUTED}; font-size: 12pt;")
        root.addWidget(self.placeholder)

        self.plot_timer = QTimer(self, interval=50, timeout=self._update_plots)    # 20 fps
        self.status_timer = QTimer(self, interval=250, timeout=self._update_status)
        self._update_rec_buttons()

    # ---------------- lifecycle ----------------
    def start(self, devices):
        self.t0 = clocks.perf_ns()
        self.sessions = [ShimmerSession(mac, name, self.cfg, DEVICE_COLORS[i % len(DEVICE_COLORS)], self.t0)
                         for i, (mac, name) in enumerate(devices)]
        for c in self.cards:
            c.deleteLater()
        self.cards = [DeviceCard(s) for s in self.sessions]
        for c in self.cards:
            self.cards_row.addWidget(c)
        self.cards_row.addStretch(1)
        self._built_for = None
        self._lost_seen.clear()
        self.glw.clear()
        self._plots = []
        for s in self.sessions:
            s.start()
        self.plot_timer.start()
        self.status_timer.start()
        self._update_status()

    def shutdown(self, on_done):
        """Stop streaming and disconnect all devices (in the background)."""
        self.plot_timer.stop()
        self.status_timer.stop()
        sessions, self.sessions = self.sessions, []
        run_bg(lambda: stop_all(sessions), lambda _: on_done(), name="stop-sessions")
        while self.cards_row.count():
            it = self.cards_row.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self.cards = []
        self.glw.clear()
        self._plots = []
        self.alerts.sync({})
        self.overlay.setVisible(False)

    def _on_back(self):
        if self.recorder is not None:
            if QMessageBox.question(self, "Recording in progress",
                                    "A recording is running. Stop it and go back to the device list?") \
                    != QMessageBox.Yes:
                return
            self.stop_recording()
        self.back.emit()

    # ---------------- plots ----------------
    def _rebuild_plots(self):
        ready = tuple(s.mac for s in self.sessions if s.ring is not None)
        if ready == self._built_for:
            return
        self._built_for = ready
        self.glw.clear()
        self._plots = []
        first = None
        row = 0
        for s in self.sessions:
            if s.ring is None:
                continue
            for i, spec in enumerate(s.plot_specs):
                p = self.glw.addPlot(row=row, col=0)
                row += 1
                # No background grid: its semi-transparent full-area lines were ~2/3 of the
                # paint cost (26 -> 8 ms/frame). Axes with tick labels remain.
                p.setLabel("left", display_unit(spec.unit))
                p.setMouseEnabled(x=False, y=True)
                p.enableAutoRange(axis="y")
                p.setAutoVisible(y=True)
                p.setMenuEnabled(False)
                p.hideButtons()
                if first is None:
                    first = p
                else:
                    p.setXLink(first)
                # Keep width 1: Qt strokes wider pens on dense signals ~30x slower (900 vs 29 ms/frame).
                curve = p.plot(pen=pg.mkPen(s.color, width=1))
                # Peak downsampling keeps spikes visible while drawing ~1 point per pixel;
                # without it a 60 s ECG window is 15k points per curve and painting stalls.
                curve.setClipToView(True)
                curve.setDownsampling(auto=True, method="peak")
                p.setTitle(f"<span style='color:{INK}'><b>{s.name}</b> — {spec.label}</span>",
                           size="9pt", justify="left")
                # The live value floats in the title row's right corner; it is not part of the
                # layout, so updating it never triggers a relayout of the whole plot grid.
                value = pg.LabelItem(justify="right", color=MUTED, size="9pt")
                value.setParentItem(p)
                value.anchor(itemPos=(1, 0), parentPos=(1, 0), offset=(-8, 0))
                self._plots.append((s, i, p, curve, spec, value))
        if self._plots:
            self._plots[-1][2].setLabel("bottom", "time since live view opened (s)")
        self.glw.setMinimumHeight(PLOT_ROW_PX * max(1, len(self._plots)))
        self.placeholder.setVisible(not self._plots)
        self.scroll.setVisible(bool(self._plots))

    def _update_plots(self):
        self._rebuild_plots()
        now = (clocks.perf_ns() - self.t0) / 1e9
        x0 = now - self.window_s
        snaps = {}
        for s, i, p, curve, spec, _ in self._plots:
            if s.mac not in snaps:
                t, y = s.ring.snapshot()
                m = t >= x0 - 1.0
                snaps[s.mac] = (t[m], y[:, m])
            t, y = snaps[s.mac]
            curve.setData(t, y[i], connect="finite")
            p.setXRange(x0, now, padding=0)

    # ---------------- status, alerts ----------------
    def _update_status(self):
        for c in self.cards:
            c.refresh()
        for s, i, p, curve, spec, value in self._plots:
            v = s.last_values.get(spec.key)
            val = f"{v:.4g} {display_unit(spec.unit)}" if isinstance(v, (int, float)) and v == v else "—"
            set_if_changed(value, f"now {val}")
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
                                    ("Retry", s.retry))
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
        rec = self.recorder
        if rec is not None:
            if rec.write_error:
                a["rec-err"] = ("critical", f"Error writing the recording: {rec.write_error}", None)
            free = rec.free_disk_bytes()
            if 0 <= free < DISK_WARN_BYTES:
                a["disk"] = ("warning", f"Low disk space: {free / 1e9:.2f} GB left in {rec.folder}", None)
            missing = [s.name for s in self.sessions if s.specs and s.mac not in rec.stats]
            if missing:
                a["rec-missing"] = ("warning", f"Not part of this recording (connected after it started): "
                                               f"{', '.join(missing)}", None)
        self.alerts.sync(a)

    def _update_overlay(self):
        rec = self.recorder
        lost = [s for s in self.sessions if s.state == State.LOST] if rec else []
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
                                       f"{rec.csv_path.name}")
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
            self.recorder = Recorder(self.cfg.recordings_dir, ready)
        except OSError as e:
            QMessageBox.critical(self, "Cannot record", f"Could not create the recording file in "
                                                        f"{self.cfg.recordings_dir}:\n{e}")
            return
        self._update_status()

    def stop_recording(self):
        rec, self.recorder = self.recorder, None
        if rec is None:
            return
        summary = rec.stop()
        self._update_status()
        RecordingSummaryDialog(self, rec, summary).exec()

    @property
    def recording(self):
        return self.recorder is not None
