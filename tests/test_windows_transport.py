from types import SimpleNamespace

from shimmer_live import transport


def test_first_windows_serial_open_timeout_is_retried(monkeypatch, tmp_path):
    import serial
    from shimmer_live.config import Config

    calls = []
    link = object()

    def open_port(port):
        calls.append(port)
        if len(calls) == 1:
            raise serial.SerialException("could not open port 'COM18': OSError(22, 'timeout', None, 121)")
        return link

    monkeypatch.setattr(transport.sys, "platform", "win32")
    monkeypatch.setattr(transport, "windows_com_port", lambda *a, **k: "COM18")
    monkeypatch.setattr(transport, "SerialPortLink", open_port)
    monkeypatch.setattr(transport.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(transport, "_open_rfcomm", lambda *a: (_ for _ in ()).throw(AssertionError("must use COM port")))
    assert transport.open_link("D8:47:8F:04:BB:C9", Config(tmp_path / "config.json")) is link
    assert calls == ["COM18", "COM18"]


def test_waits_for_new_outgoing_port_and_ignores_incoming(monkeypatch):
    from serial.tools import list_ports

    incoming = SimpleNamespace(device="COM15", hwid="BTHENUM\\000000000000_00000008")
    outgoing = SimpleNamespace(device="COM16", hwid="BTHENUM\\40843256A187_C00000000")
    scans = iter([[incoming], [incoming], [incoming, outgoing]])
    monkeypatch.setattr(list_ports, "comports", lambda: next(scans))
    monkeypatch.setattr(transport.time, "sleep", lambda seconds: None)
    assert transport.windows_com_port("40:84:32:56:A1:87", wait_seconds=15) == "COM16"


def test_missing_port_wait_is_bounded(monkeypatch):
    from serial.tools import list_ports

    clock = [0.0]
    monkeypatch.setattr(list_ports, "comports", lambda: [])
    monkeypatch.setattr(transport.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(transport.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    assert transport.windows_com_port("40:84:32:56:A1:87", wait_seconds=2) is None
    assert clock[0] == 2
