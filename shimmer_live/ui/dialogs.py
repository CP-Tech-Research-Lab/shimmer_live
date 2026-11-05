"""Dialogs: PIN entry, timestamp information, recording summary."""
import datetime as dt
import html
import os
import subprocess
import sys

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QPlainTextEdit,
                               QLineEdit, QMessageBox, QPushButton, QTableWidget,
                               QTableWidgetItem, QTextBrowser, QVBoxLayout)

from .. import labels
from .. import clocks, export
from ..conversions import CALIBRATION_DOC
from .common import (CRITICAL, FONT_L, FONT_M, GOOD, MUTED, WARNING, fit_to_screen, fmt_duration, run_bg,
                     set_role, style_table)


class PinDialog(QDialog):
    def __init__(self, parent, device_name):
        super().__init__(parent)
        self.setWindowTitle("Bluetooth PIN")
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"<b>{device_name}</b> is asking for a PIN to complete pairing."))
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("PIN")
        self.edit.setMaxLength(16)
        self.edit.setStyleSheet(f"font-size: {FONT_L}pt; padding: 8px;")
        lay.addWidget(self.edit)
        hint = QLabel("Reminder: the PIN of Shimmer devices is <b>1234</b>.")
        hint.setStyleSheet(f"font-size: {FONT_M}pt;")
        lay.addWidget(hint)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.edit.setFocus()

    @staticmethod
    def ask(parent, device_name):
        d = PinDialog(parent, device_name)
        d.raise_()
        d.activateWindow()
        return d.edit.text().strip() if d.exec() == QDialog.Accepted else None


class TimingInfoDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Timestamps in recordings")
        fit_to_screen(self, 860, 760)
        rep = clocks.clock_report()
        lay = QVBoxLayout(self)
        live = QLabel()
        live.setTextInteractionFlags(Qt.TextSelectableByMouse)
        live.setStyleSheet("font-family: monospace; padding: 6px; background: #f4f4f2; border-radius: 4px;")
        lay.addWidget(live)

        def tick():
            u, k = clocks.unix_ns(), clocks.shared_clock_ticks()
            live.setText(f"now   wall clock   = {u / 1e6:.3f} ms since 1970  ({clocks.utc_iso(u)})\n"
                         f"      shared clock = {clocks.shared_clock_seconds(k)!r} s")
        tick()
        self._t = QTimer(self, interval=100, timeout=tick)
        self._t.start()

        hu, sc = rep["host_unix_ns"], rep["shared_clock"]
        html = f"""
<h3>Timestamps in every recorded file</h3>
<table cellpadding="4" border="1" cellspacing="0" style="border-collapse:collapse">
<tr><th>Column</th><th>Unit</th><th>Clock (this machine)</th></tr>
<tr><td><b>&lt;device&gt;_Timestamp_Unix_CAL</b></td><td>ms</td><td>PC wall clock, UTC epoch: <code>{hu['python_call']}</code>
&rarr; <code>{hu['implementation']}</code>. Same convention as Consensys. NTP can adjust it.</td></tr>
<tr><td><b>&lt;device&gt;_Timestamp_Shared_CAL</b></td><td>s</td><td>shared clock: <code>{sc['source']}</code>.
Monotonic, never jumps, origin = system boot, valid on this PC only.</td></tr>
<tr><td><b>&lt;device&gt;_Timestamp_CAL</b></td><td>ms</td><td>the Shimmer's own clock (32768 Hz crystal, resolution
30.5 &micro;s), 0 = first sample of the live session. Sample spacing is exact in this clock.</td></tr>
</table>
<p>Platform: {rep['platform']}, Python {rep['python']}.</p>

<h3>Sampling time, not arrival time</h3>
<p>Bluetooth delays packets by a variable amount (tens to hundreds of ms; packets often arrive in bursts) but never
delivers them early. When a recording is saved, the Shimmer clock is fitted to each PC clock per connection period,
along the lower edge of the arrival times (lower convex hull). The two PC columns therefore hold the estimated
<i>sampling</i> instant of each sample, with Bluetooth latency removed and clock drift corrected.</p>

<h3>Syncing with Unreal Engine and other software on this PC</h3>
<p>On <b>Windows</b> the shared clock is the performance counter, computed exactly like Unreal Engine's</p>
<pre>double USharedClockLibrary::ReadSharedClockSeconds()
{{
    const uint64 Ticks = FPlatformTime::Cycles64();          // QueryPerformanceCounter
    return static_cast&lt;double&gt;(Ticks)
         * FPlatformTime::GetSecondsPerCycle64();            // 1.0 / QueryPerformanceFrequency
}}</pre>
<p>so <code>Timestamp_Shared_CAL</code> can be compared directly with values from that function. On Linux the shared
clock is <code>clock_gettime(CLOCK_MONOTONIC)</code>.</p>

<h3>Syncing with other computers</h3>
<ul>
<li>Use <code>Timestamp_Unix_CAL</code> and make sure all clocks are NTP/PTP synchronised (Linux:
<code>timedatectl</code> / <code>chronyc tracking</code>; Windows: <code>w32tm /query /status</code>).</li>
<li>Keypoints (<code>keypoints.csv</code>) list every mark on all clocks, including each Shimmer's own clock.</li>
<li><code>info.json</code> in each recording repeats the clock sources used.</li>
</ul>
"""
        tb = QTextBrowser()
        tb.setHtml(html)
        lay.addWidget(tb, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


CALIBRATION_HTML = f"""
<h3>Raw or calibrated values</h3>
<p><b>Raw</b> (default): every channel is saved exactly as the Shimmer sent it (integers), with column names ending in
<code>_RAW</code>. Nothing is lost or rounded; you can apply the formulas below, or different ones, later.</p>
<p><b>Calibrated</b>: values are converted to physical units (mV, &micro;S) while recording, with column names ending
in <code>_CAL</code>, like a Consensys "Calibrated" export.</p>
<p>The live plots, the plugins and the quality checks always use calibrated values. Every recording's
<code>info.json</code> lists, for each column, the formula and its parameters (e.g. the ECG gain).</p>
<h3>Formulas</h3>
<p><b>ECG / ExG</b><br>{html.escape(CALIBRATION_DOC['exg'])}</p>
<p><b>GSR / EDA</b><br>{html.escape(CALIBRATION_DOC['gsr'])}</p>
<p><b>Battery</b><br>{html.escape(CALIBRATION_DOC['vbatt'])}</p>
<p><b>Other channels</b><br>{html.escape(CALIBRATION_DOC[None])}</p>
<p>These are the nominal Shimmer3 conversions for these sensors (the same ones Consensys and pyshimmer use for ECG,
GSR+ and battery).</p>
"""


class CalibrationDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("How values are calibrated")
        fit_to_screen(self, 760, 640)
        lay = QVBoxLayout(self)
        tb = QTextBrowser()
        tb.setHtml(CALIBRATION_HTML)
        lay.addWidget(tb)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


def open_folder(path):
    if sys.platform == "win32":
        os.startfile(str(path))     # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


QC_COLORS = {"OK": GOOD, "INFO": "#1f4e8c", "WARNING": WARNING, "FAIL": CRITICAL}


class SaveRecordingDialog(QDialog):
    """Shown at Stop (or when recovering an unsaved recording): summary, quality checks, name, notes."""

    def __init__(self, parent, folder, recovered=False):
        super().__init__(parent)
        self.folder = folder
        self.analysis = None
        self.saved_path = None
        self._busy = True
        self.setWindowTitle("Recovered recording" if recovered else "Save recording")
        fit_to_screen(self, 1080, 860)
        lay = QVBoxLayout(self)
        self.status = QLabel("Analysing the recording: aligning timestamps and running quality checks…")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        if recovered:
            warn = QLabel("This recording was not saved because the app closed while recording. "
                          "Everything written up to that moment is recovered below.")
            warn.setWordWrap(True)
            warn.setStyleSheet(f"color: {WARNING}; font-weight: 700; font-size: {FONT_M}pt;")
            lay.addWidget(warn)
        self.form = QFormLayout()
        lay.addLayout(self.form)

        lay.addWidget(set_role(QLabel("Devices"), "section"))
        cols = ["Device", "Rate", "Samples", "Expected", "Complete", "Disconnects", "Time lost", "Longest gap"]
        self.devices = QTableWidget(0, len(cols))
        self.devices.setHorizontalHeaderLabels(cols)
        self.devices.verticalHeader().setVisible(False)
        self.devices.setEditTriggers(QTableWidget.NoEditTriggers)
        style_table(self.devices)
        lay.addWidget(self.devices)

        qc_head = QHBoxLayout()
        qc_head.addWidget(set_role(QLabel("Quality checks"), "section"))
        qc_head.addWidget(set_role(QLabel("heuristics: they flag likely problems, they are not a clinical "
                                          "assessment"), "muted"))
        qc_head.addStretch(1)
        lay.addLayout(qc_head)
        cols = ["Device", "Signal", "Check", "Result", "Value", "Details"]
        self.qc = QTableWidget(0, len(cols))
        self.qc.setHorizontalHeaderLabels(cols)
        self.qc.verticalHeader().setVisible(False)
        self.qc.setEditTriggers(QTableWidget.NoEditTriggers)
        style_table(self.qc)
        self.qc.horizontalHeader().setStretchLastSection(True)
        lay.addWidget(self.qc, 1)

        row = QHBoxLayout()
        row.addWidget(set_role(QLabel("Name"), "section"))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g. subject03_baseline — the start time is always part of the file name")
        row.addWidget(self.name_edit, 1)
        lay.addLayout(row)
        lay.addWidget(set_role(QLabel("Notes (saved in the file)"), "section"))
        self.notes = QPlainTextEdit()
        self.notes.setPlaceholderText("Anything worth remembering about this session: participant state, "
                                      "electrode issues, interruptions…")
        self.notes.setMinimumHeight(80)
        self.notes.setMaximumHeight(110)
        if recovered:
            self.notes.setPlainText("[recovered after the app closed while recording]\n")
        self.status.setStyleSheet(f"font-size: {FONT_M}pt; font-weight: 700;")
        lay.addWidget(self.notes)

        btns = QHBoxLayout()
        self.btn_discard = QPushButton("Discard recording…")
        self.btn_discard.setToolTip("Delete this recording permanently (you are asked to confirm)")
        self.btn_discard.clicked.connect(self._discard)
        btns.addWidget(self.btn_discard)
        btns.addStretch(1)
        self.btn_folder = QPushButton("Open folder")
        self.btn_folder.setToolTip("Open the folder where recordings are saved")
        self.btn_folder.clicked.connect(lambda: open_folder(self.folder.parent))
        btns.addWidget(self.btn_folder)
        self.btn_save = set_role(QPushButton("Save recording"), "primary")
        self.btn_save.setToolTip("Write the recording as one .zip file (CSV per Shimmer, keypoints, quality checks, "
                                 "notes), named with its start time and the name above")
        self.btn_save.setDefault(True)
        self.btn_save.clicked.connect(self._save)
        btns.addWidget(self.btn_save)
        lay.addLayout(btns)
        self._enable(False)
        run_bg(lambda: export.analyze(folder), self._analysed, self._analysis_failed, "analyse")

    def _enable(self, on):
        self._busy = not on
        for b in (self.btn_save, self.btn_discard):
            b.setEnabled(on)

    def _analysis_failed(self, e):
        self._busy = False
        self.btn_discard.setEnabled(True)
        self.status.setText(f"✖ Could not analyse the recording: {e}\nThe raw working files are kept in {self.folder}.")
        self.status.setStyleSheet(f"color: {CRITICAL}; font-weight: 700; font-size: {FONT_M}pt;")

    def _analysed(self, a):
        self.analysis = a
        s = a.summary
        start = dt.datetime.fromtimestamp(s["start_unix_ns"] / 1e9).astimezone()
        self.form.addRow("Duration:", QLabel(f"<b>{fmt_duration(s['duration_s'])}</b> ({s['duration_s']:.1f} s)"))
        self.form.addRow("Started:", QLabel(f"{start:%Y-%m-%d %H:%M:%S} {start:%Z}  (UTC {s['start_utc']})"))
        self.form.addRow("Keypoints:", QLabel(", ".join(a.keypoints["Keypoint"]) if len(a.keypoints) else "none"))
        values = a.session.get("values", "calibrated")
        self.form.addRow("Values:", QLabel("raw device values (formulas to convert them are in info.json)"
                                           if values == "raw" else "calibrated (mV, µS)"))
        if s.get("write_error"):
            err = QLabel(f"Write error during recording: {s['write_error']}")
            err.setStyleSheet(f"color: {CRITICAL}; font-weight: 700; font-size: {FONT_M}pt;")
            self.form.addRow("Problem:", err)
        self.devices.setRowCount(len(a.devices))
        for r, d in enumerate(a.devices):
            st = d.stats
            vals = [labels.plain(d.meta["name"], d.meta.get("label", "")), f"{d.meta['sampling_rate_hz']:g} Hz", f"{st['samples']:,}",
                    f"{st['expected_samples']:,}", f"{st['completeness_pct']:.1f} %", str(st["disconnects"]),
                    f"{st['downtime_s']:.1f} s", f"{st['longest_gap_s']:.1f} s"]
            for c, v in enumerate(vals):
                self.devices.setItem(r, c, QTableWidgetItem(v))
        self.devices.resizeColumnsToContents()
        h = self.devices.horizontalHeader().height() + self.devices.verticalHeader().length() + 6
        self.devices.setFixedHeight(h)
        self.qc.setRowCount(len(a.qc))
        for r, q in enumerate(a.qc):
            for c, v in enumerate([q.device, q.signal, q.check, q.status, q.value, q.detail]):
                it = QTableWidgetItem(v)
                if c == 3:
                    it.setForeground(QColor(QC_COLORS.get(q.status, MUTED)))
                    f = it.font()
                    f.setBold(True)
                    it.setFont(f)
                self.qc.setItem(r, c, it)
        self.qc.resizeColumnsToContents()
        worst = "FAIL" if any(q.status == "FAIL" for q in a.qc) else \
            "WARNING" if any(q.status == "WARNING" for q in a.qc) else "OK"
        msg = {"OK": "✔ All quality checks passed.",
               "WARNING": "▲ Some quality checks raised warnings — see below and consider noting them.",
               "FAIL": "✖ Some quality checks failed — see below."}[worst]
        self.status.setText(msg)
        self.status.setStyleSheet(f"color: {QC_COLORS[worst]}; font-weight: 700; font-size: {FONT_M}pt;")
        self._enable(True)

    def _save(self):
        if self.analysis is None:
            return
        self._enable(False)
        self.status.setText("Saving…")
        self.status.setStyleSheet("")
        name, notes = self.name_edit.text().strip(), self.notes.toPlainText().strip()

        def done(path):
            self.saved_path = path
            self._busy = False
            QMessageBox.information(self, "Recording saved", f"Saved as\n{path}")
            self.accept()

        def failed(e):
            self._enable(True)
            self.status.setText(f"✖ Saving failed: {e}")
            self.status.setStyleSheet(f"color: {CRITICAL}; font-weight: 700; font-size: {FONT_M}pt;")

        run_bg(lambda: export.write_zip(self.analysis, name, notes), done, failed, "save-recording")

    def _discard(self):
        if QMessageBox.question(self, "Discard recording",
                                "Delete this recording permanently? This cannot be undone.",
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        export.discard(self.folder)
        self._busy = False
        self.accept()

    def reject(self):
        """Closing the dialog must never lose data: save under the default name instead."""
        if self._busy:
            return
        if self.saved_path is None and self.folder.exists() and self.analysis is not None:
            if QMessageBox.question(self, "Save recording",
                                    "The recording has not been saved yet. Save it now (with the name and notes "
                                    "entered so far)?") == QMessageBox.Yes:
                self._save()
                return
            return
        super().reject()
