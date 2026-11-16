"""MPRIS-модель поверх ядра: состояние и команды без D-Bus.

Здесь нет ни `dbus-next`, ни `PySide6` — только перевод состояния
`PlayerService` в модель MPRIS и обратный перевод команд. Это позволяет
проверять всю семантику тестами без демона шины и без дисплея; D-Bus-обвязка
живёт в :mod:`neurowave.integrations.mpris_dbus` и только вызывает этот модуль.

Соответствие требованиям MPRIS 2.2:

* ``PlaybackStatus`` — ``Playing`` / ``Paused`` / ``Stopped``;
* ``Metadata`` — только реальные данные трека, ``mpris:length`` в микросекундах;
* ``Volume`` — 0.0 … 1.3 (ядро разрешает усиление до 130 % только с лимитером);
* ``Seek``/``SetPosition`` — в микросекундах, относительный и абсолютный.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Protocol

from ..domain import Quality, Track
from ..state import PlayerStatus

BUS_NAME = "org.mpris.MediaPlayer2.neurowave"
OBJECT_PATH = "/org/mpris/MediaPlayer2"
IDENTITY = "NeuroWave"
DESKTOP_ENTRY = "neurowave"
TRACK_PATH_ROOT = "/org/neurowave/Track/"
MAX_VOLUME_FRACTION = 1.3
ACTIVE_STATES = (PlayerStatus.PLAYING, PlayerStatus.PAUSED)

_PLAYBACK_STATUS = {
    PlayerStatus.PLAYING: "Playing",
    PlayerStatus.PAUSED: "Paused",
}
_UNSAFE_PATH_CHARS = re.compile(r"[^A-Za-z0-9_]")


def playback_status(status: PlayerStatus) -> str:
    """MPRIS-статус для состояния ядра; всё неиграющее — ``Stopped``."""
    return _PLAYBACK_STATUS.get(status, "Stopped")


def track_object_path(track_id: str | None) -> str:
    """Валидный D-Bus object path для трека.

    MPRIS требует ``o`` (object path), поэтому числовые ID используются как есть,
    а всё постороннее заменяется на безопасные символы.
    """
    if not track_id:
        return f"{TRACK_PATH_ROOT}none"
    safe = _UNSAFE_PATH_CHARS.sub("_", str(track_id)).strip("_") or "none"
    if not safe[0].isalpha():
        safe = f"t{safe}"
    return f"{TRACK_PATH_ROOT}{safe}"


def volume_fraction(percent: int | float) -> float:
    """Проценты громкости ядра → доля MPRIS (0.0 … 1.3)."""
    try:
        value = float(percent) / 100.0
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(MAX_VOLUME_FRACTION, value))


def volume_percent(fraction: float) -> int:
    """Доля MPRIS → проценты ядра (0 … 130)."""
    try:
        value = float(fraction)
    except (TypeError, ValueError):
        return 0
    return int(round(max(0.0, min(MAX_VOLUME_FRACTION, value)) * 100.0))


@dataclass(frozen=True)
class MprisSnapshot:
    """Один срез состояния плеера для отдачи в D-Bus."""

    status: PlayerStatus = PlayerStatus.IDLE
    track: Track | None = None
    quality: Quality | None = None
    volume_percent: int = 80
    position_us: int = 0

    @property
    def playback_status(self) -> str:
        return playback_status(self.status)

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATES


class MprisTarget(Protocol):
    """То, чем MPRIS управляет. Ядро подходит через :class:`PlayerTarget`."""

    def snapshot(self) -> MprisSnapshot: ...

    def play(self) -> None: ...

    def pause(self) -> None: ...

    def play_pause(self) -> None: ...

    def next(self) -> None: ...

    def previous(self) -> None: ...

    def stop(self) -> None: ...

    def seek_us(self, offset_us: int) -> None: ...

    def set_position_us(self, position_us: int) -> None: ...

    def set_volume_fraction(self, fraction: float) -> None: ...


class PlayerTarget:
    """Адаптер ``PlayerService`` к модели MPRIS.

    Ничего не меняет в ядре: только вызывает его публичные методы. Все ошибки
    ядра (``PlayerError`` и наследники) пробрасываются наружу — D-Bus-слой
    превратит их в ошибку метода, а не уронит сессию.
    """

    def __init__(self, player: Any):
        self._player = player

    # --- состояние ----------------------------------------------------
    def snapshot(self) -> MprisSnapshot:
        player = self._player
        position = 0
        try:
            position = int(player.backend.position_ms()) * 1000
        except Exception:
            position = 0
        return MprisSnapshot(
            status=player.state.status,
            track=player.current_track,
            quality=player.current_quality,
            volume_percent=player.volume,
            position_us=max(0, position),
        )

    # --- команды ------------------------------------------------------
    def play(self) -> None:
        status = self._player.state.status
        if status is PlayerStatus.PAUSED:
            self._player.toggle_pause()
            return
        if status is PlayerStatus.PLAYING:
            return
        track = self._player.current_track
        if track is not None:
            self._player.play_track(track)

    def pause(self) -> None:
        if self._player.state.status is PlayerStatus.PLAYING:
            self._player.toggle_pause()

    def play_pause(self) -> None:
        if self._player.state.status in ACTIVE_STATES:
            self._player.toggle_pause()
        else:
            self.play()

    def next(self) -> None:
        self._player.next_track()

    def previous(self) -> None:
        self._player.previous_track()

    def stop(self) -> None:
        self._player.stop()

    def seek_us(self, offset_us: int) -> None:
        current_us = self.snapshot().position_us
        target_ms = max(0, (current_us + int(offset_us)) // 1000)
        self._player.seek(target_ms)

    def set_position_us(self, position_us: int) -> None:
        self._player.seek(max(0, int(position_us)) // 1000)

    def set_volume_fraction(self, fraction: float) -> None:
        self._player.set_volume(volume_percent(fraction))


def metadata(snapshot: MprisSnapshot) -> dict[str, Any]:
    """MPRIS ``Metadata``: только реальные поля трека, без выдуманных значений."""
    track = snapshot.track
    if track is None:
        return {}
    data: dict[str, Any] = {
        "mpris:trackid": track_object_path(track.id),
        "xesam:title": track.title,
        "xesam:artist": list(track.artists) or ["Неизвестный исполнитель"],
    }
    if track.duration_ms:
        data["mpris:length"] = int(track.duration_ms) * 1000
    if track.album:
        data["xesam:album"] = track.album
    quality = snapshot.quality
    if quality is not None and (quality.codec or quality.bitrate_kbps):
        data["neurowave:quality"] = quality.label()
        data["neurowave:lossless"] = bool(quality.lossless)
    return data


def properties(snapshot: MprisSnapshot) -> dict[str, Any]:
    """Все свойства ``org.mpris.MediaPlayer2.Player`` как обычные Python-значения."""
    has_track = snapshot.track is not None
    return {
        "PlaybackStatus": snapshot.playback_status,
        "Metadata": metadata(snapshot),
        "Position": int(snapshot.position_us),
        "Volume": volume_fraction(snapshot.volume_percent),
        "Rate": 1.0,
        "MinimumRate": 1.0,
        "MaximumRate": 1.0,
        "CanPlay": has_track and snapshot.status is not PlayerStatus.STOPPED,
        "CanPause": snapshot.status is PlayerStatus.PLAYING,
        "CanGoNext": has_track,
        "CanGoPrevious": has_track,
        "CanSeek": has_track and snapshot.is_active,
        "CanControl": True,
    }


class MprisController:
    """Семантика MPRIS поверх :class:`MprisTarget`.

    Держит короткий кеш среза состояния (MPRIS-клиенты читают свойства
    пачками) и реализует правила методов спецификации: ``Seek`` в неиграющем
    состоянии ничего не делает, ``SetPosition`` игнорирует чужой ``trackid``.
    """

    def __init__(self, target: MprisTarget, quit_callback: Any = None, ttl: float = 0.2):
        self._target = target
        self._quit_callback = quit_callback
        self._ttl = ttl
        self._snapshot: MprisSnapshot | None = None
        self._fetched_at = 0.0

    # --- состояние ----------------------------------------------------
    @property
    def can_quit(self) -> bool:
        return self._quit_callback is not None

    def snapshot(self, force: bool = False) -> MprisSnapshot:
        now = time.monotonic()
        if force or self._snapshot is None or now - self._fetched_at >= self._ttl:
            self._snapshot = self._target.snapshot()
            self._fetched_at = now
        return self._snapshot

    def properties(self) -> dict[str, Any]:
        return properties(self.snapshot())

    def metadata(self) -> dict[str, Any]:
        return metadata(self.snapshot())

    def root_properties(self) -> dict[str, Any]:
        return {
            "Identity": IDENTITY,
            "DesktopEntry": DESKTOP_ENTRY,
            "CanQuit": self.can_quit,
            "CanRaise": False,
            "HasTrackList": False,
            "SupportedUriSchemes": [],
            "SupportedMimeTypes": [],
        }

    # --- методы -------------------------------------------------------
    def play(self) -> None:
        self._target.play()
        self.snapshot(force=True)

    def pause(self) -> None:
        self._target.pause()
        self.snapshot(force=True)

    def play_pause(self) -> None:
        self._target.play_pause()
        self.snapshot(force=True)

    def next(self) -> None:
        self._target.next()
        self.snapshot(force=True)

    def previous(self) -> None:
        self._target.previous()
        self.snapshot(force=True)

    def stop(self) -> None:
        self._target.stop()
        self.snapshot(force=True)

    def seek(self, offset_us: int) -> int | None:
        """Относительный переход; возвращает новую позицию или ``None``."""
        if not self.snapshot().is_active:
            return None
        self._target.seek_us(int(offset_us))
        return self.snapshot(force=True).position_us

    def set_position(self, track_id: str, position_us: int) -> None:
        snapshot = self.snapshot(force=True)
        if snapshot.track is None or track_object_path(snapshot.track.id) != track_id:
            return
        if int(position_us) < 0:
            return
        self._target.set_position_us(int(position_us))
        self.snapshot(force=True)

    def set_volume(self, fraction: float) -> float:
        self._target.set_volume_fraction(fraction)
        return volume_fraction(self.snapshot(force=True).volume_percent)

    def raise_player(self) -> None:
        """``Raise`` не поддерживается: окна может не быть (CLI-сессия)."""

    def quit(self) -> None:
        if self._quit_callback is not None:
            self._quit_callback()
