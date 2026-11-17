"""Аудиовыходы: что есть в системе и чем оно на самом деле является.

Модуль нужен, чтобы интерфейс показывал реальный маршрут: Bluetooth с кодеком
SBC, а не «какой-то pulse», и HDMI, а не «автоустройство». Он ничего не меняет
в PipeWire/WirePlumber: только читает список устройств mpv и свойства sink'ов
через `pactl`, а переключение делает mpv своей командой.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

KIND_BLUETOOTH = "bluetooth"
KIND_HDMI = "hdmi"
KIND_USB = "usb"
KIND_ANALOG = "analog"
KIND_NETWORK = "network"
KIND_AUTO = "auto"
KIND_OTHER = "other"

_MPV_LINE = re.compile(r"^\s*'(?P<id>[^']+)'\s*\((?P<desc>.*)\)\s*$")


@dataclass(frozen=True)
class AudioOutput:
    """Один выход, как его видит mpv, плюс то, что знает система."""

    id: str
    name: str
    kind: str = KIND_OTHER
    codec: str | None = None
    profile: str | None = None
    current: bool = False
    #: True, если играет не выбранный пользователем выход, а системный (auto).
    via_auto: bool = False

    @property
    def is_bluetooth(self) -> bool:
        return self.kind == KIND_BLUETOOTH

    def label(self) -> str:
        parts = [self.name]
        if self.kind != KIND_OTHER:
            parts.append(self.kind)
        if self.codec:
            parts.append(self.codec.upper())
        return " · ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "codec": self.codec,
            "profile": self.profile,
            "current": self.current,
            "via_auto": self.via_auto,
        }


def classify(device_id: str, description: str = "") -> str:
    """Тип выхода по id mpv и описанию.

    Порядок проверок важен: `pulse/bluez_output…hdmi` встречается, но это всё
    равно Bluetooth, а `alsa_output…hdmi-stereo` — HDMI, а не «аналог».
    """
    text = f"{device_id} {description}".lower()
    if device_id == "auto":
        return KIND_AUTO
    if "bluez" in text or "bluetooth" in text or "a2dp" in text:
        return KIND_BLUETOOTH
    if "hdmi" in text:
        return KIND_HDMI
    if "usb" in text:
        return KIND_USB
    if "raop" in text or "airplay" in text or "network" in text:
        return KIND_NETWORK
    if "alsa_output" in text or "analog" in text or "analogue" in text:
        return KIND_ANALOG
    return KIND_OTHER


def parse_mpv_device_list(text: str) -> list[tuple[str, str]]:
    """Разобрать вывод `mpv --audio-device=help` в (id, описание)."""
    devices: list[tuple[str, str]] = []
    for line in text.splitlines():
        match = _MPV_LINE.match(line)
        if match:
            devices.append((match.group("id"), match.group("desc").strip()))
    return devices


def _sink_key(device_id: str) -> str:
    """`pulse/bluez_output.X` → `bluez_output.X`: имя sink в pactl."""
    return device_id.split("/", 1)[1] if device_id.startswith("pulse/") else device_id


def parse_sinks(text: str) -> dict[str, dict[str, str]]:
    """Разобрать `pactl list sinks` в свойства по `node.name`.

    Заголовки pactl локализованы («Имя:», «Описание:»), а ключи свойств — нет,
    поэтому блок определяется по наличию `node.name`, а не по тексту заголовка.
    Нужны только кодек и профиль Bluetooth: остальное PipeWire решает сам.
    """
    sinks: dict[str, dict[str, str]] = {}
    block: dict[str, str] = {}
    for raw in list(text.splitlines()) + [""]:
        line = raw.strip()
        if not line:
            name = block.get("node.name")
            if name:
                sinks[name] = block
            block = {}
            continue
        if " = " in line:
            key, _, value = line.partition(" = ")
            block[key.strip()] = value.strip().strip('"')
    return sinks


#: Служебные ALSA-устройства mpv: в интерфейсе они только мешают.
_PLUGIN_MARKERS = (
    "lavrate",
    "samplerate",
    "speexrate",
    "jack",
    "pipewire",
    "lavcac3enc",
    "vdownmix",
    "dmix",
    "null",
    "oss",
    "speex",
    "sndio",
    "sdl",
    "upmix",
)


def is_plugin_device(device_id: str) -> bool:
    tail = device_id.split("/", 1)[-1]
    return any(marker in tail for marker in _PLUGIN_MARKERS)


def probe_sinks() -> dict[str, dict[str, str]]:
    """Свойства sink'ов из pactl; без pactl или сервера — пустой словарь."""
    binary = shutil.which("pactl")
    if binary is None:
        return {}
    try:
        result = subprocess.run(
            [binary, "list", "sinks"], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if result.returncode != 0:
        return {}
    return parse_sinks(result.stdout)


def parse_default_sink(text: str) -> str | None:
    """Имя sink по умолчанию из `pactl get-default-sink`."""
    name = text.strip().splitlines()[0].strip() if text.strip() else ""
    return name or None


def probe_default_sink() -> str | None:
    """Системный sink по умолчанию; без pactl или сервера — None."""
    binary = shutil.which("pactl")
    if binary is None:
        return None
    try:
        result = subprocess.run(
            [binary, "get-default-sink"], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return parse_default_sink(result.stdout)


def probe_mpv_devices(binary: str = "mpv", timeout: float = 5.0) -> list[tuple[str, str]]:
    """Список устройств без запуска плеера: `mpv --audio-device=help`."""
    executable = shutil.which(binary) or binary
    try:
        result = subprocess.run(
            [executable, "--audio-device=help"], capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_mpv_device_list(result.stdout)


def build_outputs(
    devices: Iterable[Mapping[str, Any] | tuple[str, str]],
    sinks: Mapping[str, Mapping[str, str]] | None = None,
    current: str | None = None,
    default_sink: str | None = None,
) -> list[AudioOutput]:
    """Слить список mpv и свойства sink'ов в один список выходов."""
    sinks = sinks or {}
    items = list(devices)
    # При работающем PipeWire/Pulse «сырые» alsa/-устройства дублируют pulse/-.
    if any(str((i.get("name") if isinstance(i, Mapping) else i[0])).startswith("pulse/") for i in items):
        items = [
            i
            for i in items
            if not str((i.get("name") if isinstance(i, Mapping) else i[0])).startswith("alsa")
        ]
    outputs: list[AudioOutput] = []
    for item in items:
        if isinstance(item, Mapping):
            device_id = str(item.get("name", ""))
            description = str(item.get("description", ""))
        else:
            device_id, description = item
        if not device_id or is_plugin_device(device_id):
            continue
        kind = classify(device_id, description)
        sink = sinks.get(_sink_key(device_id), {})
        is_current = bool(current) and current == device_id
        via_auto = False
        if not is_current and (current in (None, "auto")) and default_sink:
            # mpv с «auto» не говорит, куда играет; системный sink говорит.
            via_auto = _sink_key(device_id) == default_sink
            is_current = via_auto
        outputs.append(
            AudioOutput(
                id=device_id,
                name=description or device_id,
                kind=kind,
                codec=sink.get("api.bluez5.codec") if kind == KIND_BLUETOOTH else None,
                profile=sink.get("api.bluez5.profile") if kind == KIND_BLUETOOTH else None,
                current=is_current,
                via_auto=via_auto,
            )
        )
    return outputs


def find_output(outputs: Iterable[AudioOutput], device_id: str) -> AudioOutput | None:
    for output in outputs:
        if output.id == device_id:
            return output
    return None


def describe_kind(kind: str) -> str:
    return {
        KIND_BLUETOOTH: "Bluetooth",
        KIND_HDMI: "HDMI",
        KIND_USB: "USB",
        KIND_ANALOG: "аналоговый",
        KIND_NETWORK: "сеть",
        KIND_AUTO: "авто",
        KIND_OTHER: "другое",
    }.get(kind, kind)
