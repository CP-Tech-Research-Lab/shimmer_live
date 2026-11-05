"""Update a docked Shimmer's firmware from the app (procedure: shimmer_live/fw_update.py)."""
import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QDialog, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit, QProgressBar,
                               QPushButton, QVBoxLayout)

from .. import fw_update
from .common import CRITICAL, GOOD, MUTED, WARNING, run_bg, set_role, ui_call

log = logging.getLogger("shimmerlive.ui.firmware")


class FirmwareDialog(QDialog):
    def __init__(self, parent, watcher, docked, name: str, current: str):
        """docked: base_panel.Docked for the Shimmer (its base link and slot)."""
        super().__init__(parent)
        self.watcher, self.docked, self.name, self.current = watcher, docked, name, current
        self.running = False
        self.setWindowTitle(f"Update firmware — {name}")
        self.setMinimumWidth(620)
        lay = QVBoxLayout(self)
        where = watcher.where(docked.unit, docked.slot)
        head = QLabel(f"<b>{name}</b> · {where} · now running <b>{current or 'unknown'}</b>")
        head.setTextFormat(Qt.RichText)
        lay.addWidget(head)

        row = QHBoxLayout()
        row.addWidget(QLabel("New firmware:"))
        self.choice = QComboBox()
        self.choice.setToolTip("Only official Shimmer images whose checksum the app knows are offered. Files live in "
                               f"{fw_update.FIRMWARE_DIR}")
        row.addWidget(self.choice, 1)
        lay.addLayout(row)
        self._fill_choices()
        self.choice.currentIndexChanged.connect(self._describe)

        self.info = QLabel()
        self.info.setWordWrap(True)
        self.info.setTextFormat(Qt.RichText)
        lay.addWidget(self.info)
        warn = QLabel("<b>Before you start:</b> keep the Shimmer in the base and switched on, and don't unplug the "
                      "base or its USB cable (about 1 minute). Its configuration and calibration are saved first; if "
                      "anything goes wrong the update can simply be run again. <b>Afterwards:</b> switch the Shimmer "
                      "off and on once, then press <i>Test connection</i>.")
        warn.setWordWrap(True)
        warn.setTextFormat(Qt.RichText)
        lay.addWidget(warn)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setVisible(False)
        lay.addWidget(self.bar)
        self.status = QLabel()
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(140)
        self.log.setVisible(False)
        lay.addWidget(self.log)

        btns = QHBoxLayout()
        btns.addStretch(1)
        self.btn_close = QPushButton("Close")
        self.btn_close.clicked.connect(self.reject)
        self.btn_go = set_role(QPushButton("Update firmware"), "primary")
        self.btn_go.setToolTip("Back up the configuration, write and verify the new firmware, restore the configuration")
        self.btn_go.clicked.connect(self._start)
        btns.addWidget(self.btn_close)
        btns.addWidget(self.btn_go)
        lay.addLayout(btns)
        self._describe()

    def _fill_choices(self):
        local = {img.tag: path for img, path in fw_update.local_images()}
        for img in fw_update.KNOWN_IMAGES:
            if img.tag in local or img is fw_update.RECOMMENDED:
                text = f"LogAndStream {img.version}" + ("  (latest)" if img is fw_update.RECOMMENDED else "")
                if img.tag not in local:
                    text += "  — will be downloaded from Shimmer's GitHub"
                self.choice.addItem(text, (img, local.get(img.tag)))

    def _describe(self):
        img, path = self.choice.currentData() or (None, None)
        if img is None:
            self.info.setText("")
            self.btn_go.setEnabled(False)
            return
        parts = []
        if img.note:
            parts.append(img.note)
        if not img.published_sha:
            parts.append("checksum taken from our own download (Shimmer publishes none for this old release)")
        try:
            cur = fw_update.version_tuple(self.current)
            new = fw_update.version_tuple(img.version)
            if cur == new:
                parts.append(f"<span style='color:{MUTED}'>already running this version</span>")
            elif cur < (1, 0, 0) <= new:
                parts.append("from 0.x to 1.x: the 'Bluetooth disabled' setting, ignored until now, is cleared so "
                             "Bluetooth keeps working")
        except (ValueError, IndexError):
            pass
        self.info.setText(" · ".join(parts))
        self.btn_go.setEnabled(not self.running)

    def _start(self):
        img, path = self.choice.currentData()
        if QMessageBox.question(self, "Update firmware",
                                f"Update {self.name} from {self.current} to LogAndStream {img.version}?\n\n"
                                "Keep it in the base and don't unplug anything for about a minute.") != QMessageBox.Yes:
            return
        self.running = True
        self.watcher.updating = True                       # the base's ports are needed exclusively
        for w in (self.btn_go, self.btn_close, self.choice):
            w.setEnabled(False)
        self.bar.setVisible(True)
        self.log.setVisible(True)
        link, slot, mac = self.docked.link, self.docked.slot, self.docked.info.mac

        def progress(text):
            ui_call(self._progress, text)

        def work():
            p = path
            if p is None:
                progress("downloading from Shimmer's GitHub…")
                p = fw_update.download(img)
            return fw_update.update(link, slot, mac, p, img.sha256, progress)

        run_bg(work, self._done, self._failed, "firmware-update")

    def _progress(self, text):
        if text.startswith("writing… "):
            self.bar.setValue(int(text.split()[-1].rstrip("%")))
        else:
            self.log.appendPlainText(text)
        self.status.setText(text)

    def _finish(self):
        self.running = False
        self.watcher.updating = False
        self.watcher.rescan()                              # read the slot again: new firmware version
        self.btn_close.setEnabled(True)
        self.btn_close.setText("Close")

    def _done(self, r):
        self._finish()
        self.bar.setValue(100)
        for n in r.notes:
            self.log.appendPlainText(n)
        for off, label, want, got in r.changed:
            self.log.appendPlainText(f"the new firmware adjusted byte {off} ({label})")
        self.status.setText(f"<span style='color:{GOOD}; font-weight:700'>Done: {r.before} → {r.after}</span> in "
                            f"{r.seconds:.0f} s. <b>Now switch {self.name} off and on once</b>, then press "
                            f"<i>Test connection</i>. Backup: {r.backup_dir}")
        self.status.setTextFormat(Qt.RichText)
        log.info("%s firmware updated: %s -> %s", self.name, r.before, r.after)

    def _failed(self, e):
        self._finish()
        self.status.setTextFormat(Qt.RichText)
        self.status.setText(f"<span style='color:{CRITICAL}; font-weight:700'>Update failed:</span> {e}")
        self.log.appendPlainText(f"FAILED: {e}")
        self.btn_go.setEnabled(True)
        self.choice.setEnabled(True)
        log.warning("%s firmware update failed: %s", self.name, e)
