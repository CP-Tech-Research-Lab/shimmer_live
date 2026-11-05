"""Linux backend: BlueZ over the system D-Bus (no root, no bluetoothctl scraping).

Pairing registers our own org.bluez.Agent1 so that PIN requests from legacy-pairing
radios (RN42 -> PIN 1234) are routed to the application UI. The agent is not made the
system default, so the desktop's own Bluetooth agent is left untouched.
"""
import asyncio
import logging
import threading

from dbus_fast import BusType, DBusError, Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, method

from .base import (NOT_RESPONDING_HINT, BackendUnavailable, BluetoothBackend, BtDevice,
                   PairingError)

log = logging.getLogger("shimmerlive.bluez")

BLUEZ = "org.bluez"
ADAPTER = "org.bluez.Adapter1"
DEVICE = "org.bluez.Device1"
PROPS = "org.freedesktop.DBus.Properties"
AGENT_PATH = "/org/shimmerlive/agent"


class BluezError(Exception):
    def __init__(self, name, text):
        super().__init__(f"{name}: {text}")
        self.name, self.text = name, text


def _v(props, key, default=None):
    v = props.get(key)
    return v.value if isinstance(v, Variant) else (default if v is None else v)


def map_pair_error(e: BluezError) -> PairingError:
    n, t = e.name.rsplit(".", 1)[-1], (e.text or "").lower()
    if n == "AuthenticationFailed":
        return PairingError("Authentication failed - wrong PIN, or the device rejected it.",
                            "The PIN of Shimmer devices is 1234. Try pairing again.", retryable=False)
    if n in ("AuthenticationCanceled", "AuthenticationRejected") or "cancel" in t:
        return PairingError("Pairing was cancelled.", "", retryable=False)
    if n == "AuthenticationTimeout":
        return PairingError("The device stopped answering during pairing.", NOT_RESPONDING_HINT, retryable=True)
    if n == "InProgress":
        return PairingError("Another pairing operation is in progress.", "Wait a few seconds and retry.",
                            retryable=True)
    if n == "DoesNotExist" or "unknown object" in t:
        return PairingError("The device is no longer visible.", "Scan again, then pair.", retryable=False)
    if n == "ConnectionAttemptFailed" or any(k in t for k in ("page timeout", "host is down", "timeout",
                                                               "not available", "connection refused")):
        return PairingError("The device did not respond.", NOT_RESPONDING_HINT, retryable=True)
    return PairingError(f"Pairing failed ({e.name.rsplit('.', 1)[-1]}: {e.text}).", NOT_RESPONDING_HINT,
                        retryable=True)


class _Agent(ServiceInterface):
    def __init__(self, backend: "BluezBackend"):
        super().__init__("org.bluez.Agent1")
        self._b = backend

    async def _ask(self, device_path) -> str:
        name = await self._b._device_name(device_path)
        provider = self._b._pin_provider
        if provider is None:
            raise DBusError("org.bluez.Error.Rejected", "no pairing in progress")
        pin = await asyncio.get_running_loop().run_in_executor(None, provider, name)
        if not pin:
            raise DBusError("org.bluez.Error.Canceled", "PIN entry cancelled")
        return pin

    @method()
    def Release(self):
        pass

    @method()
    async def RequestPinCode(self, device: "o") -> "s":
        log.info("PIN requested for %s", device)
        return await self._ask(device)

    @method()
    async def RequestPasskey(self, device: "o") -> "u":
        log.info("passkey requested for %s", device)
        pin = await self._ask(device)
        try:
            return int(pin)
        except ValueError:
            raise DBusError("org.bluez.Error.Rejected", "passkey must be numeric")

    @method()
    def DisplayPinCode(self, device: "o", pincode: "s"):
        log.info("display PIN %s for %s", pincode, device)

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):
        pass

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u"):
        # Secure Simple Pairing numeric comparison: Shimmers have no display; we
        # initiated the pairing, so accept.
        log.info("auto-confirming SSP passkey %06d for %s", passkey, device)

    @method()
    def RequestAuthorization(self, device: "o"):
        pass

    @method()
    def AuthorizeService(self, device: "o", uuid: "s"):
        pass

    @method()
    def Cancel(self):
        log.info("BlueZ cancelled the agent request")


class BluezBackend(BluetoothBackend):
    name = "BlueZ (Linux)"

    def __init__(self):
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True, name="bluez-loop").start()
        self._pin_provider = None
        self._agent_registered = False
        self._pair_lock = threading.Lock()
        try:
            self._run(self._connect(), 10)
        except Exception as e:
            raise BackendUnavailable(f"Cannot talk to BlueZ on the system D-Bus: {e}") from e

    def _run(self, coro, timeout=30):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    async def _connect(self):
        self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        objs = await self._objects()
        adapters = sorted(p for p, ifs in objs.items() if ADAPTER in ifs)
        if not adapters:
            raise BackendUnavailable("No Bluetooth adapter found.")
        self.adapter = adapters[0]

    async def _call(self, path, iface, member, sig="", body=()):
        reply = await self.bus.call(Message(destination=BLUEZ, path=path, interface=iface, member=member,
                                            signature=sig, body=list(body)))
        if reply.message_type == MessageType.ERROR:
            raise BluezError(reply.error_name, reply.body[0] if reply.body else "")
        return reply.body

    async def _objects(self):
        (objs,) = await self._call("/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects")
        return objs

    async def _devices(self):
        out = []
        for path, ifs in (await self._objects()).items():
            p = ifs.get(DEVICE)
            if not p or not path.startswith(self.adapter + "/"):
                continue
            out.append(BtDevice(address=_v(p, "Address", "").upper(),
                                name=_v(p, "Name") or _v(p, "Alias", ""),
                                paired=bool(_v(p, "Paired", False)),
                                connected=bool(_v(p, "Connected", False)),
                                rssi=_v(p, "RSSI"), platform_id=path))
        return out

    async def _device_name(self, path):
        try:
            (v,) = await self._call(path, PROPS, "Get", "ss", [DEVICE, "Alias"])
            return v.value
        except BluezError:
            return path.rsplit("/", 1)[-1]

    async def _adapter_prop(self, key):
        (v,) = await self._call(self.adapter, PROPS, "Get", "ss", [ADAPTER, key])
        return v.value

    # ---- public API (blocking; call from worker threads) ----
    def adapter_status(self):
        try:
            powered = self._run(self._adapter_prop("Powered"), 5)
            addr = self._run(self._adapter_prop("Address"), 5)
        except Exception as e:
            return False, f"Bluetooth adapter not reachable: {e}"
        if not powered:
            return False, f"Bluetooth adapter {addr} is switched OFF"
        return True, f"Bluetooth adapter {addr} ({self.adapter.rsplit('/', 1)[-1]}) is on"

    def power_on(self):
        self._run(self._call(self.adapter, PROPS, "Set", "ssv", [ADAPTER, "Powered", Variant("b", True)]), 10)

    def list_paired(self):
        return sorted((d for d in self._run(self._devices(), 10) if d.paired), key=lambda d: d.name.lower())

    def discover(self, seconds, on_update):
        return self._run(self._discover(seconds, on_update), seconds + 30)

    async def _discover(self, seconds, on_update):
        try:
            await self._call(self.adapter, ADAPTER, "SetDiscoveryFilter", "a{sv}",
                             [{"Transport": Variant("s", "bredr")}])
        except BluezError as e:
            log.info("SetDiscoveryFilter: %s", e)
        try:
            await self._call(self.adapter, ADAPTER, "StartDiscovery")
        except BluezError as e:
            if "InProgress" not in e.name:
                raise
        seen = {}
        loop = asyncio.get_running_loop()
        end = loop.time() + seconds
        try:
            while loop.time() < end:
                for d in await self._devices():
                    if not d.paired and d.rssi is not None:
                        seen[d.address] = d
                on_update(list(seen.values()))
                await asyncio.sleep(1.0)
        finally:
            try:
                await self._call(self.adapter, ADAPTER, "StopDiscovery")
            except BluezError:
                pass
        return list(seen.values())

    def nearby(self, addresses, seconds, should_stop=lambda: False):
        return self._run(self._nearby({a.upper() for a in addresses}, seconds, should_stop), seconds + 15)

    async def _nearby(self, want, seconds, should_stop):
        # BlueZ sets a device's RSSI only while a discovery is running and it answered the inquiry
        # (cleared when discovery stops), also for paired devices: that is "switched on and in range".
        try:
            await self._call(self.adapter, ADAPTER, "SetDiscoveryFilter", "a{sv}",
                             [{"Transport": Variant("s", "bredr")}])
        except BluezError:
            pass
        try:
            await self._call(self.adapter, ADAPTER, "StartDiscovery")
        except BluezError as e:
            if "InProgress" not in e.name:
                raise
        seen = {}
        loop = asyncio.get_running_loop()
        end = loop.time() + seconds
        try:
            while loop.time() < end and want - set(seen) and not should_stop():
                for d in await self._devices():
                    if d.address in want and (d.rssi is not None or d.connected):
                        seen[d.address] = d.rssi
                await asyncio.sleep(0.5)
        finally:
            try:
                await self._call(self.adapter, ADAPTER, "StopDiscovery")
            except BluezError:
                pass
        return seen

    def find(self, addresses, timeout=20.0):
        """Targeted scan: stop as soon as every requested address has been seen."""
        return self._run(self._find({a.upper() for a in addresses}, timeout), timeout + 15)

    async def _find(self, want, timeout):
        found = {d.address: d for d in await self._devices() if d.address in want and d.paired}
        if not want - set(found):
            return found
        try:
            await self._call(self.adapter, ADAPTER, "SetDiscoveryFilter", "a{sv}",
                             [{"Transport": Variant("s", "bredr")}])
        except BluezError:
            pass
        try:
            await self._call(self.adapter, ADAPTER, "StartDiscovery")
        except BluezError as e:
            if "InProgress" not in e.name:
                raise
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        try:
            while loop.time() < end and want - set(found):
                for d in await self._devices():
                    if d.address in want and (d.rssi is not None or d.paired):
                        found[d.address] = d
                await asyncio.sleep(0.25)
        finally:
            try:
                await self._call(self.adapter, ADAPTER, "StopDiscovery")
            except BluezError:
                pass
        return found

    def pair(self, device, pin_provider):
        if not self._pair_lock.acquire(blocking=False):
            raise PairingError("Another pairing is in progress.", "Wait for it to finish.", retryable=True)
        try:
            self._pin_provider = pin_provider
            self._run(self._pair(device.address), 120)
        except BluezError as e:
            raise map_pair_error(e) from e
        except TimeoutError as e:
            raise PairingError("Pairing timed out.", NOT_RESPONDING_HINT, retryable=True) from e
        finally:
            self._pin_provider = None
            self._pair_lock.release()

    async def _pair(self, address):
        if not self._agent_registered:
            self.bus.export(AGENT_PATH, _Agent(self))
            await self._call("/org/bluez", "org.bluez.AgentManager1", "RegisterAgent", "os",
                             [AGENT_PATH, "KeyboardDisplay"])
            self._agent_registered = True
        dev = next((d for d in await self._devices() if d.address == address.upper()), None)
        scanning = False
        if dev is None:
            # BlueZ keeps an unpaired device only while it is being discovered: scan for this
            # address and pair while the scan is still running, so the record cannot vanish.
            try:
                await self._call(self.adapter, ADAPTER, "StartDiscovery")
                scanning = True
            except BluezError as e:
                scanning = "InProgress" in e.name
            loop = asyncio.get_running_loop()
            end = loop.time() + 20
            while dev is None and loop.time() < end:
                await asyncio.sleep(0.25)
                dev = next((d for d in await self._devices() if d.address == address.upper()), None)
            if dev is None:
                await self._stop_discovery(scanning)
                raise PairingError("The device is not visible over Bluetooth.", NOT_RESPONDING_HINT, retryable=True)
        try:
            await asyncio.wait_for(self._call(dev.platform_id, DEVICE, "Pair"), 100)
        except BluezError as e:
            if not e.name.endswith("AlreadyExists"):
                raise
        finally:
            await self._stop_discovery(scanning)
        await self._call(dev.platform_id, PROPS, "Set", "ssv", [DEVICE, "Trusted", Variant("b", True)])
        try:
            await self._call(dev.platform_id, DEVICE, "Disconnect")
        except BluezError:
            pass

    async def _stop_discovery(self, scanning):
        if scanning:
            try:
                await self._call(self.adapter, ADAPTER, "StopDiscovery")
            except BluezError:
                pass

    def forget(self, device):
        path = device.platform_id
        if not path:
            dev = next((d for d in self._run(self._devices(), 10) if d.address == device.address.upper()), None)
            if dev is None:
                return
            path = dev.platform_id
        self._run(self._call(self.adapter, ADAPTER, "RemoveDevice", "o", [path]), 15)
