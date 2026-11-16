"""Аудиовывод: интерфейс и простая эталонная реализация для тестов."""

from __future__ import annotations

import time
from typing import Any, Callable, Protocol, runtime_checkable

EndHandler = Callable[[str], None]


@runtime_checkable
class AudioBackend(Protocol):
    on_end: EndHandler | None

    def load(self, url: str, start_ms: int = 0) -> None: ...
    def play(self) -> None: ...
    def pause(self) -> None: ...
    def stop(self) -> None: ...
    def set_volume(self, volume: int) -> None: ...
    def seek(self, position_ms: int) -> None: ...
    def position_ms(self) -> int: ...
    def is_playing(self) -> bool: ...
    def set_audio_filter(self, graph: str) -> None: ...
    def audio_filter(self) -> str | None: ...
    def audio_devices(self) -> list[dict[str, Any]]: ...
    def audio_device(self) -> str | None: ...
    def set_audio_device(self, device_id: str) -> None: ...
    def shutdown(self) -> None: ...


class NullBackend:
    """Ничего не воспроизводит, но честно повторяет контракт.

    Используется для тестов, диагностики и окружений без звука.
    """

    def __init__(self, on_end: EndHandler | None = None):
        self.on_end = on_end
        self.calls: list[tuple[str, object]] = []
        self.loaded_url: str | None = None
        self.volume = 100
        self.audio_filter_value: str | None = None
        self.audio_device_value: str = "auto"
        self._playing = False
        self._position = 0
        self._started_at: float | None = None

    def load(self, url: str, start_ms: int = 0) -> None:
        self.calls.append(("load", url))
        self.loaded_url = url
        self._position = max(0, int(start_ms))
        self._playing = False
        self._started_at = None

    def play(self) -> None:
        self.calls.append(("play", None))
        if not self._playing:
            self._started_at = time.monotonic()
        self._playing = True

    def pause(self) -> None:
        self.calls.append(("pause", None))
        if self._playing:
            self._position = self.position_ms()
        self._playing = False
        self._started_at = None

    def stop(self) -> None:
        self.calls.append(("stop", None))
        self._playing = False
        self._position = 0
        self._started_at = None

    def set_volume(self, volume: int) -> None:
        self.calls.append(("volume", volume))
        self.volume = int(volume)

    def seek(self, position_ms: int) -> None:
        self.calls.append(("seek", position_ms))
        self._position = max(0, int(position_ms))
        if self._playing:
            self._started_at = time.monotonic()

    def position_ms(self) -> int:
        if self._playing and self._started_at is not None:
            return self._position + int((time.monotonic() - self._started_at) * 1000)
        return self._position

    def is_playing(self) -> bool:
        return self._playing

    def set_audio_filter(self, graph: str) -> None:
        self.calls.append(("af", graph))
        self.audio_filter_value = graph or None

    def audio_devices(self) -> list[dict[str, Any]]:
        return [{"name": "auto", "description": "Autoselect device"}]

    def audio_device(self) -> str | None:
        return self.audio_device_value

    def set_audio_device(self, device_id: str) -> None:
        self.calls.append(("device", device_id))
        self.audio_device_value = device_id

    def audio_filter(self) -> str | None:
        return self.audio_filter_value

    def shutdown(self) -> None:
        self.calls.append(("shutdown", None))
        self._playing = False

    def finish(self, reason: str = "eof") -> None:
        """Симулировать завершение трека (для тестов и демо-режима)."""
        self._playing = False
        self._position = 0
        if self.on_end:
            self.on_end(reason)
