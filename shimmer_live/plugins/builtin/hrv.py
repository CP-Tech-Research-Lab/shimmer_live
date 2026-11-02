"""Real-time heart rate and HRV from ECG.

QRS detection follows Pan & Tompkins (1985): band-pass 5-15 Hz, derivative, squaring,
150 ms moving integration, adaptive signal/noise peak thresholds and a refractory period.
Each detection is refined to the R peak of the band-passed ECG. Beats are reported with
~0.3 s delay (the look-ahead needed to confirm a peak).

RR intervals outside 300-2000 ms, or more than 30 % away from the median of recent valid
intervals, are treated as artifacts (missed/extra beats) and excluded from the outputs.
RMSSD is computed over the valid successive differences of the last 30 s.
"""
import numpy as np
from scipy.ndimage import maximum_filter1d
from scipy.signal import butter, group_delay, sos2tf, sosfilt, sosfilt_zi

from ..api import InputSpec, OutputSpec, Plugin

LEARN_S = 2.0
REFRACTORY_S = 0.25
INTEGRATION_S = 0.15
LOOKAHEAD_S = 0.2
SEARCH_BACK_S = 0.25          # where the R peak lies relative to the integrated-signal peak
KEEP_S = 4.0
RR_MIN_MS, RR_MAX_MS = 300.0, 2000.0
RR_MAX_DEVIATION = 0.30
RMSSD_WINDOW_S = 30.0
RMSSD_MIN_DIFFS = 4


class HRV(Plugin):
    id = "hrv"
    name = "HRV"
    description = ("Real-time R-peak detection on ECG (Pan-Tompkins). Outputs heart rate, RR interval and RMSSD "
                   "over the last 30 s. Artifact beats are excluded.")
    version = "1.0"
    inputs = [InputSpec(kind="exg", min_rate=100.0, preferred=("EXG1_CH1", "EXG1_CH2"),
                        description="ECG lead (LL-RA preferred)")]
    outputs = [OutputSpec("hr", "Heart rate", "bpm", style="points"),
               OutputSpec("rr", "RR interval", "ms", style="points"),
               OutputSpec("rmssd", "RMSSD (30 s)", "ms", style="points")]

    def start(self, inputs):
        self._inputs = inputs
        fs = inputs[0].rate
        self.fs = fs
        self.sos = butter(2, [5.0, 15.0], btype="bandpass", fs=fs, output="sos")
        # The band-pass delays the QRS by its group delay in the pass band; report beats at the
        # true R time by subtracting it (evaluated at the QRS band centre, 10 Hz).
        _, gd = group_delay(sos2tf(self.sos), w=[10.0], fs=fs)
        self.delay_s = float(gd[0]) / fs
        self.zi = None
        self.win = max(1, int(round(INTEGRATION_S * fs)))
        self.half = max(1, int(round(LOOKAHEAD_S * fs)))
        self.t = np.empty(0)
        self.bp = np.empty(0)
        self.sq = np.empty(0)
        self.integ = np.empty(0)
        self.next_i = 0                 # first buffer index not yet examined
        self.t0 = None
        self.spki = self.npki = None
        self.last_peak_t = -np.inf
        self.last_r_t = None
        self.valid_rr = []              # (time, rr_ms) of accepted intervals
        self.beats = []                 # (time, rr_ms, valid) of every interval, for RMSSD

    def process(self, t, x):
        t = np.asarray(t, dtype=float)
        ecg = np.asarray(x, dtype=float)[:, 0]
        if len(t) == 0:
            return {}
        if self.zi is None:
            self.zi = sosfilt_zi(self.sos) * ecg[0]
            self.t0 = t[0]
        bp, self.zi = sosfilt(self.sos, ecg, zi=self.zi)
        prev_bp_last = self.bp[-1] if len(self.bp) else bp[0]
        sq = np.diff(np.concatenate([[prev_bp_last], bp])) ** 2
        # running mean of the squared derivative over the integration window
        hist = self.sq[-(self.win - 1):] if self.win > 1 else np.empty(0)
        both = np.concatenate([hist, sq])
        c = np.cumsum(np.concatenate([[0.0], both]))
        integ = (c[self.win:] - c[:-self.win]) / self.win if len(both) >= self.win else np.zeros(len(sq))
        integ = integ[-len(sq):] if len(integ) >= len(sq) else np.concatenate([np.zeros(len(sq) - len(integ)), integ])

        self.t = np.concatenate([self.t, t])
        self.bp = np.concatenate([self.bp, bp])
        self.sq = np.concatenate([self.sq, sq])
        self.integ = np.concatenate([self.integ, integ])

        out = {"hr": ([], []), "rr": ([], []), "rmssd": ([], [])}
        if self.spki is None:
            if self.t[-1] - self.t0 < LEARN_S:
                return {}
            learn = self.integ[self.t - self.t0 < LEARN_S]
            self.spki = 0.25 * float(np.max(learn))
            self.npki = 0.5 * float(np.mean(learn))

        end = len(self.integ) - self.half                 # need look-ahead to confirm a maximum
        if end > self.next_i:
            local_max = maximum_filter1d(self.integ, size=2 * self.half + 1, mode="nearest")
            for i in range(self.next_i, end):
                v = self.integ[i]
                if v <= 0 or v != local_max[i]:
                    continue
                threshold = self.npki + 0.25 * (self.spki - self.npki)
                if v > threshold and self.t[i] - self.last_peak_t > REFRACTORY_S:
                    self.spki = 0.125 * v + 0.875 * self.spki
                    self.last_peak_t = self.t[i]
                    self._beat(i, out)
                else:
                    self.npki = 0.125 * v + 0.875 * self.npki
            self.next_i = end

        keep = self.t > self.t[-1] - KEEP_S
        drop = int(np.argmax(keep))
        if drop:
            self.t, self.bp, self.sq, self.integ = (a[drop:] for a in (self.t, self.bp, self.sq, self.integ))
            self.next_i = max(0, self.next_i - drop)
        return {k: v for k, v in out.items() if v[0]}

    def _beat(self, i, out):
        lo = max(0, i - int(round(SEARCH_BACK_S * self.fs)))
        r = lo + int(np.argmax(np.abs(self.bp[lo:i + 1])))
        r_t = float(self.t[r]) - self.delay_s
        if self.last_r_t is not None:
            rr = (r_t - self.last_r_t) * 1000.0
            recent = [v for _, v in self.valid_rr[-8:]]
            ok = RR_MIN_MS <= rr <= RR_MAX_MS
            if ok and len(recent) >= 3:
                med = float(np.median(recent))
                ok = abs(rr - med) <= RR_MAX_DEVIATION * med
            self.beats = [b for b in self.beats if b[0] > r_t - RMSSD_WINDOW_S] + [(r_t, rr, ok)]
            if ok:
                self.valid_rr = self.valid_rr[-16:] + [(r_t, rr)]
                out["hr"][0].append(r_t)
                out["hr"][1].append(60000.0 / rr)
                out["rr"][0].append(r_t)
                out["rr"][1].append(rr)
                # successive differences only between consecutive intervals that are both valid
                diffs = [b[1] - a[1] for a, b in zip(self.beats, self.beats[1:]) if a[2] and b[2]]
                if len(diffs) >= RMSSD_MIN_DIFFS:
                    out["rmssd"][0].append(r_t)
                    out["rmssd"][1].append(float(np.sqrt(np.mean(np.square(diffs)))))
        self.last_r_t = r_t
