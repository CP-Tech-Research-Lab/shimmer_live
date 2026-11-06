"""Keypoints: labelled instants marked during a recording (rest start, task end, ...)."""
from dataclasses import asdict, dataclass

DEFAULT_KEYPOINTS = [
    ("rest start", "#1baf7a"), ("rest end", "#127a55"),
    ("experiment start", "#2a78d6"), ("experiment end", "#1a4f91"),
    ("task start", "#eb6834"), ("task end", "#a8461f"),
    ("discard", "#e34948"),
]
# Colours offered for custom keypoints, in order.
CUSTOM_COLORS = ["#6250d6", "#eda100", "#e87ba4", "#008300", "#00838f", "#8d6e63", "#5c6bc0", "#c0ca33"]


@dataclass
class Keypoint:
    name: str
    color: str
    builtin: bool = False
    port: int | None = None        # TCP port that marks it; given once (assign_ports), then fixed


def load(cfg) -> list[Keypoint]:
    stored = cfg.data.get("keypoints")
    if not stored:
        return [Keypoint(n, c, True) for n, c in DEFAULT_KEYPOINTS]
    return [Keypoint(**k) for k in stored]


def tcp_ports(keypoints: list[Keypoint], base_port: int) -> list[int]:
    """The TCP port of each keypoint (base_port + its number for one not assigned yet)."""
    return [k.port or base_port + i + 1 for i, k in enumerate(keypoints)]


def assign_ports(keypoints: list[Keypoint], name_port: int) -> bool:
    """Give every keypoint without a port its own fixed one: name_port + its number (as before, so existing
    setups keep 4001, 4002, ...) or, if taken, the next free one. Returns True if any port was assigned."""
    used = {k.port for k in keypoints if k.port} | {name_port}
    changed = False
    for i, k in enumerate(keypoints):
        if not k.port:
            p = name_port + i + 1
            while p in used:
                p += 1
            k.port, changed = p, True
            used.add(p)
    return changed


def next_free_port(keypoints: list[Keypoint], name_port: int) -> int:
    used = {k.port for k in keypoints if k.port} | {name_port}
    p = name_port + 1
    while p in used:
        p += 1
    return p


def save(cfg, keypoints: list[Keypoint]) -> None:
    cfg.data["keypoints"] = [asdict(k) for k in keypoints]
    cfg.save()


def next_color(keypoints: list[Keypoint]) -> str:
    used = {k.color for k in keypoints}
    return next((c for c in CUSTOM_COLORS if c not in used), CUSTOM_COLORS[len(keypoints) % len(CUSTOM_COLORS)])


# Listeners told about every keypoint marked (app-wide plugin parts). Called on the UI thread.
_listeners = []


def add_listener(fn) -> None:
    if fn not in _listeners:
        _listeners.append(fn)


def remove_listener(fn) -> None:
    if fn in _listeners:
        _listeners.remove(fn)


def notify(event: dict) -> None:
    for fn in list(_listeners):
        try:
            fn(event)
        except Exception:                          # noqa: BLE001 - a listener must never stop a keypoint
            import logging
            logging.getLogger("shimmerlive.keypoints").exception("keypoint listener failed")
