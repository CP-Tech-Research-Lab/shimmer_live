"""Keypoints from other programs over TCP: a message on a port marks a keypoint at that moment.

Ports (base port B, default 4000; keypoint N is the N-th in the Keypoints list):
  B + N   any line (even empty: "\\n") marks keypoint N (B + N is the default: each keypoint's port can be set)
  B       a line with a keypoint's name or number ("task start", "3") marks that keypoint

The time is taken the instant the line arrives, before the app does anything else, so a busy UI does
not delay it. Each line is answered with one line:
  OK <keypoint name> <unix time in ns>
  ERR <reason>                         (e.g. nothing is recording)

Only this computer can connect unless 'network' is enabled. Plain Python (no Qt): the handler decides
what a mark means and is called on the server thread."""
import logging
import selectors
import socket
import sys
import threading

from . import clocks

log = logging.getLogger("shimmerlive.tcp")

DEFAULT_BASE_PORT = 4000
MAX_LINE = 1024
CONFIG_KEY = "keypoints_tcp"


def settings(cfg) -> dict:
    s = {"enabled": True, "base_port": DEFAULT_BASE_PORT, "network": False}
    s.update(cfg.data.get(CONFIG_KEY) or {})
    return s


def save_settings(cfg, **changes) -> None:
    s = settings(cfg)
    s.update(changes)
    cfg.data[CONFIG_KEY] = s
    cfg.save()


def now() -> dict:
    """The instant of a mark on every clock the recordings use."""
    return {"unix_ns": clocks.unix_ns(), "perf_ns": clocks.perf_ns(), "shared_ticks": clocks.shared_clock_ticks()}


class KeypointServer:
    def __init__(self, handler):
        """handler(target, at) -> reply line. target: keypoint index (0-based) or the text sent to the base port;
        at: now() taken when the line arrived, plus "port"."""
        self.handler = handler
        self.base_port = DEFAULT_BASE_PORT
        self.targets = {}              # port -> keypoint index (None: the base port, which takes names/numbers)
        self.ports = []                # ports actually listening
        self.errors = {}               # port -> reason it could not listen
        self._thread = None
        self._stop = None

    @property
    def running(self) -> bool:
        return self._thread is not None

    def start(self, base_port: int, ports, network: bool = False) -> None:
        """Listen on base_port (names / numbers) and on ports[i] for keypoint i (keypoints.tcp_ports)."""
        self.stop()
        self.base_port = base_port
        self.targets = {base_port: None}
        for i, p in enumerate(ports):
            self.targets.setdefault(p, i)
        host = "0.0.0.0" if network else "127.0.0.1"
        sel = selectors.DefaultSelector()
        self.ports, self.errors = [], {}
        for port in self.targets:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                if sys.platform == "win32":     # SO_REUSEADDR on Windows would let two programs share a port
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                else:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((host, port))
                s.listen(8)
                s.setblocking(False)
                sel.register(s, selectors.EVENT_READ, ("listen", port))
                self.ports.append(port)
            except OSError as e:
                s.close()
                self.errors[port] = e.strerror or str(e)
                log.warning("keypoints over TCP: port %d not available: %s", port, self.errors[port])
        wake_r, wake_w = socket.socketpair()
        wake_r.setblocking(False)
        sel.register(wake_r, selectors.EVENT_READ, ("wake", None))
        self._stop = wake_w
        self._thread = threading.Thread(target=self._loop, args=(sel,), daemon=True, name="keypoints-tcp")
        self._thread.start()
        if self.ports:
            log.info("keypoints over TCP: listening on %s ports %d-%d", host, self.ports[0], self.ports[-1])

    def stop(self) -> None:
        if self._thread is None:
            return
        try:
            self._stop.send(b"x")
        except OSError:
            pass
        self._thread.join(timeout=2)
        self._stop.close()
        self._thread = self._stop = None
        self.ports = []

    # ---------------- server thread ----------------
    def _loop(self, sel):
        bufs = {}
        try:
            while True:
                for key, _ in sel.select():
                    kind, port = key.data
                    if kind == "wake":
                        return
                    if kind == "listen":
                        try:
                            conn, _addr = key.fileobj.accept()
                        except OSError:
                            continue
                        conn.setblocking(False)
                        bufs[conn] = b""
                        sel.register(conn, selectors.EVENT_READ, ("client", port))
                        continue
                    conn = key.fileobj
                    try:
                        data = conn.recv(4096)
                    except (BlockingIOError, InterruptedError):
                        continue
                    except OSError:
                        data = b""
                    at = now()                                   # the moment the message arrived
                    if data:
                        buf = bufs[conn] + data
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            self._answer(conn, port, line, at)
                        if len(buf) > MAX_LINE:
                            self._reply(conn, "ERR line too long")
                            buf = b""
                        bufs[conn] = buf
                    else:                                        # closed: a last message without newline counts
                        if bufs.get(conn):
                            self._answer(conn, port, bufs[conn], at)
                        sel.unregister(conn)
                        conn.close()
                        bufs.pop(conn, None)
        finally:
            for key in list(sel.get_map().values()):
                key.fileobj.close()
            sel.close()

    def _answer(self, conn, port, line, at):
        at = dict(at, port=port)
        text = line.decode("utf-8", "replace").strip()
        index = self.targets.get(port)
        target = index if index is not None else text
        if target == "":
            reply = "ERR send a keypoint name or number"
        else:
            try:
                reply = self.handler(target, at)
            except Exception as e:                   # noqa: BLE001 - never let a client stop the server
                log.warning("keypoint over TCP failed: %s", e)
                reply = f"ERR {e}"
        self._reply(conn, reply)

    @staticmethod
    def _reply(conn, text):
        try:
            conn.setblocking(True)
            conn.settimeout(1.0)
            conn.sendall((text + "\n").encode())
        except OSError:
            pass
        finally:
            try:
                conn.setblocking(False)
            except OSError:
                pass


def resolve(keypoints, target):
    """The keypoint a TCP message refers to: an index (per-keypoint port), or a name / 1-based number."""
    if isinstance(target, int):
        return keypoints[target] if 0 <= target < len(keypoints) else None
    t = target.strip()
    if t.isdigit():
        n = int(t)
        return keypoints[n - 1] if 1 <= n <= len(keypoints) else None
    return next((k for k in keypoints if k.name.lower() == t.lower()), None)
