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
    port: int | None = None        # TCP port that marks it (None: the default, first port + its number)


def load(cfg) -> list[Keypoint]:
    stored = cfg.data.get("keypoints")
    if not stored:
        return [Keypoint(n, c, True) for n, c in DEFAULT_KEYPOINTS]
    return [Keypoint(**k) for k in stored]


def tcp_ports(keypoints: list[Keypoint], base_port: int) -> list[int]:
    """The TCP port of each keypoint: its own if set, else base_port + its number (1, 2, ...)."""
    return [k.port or base_port + i + 1 for i, k in enumerate(keypoints)]


def save(cfg, keypoints: list[Keypoint]) -> None:
    cfg.data["keypoints"] = [asdict(k) for k in keypoints]
    cfg.save()


def next_color(keypoints: list[Keypoint]) -> str:
    used = {k.color for k in keypoints}
    return next((c for c in CUSTOM_COLORS if c not in used), CUSTOM_COLORS[len(keypoints) % len(CUSTOM_COLORS)])
