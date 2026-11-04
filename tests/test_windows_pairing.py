"""Exercise the Windows pairing handshake without Bluetooth hardware."""
import asyncio
import sys
import threading
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows pairing backend")


def test_run_accepts_winrt_style_awaitable():
    from shimmer_live.bt.windows import WindowsBackend

    class Operation:
        def __await__(self):
            return asyncio.sleep(0, result="radio on").__await__()

    backend = object.__new__(WindowsBackend)
    backend._loop = asyncio.new_event_loop()
    worker = threading.Thread(target=backend._loop.run_forever)
    worker.start()
    try:
        assert backend._run(Operation(), timeout=2) == "radio on"
    finally:
        backend._loop.call_soon_threadsafe(backend._loop.stop)
        worker.join(2)
        backend._loop.close()


@pytest.mark.parametrize("ceremony", ["PROVIDE_PIN", "CONFIRM_ONLY", "CONFIRM_PIN_MATCH"])
def test_pairing_never_requests_keyboard_input_on_shimmer(monkeypatch, ceremony):
    from shimmer_live.bt import windows
    from shimmer_live.bt.base import BtDevice

    kinds = windows.DevicePairingKinds
    accepted, requested_pins, removed = [], [], []

    class CustomPairing:
        def add_pairing_requested(self, handler):
            self.handler = handler
            return 17

        async def pair_async(self, supported):
            assert not supported & kinds.DISPLAY_PIN
            assert supported & getattr(kinds, ceremony)
            args = SimpleNamespace(pairing_kind=getattr(kinds, ceremony),
                                   accept=lambda *pin: accepted.append(pin),
                                   get_deferral=lambda: SimpleNamespace(complete=lambda: None))
            self.handler(self, args)
            return SimpleNamespace(status=windows.DevicePairingResultStatus.PAIRED)

        def remove_pairing_requested(self, token):
            removed.append(token)

    async def create_info(device_id):
        return SimpleNamespace(pairing=SimpleNamespace(is_paired=False, custom=CustomPairing()))

    monkeypatch.setattr(windows, "DeviceInformation", SimpleNamespace(create_from_id_async=create_info))

    def pin_provider(name):
        requested_pins.append(name)
        return "1234"

    device = BtDevice("00:06:66:B8:D1:A4", "Shimmer3-D1A4", False, platform_id="test-device")
    backend = object.__new__(windows.WindowsBackend)
    asyncio.run(backend._pair(device, pin_provider))
    assert accepted == ([("1234",)] if ceremony == "PROVIDE_PIN" else [()])
    assert requested_pins == ([device.name] if ceremony == "PROVIDE_PIN" else [])
    assert removed == [17]
