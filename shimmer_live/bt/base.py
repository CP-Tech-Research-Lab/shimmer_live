"""Platform-neutral Bluetooth pairing/discovery interface."""
from dataclasses import dataclass, field
from typing import Callable, Optional

# Roving Networks (RN42, used by most Shimmer3 units) OUI.
SHIMMER_OUIS = ("00:06:66",)
SHIMMER_NAME_PREFIXES = ("shimmer", "rn42", "s3ble")

PinProvider = Callable[[str], Optional[str]]     # device name -> PIN, or None if cancelled


@dataclass
class BtDevice:
    address: str
    name: str
    paired: bool
    connected: bool = False
    rssi: Optional[int] = None
    platform_id: Optional[str] = None             # BlueZ object path / Windows device id
    extra: dict = field(default_factory=dict)

    @property
    def looks_like_shimmer(self) -> bool:
        return (self.name or "").lower().startswith(SHIMMER_NAME_PREFIXES) or \
            self.address.upper().startswith(SHIMMER_OUIS)


class BackendUnavailable(Exception):
    pass


class PairingError(Exception):
    def __init__(self, message: str, hint: str = "", retryable: bool = False):
        super().__init__(message)
        self.hint = hint
        self.retryable = retryable


NOT_RESPONDING_HINT = (
    "The Shimmer did not answer. Check that:\n"
    "  • it is switched on (slide switch) and the blue LED blinks briefly every 2 s;\n"
    "  • it is within 1-2 m of this computer;\n"
    "  • it is not connected to another program or computer (solid blue LED = already connected);\n"
    "  • the battery is not flat (put it in the dock/base).\n"
    "A Shimmer only listens for connections 12.5% of the time, so several attempts are normal.\n"
    "If it keeps failing: reset it (switch off/on, or press the dock reset button) and try again.")


class BluetoothBackend:
    name = "none"

    def adapter_status(self) -> tuple[bool, str]:
        """(usable, human-readable status)"""
        raise NotImplementedError

    def power_on(self) -> None:
        raise NotImplementedError

    def list_paired(self) -> list[BtDevice]:
        raise NotImplementedError

    def discover(self, seconds: float, on_update: Callable[[list[BtDevice]], None]) -> list[BtDevice]:
        """Scan for unpaired devices; on_update is called (any thread) as devices appear."""
        raise NotImplementedError

    def pair(self, device: BtDevice, pin_provider: PinProvider) -> None:
        """Pair and trust. Raises PairingError with a user-facing hint."""
        raise NotImplementedError

    def forget(self, device: BtDevice) -> None:
        raise NotImplementedError

    def find(self, addresses, timeout: float = 20.0) -> dict:
        """Locate specific devices by address (e.g. read from a dock): {address: BtDevice}.

        Default: paired list plus one scan. Backends override this with something faster."""
        want = {a.upper() for a in addresses}
        found = {d.address: d for d in self.list_paired() if d.address in want}
        if want - set(found):
            for d in self.discover(min(timeout, 15.0), lambda _: None):
                if d.address in want:
                    found.setdefault(d.address, d)
        return found
