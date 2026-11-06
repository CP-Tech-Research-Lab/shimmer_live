"""LSL (Lab Streaming Layer): Shimmer data and keypoints out, markers from other programs in.

Tick "LSL" on the Plugins page to turn it on.

* Per Shimmer, while live view streams: one LSL stream named like "Shimmer3-A281 (alberto)", with every
  streamed channel in calibrated units (mV, uS, ...), at the Shimmer's sampling rate. Sample timestamps are
  the moments the samples were taken, on the shared clock (= LSL's local_clock()).
* App-wide, whatever page is open (also during SD recordings):
  - a Markers stream "Shimmer Live keypoints": every keypoint marked in the app (buttons, keys, TCP);
  - every Markers stream of other programs (PsychoPy, Unity, ...): a marker whose text is a keypoint name or
    number becomes that keypoint, at the marker's own time (corrected to this PC's clock). Other texts are
    ignored (logged). Markers only count while something is recording, like TCP keypoints.
"""
import logging
import socket
import threading

from ... import clocks
from ..api import Plugin

log = logging.getLogger("shimmerlive.plugins.lsl")

MARKER_STREAM = "Shimmer Live keypoints"
RESOLVE_EVERY_S = 3.0
PULL_TIMEOUT_S = 0.5


def _lsl():
    try:
        import pylsl
    except Exception as e:                       # noqa: BLE001 - reported as this plugin's error only
        raise RuntimeError(f"pylsl is not installed or cannot load ({e}): run the setup again "
                           "(pip install -r requirements.txt)") from e
    return pylsl


def lsl_minus_shared(pylsl) -> float:
    """LSL local_clock() - shared clock seconds: 0 on Windows and Linux (same OS clock), measured anyway."""
    return pylsl.local_clock() - clocks.shared_clock_seconds(clocks.shared_clock_ticks())


def stream_type(inputs) -> str:
    kinds = {i.kind for i in inputs}
    return "ExG" if "exg" in kinds else "GSR" if "gsr" in kinds else "Shimmer"


class LSL(Plugin):
    id = "lsl"
    name = "LSL"
    description = ("Lab Streaming Layer. Publishes each Shimmer as an LSL stream in live view, sends keypoints as an LSL Markers "
                   "stream, and turns markers from other programs (PsychoPy, Unity, ...) into keypoints, also "
                   "during SD recordings. Record everything together with LabRecorder.")
    version = "1.0"
    per_device = True
    app_wide = True

    # ---------------- per Shimmer: its data ----------------
    @classmethod
    def match(cls, signals):
        return (list(signals), "") if signals else (None, "no signals to publish")

    def start(self, inputs):
        pylsl = _lsl()
        self._off = lsl_minus_shared(pylsl)
        d = inputs[0]
        name = f"{d.device} ({d.device_label})" if d.device_label else d.device
        info = pylsl.StreamInfo(name, stream_type(inputs), len(inputs), d.rate, pylsl.cf_float32,
                                f"shimmer-{d.mac.replace(':', '')}")
        desc = info.desc()
        desc.append_child_value("manufacturer", "Shimmer")
        desc.append_child_value("device", d.device)
        desc.append_child_value("label", d.device_label)
        desc.append_child_value("address", d.mac)
        chans = desc.append_child("channels")
        for i in inputs:
            ch = chans.append_child("channel")
            ch.append_child_value("label", i.key)
            ch.append_child_value("description", i.label)
            ch.append_child_value("unit", i.unit)
            ch.append_child_value("type", i.kind)
        self.outlet = pylsl.StreamOutlet(info, 0, 360)
        log.info("LSL stream '%s' (%d channels, %g Hz)", name, len(inputs), d.rate)

    def process(self, t, x):
        if len(t):
            ts = self.host_time(t) + self._off
            self.outlet.push_chunk(x.tolist(), ts.tolist())
        return {}

    def reset(self):
        """Connection lost: keep the same stream (receivers see a gap in the timestamps)."""

    def stop(self):
        self.outlet = None

    # ---------------- app-wide: keypoints out, markers in ----------------
    def start_app(self, app):
        pylsl = _lsl()
        self._pylsl, self.app = pylsl, app
        self._app_off = lsl_minus_shared(pylsl)
        self.source_id = f"shimmerlive-keypoints-{socket.gethostname()}"
        info = pylsl.StreamInfo(MARKER_STREAM, "Markers", 1, pylsl.IRREGULAR_RATE, pylsl.cf_string, self.source_id)
        self.marker_outlet = pylsl.StreamOutlet(info)
        self._stop = threading.Event()
        self._inlets = {}                        # source id / uid -> thread
        self._resolver = threading.Thread(target=self._resolve_loop, daemon=True, name="lsl-resolve")
        self._resolver.start()
        log.info("LSL: sending keypoints as '%s', listening for Markers streams", MARKER_STREAM)

    def on_keypoint(self, event):
        if event.get("source", "").startswith("lsl:"):
            return                               # it came from LSL: don't echo it back
        self.marker_outlet.push_sample([event["name"]], event["time"] + self._app_off)

    def stop_app(self):
        self._stop.set()
        self.marker_outlet = None

    def _resolve_loop(self):
        pylsl = self._pylsl
        resolver = pylsl.ContinuousResolver(prop="type", value="Markers")
        while not self._stop.wait(RESOLVE_EVERY_S):
            for info in resolver.results():
                key = info.source_id() or info.uid()
                if info.source_id() == self.source_id or key in self._inlets:
                    continue
                th = threading.Thread(target=self._pull_loop, args=(info,), daemon=True,
                                      name=f"lsl-markers-{info.name()}")
                self._inlets[key] = th
                th.start()

    def _pull_loop(self, info):
        pylsl = self._pylsl
        name = info.name()
        key = info.source_id() or info.uid()
        try:
            inlet = pylsl.StreamInlet(info, recover=True)
            inlet.open_stream(timeout=5)
            log.info("LSL: listening to markers from '%s'", name)
            while not self._stop.is_set():
                sample, ts = inlet.pull_sample(timeout=PULL_TIMEOUT_S)
                if sample is None:
                    continue
                text = str(sample[0]).strip()
                when = ts + inlet.time_correction() - self._app_off      # sender's time on this PC's clock
                reply = self.app.mark(text, when, f"lsl:{name}")
                if not reply.startswith("OK"):
                    log.info("LSL marker '%s' from '%s' ignored: %s", text, name, reply[4:])
        except Exception as e:                   # noqa: BLE001 - one stream failing must not stop the others
            log.info("LSL: markers from '%s' stopped: %s", name, e)
        finally:
            self._inlets.pop(key, None)          # found again by the resolver if it comes back
