"""Windows backend: WinRT Windows.Devices.Enumeration / Windows.Devices.Bluetooth.

Requires the pywinrt packages listed in requirements.txt (Windows 10 1809+).
Pairing uses DeviceInformationCustomPairing so a PIN request (legacy pairing, Shimmer
RN42 -> 1234) is answered through the application's PIN dialog. After pairing, Windows
creates the "Standard Serial over Bluetooth link" COM ports used for streaming.

NOTE: written against the WinRT API but not exercised on a Windows machine yet.
"""
import asyncio
import logging
import threading

from .base import (NOT_RESPONDING_HINT, BackendUnavailable, BluetoothBackend, BtDevice,
                   PairingError)

log = logging.getLogger("shimmerlive.winrt")

try:
    from winrt.windows.devices.bluetooth import BluetoothDevice
    from winrt.windows.devices.enumeration import (DeviceInformation, DevicePairingKinds,
                                                   DevicePairingResultStatus)
except ImportError as e:     # pragma: no cover - only on Windows
    raise BackendUnavailable(
        "Windows Bluetooth support needs the WinRT packages: pip install -r requirements.txt") from e


def _mac_from_int(a: int) -> str:
    return ":".join(f"{(a >> (8 * i)) & 0xFF:02X}" for i in reversed(range(6)))


async def _find_all(selector):
    # pywinrt 4.x has no single-string overload: FindAllAsync(aqsFilter, additionalProperties).
    return await DeviceInformation.find_all_async(selector, None)


PAIR_STATUS_TEXT = {
    "AUTHENTICATION_FAILURE": ("Authentication failed - wrong PIN?", "The PIN of Shimmer devices is 1234.", False),
    "AUTHENTICATION_TIMEOUT": ("The device stopped answering during pairing.", NOT_RESPONDING_HINT, True),
    "CONNECTION_REJECTED": ("The device rejected the connection.", NOT_RESPONDING_HINT, True),
    "NO_SUPPORTED_PROFILES": ("The device offers no usable Bluetooth profile.", "", False),
    "PAIRING_CANCELED": ("Pairing was cancelled.", "", False),
    "OPERATION_ALREADY_IN_PROGRESS": ("Another pairing is in progress.", "Wait and retry.", True),
    "REQUIRED_HANDLER_NOT_REGISTERED": ("Windows needs a PIN handler (internal error).", "", False),
    "REJECTED_BY_HANDLER": ("PIN entry was cancelled.", "", False),
    "HARDWARE_FAILURE": ("Bluetooth hardware failure.", "Check the Bluetooth adapter.", False),
    "FAILED": ("Pairing failed.", NOT_RESPONDING_HINT, True),
}


class WindowsBackend(BluetoothBackend):
    name = "WinRT (Windows)"

    def __init__(self):
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True, name="winrt-loop").start()

    def _run(self, coro, timeout=60):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    def adapter_status(self):
        try:
            from winrt.windows.devices.bluetooth import BluetoothAdapter
            from winrt.windows.devices.radios import RadioState
            adapter = self._run(BluetoothAdapter.get_default_async(), 10)
            if adapter is None:
                return False, "No Bluetooth adapter found"
            radio = self._run(adapter.get_radio_async(), 10)
            if radio is not None and radio.state != RadioState.ON:
                return False, "Bluetooth is switched OFF (Settings > Bluetooth & devices)"
            return True, f"Bluetooth adapter {_mac_from_int(adapter.bluetooth_address)} is on"
        except Exception as e:
            return True, f"Bluetooth status unknown ({e})"

    def power_on(self):
        from winrt.windows.devices.bluetooth import BluetoothAdapter
        from winrt.windows.devices.radios import RadioState
        adapter = self._run(BluetoothAdapter.get_default_async(), 10)
        radio = self._run(adapter.get_radio_async(), 10)
        self._run(radio.set_state_async(RadioState.ON), 15)

    async def _list(self, paired: bool):
        sel = BluetoothDevice.get_device_selector_from_pairing_state(paired)
        out = []
        for info in await _find_all(sel):
            try:
                bt = await BluetoothDevice.from_id_async(info.id)
                addr = _mac_from_int(bt.bluetooth_address)
            except Exception as e:
                log.info("skipping %s: %s", info.name, e)
                continue
            out.append(BtDevice(address=addr, name=info.name or bt.name, paired=paired, platform_id=info.id))
        return out

    def list_paired(self):
        return sorted(self._run(self._list(True), 60), key=lambda d: d.name.lower())

    def discover(self, seconds, on_update):
        # Windows runs a full inquiry inside FindAllAsync (~10-30 s); results arrive at the end.
        found = self._run(self._list(False), max(60, seconds + 45))
        on_update(found)
        return found

    def pair(self, device, pin_provider):
        try:
            self._run(self._pair(device, pin_provider), 150)
        except TimeoutError as e:
            raise PairingError("Pairing timed out.", NOT_RESPONDING_HINT, retryable=True) from e

    async def _pair(self, device, pin_provider):
        info = await DeviceInformation.create_from_id_async(device.platform_id)
        if info.pairing.is_paired:
            return
        custom = info.pairing.custom

        def on_request(sender, args):
            kind = args.pairing_kind
            if kind == DevicePairingKinds.PROVIDE_PIN:
                deferral = args.get_deferral()
                try:
                    pin = pin_provider(device.name)
                    if pin:
                        args.accept(pin)
                finally:
                    deferral.complete()
            else:
                args.accept()        # confirm-only / pin match: Shimmers have no display

        token = custom.add_pairing_requested(on_request)
        try:
            kinds = (DevicePairingKinds.CONFIRM_ONLY | DevicePairingKinds.PROVIDE_PIN |
                     DevicePairingKinds.CONFIRM_PIN_MATCH | DevicePairingKinds.DISPLAY_PIN)
            result = await custom.pair_async(kinds)
        finally:
            custom.remove_pairing_requested(token)
        status = result.status
        if status in (DevicePairingResultStatus.PAIRED, DevicePairingResultStatus.ALREADY_PAIRED):
            return
        name = getattr(status, "name", str(status))
        msg, hint, retry = PAIR_STATUS_TEXT.get(name, (f"Pairing failed ({name}).", NOT_RESPONDING_HINT, True))
        raise PairingError(msg, hint, retry)

    def forget(self, device):
        async def go():
            info = await DeviceInformation.create_from_id_async(device.platform_id)
            await info.pairing.unpair_async()
        self._run(go(), 30)
