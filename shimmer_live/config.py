"""Small persistent JSON config: known devices (RFCOMM channel, last check) and folders."""
import json
import os
import sys
import threading
from pathlib import Path

APP_NAME = "ShimmerLive"


def config_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        d = base / APP_NAME
    else:
        d = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "shimmer_live"
    d.mkdir(parents=True, exist_ok=True)
    return d


def default_recordings_dir() -> Path:
    docs = Path.home() / "Documents"
    return (docs if docs.is_dir() else Path.home()) / "ShimmerRecordings"


class Config:
    def __init__(self, path: Path | None = None):
        self.path = path or config_dir() / "config.json"
        self._lock = threading.Lock()
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}
        self.data.setdefault("devices", {})
        self.data.setdefault("recordings_dir", str(default_recordings_dir()))

    def save(self):
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)

    def device(self, mac: str) -> dict:
        with self._lock:
            return self.data["devices"].setdefault(mac.upper(), {})

    def update_device(self, mac: str, **fields):
        with self._lock:
            self.data["devices"].setdefault(mac.upper(), {}).update(fields)
        self.save()

    @property
    def recordings_dir(self) -> Path:
        return Path(self.data["recordings_dir"])

    @recordings_dir.setter
    def recordings_dir(self, p):
        self.data["recordings_dir"] = str(p)
        self.save()
