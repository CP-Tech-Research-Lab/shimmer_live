import sys

from .base import BackendUnavailable, BluetoothBackend, BtDevice, PairingError  # noqa: F401


def get_backend() -> BluetoothBackend:
    """Platform Bluetooth backend; raises BackendUnavailable with a user-facing reason."""
    if sys.platform.startswith("linux"):
        from .bluez import BluezBackend
        return BluezBackend()
    if sys.platform == "win32":
        from .windows import WindowsBackend
        return WindowsBackend()
    raise BackendUnavailable(f"Bluetooth pairing is not supported on {sys.platform}")
