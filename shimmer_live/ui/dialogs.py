"""Dialogs: PIN entry, timestamp information, recording summary."""
import datetime as dt
import os
import subprocess
import sys

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QMessageBox, QPushButton, QTableWidget,
                               QTableWidgetItem, QTextBrowser, QVBoxLayout)

from .. import clocks
from .common import CRITICAL, MUTED, fmt_duration


class PinDialog(QDialog):
    def __init__(self, parent, device_name):
        super().__init__(parent)
        self.setWindowTitle("Bluetooth PIN")
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"<b>{device_name}</b> is asking for a PIN to complete pairing."))
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("PIN")
        self.edit.setMaxLength(16)
        lay.addWidget(self.edit)
        hint = QLabel("Reminder: the PIN of Shimmer devices is <b>1234</b>.")
        hint.setStyleSheet(f"color: {MUTED};")
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
        self.resize(720, 640)
        rep = clocks.clock_report()
        lay = QVBoxLayout(self)
        live = QLabel()
        live.setTextInteractionFlags(Qt.TextSelectableByMouse)
        live.setStyleSheet("font-family: monospace; padding: 6px; background: #f4f4f2; border-radius: 4px;")
        lay.addWidget(live)

        def tick():
            u, k = clocks.unix_ns(), clocks.shared_clock_ticks()
            live.setText(f"now  host_unix_ns       = {u}   ({clocks.utc_iso(u)})\n"
                         f"     shared_clock_ticks = {k}\n"
                         f"     shared_clock_s     = {clocks.shared_clock_seconds(k)!r}")
        tick()
        self._t = QTimer(self, interval=100, timeout=tick)
        self._t.start()

        hu, sc = rep["host_unix_ns"], rep["shared_clock"]
        html = f"""
<h3>System clocks used on this computer</h3>
<p>Platform: <b>{rep['platform']}</b>, Python {rep['python']}</p>
<table cellpadding="4" border="1" cellspacing="0" style="border-collapse:collapse">
<tr><th>CSV column</th><th>Source (this machine)</th><th>Properties</th></tr>
<tr><td><b>host_unix_ns</b><br>host_utc_iso</td><td><code>{hu['python_call']}</code> &rarr;
<code>{hu['implementation']}</code></td><td>wall clock, UTC epoch; adjustable (NTP can step/slew it)</td></tr>
<tr><td><b>shared_clock_ticks</b><br><b>shared_clock_s</b></td><td><code>{sc['source']}</code><br>
seconds per tick = {sc['seconds_per_cycle']!r}</td>
<td>monotonic, never jumps; origin = system boot; valid on this machine only</td></tr>
</table>

<h3>Shared clock (sync with Unreal Engine and other software on this PC)</h3>
<p>On <b>Windows</b> the shared clock is the performance counter, computed exactly like Unreal Engine's</p>
<pre>double USharedClockLibrary::ReadSharedClockSeconds()
{{
    const uint64 Ticks = FPlatformTime::Cycles64();          // QueryPerformanceCounter
    return static_cast&lt;double&gt;(Ticks)
         * FPlatformTime::GetSecondsPerCycle64();            // 1.0 / QueryPerformanceFrequency
}}</pre>
<p>so <code>shared_clock_s</code> in the CSV can be compared directly with values from that function.
<code>shared_clock_ticks</code> is the raw counter (exact integer). On Linux the shared clock is
<code>clock_gettime(CLOCK_MONOTONIC)</code> in nanoseconds.</p>

<h3>Shimmer clock</h3>
<p><b>shimmer_ts_raw</b> is the device's own timestamp sent in every packet: a 24-bit counter of the Shimmer's
32768 Hz crystal (resolution 30.5 &micro;s), wrapping every 512 s. <b>shimmer_ticks</b> is the same counter unwrapped
(0 at the first sample of the session) and <b>shimmer_time_s</b> = shimmer_ticks / 32768.
Sample spacing is exact in this clock; use it for signal processing.</p>

<h3>How to synchronise with other systems</h3>
<ul>
<li>host_* values are taken when this PC <i>receives</i> the packet. Bluetooth adds a variable delay
(typically 10&ndash;100 ms; packets can arrive in bursts), so individual host stamps jitter.</li>
<li>Best estimate of when a sample was taken in host time: fit a straight line
<code>host_unix_ns &asymp; a&middot;shimmer_time_s + b</code> per device (and per connection period) and use the fitted
value; the minimum envelope of host stamps is a good estimate of the offset.</li>
<li>Other software on <b>this</b> PC: read the shared clock (above) and compare with <code>shared_clock_s</code>,
or read the wall clock and compare with host_unix_ns.</li>
<li>Other <b>computers</b>: make sure all clocks are NTP/PTP synchronised (Linux: <code>timedatectl</code> /
<code>chronyc tracking</code>; Windows: <code>w32tm /query /status</code>).</li>
<li>Each recording's <code>.meta.json</code> repeats this information and stores the recording start in all clocks.</li>
</ul>
"""
        tb = QTextBrowser()
        tb.setHtml(html)
        lay.addWidget(tb, 1)
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


class RecordingSummaryDialog(QDialog):
    def __init__(self, parent, recorder, summary):
        super().__init__(parent)
        self.recorder = recorder
        self.setWindowTitle("Recording finished")
        self.resize(820, 460)
        lay = QVBoxLayout(self)

        start_local = dt.datetime.fromtimestamp(recorder.start_unix_ns / 1e9).astimezone()
        form = QFormLayout()
        form.addRow("Duration:", QLabel(f"<b>{fmt_duration(summary['duration_s'])}</b> "
                                        f"({summary['duration_s']:.1f} s)"))
        form.addRow("Started:", QLabel(f"{start_local:%Y-%m-%d %H:%M:%S.%f}"[:-3] +
                                       f" {start_local:%Z}  (UTC {summary['start_utc']})"))
        form.addRow("Size:", QLabel(f"{summary['file_bytes'] / 1e6:.2f} MB"))
        if summary.get("write_error"):
            err = QLabel(f"Write error during recording: {summary['write_error']}")
            err.setStyleSheet(f"color: {CRITICAL}; font-weight: 600;")
            form.addRow("Problem:", err)
        lay.addLayout(form)

        cols = ["Device", "Rate", "Samples", "Expected", "Complete", "Disconnects", "Time lost", "Longest gap"]
        tab = QTableWidget(len(summary["devices"]), len(cols))
        tab.setHorizontalHeaderLabels(cols)
        tab.verticalHeader().setVisible(False)
        tab.setEditTriggers(QTableWidget.NoEditTriggers)
        for r, d in enumerate(summary["devices"]):
            pct = d["completeness_pct"]
            vals = [d["name"], f"{d['rate_hz']:.0f} Hz", f"{d['samples']:,}", f"{d['expected_samples']:,}",
                    f"{pct:.1f} %", str(d["disconnects"]), f"{d['downtime_s']:.1f} s", f"{d['longest_gap_s']:.1f} s"]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if c == 4:
                    it.setForeground(Qt.darkGreen if pct >= 99 else Qt.darkYellow if pct >= 90 else Qt.red)
                tab.setItem(r, c, it)
        tab.resizeColumnsToContents()
        tab.horizontalHeader().setStretchLastSection(True)
        lay.addWidget(tab, 1)
        note = QLabel("“Expected” = sampling rate × duration. Missing samples come from connection losses "
                      "and from the first second after start while Bluetooth buffers fill.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {MUTED};")
        lay.addWidget(note)

        self.path_label = QLabel()
        self.path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.path_label.setWordWrap(True)
        lay.addWidget(self.path_label)
        self._show_path()

        row = QHBoxLayout()
        row.addWidget(QLabel("Name this recording (optional):"))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g. subject03_baseline — the start timestamp is always kept")
        row.addWidget(self.name_edit, 1)
        lay.addLayout(row)

        btns = QHBoxLayout()
        b_open = QPushButton("Open folder")
        b_open.clicked.connect(lambda: open_folder(self.recorder.folder))
        btns.addWidget(b_open)
        btns.addStretch(1)
        b_save = QPushButton("Save && close")
        b_save.setDefault(True)
        b_save.clicked.connect(self._save)
        btns.addWidget(b_save)
        lay.addLayout(btns)

    def _show_path(self):
        self.path_label.setText(f"Saved as <b>{self.recorder.csv_path.name}</b> (+ .meta.json)<br>"
                                f"<span style='color:{MUTED}'>in {self.recorder.folder}</span>")

    def _save(self):
        name = self.name_edit.text().strip()
        if name:
            try:
                self.recorder.rename(name)
            except OSError as e:
                QMessageBox.warning(self, "Rename failed", f"Could not rename the files:\n{e}\n\n"
                                    f"They are kept as {self.recorder.csv_path.name}.")
                return
        self.accept()

