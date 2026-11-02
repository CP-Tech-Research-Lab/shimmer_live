"""Find plugins: the built-in ones and user plugin files in the plugins folder."""
import importlib
import importlib.util
import inspect
import logging
import pkgutil
from dataclasses import dataclass
from pathlib import Path

from ..config import config_dir
from . import builtin
from .api import Plugin

log = logging.getLogger("shimmerlive.plugins")


@dataclass
class PluginEntry:
    cls: type
    source: str               # "built-in" or the file path


def user_plugins_dir() -> Path:
    d = config_dir() / "plugins"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _plugin_classes(module):
    return [c for _, c in inspect.getmembers(module, inspect.isclass)
            if issubclass(c, Plugin) and c is not Plugin and c.id and c.__module__ == module.__name__]


def discover(user_dir: Path | None = None) -> tuple[list, list]:
    """Return (plugins, errors). errors: [(source, message)] for files that failed to load."""
    entries, errors, seen = [], [], set()

    def add(cls, source):
        if cls.id in seen:
            errors.append((source, f"plugin id '{cls.id}' is already used by another plugin - skipped"))
            return
        seen.add(cls.id)
        entries.append(PluginEntry(cls, source))

    for info in pkgutil.iter_modules(builtin.__path__):
        mod = importlib.import_module(f"{builtin.__name__}.{info.name}")
        for cls in _plugin_classes(mod):
            add(cls, "built-in")

    folder = user_dir if user_dir is not None else user_plugins_dir()
    for path in sorted(Path(folder).glob("*.py")):
        try:
            spec = importlib.util.spec_from_file_location(f"shimmerlive_user_plugin_{path.stem}", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            found = _plugin_classes(mod)
            if not found:
                errors.append((str(path), "no Plugin subclass with an 'id' found"))
            for cls in found:
                add(cls, str(path))
        except Exception as e:                     # a broken user file must not break the app
            log.warning("plugin file %s failed: %s", path, e)
            errors.append((str(path), f"{type(e).__name__}: {e}"))
    return entries, errors
