"""Byte transports to a Shimmer's Bluetooth Serial Port Profile link.

pyshimmer needs a pyserial-like object: blocking read() (timeout=None) that can be
aborted with cancel_read(), write(), reset_input_buffer(), close().

* Linux:   native AF_BLUETOOTH RFCOMM socket (no `rfcomm bind`, no root). The RFCOMM
           channel differs between Shimmer radios (RN42: 1, newer modules e.g. 6), so it
           is looked up via SDP (sdptool) or probed, then cached in the config.
* Windows: the outgoing "Standard Serial over Bluetooth link" COM port that Windows
           creates when a Shimmer is paired (the method the Shimmer manual describes),
           falling back to a native RFCOMM socket.
"""
import errno
import logging
import re
import shutil
import socket
import subprocess
import sys
import threading
import time

log = logging.getLogger("shimmerlive.transport")

GET_FW_VERSION = 0x2E
FW_VERSION_RESPONSE = 0x2F
ACK = 0xFF
STOP_STREAMING = 0x20
PROBE_CHANNELS = range(1, 11)

# errnos meaning "the device did not answer at all" (off, out of range, busy paging)
UNREACHABLE = {getattr(errno, n, -1) for n in ("EHOSTDOWN", "EHOSTUNREACH", "ETIMEDOUT", "WSAETIMEDOUT",
                                              "WSAEHOSTDOWN", "WSAEHOSTUNREACH")} | {10060, 10064, 10065}


class DeviceUnreachable(ConnectionError):
    """The Shimmer did not answer the Bluetooth page (off, out of range, or connected elsewhere)."""


BUSY = {getattr(errno, "EBUSY", -1), getattr(errno, "EAGAIN", -1), getattr(errno, "EALREADY", -1),
        getattr(errno, "EINPROGRESS", -1)}
FULL = {getattr(errno, "EMLINK", -1)}
_open_lock = threading.Lock()
_open_links = 0                          # RFCOMM links this app has open right now
# Connections are set up one at a time. Links still being set up also count against the adapter's
# limit, so with 10 Shimmers connecting together "Too many links" came when only 1 was open, and a
# Shimmer within the limit was turned away. One at a time, "full" means full (6 on an Intel AX211).
CONNECT_LOCK = threading.Lock()


class AdapterBusy(ConnectionError):
    """This PC's Bluetooth adapter was busy setting up another connection: try again shortly."""


class AdapterFull(ConnectionError):
    """This PC's Bluetooth adapter holds no more connections ("Too many links"). Measured on an Intel
    AX211 under Linux: 6 Shimmers at once, the 7th is refused at once with EMLINK."""

    def __init__(self, limit: int):
        super().__init__(f"this computer's Bluetooth is full ({limit} connections at once): stream fewer "
                         f"Shimmers, or record the others on their SD cards")
        self.limit = limit


def _count(delta):
    global _open_links
    with _open_lock:
        _open_links += delta


class RfcommSocket:
    timeout = None
    _counted = False

    def __init__(self, mac: str, channel: int, connect_timeout: float = 12.0):
        self.mac, self.channel = mac, channel
        self._sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        self._sock.settimeout(connect_timeout)
        try:
            with CONNECT_LOCK:
                self._sock.connect((mac, channel))
        except OSError as e:
            self._sock.close()
            if e.errno in FULL:
                raise AdapterFull(_open_links) from e
            if e.errno in BUSY:
                raise AdapterBusy(f"{mac}: Bluetooth adapter busy ({e.strerror or e})") from e
            raise
        _count(+1)
        self._counted = True
        self._sock.settimeout(None)
        self._cancelled = False
        self.description = f"RFCOMM {mac} ch {channel}"

    def read(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n and not self._cancelled:
            try:
                chunk = self._sock.recv(n - len(buf))
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
        return bytes(buf)

    def read_timeout(self, n: int, timeout: float) -> bytes:
        self._sock.settimeout(timeout)
        try:
            return self._sock.recv(n)
        except (socket.timeout, OSError):
            return b""
        finally:
            self._sock.settimeout(None)

    def write(self, data: bytes) -> int:
        self._sock.sendall(data)
        return len(data)

    def reset_input_buffer(self) -> None:
        self._sock.settimeout(0.0)
        try:
            while self._sock.recv(4096):
                pass
        except OSError:
            pass
        finally:
            self._sock.settimeout(None)

    def flush(self) -> None:
        pass

    def cancel_read(self) -> None:
        self._cancelled = True
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def close(self) -> None:
        if self._counted:
            self._counted = False
            _count(-1)
        try:
            self._sock.close()
        except OSError:
            pass


class SerialPortLink:
    """Wraps pyserial for a Windows Bluetooth COM port (opening it pages the device)."""
    timeout = None

    def __init__(self, port: str):
        import serial
        self._ser = serial.Serial(port, 115200, timeout=None, write_timeout=5)
        self.description = f"{port}"

    def read(self, n):
        return self._ser.read(n)

    def read_timeout(self, n, timeout):
        self._ser.timeout = timeout
        try:
            return self._ser.read(n)
        finally:
            self._ser.timeout = None

    def write(self, data):
        return self._ser.write(data)

    def reset_input_buffer(self):
        self._ser.reset_input_buffer()

    def flush(self):
        self._ser.flush()

    def cancel_read(self):
        try:
            self._ser.cancel_read()
        except Exception:
            pass

    def close(self):
        try:
            self._ser.close()
        except Exception:
            pass


def quiesce(link) -> None:
    """A Shimmer left streaming (e.g. by a crashed session) floods the link with data
    packets, which breaks pyshimmer's command/response handshake. Stop it and drain."""
    link.write(bytes([STOP_STREAMING]))
    deadline = time.monotonic() + 0.6
    while time.monotonic() < deadline:
        time.sleep(0.15)
        link.reset_input_buffer()


def answers_as_shimmer(link) -> bool:
    """Ask for the firmware version; a Shimmer replies ACK + 0x2F + 6 bytes."""
    quiesce(link)
    link.write(bytes([GET_FW_VERSION]))
    reply = b""
    deadline = time.monotonic() + 2.5
    while time.monotonic() < deadline and len(reply) < 2:
        reply += link.read_timeout(8, 0.5)
    return len(reply) >= 2 and reply[0] == ACK and reply[1] == FW_VERSION_RESPONSE


def sdp_serial_channel(mac: str) -> int | None:
    if not shutil.which("sdptool"):
        return None
    try:
        out = subprocess.run(["sdptool", "browse", mac], capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    for block in out.split("Service Name:")[1:]:
        if re.match(r"\s*Serial\s*Port", block, re.I):
            m = re.search(r"Channel:\s*(\d+)", block)
            if m:
                return int(m.group(1))
    return None


def _is_unreachable(e: OSError) -> bool:
    return isinstance(e, socket.timeout) or e.errno in UNREACHABLE or getattr(e, "winerror", None) in UNREACHABLE


def find_rfcomm_channel(mac: str) -> int:
    ch = sdp_serial_channel(mac)
    if ch:
        log.info("%s: SDP reports Serial Port on RFCOMM channel %d", mac, ch)
        return ch
    log.info("%s: no SDP answer, probing RFCOMM channels %s", mac, list(PROBE_CHANNELS))
    for ch in PROBE_CHANNELS:
        try:
            link = RfcommSocket(mac, ch)
        except (AdapterBusy, AdapterFull):
            raise
        except OSError as e:
            if _is_unreachable(e):
                raise DeviceUnreachable(f"{mac} did not answer ({e})") from e
            continue                         # refused: nothing on this channel
        try:
            if answers_as_shimmer(link):
                log.info("%s: Shimmer answers on RFCOMM channel %d", mac, ch)
                return ch
        except OSError:
            pass
        finally:
            link.close()
    raise ConnectionError(f"{mac}: no RFCOMM channel answered like a Shimmer")


def windows_com_port(mac: str) -> str | None:
    """Outgoing SPP COM port Windows assigned to this MAC (hwid contains the address)."""
    from serial.tools import list_ports
    needle = mac.replace(":", "").upper()
    for p in list_ports.comports():
        if needle in (p.hwid or "").upper().replace(":", ""):
            return p.device
    return None


def open_link(mac: str, cfg) -> object:
    """Open a raw link to a Shimmer, discovering and caching the RFCOMM channel."""
    mac = mac.upper()
    if sys.platform == "win32":
        port = windows_com_port(mac)
        if port:
            try:
                return SerialPortLink(port)
            except Exception as e:
                log.warning("%s: opening %s failed (%s), trying a direct RFCOMM socket", mac, port, e)
        if not hasattr(socket, "BTPROTO_RFCOMM"):
            raise ConnectionError(f"{mac}: no Bluetooth COM port found - is the device paired?")

    try:
        return _open_rfcomm(mac, cfg)
    except AdapterFull as e:
        if e.limit and cfg.data.get("bt_max_links") != e.limit:
            cfg.data["bt_max_links"] = e.limit          # remembered: the start page warns before exceeding it
            cfg.save()
        raise


def _open_rfcomm(mac, cfg):
    entry = cfg.device(mac)
    ch = entry.get("rfcomm_channel")
    if ch:
        try:
            return RfcommSocket(mac, ch)
        except (AdapterBusy, AdapterFull):
            raise                                # not a channel problem
        except OSError as e:
            if _is_unreachable(e):
                raise DeviceUnreachable(f"{mac} did not answer ({e.strerror or e})") from e
            log.info("%s: cached channel %d refused (%s), rediscovering", mac, ch, e)
    ch = find_rfcomm_channel(mac)
    cfg.update_device(mac, rfcomm_channel=ch)
    time.sleep(1.0)       # the radio reports EBUSY if re-paged right after the probe closed
    try:
        return RfcommSocket(mac, ch)
    except OSError as e:
        if _is_unreachable(e):
            raise DeviceUnreachable(f"{mac} did not answer ({e.strerror or e})") from e
        raise
