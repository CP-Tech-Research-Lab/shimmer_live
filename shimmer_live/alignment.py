"""Estimate when each sample was taken, in PC time.

Host timestamps are taken when a packet *arrives*; Bluetooth delays packets by a variable
amount (tens to hundreds of ms, often in bursts) but never delivers them early. The Shimmer's
own clock spaces samples exactly. So, per connection period, fit host time against Shimmer
time (the slope absorbs drift between the two crystals) and place the line on the lower edge
of the arrival times.
"""
import numpy as np


def split_periods(ticks: np.ndarray) -> np.ndarray:
    """Period number per sample: a new period starts where the Shimmer clock jumps (reconnect)."""
    if len(ticks) < 2:
        return np.zeros(len(ticks), dtype=int)
    step = np.diff(ticks)
    nominal = np.median(step)
    return np.concatenate([[0], np.cumsum(step > 2.5 * nominal)])


MAX_DRIFT = 500e-6     # |slope - 1| bound; real Shimmer/PC crystals differ by ~20-50 ppm


def _lower_hull(t: np.ndarray, y: np.ndarray):
    """Vertices of the lower convex hull of points sorted by t (Andrew's monotone chain)."""
    hull = []
    for p in zip(t.tolist(), y.tolist()):
        while len(hull) >= 2:
            (x1, y1), (x2, y2) = hull[-2], hull[-1]
            if (x2 - x1) * (p[1] - y1) - (y2 - y1) * (p[0] - x1) <= 0:
                hull.pop()
            else:
                break
        hull.append(p)
    return np.array(hull)


def fit_lower_edge(t: np.ndarray, host: np.ndarray) -> tuple[float, float]:
    """Line host = a*t + b lying under every arrival time and as close to them as possible.

    This is the linear program "minimise mean(host - a*t - b) subject to host >= a*t + b",
    the standard convex-hull clock-synchronisation estimate: its optimum lies on an edge of the
    lower convex hull of the points, so only hull edges (and the drift bounds) are candidates.
    """
    t = np.asarray(t, dtype=float)
    host = np.asarray(host, dtype=float)
    if len(t) < 2 or np.ptp(t) == 0:
        return 1.0, float(np.min(host - t))
    order = np.argsort(t, kind="stable")
    t, host = t[order], host[order]
    hull = _lower_hull(t, host)
    edges = np.diff(hull[:, 1]) / np.diff(hull[:, 0]) if len(hull) > 1 else np.empty(0)
    candidates = np.unique(np.concatenate([np.clip(edges, 1 - MAX_DRIFT, 1 + MAX_DRIFT),
                                           [1 - MAX_DRIFT, 1.0, 1 + MAX_DRIFT]]))
    mean_t, mean_h = float(np.mean(t)), float(np.mean(host))
    best = None
    for a in candidates:
        b = float(np.min(hull[:, 1] - a * hull[:, 0]))   # the lowest point relative to slope a is a hull vertex
        cost = mean_h - a * mean_t - b
        if best is None or cost < best[0]:
            best = (cost, float(a), b)
    return best[1], best[2]


class PeriodFits:
    """Per-period mappings from Shimmer time (s) to host time (s, relative to a reference)."""

    def __init__(self, t: np.ndarray, host: np.ndarray, periods: np.ndarray):
        self.fits = []                     # (t_first, t_last, a, b)
        for p in np.unique(periods):
            m = periods == p
            a, b = fit_lower_edge(t[m], host[m])
            self.fits.append((float(t[m][0]), float(t[m][-1]), a, b))

    def __call__(self, t) -> np.ndarray:
        """Map Shimmer times to host time; times between periods use the following period."""
        t = np.asarray(t, dtype=float)
        out = np.empty_like(t)
        starts = np.array([f[0] for f in self.fits])
        idx = np.clip(np.searchsorted(starts, t, side="right") - 1, 0, len(self.fits) - 1)
        for i, (_, _, a, b) in enumerate(self.fits):
            m = idx == i
            out[m] = a * t[m] + b
        return out
