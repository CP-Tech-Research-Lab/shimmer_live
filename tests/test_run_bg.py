"""run_bg never lets a worker thread drop the last reference to what the callbacks capture (widgets)."""
import gc
import os
import threading
import time

import pytest


class Tracked:
    died_on = []

    def __del__(self):
        Tracked.died_on.append(threading.current_thread().name)


@pytest.mark.parametrize("fails", [False, True])
def test_callbacks_are_released_on_the_gui_thread(fails, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    widgets = pytest.importorskip("PySide6.QtWidgets")
    from shimmer_live.ui import common
    from shimmer_live.ui.common import init_bridge, run_bg
    delivered = threading.Event()

    class LateThread(threading.Thread):     # the race: the worker outlives the GUI thread's use of the result
        def __init__(self, target, **kw):
            def late():
                target()
                delivered.wait(3)
            super().__init__(target=late, **kw)

    monkeypatch.setattr(common.threading, "Thread", LateThread)
    app = widgets.QApplication.instance() or widgets.QApplication([])
    init_bridge()
    Tracked.died_on = []
    got = []

    def start():
        held = Tracked()                    # stands for a widget captured by the callbacks

        def work():
            time.sleep(0.05)
            if fails:
                raise RuntimeError("boom")
            return 1

        run_bg(work, lambda r: got.append((held, r)), lambda e: got.append((held, e)), "t")
    start()                                 # the GUI thread keeps no reference of its own
    end = time.time() + 3
    while time.time() < end and not got:
        app.processEvents()
        time.sleep(0.01)
    got.clear()
    delivered.set()
    for _ in range(20):                     # let the worker thread finish and exit
        app.processEvents()
        time.sleep(0.01)
    gc.collect()
    assert Tracked.died_on == [threading.main_thread().name]
