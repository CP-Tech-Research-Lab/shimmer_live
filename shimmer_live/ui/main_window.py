"""Main window: devices page and live page in a stack."""
import logging

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMainWindow, QMessageBox, QStackedWidget

from .. import export
from ..bt import BackendUnavailable, get_backend
from .devices_page import DevicesPage
from .dialogs import SaveRecordingDialog
from .icons import app_icon
from .live_page import LivePage
from .plugins_page import PluginsPage

log = logging.getLogger("shimmerlive.main")


class MainWindow(QMainWindow):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.setWindowTitle("Shimmer Live")
        self.setWindowIcon(app_icon())
        self.resize(1320, 880)
        try:
            backend, err = get_backend(), None
        except BackendUnavailable as e:
            backend, err = None, str(e)
            log.warning("Bluetooth backend unavailable: %s", e)
        self.stack = QStackedWidget()
        self.devices = DevicesPage(cfg, backend, err)
        self.live = LivePage(cfg)
        self.plugins = PluginsPage(cfg)
        for page in (self.devices, self.live, self.plugins):
            self.stack.addWidget(page)
        self.setCentralWidget(self.stack)
        self.devices.show_live.connect(self._open_live)
        self.devices.show_plugins.connect(lambda: self.stack.setCurrentWidget(self.plugins))
        self.plugins.back.connect(lambda: self.stack.setCurrentWidget(self.devices))
        self.live.back.connect(self._close_live)
        self.devices.refresh()
        QTimer.singleShot(500, self._recover_unsaved)

    def _recover_unsaved(self):
        """Offer to save recordings left unsaved because the app closed while recording."""
        for folder in export.unfinished(self.cfg.recordings_dir):
            if QMessageBox.question(self, "Unsaved recording found",
                                    f"A recording was not saved because the app closed while recording:\n"
                                    f"{folder.name.lstrip('.').removesuffix('.partial')}\n\nRecover and save it now?"
                                    ) == QMessageBox.Yes:
                SaveRecordingDialog(self, folder, recovered=True).exec()

    def _open_live(self, devices):
        log.info("live view: %s", ", ".join(n for _, n in devices))
        self.live.start(devices, self.plugins.enabled_entries())
        self.stack.setCurrentWidget(self.live)

    def _close_live(self):
        self.stack.setCurrentWidget(self.devices)
        self.devices.set_live_blocked(True, "Disconnecting from the previous live session…")
        self.live.shutdown(lambda: (self.devices.set_live_blocked(False), self.devices.refresh()))

    def closeEvent(self, ev):
        if self.live.recording:
            if QMessageBox.question(self, "Recording in progress",
                                    "A recording is running. Stop it, save it and quit?") != QMessageBox.Yes:
                ev.ignore()
                return
            self.live.stop_recording()
        self.live.stop_now(timeout=8.0)                # send stop-streaming before exiting
        ev.accept()
