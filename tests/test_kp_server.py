"""Keypoints from other programs over TCP: per-keypoint ports, names on the base port, answers, timing."""
import socket
import time

import pytest

from shimmer_live import kp_server
from shimmer_live.keypoints import Keypoint


def free_base(n):
    """A base port with n+1 free ports after it."""
    for base in range(47000, 60000, 50):
        socks = []
        try:
            for p in range(base, base + n + 1):
                s = socket.socket()
                s.bind(("127.0.0.1", p))
                socks.append(s)
            return base
        except OSError:
            continue
        finally:
            for s in socks:
                s.close()
    pytest.skip("no free ports")


@pytest.fixture
def server():
    kps = [Keypoint("rest start", "#111"), Keypoint("task start", "#222"), Keypoint("task end", "#333")]
    calls = []

    def handler(target, at):
        kp = kp_server.resolve(kps, target)
        calls.append((target, at, time.time_ns()))
        return f"OK {kp.name} {at['unix_ns']}" if kp else "ERR unknown"

    srv = kp_server.KeypointServer(handler)
    base = free_base(len(kps))
    srv.start(base, len(kps))
    yield srv, base, calls
    srv.stop()


def ask(port, payload, lines=1):
    with socket.create_connection(("127.0.0.1", port), timeout=3) as c:
        c.sendall(payload)
        f = c.makefile("r")
        return [f.readline().strip() for _ in range(lines)]


def test_each_keypoint_has_its_own_port(server):
    srv, base, calls = server
    assert srv.ports == [base, base + 1, base + 2, base + 3] and not srv.errors
    assert ask(base + 2, b"\n")[0].startswith("OK task start ")
    assert calls[-1][0] == 1 and calls[-1][1]["port"] == base + 2


def test_base_port_takes_names_and_numbers(server):
    srv, base, calls = server
    assert ask(base, b"Task End\n")[0].startswith("OK task end")
    assert ask(base, b"1\n")[0].startswith("OK rest start")
    assert ask(base, b"nope\n") == ["ERR unknown"]
    assert ask(base, b"\n") == ["ERR send a keypoint name or number"]


def test_several_lines_on_one_connection_and_last_line_without_newline(server):
    srv, base, calls = server
    assert [r.split()[0] for r in ask(base + 1, b"a\nb\n", lines=2)] == ["OK", "OK"]
    with socket.create_connection(("127.0.0.1", base + 3), timeout=3) as c:
        c.sendall(b"go")
        c.shutdown(socket.SHUT_WR)
        assert c.makefile("r").readline().startswith("OK task end")


def test_time_is_taken_when_the_message_arrives(server):
    srv, base, calls = server
    before = time.time_ns()
    ask(base + 1, b"\n")
    target, at, handled = calls[-1]
    assert before <= at["unix_ns"] <= handled and {"perf_ns", "shared_ticks"} <= set(at)


def test_busy_port_is_reported_and_stop_frees_ports(server):
    srv, base, calls = server
    other = kp_server.KeypointServer(lambda t, a: "OK")
    other.start(base, 3)
    assert set(other.errors) == {base, base + 1, base + 2, base + 3}
    other.stop()
    srv.stop()
    again = kp_server.KeypointServer(lambda t, a: "OK x 1")
    again.start(base, 3)
    assert not again.errors
    assert ask(base + 1, b"\n") == ["OK x 1"]
    again.stop()




def test_sd_keypoint_keeps_the_arrival_time_and_source(tmp_path):
    from shimmer_live import sdlog
    from shimmer_live.config import Config
    cfg = Config(tmp_path / "c.json")
    sdlog.new_session(cfg, "P1", [("00:06:66:00:00:01", "Shimmer3-0001")], "raw", [])
    at = {"unix_ns": 123, "perf_ns": 1, "shared_ticks": 456, "port": 4002}
    ev = sdlog.add_keypoint(cfg, Keypoint("task start", "#222"), at, "tcp:4002")
    assert (ev["unix_ns"], ev["shared_ticks"], ev["source"]) == (123, 456, "tcp:4002")
    assert "source" not in sdlog.add_keypoint(cfg, Keypoint("task end", "#333"))
