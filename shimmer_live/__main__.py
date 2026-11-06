"""Entry point: python -m shimmer_live"""
import logging
import logging.handlers
import os
import sys

from PySide6.QtWidgets import QApplication

from .config import Config, config_dir
from .ui.common import STYLESHEET, collect_garbage_on_gui_thread, init_bridge
from .ui.main_window import MainWindow


def smoke_test(win) -> int:
    """SHIMMER_LIVE_SMOKE_TEST=<report file>: start, check that everything the app needs is there, quit with
    exit code 0 (OK) or 1. The Windows build on GitHub runs the finished .exe this way."""
    problems = []
    ids = sorted(e.cls.id for e in win.plugins.entries)
    if not {"hrv", "lsl"} <= set(ids):
        problems.append(f"built-in plugins missing (found {ids})")
    problems += [f"plugin {src}: {msg}" for src, msg in win.plugins.errors]
    try:
        import pylsl
        pylsl.library_version()
    except Exception as e:                      # noqa: BLE001
        problems.append(f"pylsl / liblsl: {e}")
    if sys.platform == "win32":
        try:
            import winrt.windows.devices.bluetooth  # noqa: F401
            import winrt.windows.devices.bluetooth.rfcomm  # noqa: F401
            import winrt.windows.devices.enumeration  # noqa: F401
            import winrt.windows.devices.radios  # noqa: F401
        except Exception as e:                  # noqa: BLE001
            problems.append(f"Windows Bluetooth (winrt): {e}")
    from .ui.icons import LOGO_SVG
    if not LOGO_SVG.exists():
        problems.append(f"logo missing: {LOGO_SVG}")
    import scipy.signal  # noqa: F401  (HRV)
    report = "\n".join(["SMOKE TEST " + ("OK" if not problems else "FAILED"), f"plugins: {ids}"] + problems)
    path = os.environ.get("SHIMMER_LIVE_SMOKE_TEST", "")
    if path not in ("", "1"):
        with open(path, "w", encoding="utf-8") as f:
            f.write(report + "\n")
    print(report)
    return 1 if problems else 0


def main():
    logger = logging.getLogger("shimmerlive")
    logger.setLevel(logging.INFO)
    fh = logging.handlers.RotatingFileHandler(config_dir() / "shimmer_live.log", maxBytes=2_000_000,
                                              backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("%(asctime)s %(name)s: %(message)s", "%H:%M:%S"))
    logger.addHandler(sh)

    app = QApplication(sys.argv)
    app.setApplicationName("Shimmer Live")
    app.setStyle("Fusion")
    try:
        from PySide6.QtCore import Qt
        app.styleHints().setColorScheme(Qt.ColorScheme.Light)     # Qt >= 6.8
    except AttributeError:
        pass
    app.setStyleSheet(STYLESHEET)
    init_bridge()
    gc_timer = collect_garbage_on_gui_thread(app)          # noqa: F841 - kept alive with the app
    win = MainWindow(Config())
    win.showMaximized()                    # fills the screen; F11 toggles true full screen
    if os.environ.get("SHIMMER_LIVE_SMOKE_TEST"):
        from PySide6.QtCore import QTimer
        QTimer.singleShot(3000, lambda: app.exit(smoke_test(win)))
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
