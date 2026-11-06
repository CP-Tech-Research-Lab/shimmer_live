"""App-wide parts of plugins (Plugin.app_wide): one instance per ticked plugin, running whatever page is open
(live view, SD recording, start page). They hear every keypoint and can mark keypoints themselves."""
import concurrent.futures
import logging
import threading

from .. import clocks
from .. import keypoints as kpmod
from .. import kp_server
from .api import AppContext

log = logging.getLogger("shimmerlive.plugins")


def at_shared(when):
    """The instant `when` (shared-clock seconds; None = now) on every clock a keypoint records."""
    now = kp_server.now()
    if when is None:
        return now
    ago = clocks.shared_clock_seconds(now["shared_ticks"]) - float(when)
    return {"unix_ns": now["unix_ns"] - int(round(ago * 1e9)), "perf_ns": now["perf_ns"] - int(round(ago * 1e9)),
            "shared_ticks": now["shared_ticks"] - int(round(ago / clocks.SHARED_CLOCK_SECONDS_PER_CYCLE))}


class AppPlugins:
    def __init__(self, cfg, mark, ui_call):
        """mark(target, at, source) -> reply: marks a keypoint on the UI thread (MainWindow._mark_from).
        ui_call(fn): runs fn on the UI thread."""
        self.cfg, self._mark_ui, self._ui_call = cfg, mark, ui_call
        self.running = {}              # plugin id -> instance
        self.errors = {}               # plugin id -> why it stopped
        kpmod.add_listener(self.notify)

    def sync(self, entries):
        """Run the app-wide part of exactly the ticked plugins that have one."""
        want = {e.cls.id: e.cls for e in entries if getattr(e.cls, "app_wide", False)}
        for pid in [p for p in self.running if p not in want]:
            self._stop(pid)
        for pid, cls in want.items():
            if pid in self.running:
                continue
            self.errors.pop(pid, None)
            inst = cls()
            try:
                inst.start_app(AppContext(lambda k, when=None, source="", pid=pid: self._mark(pid, k, when, source),
                                          lambda: [k.name for k in kpmod.load(self.cfg)]))
            except Exception as e:                 # noqa: BLE001 - a plugin must never break the app
                self._fail(pid, f"start failed: {type(e).__name__}: {e}")
                continue
            self.running[pid] = inst
            log.info("plugin %s: app-wide part running", cls.name)

    def notify(self, ev):
        """A keypoint was marked (UI thread)."""
        event = {"name": ev["name"], "color": ev.get("color", ""), "unix_ns": ev["unix_ns"],
                 "time": clocks.shared_clock_seconds(ev["shared_ticks"]), "source": ev.get("source", "")}
        for pid, inst in list(self.running.items()):
            try:
                inst.on_keypoint(dict(event))
            except Exception as e:                 # noqa: BLE001
                self._fail(pid, f"on_keypoint failed: {type(e).__name__}: {e}")
                self._stop(pid)

    def _mark(self, pid, keypoint, when, source):
        at = at_shared(when)
        source = source or f"plugin:{pid}"
        if threading.current_thread() is threading.main_thread():
            return self._mark_ui(str(keypoint), at, source)
        fut = concurrent.futures.Future()
        self._ui_call(lambda: fut.set_result(self._mark_ui(str(keypoint), at, source)))
        try:
            return fut.result(timeout=3)
        except concurrent.futures.TimeoutError:
            return "OK pending (the app is busy; marked at the requested time)"

    def _fail(self, pid, why):
        self.errors[pid] = why
        log.warning("plugin %s stopped: %s", pid, why)

    def _stop(self, pid):
        inst = self.running.pop(pid, None)
        if inst is None:
            return
        try:
            inst.stop_app()
        except Exception as e:                     # noqa: BLE001
            log.warning("plugin %s: stop_app failed: %s", pid, e)

    def stop_all(self):
        for pid in list(self.running):
            self._stop(pid)
        kpmod.remove_listener(self.notify)
