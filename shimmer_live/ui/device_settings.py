"""Device settings (gear button): firmware info and the configuration a Shimmer3 allows changing."""
from dataclasses import replace

from PySide6.QtCore import QRegularExpression, Qt
from PySide6.QtGui import QDoubleValidator, QRegularExpressionValidator
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGridLayout,
                               QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
                               QScrollArea, QVBoxLayout, QWidget)

from ..device_config import (BASE_SENSORS, BT_LIMIT_BYTES_S, BT_WARN_BYTES_S, CONFLICTS, DEVICE_CLOCK_HZ,
                             EXPANSION_SENSORS, GSR_RANGES, NAME_MAX_LEN, RATE_MAX_HZ, RATE_MIN_HZ, RATE_PRESETS,
                             DeviceConfigurator, achievable_rate, bluetooth_load, packet_bytes, with_exg_changes)
from .common import CRITICAL, FONT_M, GOOD, MUTED, WARNING, fit_to_screen, run_bg, set_role, ui_call

EXG_LABELS = {(0, 0): "Chip 1 CH1 (ECG LL-RA)", (0, 1): "Chip 1 CH2 (ECG LA-RA)",
              (1, 0): "Chip 2 CH1 (ExG2 CH1)", (1, 1): "Chip 2 CH2 (ECG Vx-RL)"}
GAINS = [1, 2, 3, 4, 6, 8, 12]


def _fmt_rate(r: float) -> str:
    return f"{r:g} Hz"


class DeviceSettingsDialog(QDialog):
    """Connects to a (non-streaming) Shimmer, shows its configuration and applies changes."""

    def __init__(self, parent, mac, name, cfg, configurator=None):
        super().__init__(parent)
        self.setWindowTitle(f"{name} — device settings")
        fit_to_screen(self, 760, 900)
        self.name = name
        self.conf = configurator or DeviceConfigurator(mac, cfg)
        self.info = None
        self.applied = False
        self._busy = False
        self._closed = False

        lay = QVBoxLayout(self)
        self.status = QLabel(f"Connecting to {name}…")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setVisible(False)
        lay.addWidget(self.scroll, 1)

        row = QHBoxLayout()
        self.btn_clock = QPushButton("Set device clock to PC time")
        self.btn_clock.setToolTip("Sets the Shimmer's real-time clock (stops the blue/green 'RTC not set' blinking)")
        self.btn_clock.clicked.connect(self._set_clock)
        row.addWidget(self.btn_clock)
        row.addStretch(1)
        self.btn_apply = set_role(QPushButton("Apply changes"), "primary")
        self.btn_apply.setToolTip("Write the changed settings to the Shimmer and read them back to check them. "
                                  "You see a summary and confirm first.")
        self.btn_apply.clicked.connect(self._apply)
        self.btn_close = QPushButton("Close")
        self.btn_close.setToolTip("Close without writing anything that was not applied")
        self.btn_close.clicked.connect(self.accept)
        row.addWidget(self.btn_apply)
        row.addWidget(self.btn_close)
        lay.addLayout(row)
        self._set_busy(True)

        def progress(text):
            ui_call(self.status.setText, f"{name}: {text}")

        def work():
            self.conf.connect(progress=progress)
            return self.conf.read()

        run_bg(work, self._loaded, self._failed, "settings-read")

    # ---------------- state ----------------
    def _set_busy(self, busy):
        self._busy = busy
        ready = not busy and self.info is not None
        self.btn_apply.setEnabled(ready)
        self.btn_clock.setEnabled(ready)

    def _failed(self, e):
        if self._closed:
            return
        self._set_busy(False)
        self.status.setText(f"✖ {self.name}: {e}\n\nCheck it is switched on (blue LED blinking), close by and "
                            f"not connected to another program, then open the settings again.")
        self.status.setStyleSheet(f"color: {CRITICAL}; font-weight: 700; font-size: {FONT_M}pt;")

    def _loaded(self, info):
        if self._closed:                 # dialog closed while connecting: release the link now
            run_bg(self.conf.close, name="settings-close")
            return
        self.info = info
        self._set_busy(False)
        self.status.setText(f"✔ Connected to {self.name}. Change what you need and press Apply. "
                            f"Changes are stored on the Shimmer.")
        self.status.setStyleSheet(f"color: {GOOD}; font-weight: 700; font-size: {FONT_M}pt;")
        self._build(info)
        self.scroll.setVisible(True)

    # ---------------- form ----------------
    def _build(self, info):
        s = info.settings
        body = QWidget()
        v = QVBoxLayout(body)

        g = QGroupBox("Device")
        f = QFormLayout(g)
        self.name_edit = QLineEdit(s.name)
        self.name_edit.setMaxLength(NAME_MAX_LEN)
        self.name_edit.setValidator(QRegularExpressionValidator(QRegularExpression(r"[A-Za-z0-9_\-]*")))
        f.addRow("Name:", self.name_edit)
        f.addRow("Address:", QLabel(info.mac))
        f.addRow("Firmware:", QLabel(info.firmware))
        f.addRow("Connection:", QLabel(info.link or "—"))
        batt = f"{info.battery_pct:.0f}%" if info.battery_pct is not None else "—"
        f.addRow("Battery:", QLabel(batt))
        if info.experiment_id:
            f.addRow("Experiment ID:", QLabel(info.experiment_id))
        v.addWidget(g)

        g = QGroupBox("Sampling")
        f = QFormLayout(g)
        self.rate_combo = QComboBox()
        rates = sorted(set(RATE_PRESETS) | {s.sampling_rate}, reverse=True)
        for r in rates:
            self.rate_combo.addItem(_fmt_rate(r) + ("  (current)" if r == s.sampling_rate else ""), r)
        self.rate_combo.setCurrentIndex(rates.index(s.sampling_rate))
        self.rate_combo.currentIndexChanged.connect(self._preset_chosen)
        self.rate_custom = QLineEdit()
        self.rate_custom.setPlaceholderText("custom, e.g. 300")
        self.rate_custom.setValidator(QDoubleValidator(RATE_MIN_HZ, RATE_MAX_HZ, 3))
        self.rate_custom.setMaximumWidth(170)
        self.rate_custom.textChanged.connect(self._update_load)
        rate_row = QHBoxLayout()
        rate_row.addWidget(self.rate_combo, 1)
        rate_row.addWidget(QLabel("or"))
        rate_row.addWidget(self.rate_custom)
        rate_row.addWidget(QLabel("Hz"))
        f.addRow("Sampling rate:", rate_row)
        self.rate_actual = QLabel()
        self.rate_actual.setWordWrap(True)
        f.addRow("Device will use:", self.rate_actual)
        self.load_label = QLabel()
        self.load_label.setWordWrap(True)
        f.addRow("Bluetooth load:", self.load_label)
        hint = QLabel("Recordings always store every sample at this rate. ECG/ExG chips are set to a matching "
                      "internal data rate automatically.")
        hint.setWordWrap(True)
        set_role(hint, "muted")
        f.addRow(hint)
        v.addWidget(g)

        g = QGroupBox("Sensors")
        gv = QVBoxLayout(g)
        self.sensor_boxes = {}
        grid = QGridLayout()
        for i, (sensor, label) in enumerate(BASE_SENSORS):
            cb = QCheckBox(label)
            cb.setChecked(sensor in s.sensors)
            self.sensor_boxes[sensor] = cb
            grid.addWidget(cb, i // 2, i % 2)
        gv.addWidget(QLabel("<b>Main board</b> (every Shimmer3)"))
        gv.addLayout(grid)
        gv.addWidget(QLabel("<b>Expansion board</b>"))
        warn = QLabel("Enable only sensors your Shimmer's expansion board actually has — the others stream "
                      "meaningless numbers.")
        warn.setWordWrap(True)
        warn.setStyleSheet(f"color: {WARNING}; font-weight: 600;")
        gv.addWidget(warn)
        grid = QGridLayout()
        for i, (sensor, label) in enumerate(EXPANSION_SENSORS):
            cb = QCheckBox(label)
            cb.setChecked(sensor in s.sensors)
            self.sensor_boxes[sensor] = cb
            grid.addWidget(cb, i // 2, i % 2)
        gv.addLayout(grid)
        for sensor, cb in self.sensor_boxes.items():
            cb.toggled.connect(lambda on, sn=sensor: self._sensor_toggled(sn, on))
        v.addWidget(g)

        self.gain_combos = {}
        if s.exg_regs:
            g = QGroupBox("ECG / ExG gains")
            f = QFormLayout(g)
            for chip in sorted(s.exg_regs):
                for ch in (0, 1):
                    c = QComboBox()
                    for gain in GAINS:
                        c.addItem(f"× {gain}", gain)
                    c.setCurrentIndex(GAINS.index(s.exg_gain(chip, ch)))
                    self.gain_combos[(chip, ch)] = c
                    f.addRow(EXG_LABELS[(chip, ch)] + ":", c)
            note = QLabel("Higher gain = finer resolution but smaller input range (saturates sooner). "
                          "Shimmer's default for ECG is × 4.")
            note.setWordWrap(True)
            set_role(note, "muted")
            f.addRow(note)
            v.addWidget(g)

        self.gsr_combo = None
        if s.gsr_range is not None:
            g = QGroupBox("GSR")
            f = QFormLayout(g)
            self.gsr_combo = QComboBox()
            for code, label in GSR_RANGES.items():
                self.gsr_combo.addItem(label, code)
            self.gsr_combo.setCurrentIndex(list(GSR_RANGES).index(s.gsr_range))
            f.addRow("Resistance range:", self.gsr_combo)
            v.addWidget(g)

        v.addStretch(1)
        self.scroll.setWidget(body)
        self._update_load()

    def _sensor_toggled(self, sensor, on):
        if on:
            for a, b in CONFLICTS:
                other = b if a == sensor else a if b == sensor else None
                if other is not None and self.sensor_boxes[other].isChecked():
                    self.sensor_boxes[other].setChecked(False)
        self._update_load()

    def _selected_sensors(self):
        return frozenset(s for s, cb in self.sensor_boxes.items() if cb.isChecked())

    def _preset_chosen(self, *_):
        self.rate_custom.blockSignals(True)
        self.rate_custom.clear()                       # a preset replaces any custom value
        self.rate_custom.blockSignals(False)
        self._update_load()

    def _chosen_rate(self) -> float:
        """Rate the device will actually use: a custom value (if valid) or the preset."""
        text = self.rate_custom.text().strip().replace(",", ".")
        try:
            requested = float(text)
        except ValueError:
            requested = None
        if requested is not None and RATE_MIN_HZ <= requested <= RATE_MAX_HZ:
            return achievable_rate(requested)[1]
        return self.rate_combo.currentData()

    def _update_load(self, *_):
        rate = self._chosen_rate()
        divider = round(DEVICE_CLOCK_HZ / rate)
        self.rate_actual.setText(f"<b>{rate:.6g} Hz</b>  ({DEVICE_CLOCK_HZ} Hz clock ÷ {divider}). Any rate from "
                                 f"{RATE_MIN_HZ:g} to {RATE_MAX_HZ:g} Hz can be typed; it is rounded to the nearest "
                                 f"rate the clock can produce.")
        sensors = self._selected_sensors()
        load = bluetooth_load(sensors, rate)
        text = f"≈ {load / 1000:.1f} kB/s ({packet_bytes(sensors)} bytes × {rate:g} packets/s)"
        if load > BT_LIMIT_BYTES_S:
            text += " — too much for older (RN42) Shimmer radios: expect lost samples."
            color = CRITICAL
        elif load > BT_WARN_BYTES_S:
            text += " — close to the limit of older (RN42) radios; watch the received rate in live view."
            color = WARNING
        else:
            color = GOOD
        self.load_label.setText(text)
        self.load_label.setStyleSheet(f"color: {color};")

    def _wanted(self):
        old = self.info.settings
        rate = self._chosen_rate()
        regs = {}
        for chip, r in old.exg_regs.items():
            gains = {ch: self.gain_combos[(chip, ch)].currentData() for ch in (0, 1)}
            # The chip data rate only follows the sampling rate when that changes, so other edits
            # never touch a custom data-rate setting.
            regs[chip] = with_exg_changes(r, sampling_rate=rate if rate != old.sampling_rate else None,
                                          gains=gains)
        return replace(old, name=self.name_edit.text().strip() or old.name, sampling_rate=rate,
                       sensors=self._selected_sensors(), exg_regs=regs,
                       gsr_range=self.gsr_combo.currentData() if self.gsr_combo else old.gsr_range)

    @staticmethod
    def describe_changes(old, new):
        out = []
        if new.name != old.name:
            out.append(f"Name: {old.name} → {new.name}")
        if new.sampling_rate != old.sampling_rate:
            out.append(f"Sampling rate: {old.sampling_rate:g} → {new.sampling_rate:g} Hz")
        for s in sorted(new.sensors - old.sensors, key=lambda x: x.name):
            out.append(f"Enable {s.name}")
        for s in sorted(old.sensors - new.sensors, key=lambda x: x.name):
            out.append(f"Disable {s.name}")
        for chip in new.exg_regs:
            for ch in (0, 1):
                a, b = old.exg_gain(chip, ch), new.exg_gain(chip, ch)
                if a != b:
                    out.append(f"{EXG_LABELS[(chip, ch)]} gain: × {a} → × {b}")
        if new.gsr_range != old.gsr_range:
            out.append(f"GSR range: {GSR_RANGES.get(old.gsr_range)} → {GSR_RANGES.get(new.gsr_range)}")
        return out

    # ---------------- actions ----------------
    def _apply(self):
        old, new = self.info.settings, self._wanted()
        changes = self.describe_changes(old, new)
        if not changes and new.exg_regs == old.exg_regs:
            QMessageBox.information(self, "Device settings", "Nothing to change.")
            return
        if not new.sensors:
            QMessageBox.warning(self, "Device settings", "Enable at least one sensor.")
            return
        if QMessageBox.question(self, "Apply to the Shimmer",
                                "These settings will be written to the Shimmer:\n\n• " +
                                "\n• ".join(changes or ["ExG data rate to match the sampling rate"]) +
                                "\n\nApply?") != QMessageBox.Yes:
            return
        self._set_busy(True)
        self.status.setText("Writing settings…")
        self.status.setStyleSheet("")

        def work():
            problems = self.conf.apply(old, new)
            return problems, self.conf.read()

        def done(res):
            problems, info = res
            self.applied = True
            self._loaded(info)
            if problems:
                self.status.setText("▲ Written, but the device reports different values for: " + ", ".join(problems))
                self.status.setStyleSheet(f"color: {WARNING}; font-weight: 700; font-size: {FONT_M}pt;")
            else:
                self.status.setText("✔ Settings written and verified on the Shimmer.")

        run_bg(work, done, self._failed, "settings-apply")

    def _set_clock(self):
        self._set_busy(True)

        def done(_):
            self._set_busy(False)
            self.status.setText("✔ Device clock set to this PC's time.")
            self.status.setStyleSheet(f"color: {GOOD}; font-weight: 700; font-size: {FONT_M}pt;")

        run_bg(self.conf.set_clock_to_pc, done, self._failed, "settings-clock")

    def done(self, r):
        if self._busy and self.info is not None:
            QMessageBox.information(self, "Device settings", "Please wait until the current operation finishes.")
            return
        self._closed = True
        run_bg(self.conf.close, name="settings-close")
        super().done(r)


class DeviceInfoDialog(QDialog):
    """Read-only info for a device that is streaming (settings can only change when not streaming)."""

    def __init__(self, parent, session):
        super().__init__(parent)
        s = session
        self.setWindowTitle(f"{s.name} — device info")
        lay = QVBoxLayout(self)
        f = QFormLayout()
        f.addRow("Name:", QLabel(s.name))
        f.addRow("Address:", QLabel(s.mac))
        f.addRow("Type:", QLabel(s.kind or "—"))
        f.addRow("Firmware:", QLabel(s.firmware or "—"))
        f.addRow("Connection:", QLabel(s.link_desc or "—"))
        f.addRow("Sampling rate:", QLabel(f"{s.rate:g} Hz (plotted at {s.rate / max(1, s.plot_decim):g} Hz)"
                                          if s.rate else "—"))
        chans = "<br>".join(f"{sp.label} [{sp.unit or 'raw'}]" for sp in (s.specs or []))
        lab = QLabel(chans or "—")
        lab.setTextFormat(Qt.RichText)
        f.addRow("Channels:", lab)
        if s.battery_pct is not None:
            f.addRow("Battery:", QLabel(f"{s.battery_pct:.0f}%"))
        lay.addLayout(f)
        note = QLabel("To change sampling rate, sensors, gains or name: go back to Devices and use the ⚙ button "
                      "(settings can only be changed while the device is not streaming).")
        note.setWordWrap(True)
        set_role(note, "muted")
        lay.addWidget(note)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
