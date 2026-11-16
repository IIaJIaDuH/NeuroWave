"""Аудиовыходы: классификация, кодек Bluetooth и безопасное переключение.

Главный риск, который здесь закрывается: mpv молча принимает несуществующий
id устройства и продолжает «играть в никуда». Поэтому id проверяется по
списку mpv до переключения, а не после.
"""

from __future__ import annotations

import json

import pytest

from neurowave.audio import NullBackend
from neurowave.audio.devices import (
    AudioOutput,
    build_outputs,
    classify,
    describe_kind,
    find_output,
    is_plugin_device,
    parse_mpv_device_list,
    parse_sinks,
)
from neurowave.audio.mpv import MpvBackend
from neurowave.cli import main
from neurowave.config import ConfigStore, Paths, Settings
from neurowave.errors import PlayerError
from neurowave.player import PlayerService
from tests.conftest import FakeAdapter, make_track

MPV_HELP = """List of detected audio devices:
  'auto' (Autoselect device)
  'pulse/bluez_output.59_19_70_A9_CF_F8.1' (QuietComfort Ultra)
  'pulse/alsa_output.pci-0000_09_00.1.hdmi-stereo' (HDA NVidia Цифровой стерео (HDMI))
  'alsa' (Default (alsa))
  'alsa/lavrate' (Rate Converter Plugin Using Libav/FFmpeg Library)
"""

# Реальный фрагмент `pactl list sinks` (локаль — русская, заголовки локализованы).
PACTL = """Аудиоприёмник №3748
	Состояние: RUNNING
	Имя: bluez_output.59_19_70_A9_CF_F8.1
	Описание: QuietComfort Ultra
	Свойства:
		api.bluez5.codec = "sbc"
		api.bluez5.profile = "a2dp-sink"
		device.description = "QuietComfort Ultra"
		node.name = "bluez_output.59_19_70_A9_CF_F8.1"

Аудиоприёмник №3760
	Имя: alsa_output.pci-0000_09_00.1.hdmi-stereo
	Описание: HDA NVidia
	Свойства:
		device.profile.name = "hdmi-stereo"
		node.name = "alsa_output.pci-0000_09_00.1.hdmi-stereo"
"""


@pytest.mark.parametrize(
    "device_id,description,expected",
    [
        ("auto", "Autoselect device", "auto"),
        ("pulse/bluez_output.59_19_70_A9_CF_F8.1", "QuietComfort Ultra", "bluetooth"),
        ("pulse/bluez_output.X", "Headset (Hands-Free)", "bluetooth"),
        ("pulse/alsa_output.pci-0000_09_00.1.hdmi-stereo", "HDA NVidia HDMI", "hdmi"),
        ("alsa/hdmi:CARD=NVidia,DEV=0", "", "hdmi"),
        ("pulse/alsa_output.usb-Focusrite", "USB DAC", "usb"),
        ("pulse/alsa_output.pci-0000_00_1f.3.analog-stereo", "Analog", "analog"),
        ("pulse/alsa_output.pci-0000_00_1f.3.analog-stereo", "Speakers", "analog"),
        ("pulse/raop_sink", "AirPlay", "network"),
        ("coreaudio/xyz", "Something", "other"),
    ],
)
def test_classify(device_id: str, description: str, expected: str) -> None:
    assert classify(device_id, description) == expected


def test_parse_mpv_device_list_skips_plugins_text() -> None:
    devices = parse_mpv_device_list(MPV_HELP)
    assert devices[0] == ("auto", "Autoselect device")
    assert devices[1][0].startswith("pulse/bluez_output")
    assert len(devices) == 5


def test_parse_sinks_is_locale_independent() -> None:
    sinks = parse_sinks(PACTL)
    bt = sinks["bluez_output.59_19_70_A9_CF_F8.1"]
    assert bt["api.bluez5.codec"] == "sbc"
    assert bt["api.bluez5.profile"] == "a2dp-sink"
    assert "alsa_output.pci-0000_09_00.1.hdmi-stereo" in sinks


def test_build_outputs_merges_codec_and_marks_current() -> None:
    devices = parse_mpv_device_list(MPV_HELP)
    outputs = build_outputs(devices, parse_sinks(PACTL), "pulse/bluez_output.59_19_70_A9_CF_F8.1")
    bt = find_output(outputs, "pulse/bluez_output.59_19_70_A9_CF_F8.1")
    hdmi = find_output(outputs, "pulse/alsa_output.pci-0000_09_00.1.hdmi-stereo")
    assert bt is not None and bt.codec == "sbc" and bt.profile == "a2dp-sink"
    assert bt.is_bluetooth and bt.current is True
    assert hdmi is not None and hdmi.codec is None and hdmi.kind == "hdmi" and hdmi.current is False
    assert "SBC" in bt.label()


def test_raw_alsa_devices_are_hidden_when_pulse_exists() -> None:
    devices = parse_mpv_device_list(MPV_HELP)
    outputs = build_outputs(devices, {}, None)
    ids = [output.id for output in outputs]
    assert "alsa" not in ids  # сырой дубль pulse-маршрута
    assert "pulse/bluez_output.59_19_70_A9_CF_F8.1" in ids


def test_alsa_devices_stay_when_pulse_absent() -> None:
    outputs = build_outputs([("auto", "Autoselect"), ("alsa", "Default (alsa)")], {}, None)
    assert [output.id for output in outputs] == ["auto", "alsa"]


def test_plugin_devices_are_recognized() -> None:
    assert is_plugin_device("alsa/lavrate")
    assert is_plugin_device("alsa/speexrate")
    assert not is_plugin_device("pulse/alsa_output.pci-0000_09_00.1.hdmi-stereo")
    assert not is_plugin_device("auto")


def test_describe_kind_has_human_names() -> None:
    assert describe_kind("bluetooth") == "Bluetooth"
    assert describe_kind("hdmi") == "HDMI"


class RecordingBackend(NullBackend):
    is_running = True

    def __init__(self, devices: list[dict] | None = None, fail_on: str | None = None):
        super().__init__()
        self._devices = devices or [
            {"name": "auto", "description": "Autoselect"},
            {"name": "pulse/fake-sink", "description": "Fake Sink"},
        ]
        self.fail_on = fail_on

    def audio_devices(self) -> list[dict]:
        return self._devices

    def set_audio_device(self, device_id: str) -> None:
        if self.fail_on == device_id:
            raise PlayerError("mpv отклонил переключение")
        super().set_audio_device(device_id)


def _player(backend: NullBackend, **settings) -> PlayerService:
    return PlayerService(
        adapter=FakeAdapter(), backend=backend, settings=Settings(**settings), prefetch=False
    )


def test_unknown_output_is_rejected_before_ipc() -> None:
    backend = RecordingBackend()
    player = _player(backend)
    with pytest.raises(PlayerError) as exc:
        player.set_output("pulse/nonexistent")
    assert "Доступны" in str(exc.value)
    assert not [call for call in backend.calls if call[0] == "device"]  # до mpv не дошло


def test_output_is_applied_and_persisted(fake_mpv, tmp_path) -> None:
    backend = MpvBackend(
        binary=str(fake_mpv), socket_path=tmp_path / "dev.sock", extra_args=["--fake-plan=idle"]
    )
    player = _player(backend)
    try:
        player.play_tracks([make_track(1)])
        output = player.set_output("pulse/fake-sink")
        assert output.id == "pulse/fake-sink"
        assert backend.audio_device() == "pulse/fake-sink"
        assert player.settings.audio_output == "pulse/fake-sink"
    finally:
        player.close()


def test_failed_switch_restores_previous_output() -> None:
    backend = RecordingBackend(fail_on="pulse/fake-sink")
    player = _player(backend, audio_output="auto")
    with pytest.raises(PlayerError):
        player.set_output("pulse/fake-sink")
    assert player.settings.audio_output == "auto"
    assert backend.audio_device() == "auto"


def test_selection_while_mpv_stopped_is_only_saved() -> None:
    backend = NullBackend()
    backend.is_running = False  # type: ignore[attr-defined]
    player = _player(backend)
    output = player.set_output("pulse/whatever")
    assert output.id == "pulse/whatever"
    assert player.settings.audio_output == "pulse/whatever"
    assert not [call for call in backend.calls if call[0] == "device"]


def test_missing_saved_output_does_not_break_playback(fake_mpv, tmp_path) -> None:
    backend = MpvBackend(
        binary=str(fake_mpv), socket_path=tmp_path / "gone.sock", extra_args=["--fake-plan=idle"]
    )
    player = _player(backend, audio_output="pulse/unplugged")
    try:
        player.play_tracks([make_track(1)])
        assert backend.audio_device() == "auto"  # ничего не сломали
        assert player.state.status.value == "playing"
    finally:
        player.close()


def test_saved_output_is_applied_on_play(fake_mpv, tmp_path) -> None:
    backend = MpvBackend(
        binary=str(fake_mpv), socket_path=tmp_path / "saved.sock", extra_args=["--fake-plan=idle"]
    )
    player = _player(backend, audio_output="pulse/fake-sink")
    try:
        player.play_tracks([make_track(1)])
        assert backend.audio_device() == "pulse/fake-sink"
    finally:
        player.close()


def test_output_from_mpv_list_has_human_fields(fake_mpv, tmp_path) -> None:
    backend = MpvBackend(
        binary=str(fake_mpv), socket_path=tmp_path / "list.sock", extra_args=["--fake-plan=idle"]
    )
    player = _player(backend)
    try:
        backend.start()
        outputs = player.outputs()
        fake = find_output(outputs, "pulse/fake-sink")
        assert fake is not None and fake.name == "Fake Sink"
        auto = find_output(outputs, "auto")
        assert auto is not None and auto.kind == "auto"
    finally:
        player.close()


def test_cli_output_lists_and_selects(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setattr("neurowave.cli.probe_mpv_devices", lambda *a, **k: parse_mpv_device_list(MPV_HELP))
    monkeypatch.setattr("neurowave.cli.probe_sinks", lambda *a, **k: parse_sinks(PACTL))

    assert main(["--home", str(tmp_path), "output"]) == 0
    text = capsys.readouterr().out
    assert "QuietComfort Ultra" in text and "SBC" in text and "HDMI" in text

    assert main(["--home", str(tmp_path), "--json", "output"]) == 0
    data = json.loads(capsys.readouterr().out)
    kinds = {item["id"]: item["kind"] for item in data["outputs"]}
    assert kinds["pulse/bluez_output.59_19_70_A9_CF_F8.1"] == "bluetooth"

    target = "pulse/alsa_output.pci-0000_09_00.1.hdmi-stereo"
    assert main(["--home", str(tmp_path), "output", "--set", target]) == 0
    assert ConfigStore(Paths.resolve({"NEUROWAVE_HOME": str(tmp_path)})).load().audio_output == target
    capsys.readouterr()


def test_cli_output_rejects_unknown_id(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setattr("neurowave.cli.probe_mpv_devices", lambda *a, **k: parse_mpv_device_list(MPV_HELP))
    monkeypatch.setattr("neurowave.cli.probe_sinks", lambda *a, **k: {})
    assert main(["--home", str(tmp_path), "output", "--set", "pulse/nope"]) == 2
    assert "Нет такого аудиовыхода" in capsys.readouterr().err


def test_default_sink_is_resolved_for_auto() -> None:
    """mpv с «auto» не говорит, куда играет — показываем системный маршрут."""
    from neurowave.audio.devices import parse_default_sink

    assert parse_default_sink("bluez_output.59_19_70_A9_CF_F8.1\n") == "bluez_output.59_19_70_A9_CF_F8.1"
    assert parse_default_sink("") is None

    devices = parse_mpv_device_list(MPV_HELP)
    outputs = build_outputs(
        devices,
        parse_sinks(PACTL),
        current="auto",
        default_sink="bluez_output.59_19_70_A9_CF_F8.1",
    )
    bt = find_output(outputs, "pulse/bluez_output.59_19_70_A9_CF_F8.1")
    assert bt is not None and bt.current is True and bt.via_auto is True
    hdmi = find_output(outputs, "pulse/alsa_output.pci-0000_09_00.1.hdmi-stereo")
    assert hdmi is not None and hdmi.current is False and hdmi.via_auto is False


def test_explicit_choice_wins_over_default_sink() -> None:
    devices = parse_mpv_device_list(MPV_HELP)
    target = "pulse/alsa_output.pci-0000_09_00.1.hdmi-stereo"
    outputs = build_outputs(
        devices, {}, current=target, default_sink="bluez_output.59_19_70_A9_CF_F8.1"
    )
    hdmi = find_output(outputs, target)
    bt = find_output(outputs, "pulse/bluez_output.59_19_70_A9_CF_F8.1")
    assert hdmi is not None and hdmi.current is True and hdmi.via_auto is False
    assert bt is not None and bt.current is False
