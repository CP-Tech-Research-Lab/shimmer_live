"""Main window: devices page and live page in a stack."""
import logging

from PySide6.QtWidgets import QMainWindow, QMessageBox, QStackedWidget

from ..bt import BackendUnavailable, get_backend
from ..session import stop_all
from .devices_page import DevicesPage
from .live_page import LivePage

log = logging.getLogger("shimmerlive.main")


class MainWindow(QMainWindow):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.setWindowTitle("Shimmer Live")
        self.resize(1320, 880)
        try:
            backend, err = get_backend(), None
        except BackendUnavailable as e:
            backend, err = None, str(e)
            log.warning("Bluetooth backend unavailable: %s", e)
        self.stack = QStackedWidget()
        self.devices = DevicesPage(cfg, backend, err)
        self.live = LivePage(cfg)
        self.stack.addWidget(self.devices)
        self.stack.addWidget(self.live)
        self.setCentralWidget(self.stack)
        self.devices.show_live.connect(self._open_live)
        self.live.back.connect(self._close_live)
        self.devices.refresh()

    def _open_live(self, devices):
        log.info("live view: %s", ", ".join(n for _, n in devices))
        self.live.start(devices)
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
        stop_all(self.live.sessions, timeout=8.0)      # send stop-streaming before exiting
        ev.accept()
