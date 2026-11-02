"""Every module imports on this platform, and the UI pages build (off-screen, no Bluetooth)."""
import importlib
import os
import sys

import pytest

MODULES = ["shimmer_live.clocks", "shimmer_live.config", "shimmer_live.conversions", "shimmer_live.transport",
           "shimmer_live.session", "shimmer_live.recorder", "shimmer_live.device_config", "shimmer_live.bt"]
UI_MODULES = ["shimmer_live.ui.common", "shimmer_live.ui.guide", "shimmer_live.ui.dialogs",
              "shimmer_live.ui.device_settings", "shimmer_live.ui.devices_page", "shimmer_live.ui.live_page",
              "shimmer_live.ui.main_window", "shimmer_live.__main__"]


@pytest.mark.parametrize("name", MODULES)
def test_core_imports(name):
    importlib.import_module(name)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="BlueZ backend is Linux-only")
def test_bluez_backend_imports():
    importlib.import_module("shimmer_live.bt.bluez")


@pytest.mark.skipif(sys.platform != "win32", reason="WinRT backend is Windows-only")
def test_windows_backend_uses_existing_winrt_api():
    importlib.import_module("shimmer_live.bt.windows")
    from winrt.windows.devices.bluetooth import BluetoothAdapter, BluetoothDevice
    from winrt.windows.devices.enumeration import (DeviceInformation, DevicePairingKinds,
                                                   DevicePairingResultStatus)
    from winrt.windows.devices.radios import RadioState
    assert callable(DeviceInformation.find_all_async) and callable(DeviceInformation.create_from_id_async)
    assert callable(BluetoothDevice.get_device_selector_from_pairing_state)
    assert callable(BluetoothDevice.from_id_async) and callable(BluetoothAdapter.get_default_async)
    for kind in ("CONFIRM_ONLY", "PROVIDE_PIN", "CONFIRM_PIN_MATCH", "DISPLAY_PIN"):
        assert hasattr(DevicePairingKinds, kind)
    assert hasattr(DevicePairingResultStatus, "PAIRED") and hasattr(DevicePairingResultStatus, "ALREADY_PAIRED")
    assert hasattr(RadioState, "ON")
    selector = BluetoothDevice.get_device_selector_from_pairing_state(True)
    assert isinstance(selector, str) and selector


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    widgets = pytest.importorskip("PySide6.QtWidgets")
    from shimmer_live.ui.common import init_bridge
    app = widgets.QApplication.instance() or widgets.QApplication([])
    init_bridge()
    return app


@pytest.mark.parametrize("name", UI_MODULES)
def test_ui_imports(qapp, name):
    importlib.import_module(name)


def test_pages_build(qapp, tmp_path):
    from shimmer_live.config import Config
    from shimmer_live.ui.devices_page import DevicesPage
    from shimmer_live.ui.live_page import LivePage
    cfg = Config(tmp_path / "cfg.json")
    devices = DevicesPage(cfg, None, "no Bluetooth in test")
    devices.refresh()
    live = LivePage(cfg)
    assert not live.recording
    devices.deleteLater()
    live.deleteLater()
