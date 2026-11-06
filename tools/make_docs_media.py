"""Make the screenshots and animated GIFs of the README from the real app, with simulated Shimmers.

    python tools/make_docs_media.py            # writes docs/media/*.png and *.gif (needs ffmpeg for GIFs)

No Bluetooth, no base and no network are used: the app's pages run off-screen, hardware calls are replaced by
no-ops and the Shimmers' data is generated (ECG, PPG, EDA). Names and addresses are made up.
"""
import math
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

os.environ["QT_QPA_PLATFORM"] = "offscreen"
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
OUT = ROOT / "docs" / "media"
OUT.mkdir(parents=True, exist_ok=True)
W, H = 1440, 860
GIF_WIDTH = 1100
FPS = 8

import numpy as np  # noqa: E402
from PySide6.QtCore import QCoreApplication, QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QTabWidget  # noqa: E402

app = QApplication([])
from pyshimmer import EChannelType as E  # noqa: E402

from shimmer_live import clocks, kp_server, labels, sdlog  # noqa: E402
from shimmer_live import keypoints as kpmod  # noqa: E402
from shimmer_live.base import BaseDevice, SlotInfo  # noqa: E402
from shimmer_live.bt.base import BtDevice  # noqa: E402
from shimmer_live.config import Config  # noqa: E402
from shimmer_live.session import ShimmerSession, State  # noqa: E402
from shimmer_live.ui.base_panel import BaseUnit, BaseWatcher  # noqa: E402
from shimmer_live.ui.common import STYLESHEET, init_bridge  # noqa: E402
from shimmer_live.ui.devices_page import DevicesPage  # noqa: E402

app.setStyle("Fusion")
app.setStyleSheet(STYLESHEET)
init_bridge()

# ---------------- never touch hardware or the network ----------------
DevicesPage.refresh = lambda self: None
DevicesPage._presence_scan = lambda self: None
DevicesPage._auto_read = lambda self: None
BaseWatcher.start = lambda self: None
ShimmerSession.start = lambda self: None
ShimmerSession.stop = lambda self, timeout=30.0: None


def _fake_listen(self, base_port, ports, network=False):
    self.base_port, self.targets = base_port, {base_port: None, **{p: i for i, p in enumerate(ports)}}
    self.ports, self.errors = sorted(self.targets), {}


kp_server.KeypointServer.start = _fake_listen
from shimmer_live.plugins import registry  # noqa: E402
_EMPTY = pathlib.Path(tempfile.mkdtemp())
registry.user_plugins_dir = lambda: _EMPTY               # no user plugins, and no personal path in the pictures


class _ShownPath(type(_EMPTY)):
    def __str__(self):
        return "~/.config/shimmer_live/plugins"


registry.user_plugins_dir = lambda: _ShownPath(_EMPTY)
kp_server.KeypointServer.stop = lambda self: None
import shimmer_live.ui.main_window as mw  # noqa: E402


class FakeBackend:
    name = "simulated"

    def adapter_status(self):
        return True, "simulated"


mw.get_backend = lambda: FakeBackend()
from shimmer_live.plugins.app import AppPlugins  # noqa: E402
AppPlugins.sync = lambda self, entries: None        # the LSL plugin shows as ticked but opens no network streams
mw.MainWindow._keep_on_screen = lambda self: None

# ---------------- simulated Shimmers ----------------
SHIMMERS = {  # mac: (name, label, kind)
    "00:06:66:A1:4F:21": ("Shimmer3-4F21", "P01", "ECG/ExG"),
    "00:06:66:A1:7C:0D": ("Shimmer3-7C0D", "P01", "GSR+"),
    "00:06:66:A1:93:E8": ("Shimmer3-93E8", "P02", "ECG/ExG"),
    "00:06:66:A1:B2:54": ("Shimmer3-B254", "", "GSR+"),
    "00:06:66:A1:C6:19": ("Shimmer3-C619", "", "IMU"),
    "00:06:66:A1:D0:7A": ("Shimmer3-D07A", "", "ECG/ExG"),
}
DOCKED = [("00:06:66:A1:E3:42", "Shimmer3-E342"), ("00:06:66:A1:F5:8B", "Shimmer3-F58B")]
TODAY = "2026-11-06T09:12:00Z"


def make_cfg(folder):
    cfg = Config(folder / "cfg.json")
    for mac, (name, label, kind) in SHIMMERS.items():
        rate = 256.0 if kind != "IMU" else 51.2
        cfg.update_device(mac, name=name, label=label, last_check={
            "ok": True, "when": TODAY, "kind": kind, "rate_hz": rate, "battery_pct": 70 + (hash(mac) % 30),
            "firmware": "LogAndStream 1.1.9", "channels": {"ECG/ExG": ["ECG LL-RA", "ECG LA-RA"],
                                                           "GSR+": ["GSR", "PPG"], "IMU": ["Accel"]}[kind]})
    cfg.data["selected"] = list(SHIMMERS)[:3]
    cfg.data["plugins_enabled"] = ["hrv"]           # LSL is ticked only for the Plugins page picture (no streams)
    kp_server.save_settings(cfg, enabled=True, base_port=4000)
    cfg.save()
    return cfg


def beats(t):
    """Beats elapsed at time t: about 70 bpm, gently varying (breathing), always increasing."""
    return t / 0.86 + 0.5 * np.sin(2 * math.pi * t / 25)


def ecg(t):
    t = np.asarray(t, dtype=float)
    ph = beats(t) % 1.0
    g = lambda c, w, a: a * np.exp(-((ph - c) / w) ** 2)
    return (g(0.18, 0.025, 0.12) + g(0.285, 0.008, -0.12) + g(0.30, 0.009, 1.15) + g(0.315, 0.009, -0.25)
            + g(0.55, 0.05, 0.28) - 6.4 + 0.015 * np.sin(2 * math.pi * 0.25 * t))


def ppg(t):
    t = np.asarray(t, dtype=float)
    ph = beats(t - 0.22) % 1.0
    return 1360 + 230 * np.exp(-((ph - 0.25) / 0.09) ** 2) + 70 * np.exp(-((ph - 0.55) / 0.08) ** 2)


def eda(t):
    t = np.asarray(t, dtype=float)
    y = 3.1 - 0.002 * t
    for onset, amp in ((3.0, 0.45), (9.5, 0.3), (40, 0.5), (160, 0.4), (330, 0.6), (480, 0.35)):
        dt = np.clip(t - onset, 0, None)
        y = y + amp * (dt / 1.5) * np.exp(1 - dt / 1.5) * (t > onset)
    return y


def settle():
    """Run what the app's event loop would: pending events and the deletion of replaced widgets."""
    app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()


def grab(widget, path):
    settle()
    widget.grab().save(str(path))
    print("saved", path.relative_to(ROOT))


class Frames:
    """Collect frames, then write an optimised GIF with ffmpeg (palette per GIF)."""

    def __init__(self, name):
        self.name, self.dir, self.n = name, pathlib.Path(tempfile.mkdtemp()), 0

    def add(self, widget, repeat=1):
        settle()
        img = widget.grab()
        for _ in range(repeat):
            img.save(str(self.dir / f"f{self.n:04d}.png"))
            self.n += 1

    def write(self, fps=FPS):
        out = OUT / f"{self.name}.gif"
        if not shutil.which("ffmpeg"):
            print("ffmpeg not found: skipping", out.name)
            return
        vf = (f"fps={fps},scale={GIF_WIDTH}:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96:stats_mode=diff[p];"
              "[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle")
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-framerate", str(fps), "-i", str(self.dir / "f%04d.png"),
                        "-vf", vf, "-loop", "0", str(out)], check=True)
        shutil.rmtree(self.dir, ignore_errors=True)
        print(f"saved {out.relative_to(ROOT)} ({out.stat().st_size / 1e6:.1f} MB, {self.n} frames)")


def main():
    tmp = pathlib.Path(tempfile.mkdtemp())
    cfg = make_cfg(tmp)
    w = mw.MainWindow(cfg)
    w.resize(W, H)
    w.show()
    dp = w.devices
    dp.pill_bt.set("Bluetooth on", "good")
    dp.paired = [BtDevice(m, n, True) for m, (n, _, _) in SHIMMERS.items()]
    u = BaseUnit(BaseDevice("Base6U", "SR8YS557", [], "c", "s", 6), None)
    u.slots = [SlotInfo(2, True, DOCKED[0][0], "LogAndStream 1.1.9", True),
               SlotInfo(4, True, DOCKED[1][0], "LogAndStream 0.16.0", True)]
    dp.base.units = {u.serial: u}
    dp.base.state = "base"
    dp.paired.append(BtDevice(DOCKED[0][0], DOCKED[0][1], True))
    dp.presence = True
    dp._shown = True
    dp.candidates = {"00:06:66:A1:AA:3C": BtDevice("00:06:66:A1:AA:3C", "Shimmer3-AA3C", False)}
    dp._render_all()

    # ======== 1. Finding Shimmers: the start page checks which ones are switched on ========
    gif = Frames("01_finding_shimmers")
    gif.add(w, 6)
    on = {list(SHIMMERS)[i] for i in (0, 1, 2, 3)}
    order = dp._knock_order([m for m in SHIMMERS] + ["00:06:66:A1:AA:3C"])
    for mac in order:
        dp._knock_queue = order[order.index(mac):]
        dp._set_activity("knock")
        dp._render_table()
        gif.add(w, 3 if mac in on or mac.endswith("AA:3C") else 5)
        dp._knock_result(mac, mac in on or mac.endswith("AA:3C"))
        gif.add(w, 2)
    dp._knock_queue = []
    dp._set_activity("")
    dp._render_all()
    gif.add(w, 16)
    gif.write()
    grab(w, OUT / "01_shimmers_page.png")

    # ======== 2. Live view: streaming, recording, keypoints ========
    devs = [(m, SHIMMERS[m][0]) for m in list(SHIMMERS)[:3]]
    w._open_live(devs)
    lp = w.live
    chans = {devs[0][0]: [E.TIMESTAMP, E.EXG_ADS1292R_1_STATUS, E.EXG_ADS1292R_1_CH1_24BIT, E.EXG_ADS1292R_1_CH2_24BIT],
             devs[1][0]: [E.TIMESTAMP, E.INTERNAL_ADC_13, E.GSR_RAW],
             devs[2][0]: [E.TIMESTAMP, E.EXG_ADS1292R_1_STATUS, E.EXG_ADS1292R_1_CH1_24BIT, E.EXG_ADS1292R_1_CH2_24BIT]}
    for s in lp.sessions:
        s.configure(256.0, chans[s.mac], {(0, 0): 4, (0, 1): 4})
        s.firmware, s.battery_pct, s.measured_rate = "LogAndStream 1.1.9", 88.0, 256.0
        s._set_state(State.STREAMING, "streaming")
    app.processEvents()
    lp._update_status()
    row = lp.rows.get((devs[1][0], "INTERNAL_ADC_13"))
    if row is not None:
        row.eye.setChecked(True)                       # show the PPG too
    t_start = (clocks.perf_ns() - lp.t0) / 1e9 - 10
    pushed = {s.mac: t_start for s in lp.sessions}

    def value(s, sp, ti):
        if sp.kind == "exg":
            return float(ecg(ti + (0.3 if s is lp.sessions[2] else 0)) * (0.8 if "CH2" in sp.key else 1))
        if sp.kind == "gsr":
            return float(eda(ti))
        if sp.chan == E.INTERNAL_ADC_13:
            return float(ppg(ti))
        return 128.0

    def feed():
        """Samples up to now: every one to the plugins (HRV), every other one to the plots."""
        now = (clocks.perf_ns() - lp.t0) / 1e9
        for s in lp.sessions:
            t = np.arange(pushed[s.mac], now, 1 / 256)
            pushed[s.mac] = t[-1] + 1 / 256 if len(t) else pushed[s.mac]
            for k, ti in enumerate(t):
                full = [value(s, sp, ti) for sp in s.specs]
                for tap in s.taps:
                    tap.push(ti, 0.0, [full[i] for i in tap.indices])
                if k % 2 == 0:
                    s.ring.push(ti, [value(s, sp, ti) for sp in s.plot_specs])
            s.packets += len(t)

    class FakeRec:
        def __init__(self):
            self.t0, self.keypoints, self.write_error, self.folder = time.monotonic(), [], None, tmp
            self.stats = {s.mac: type("St", (), {"samples": 0})() for s in lp.sessions}

        def elapsed_s(self):
            return time.monotonic() - self.t0

        def free_disk_bytes(self):
            return 10 ** 12

        def keypoint(self, kp, at=None, source=""):
            ev = {"type": "keypoint", "name": kp.name, "color": kp.color, "unix_ns": clocks.unix_ns(),
                  "shared_ticks": clocks.shared_clock_ticks(), "source": source}
            self.keypoints.append(ev)
            return ev

    gif = Frames("02_live_view")
    kps = kpmod.load(cfg)
    script = {2.0: "rec", 4.0: kps[0], 8.0: kps[2], 11.0: kps[4]}
    t0 = time.monotonic()
    done = set()
    while time.monotonic() - t0 < 13.0:
        el = time.monotonic() - t0
        for at, what in script.items():
            if el >= at and at not in done:
                done.add(at)
                if what == "rec":
                    rec = FakeRec()
                    lp.recorder = rec
                    for s in lp.sessions:
                        s.recorder = rec
                    lp._update_rec_buttons()
                else:
                    lp.mark(what)
        feed()
        if lp.recorder is not None:
            for st in lp.recorder.stats.values():
                st.samples = int(lp.recorder.elapsed_s() * 256)
        lp._update_plots()
        lp._update_status()
        lp._update_rec_buttons()
        gif.add(w)
        time.sleep(max(0.0, 1 / FPS - 0.05))
    gif.write()
    grab(w, OUT / "02_live_view.png")
    lp.recorder = None
    for s in lp.sessions:
        s.recorder = None

    # ======== 3. Reviewing a recording: the viewer and its slider ========
    from test_recording_view import view_mod  # noqa: F401  (ensures Qt is set up like the tests)
    from shimmer_live import export
    from shimmer_live.ui.recording_view import RecordingView, WINDOWS
    import pandas as pd
    dur = 600.0
    u0 = 1_793_956_387_000_000_000
    devs_meta, cal = [], {}
    for n, (mac, rate, start, hole, chs) in enumerate((
            (devs[0][0], 256.0, 0.0, None, [("ECG_LL-RA_24BIT_CAL", "ECG LL-RA (gain 4)", "mV", "exg", ecg)]),
            (devs[1][0], 128.0, 0.0, None, [("PPG_A13_CAL", "PPG (A13)", "mV", "other", ppg),
                                            ("GSR_Skin_Conductance_CAL", "GSR skin conductance", "uS", "gsr", eda)]),
            (devs[2][0], 256.0, 21.0, (300, 312), [("ECG_LL-RA_24BIT_CAL", "ECG LL-RA (gain 4)", "mV", "exg",
                                                    lambda t: ecg(t + 0.3))]))):
        t = np.arange(start, dur, 1 / rate)
        if hole:
            t = t[(t < hole[0]) | (t > hole[1])]
        meta_ch = []
        for key, lab, unit, kind, f in chs:
            cal[(mac, key)] = f(t)
            meta_ch.append({"export_name": key, "label": lab, "unit": unit, "kind": kind})
        name, label, _ = SHIMMERS[mac]
        devs_meta.append(export.DeviceData({"name": name, "label": label, "mac": mac, "sampling_rate_hz": rate,
                                            "channels": meta_ch},
                                           pd.DataFrame({"Timestamp_Unix_CAL": u0 / 1e6 + t * 1000}), [], {}))
    kp = pd.DataFrame({"Keypoint": ["rest start", "rest end", "task start", "task end"],
                       "Color": [k.color for k in kps[:2]] + [kps[4].color, kps[5].color],
                       "Timestamp_Unix_CAL": [u0 / 1e6 + s * 1000 for s in (12, 130, 150, 455)]})
    an = export.Analysis(folder=tmp, session={}, stop={}, devices=devs_meta, plugins=[], keypoints=kp,
                         summary={"start_unix_ns": u0, "duration_s": dur}, calibrated=cal)
    v = RecordingView(None, an, "session 1")
    v.resize(W, H)
    v.show()
    gif = Frames("03_recording_viewer")
    gif.add(v, 6)
    for k in range(0, 46):                              # drag the box along the overview
        a = k * 7.0
        v.region.setRegion((a, a + v.window_s))
        v._redraw()
        gif.add(v)
    gif.add(v, 6)
    v.window_combo.setCurrentIndex([s for _, s in WINDOWS].index(10))
    v.time_bar.setValue(int(296 / 0.1))
    v._redraw()
    gif.add(v, 12)                                      # 10 s around the gap of the third Shimmer
    v.window_combo.setCurrentIndex([s for _, s in WINDOWS].index(300))
    v.time_bar.setValue(0)
    v._redraw()
    gif.add(v, 12)
    gif.write()
    v.window_combo.setCurrentIndex([s for _, s in WINDOWS].index(60))
    v.time_bar.setValue(int(120 / 0.1))
    v._redraw()
    grab(v, OUT / "03_recording_viewer.png")
    v.close()
    QCoreApplication.sendPostedEvents(v, QEvent.DeferredDelete)

    # ======== 4. Still pages ========
    w._close_live = lambda: w.stack.setCurrentWidget(w.devices)
    w.stack.setCurrentWidget(dp)
    tabs = dp.findChild(QTabWidget)
    tabs.setCurrentIndex(1)
    dp.keypoints.attach_server(w.kp_server)
    grab(w, OUT / "04_keypoints_tab.png")
    tabs.setCurrentIndex(0)

    cfg.data["plugins_enabled"] = ["hrv", "lsl"]
    w.plugins.reload()
    w.stack.setCurrentWidget(w.plugins)
    grab(w, OUT / "05_plugins.png")

    s = sdlog.new_session(cfg, "session_2", devs, "cal", [{"name": k.name, "color": k.color} for k in kps])
    for mac, _ in devs:
        s["devices"][mac].update(state=sdlog.RECORDING, firmware="LogAndStream 1.1.9",
                                 start={"started_unix": time.time() - 1260, "battery_pct": 91,
                                        "start_offset": {"offset_ms": -41.0, "pc_unix": time.time() - 1260}})
    s["keypoints"] += [{"type": "keypoint", "name": kps[i].name, "color": kps[i].color,
                        "unix_ns": int((time.time() - 1200 + 300 * i) * 1e9), "shared_ticks": 0}
                       for i in (0, 1, 2)]
    cfg.save()
    w.stack.setCurrentWidget(w.sd)
    w.sd.open()
    grab(w, OUT / "06_sd_recording.png")
    sdlog.end_session(cfg)

    from test_ppg_settings import configurator
    from shimmer_live.ui import device_settings as ds
    ds.run_bg = lambda fn, done=None, failed=None, name=None: done(fn()) if done else fn()
    conf = configurator(tmp, power=True)
    conf.connect = lambda **k: None
    conf.close = lambda: None
    labels.set_label(conf.cfg, conf.mac, "P01")
    dlg = ds.DeviceSettingsDialog(dp, conf.mac, "Shimmer3-7C0D", conf.cfg, configurator=conf)
    dlg.show()
    grab(dlg, OUT / "07_device_settings.png")
    dlg.close()
    print("done")


if __name__ == "__main__":
    main()
