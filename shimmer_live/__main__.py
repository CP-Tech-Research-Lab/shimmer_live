"""Entry point: python -m shimmer_live"""
import logging
import logging.handlers
import sys

from PySide6.QtWidgets import QApplication

from .config import Config, config_dir
from .ui.common import STYLESHEET, init_bridge
from .ui.main_window import MainWindow


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
    win = MainWindow(Config())
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
