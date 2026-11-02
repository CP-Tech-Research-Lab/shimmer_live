"""Example Shimmer Live plugin: EDA tonic level (skin conductance level, SCL).

Copy this file into the plugins folder (Plugins page -> "Open plugins folder"), restart or press
Reload, tick it, and open live data with a GSR+ Shimmer. See PLUGINS.md for the full interface.

The tonic level is estimated as the running median of the last 4 s of skin conductance, output
4 times per second. The median ignores short phasic responses (SCRs) and isolated artifacts.
"""
import numpy as np

from shimmer_live.plugins.api import InputSpec, OutputSpec, Plugin

WINDOW_S = 4.0
OUTPUT_EVERY_S = 0.25


class EDATonic(Plugin):
    id = "eda_tonic"                 # unique and stable: used in settings and recorded file names
    name = "EDA tonic"
    description = "Skin conductance level (SCL): running median of the last 4 s, updated 4 times per second."
    version = "1.0"
    inputs = [InputSpec(kind="gsr", description="skin conductance")]
    outputs = [OutputSpec("scl", "Tonic level (SCL)", "uS")]

    def start(self, inputs):
        self.rate = inputs[0].rate
        self.t = np.empty(0)
        self.x = np.empty(0)
        self.next_out = None

    def process(self, t, x):
        self.t = np.concatenate([self.t, t])
        self.x = np.concatenate([self.x, x[:, 0]])
        keep = self.t > self.t[-1] - WINDOW_S
        self.t, self.x = self.t[keep], self.x[keep]
        if self.next_out is None:
            self.next_out = self.t[0] + WINDOW_S
        out_t, out_y = [], []
        while self.next_out <= self.t[-1]:
            window = self.x[(self.t > self.next_out - WINDOW_S) & (self.t <= self.next_out)]
            finite = window[np.isfinite(window)]
            if len(finite):
                out_t.append(self.next_out)
                out_y.append(float(np.median(finite)))
            self.next_out += OUTPUT_EVERY_S
        return {"scl": (out_t, out_y)}
