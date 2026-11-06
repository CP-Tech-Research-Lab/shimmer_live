# PyInstaller recipe for Shimmer Live: a folder with ShimmerLive.exe (Windows) or ShimmerLive (Linux).
#   pyinstaller packaging/ShimmerLive.spec --noconfirm       (from the repository folder)
# The parts PyInstaller cannot find by itself are listed here: modules loaded at run time (built-in plugins,
# Windows Bluetooth), the LSL library, and the logo.
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

ROOT = Path(SPECPATH).parent
WINDOWS = sys.platform == "win32"

hidden = (collect_submodules("shimmer_live")                      # incl. plugins.builtin (found by pkgutil)
          + collect_submodules("pyshimmer")
          + ["pylsl"]
          + (collect_submodules("winrt") if WINDOWS else collect_submodules("dbus_fast")))
datas = ([(str(ROOT / "shimmer_live" / "assets"), "shimmer_live/assets")]
         + collect_data_files("pylsl"))
binaries = collect_dynamic_libs("pylsl") + (collect_dynamic_libs("winrt") if WINDOWS else [])
icon = ROOT / "packaging" / "shimmer_live.ico"

a = Analysis([str(ROOT / "packaging" / "shimmer_live_app.py")], pathex=[str(ROOT)], binaries=binaries,
             datas=datas, hiddenimports=hidden,
             excludes=["tkinter", "matplotlib", "IPython", "pyqtgraph.opengl", "pyqtgraph.examples", "pytest"],
             noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="ShimmerLive", console=False,
          icon=str(icon) if icon.exists() else None)
coll = COLLECT(exe, a.binaries, a.datas, name="ShimmerLive")
