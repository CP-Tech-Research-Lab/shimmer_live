from dataclasses import replace
from types import SimpleNamespace

import pytest
from pyshimmer.dev.channels import ESensorGroup as S, SensorChannelAssignment

from shimmer_live import device_config as dc
from shimmer_live.config import Config


class FakeDevice:
    def __init__(self, power=False, accepts_power=True):
        self.sensors = {S.GSR}
        self.power, self.accepts_power = power, accepts_power
        self.writes = []

    def get_firmware_version(self):
        return SimpleNamespace(name="LogAndStream"), SimpleNamespace(major=1, minor=1, rel=3)

    def get_inquiry(self):
        return 256, None, [c for s in self.sensors for c in SensorChannelAssignment[s]]

    def get_device_name(self):
        return "test"

    def get_experiment_id(self):
        return ""

    def get_battery_state(self, calibrated):
        return 90

    def get_status(self):
        return [False] * 8

    def set_sensors(self, sensors):
        self.sensors = set(sensors)

    def _process_and_wait(self, command):
        if isinstance(command, dc._GetGsrRange):
            return 4
        if isinstance(command, dc._GetExpansionPower):
            if self.power is None:
                raise TimeoutError("no power response")
            return self.power
        if isinstance(command, dc._SetExpansionPower):
            self.writes.append(command.enabled)
            if self.accepts_power:
                self.power = command.enabled
            return
        raise AssertionError(command)


def configurator(tmp_path, **kwargs):
    conf = dc.DeviceConfigurator("00:11:22:33:44:55", Config(tmp_path / "cfg.json"))
    conf._dev = FakeDevice(**kwargs)
    return conf


def test_ppg_apply_enables_adc_and_power_and_remembers_intent(tmp_path):
    conf = configurator(tmp_path)
    old = conf.read().settings
    assert not conf.apply(old, replace(old, ppg_enabled=True))
    assert conf._dev.writes == [True]                  # only ON, and only because it was OFF
    got = conf.read().settings
    assert got.ppg_enabled and got.expansion_power is True and S.CH_A13 in got.sensors
    assert Config(conf.cfg.path).data["ppg_inputs"][conf.mac]


@pytest.mark.parametrize("reported_power", [False, None])
def test_ppg_power_must_be_read_back_not_just_acknowledged(tmp_path, reported_power):
    conf = configurator(tmp_path, power=reported_power, accepts_power=False)
    old = conf.read().settings
    with pytest.raises((RuntimeError, TimeoutError)):
        conf.apply(old, replace(old, ppg_enabled=True))
    assert conf._dev.writes == [True]  # ON attempted (never OFF), and the failed check is reported
    assert not conf.cfg.data.get("ppg_inputs", {}).get(conf.mac)


def test_generic_a13_does_not_enable_probe_power(tmp_path):
    conf = configurator(tmp_path)
    old = conf.read().settings
    assert not conf.apply(old, replace(old, sensors=old.sensors | {S.CH_A13}))
    assert conf._dev.writes == [False] and not conf._dev.power


def test_ppg_rejects_strain_conflict_before_writing(tmp_path):
    conf = configurator(tmp_path)
    old = conf.read().settings
    with pytest.raises(ValueError, match="conflicts"):
        conf.apply(old, replace(old, ppg_enabled=True, sensors=frozenset({S.STRAIN})))
    assert conf._dev.writes == []


def test_expansion_power_protocol_bytes():
    calls = []
    serial = SimpleNamespace(write_command=lambda *args: calls.append(args),
                             read_response=lambda *args, **kwargs: 1)
    get = dc._GetExpansionPower()
    get.send(serial)
    assert get.receive(serial) is True
    dc._SetExpansionPower(True).send(serial)
    dc._SetExpansionPower(False).send(serial)
    assert calls == [(0x60,), (0x5E, "<B", 1), (0x5E, "<B", 0)]
    serial.read_response = lambda *args, **kwargs: 7
    with pytest.raises(ValueError, match="Invalid"):
        get.receive(serial)


@pytest.fixture
def dialog(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from shimmer_live.ui import device_settings as ui
    app = QApplication.instance() or QApplication([])
    conf = configurator(tmp_path, power=None)
    conf.connect = lambda **kwargs: None
    conf.close = lambda: None

    def immediate(fn, done=None, failed=None, name=None):
        result = fn()
        if done:
            done(result)

    monkeypatch.setattr(ui, "run_bg", immediate)
    dlg = ui.DeviceSettingsDialog(None, conf.mac, "test", conf.cfg, configurator=conf)
    yield dlg
    dlg.close()
    dlg.deleteLater()
    app.processEvents()


def test_ppg_ui_selects_adc_locks_power_and_describes_change(dialog):
    dialog.ppg_box.setChecked(True)
    new = dialog._wanted()
    assert S.CH_A13 in new.sensors and new.expansion_power is True and new.ppg_enabled
    assert not dialog.power_box.isEnabled()
    assert any("3 V expansion power: unknown → ON" in text
               for text in dialog.describe_changes(dialog.info.settings, new))


def test_disabling_adc_clears_ppg_but_preserves_shared_power(dialog):
    dialog.ppg_box.setChecked(True)
    dialog.sensor_boxes[S.CH_A13].setChecked(False)
    assert not dialog.ppg_box.isChecked()
    assert dialog.power_box.isEnabled() and dialog.power_box.isChecked()


def test_unknown_power_is_not_silently_treated_as_off(dialog):
    assert dialog._wanted().expansion_power is None
    dialog.sensor_boxes[S.CH_A13].setChecked(True)
    assert dialog._wanted().expansion_power is None


@pytest.mark.parametrize("power, writes", [(True, []), (False, [True])])
def test_apply_ppg_only_writes_power_when_it_is_off(tmp_path, power, writes):
    """The firmware powers the probe during acquisition by itself: never write OFF, never re-write ON."""
    conf = configurator(tmp_path, power=power)
    conf._dev.sensors.add(S.CH_A13)
    conf.cfg.data['ppg_inputs'] = {conf.mac: True}
    old = conf.read().settings
    assert not conf.apply(old, replace(old, ppg_enabled=True))
    assert conf._dev.writes == writes


def test_unchanged_ppg_settings_are_not_treated_as_nothing_to_apply(dialog):
    dialog.ppg_box.setChecked(True)
    old = dialog._wanted()
    assert any('3 V power setting is ON' in text for text in dialog.describe_changes(old, old))


def test_power_is_never_switched_off_for_ppg(tmp_path):
    from shimmer_live.expansion_power import ensure_ppg_power
    conf = configurator(tmp_path, power=True)
    conf._dev.get_status = lambda: [False, True, True, True, False, True, False, False]
    ensure_ppg_power(conf._dev, dc.timed)
    assert conf._dev.writes == []


@pytest.mark.parametrize('first', [True, False])
def test_live_start_and_reconnect_keep_power_on_without_rewriting(tmp_path, monkeypatch, first):
    from shimmer_live import session
    from .conftest import make_session
    conf = configurator(tmp_path, power=True)
    conf._dev.sensors.add(S.CH_A13)
    dev = conf._dev
    dev.initialize = lambda: None
    dev.add_stream_callback = lambda callback: None
    starts = []
    dev.start_streaming = lambda: starts.append(list(dev.writes))
    link = SimpleNamespace(description='test', cancel_read=lambda: None, close=lambda: None)
    monkeypatch.setattr(session, 'open_link', lambda *args: link)
    monkeypatch.setattr(session, 'quiesce', lambda *args: None)
    monkeypatch.setattr(session, 'ShimmerBluetooth', lambda *args: dev)
    s = make_session(tmp_path, dev.get_inquiry()[2], 256)
    s.cfg.data['ppg_inputs'] = {s.mac: True}
    s._open(first)
    assert starts == [[]]                              # already ON: nothing written before streaming
    assert not s.ppg_docked


def test_ppg_in_the_base_is_reported(tmp_path):
    """In a base the probe loses power after acquisition starts: the app must say so."""
    from shimmer_live.expansion_power import prepare_ppg
    conf = configurator(tmp_path, power=True)
    conf._dev.sensors.add(S.CH_A13)
    conf._dev.get_status = lambda: [True] + [False] * 7               # docked
    conf.cfg.data['ppg_inputs'] = {conf.mac: True}
    chans = conf._dev.get_inquiry()[2]
    assert prepare_ppg(conf._dev, conf.cfg, conf.mac, chans, dc.timed) == {"docked": True}
