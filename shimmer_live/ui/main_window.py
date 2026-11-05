"""Main window: devices page and live page in a stack."""
import concurrent.futures
import logging

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QMainWindow, QMessageBox, QStackedWidget

from .. import export
from .. import keypoints as kpmod
from .. import kp_server, sdlog
from ..bt import BackendUnavailable, get_backend
from .devices_page import DevicesPage
from .branding import APP_NAME, lab_name
from .common import fit_to_screen, ui_call
from .dialogs import SaveRecordingDialog
from .icons import app_icon
from .live_page import LivePage
from .plugins_page import PluginsPage
from .sd_page import SdPage

log = logging.getLogger("shimmerlive.main")


class MainWindow(QMainWindow):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.setWindowTitle(f"{lab_name()} — {APP_NAME}")
        self.setWindowIcon(app_icon())
        fit_to_screen(self, 1480, 940)
        try:
            backend, err = get_backend(), None
        except BackendUnavailable as e:
            backend, err = None, str(e)
            log.warning("Bluetooth backend unavailable: %s", e)
        self.stack = QStackedWidget()
        self.devices = DevicesPage(cfg, backend, err)
        self.live = LivePage(cfg)
        self.plugins = PluginsPage(cfg)
        self.sd = SdPage(cfg, self.devices.base)
        for page in (self.devices, self.live, self.plugins, self.sd):
            self.stack.addWidget(page)
        self.setCentralWidget(self.stack)
        self.stack.currentChanged.connect(lambda _: QTimer.singleShot(0, self._keep_on_screen))
        self.devices.show_live.connect(self._open_live)
        self.devices.show_plugins.connect(lambda: self.stack.setCurrentWidget(self.plugins))
        self.plugins.back.connect(lambda: self.stack.setCurrentWidget(self.devices))
        self.live.back.connect(self._close_live)
        self.devices.start_sd.connect(self._start_sd)
        self.devices.open_sd.connect(self._open_sd)
        self.sd.back.connect(self._close_sd)
        self.devices.refresh()
        self.kp_server = kp_server.KeypointServer(self._tcp_keypoint)
        self.devices.keypoints.attach_server(self.kp_server)
        QTimer.singleShot(500, self._recover_unsaved)
        full = QShortcut(QKeySequence(Qt.Key_F11), self)
        full.activated.connect(lambda: self.showMaximized() if self.isFullScreen() else self.showFullScreen())

    def _keep_on_screen(self):
        """Never let a page push the window past the screen edge (the close button must stay visible)."""
        screen = self.screen().availableGeometry() if self.screen() else None
        if screen is None or self.isFullScreen():
            return
        if self.width() > screen.width() or self.height() > screen.height():
            if self.isMaximized():
                self.showNormal()
                self.showMaximized()
            else:
                self.resize(min(self.width(), screen.width()), min(self.height(), screen.height()))

    def _tcp_keypoint(self, target, at):
        """Server thread: mark on the UI thread, answer the other program."""
        fut = concurrent.futures.Future()
        ui_call(lambda: fut.set_result(self._mark_from_tcp(target, at)))
        try:
            return fut.result(timeout=3)
        except concurrent.futures.TimeoutError:
            return "OK pending (the app is busy; marked at the time the message arrived)"

    def _mark_from_tcp(self, target, at):
        kp = kp_server.resolve(kpmod.load(self.cfg), target)
        if kp is None:
            return ("ERR no keypoint on this port" if isinstance(target, int)
                    else f"ERR unknown keypoint '{target}'")
        source = f"tcp:{at['port']}"
        ev = None
        if self.live.recorder is not None:
            ev = self.live.mark(kp, at, source)
        elif sdlog.active(self.cfg):
            ev = self.sd.mark(kp, at, source)
        if ev is None:
            log.info("keypoint '%s' over TCP (port %d) ignored: nothing is recording", kp.name, at["port"])
            return "ERR not recording"
        log.info("keypoint '%s' marked over TCP (port %d)", kp.name, at["port"])
        return f"OK {kp.name} {ev['unix_ns']}"

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

    def _start_sd(self, name, devices):
        self.stack.setCurrentWidget(self.sd)
        self.sd.start_new(name, devices)

    def _open_sd(self):
        self.sd.open()
        self.stack.setCurrentWidget(self.sd)

    def _close_sd(self):
        self.stack.setCurrentWidget(self.devices)
        self.devices._render_all()

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
        self.kp_server.stop()
        ev.accept()
