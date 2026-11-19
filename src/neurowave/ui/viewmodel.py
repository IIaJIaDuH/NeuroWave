"""Состояние интерфейса без Qt: снимки, события и команды.

Слой отделён от виджетов, чтобы проверяться обычными тестами. Правила:

* события ядра приходят из аудиопотока и только складываются в очередь —
  виджеты обновляет GUI-поток по таймеру;
* позиция читается отдельным фоновым потоком, чтобы IPC mpv никогда не
  блокировал отрисовку;
* все мутации идут через :class:`CommandDispatcher` — один поток, без гонок.
"""

from __future__ import annotations

import logging
import queue
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from ..dispatcher import Command, CommandDispatcher
from ..domain import Track
from ..errors import NeuroWaveError

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class UiState:
    """Всё, что нужно окну для одной перерисовки."""

    status: str = "idle"
    is_playing: bool = False
    title: str = "NeuroWave"
    subtitle: str = ""
    position_ms: int = 0
    duration_ms: int = 0
    duration_known: bool = True
    volume: int = 80
    applied_volume: int = 80
    volume_limited: bool = False
    profile_id: str = "flat"
    profile_name: str = "Без обработки"
    profile_mode: str = "pure"
    output_id: str = "auto"
    output_name: str = "auto"
    quality: str = ""
    last_event: str = ""
    dsp_confirmed: bool = False
    dsp_reason: str = ""

    @property
    def progress(self) -> float:
        if not self.duration_known or self.duration_ms <= 0:
            return 0.0
        return min(1.0, max(0.0, self.position_ms / self.duration_ms))

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "is_playing": self.is_playing,
            "title": self.title,
            "subtitle": self.subtitle,
            "position_ms": self.position_ms,
            "duration_ms": self.duration_ms,
            "duration_known": self.duration_known,
            "volume": self.volume,
            "applied_volume": self.applied_volume,
            "volume_limited": self.volume_limited,
            "profile_id": self.profile_id,
            "profile_name": self.profile_name,
            "profile_mode": self.profile_mode,
            "output_id": self.output_id,
            "output_name": self.output_name,
            "quality": self.quality,
            "last_event": self.last_event,
        }


@dataclass
class PendingEvent:
    name: str
    payload: dict[str, Any] = field(default_factory=dict)


def _subtitle(track: Track | None) -> str:
    if track is None:
        return ""
    parts = [" · ".join(track.artists) if track.artists else "", track.album or ""]
    return " — ".join(part for part in parts if part)


# --- D-05: ошибки команд для строки статуса ----------------------------
# Диспетчер живёт в своём потоке, поэтому сюда приходит уже готовый текст:
# никаких объектов исключений, трассировок и Qt.
_COMMAND_ERROR_TITLES = {
    "output": "не удалось выбрать аудиовыход",
    "seek": "не удалось перемотать трек",
    "profile": "не удалось применить профиль звука",
    "apply-profile": "не удалось применить профиль звука",
    "save-profile": "не удалось сохранить профиль звука",
    "delete-profile": "не удалось удалить профиль звука",
    "mode": "не удалось переключить режим обработки",
    "volume": "не удалось изменить громкость",
    "toggle": "не удалось переключить воспроизведение",
    "next": "не удалось переключить трек",
    "previous": "не удалось переключить трек",
    "stop": "не удалось остановить воспроизведение",
    "wave": "не удалось запустить волну",
}
_DEFAULT_COMMAND_ERROR = "не удалось выполнить команду"
_ERROR_TEXT_LIMIT = 300
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(bearer)\s+[^\s,;]+"),
    re.compile(
        r"(?i)\b(access[_-]?token|refresh[_-]?token|token|api[_-]?key|authorization|password|secret)"
        r"\b\s*[=:]\s*[^\s,;]+"
    ),
)


def _sanitize_error_text(text: object) -> str:
    """Однострочный безопасный текст: без секретов, переводов строк и хвостов."""
    clean = " ".join(str(text).split())
    for pattern in _SECRET_PATTERNS:
        clean = pattern.sub(r"\1 ***", clean)
    clean = clean.strip(" .;,")
    if len(clean) > _ERROR_TEXT_LIMIT:
        clean = clean[: _ERROR_TEXT_LIMIT - 1].rstrip() + "…"
    return clean


def _command_error_message(command: Command) -> str:
    """Понятное пользователю сообщение без внутренностей исключения."""
    title = _COMMAND_ERROR_TITLES.get(command.name, _DEFAULT_COMMAND_ERROR)
    error = command.error
    if isinstance(error, NeuroWaveError):
        detail = _sanitize_error_text(error)
        if detail:
            return f"{title} — {detail}"
    # Неизвестное исключение: только общий текст, подробности — в лог.
    return title


def _duration(track: Track | None) -> tuple[int, bool]:
    """Длительность для GUI: число и признак достоверности.

    ``Track.duration_ms`` по доменному контракту бывает ``None``, но окно
    делит и форматирует только числа. В снимке неизвестная длительность —
    это ``0`` вместе с ``duration_known=False``: иначе ``None // 1000``
    роняет каждое обновление интерфейса (D-06).
    """
    if track is None:
        return 0, True  # пустая очередь: достоверный ноль
    if track.duration_ms is None:
        return 0, False
    return max(0, int(track.duration_ms)), True


class PlayerViewModel:
    """Мост между `PlayerService` и окном."""

    def __init__(
        self,
        player: Any,
        dispatcher: CommandDispatcher | None = None,
        poll_interval: float = 0.25,
    ):
        self.player = player
        self.dispatcher = dispatcher or CommandDispatcher()
        self.poll_interval = poll_interval
        self._events: queue.Queue[PendingEvent] = queue.Queue()
        self._position_ms = 0
        self._position_lock = threading.Lock()
        self._stop = threading.Event()
        self._poller: threading.Thread | None = None
        self.last_error: str | None = None
        self._closed = False
        add_listener = getattr(self.dispatcher, "add_error_listener", None)
        if callable(add_listener):
            add_listener(self._on_command_error)
        player.add_listener(self.on_event)

    # --- события -------------------------------------------------------
    def on_event(self, name: str, payload: dict[str, Any] | None = None) -> None:
        """Вызывается из аудиопотока: только очередь, без виджетов."""
        self._events.put(PendingEvent(name, dict(payload or {})))

    def _on_command_error(self, command: Command) -> None:
        """Ошибка команды из рабочего потока: очередь и `last_error`.

        Здесь нельзя трогать Qt: окно забирает событие через :meth:`drain`
        в GUI-потоке. Объект исключения в очередь не попадает.
        """
        if self._closed:
            return
        message = _command_error_message(command)
        self.last_error = message
        if not isinstance(command.error, NeuroWaveError):
            LOG.debug(
                "Команда %s завершилась неожиданной ошибкой", command.name, exc_info=command.error
            )
        self._events.put(
            PendingEvent(
                name="command_error",
                payload={
                    "command": command.name,
                    "message": message,
                    "command_id": command.id,
                },
            )
        )

    def drain(self) -> list[PendingEvent]:
        events: list[PendingEvent] = []
        while True:
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                if events:
                    self._last_event = events[-1].name
                return events

    # --- позиция -------------------------------------------------------
    def set_position(self, position_ms: int) -> None:
        with self._position_lock:
            self._position_ms = max(0, int(position_ms))

    def position_ms(self) -> int:
        with self._position_lock:
            return self._position_ms

    def start_polling(self) -> None:
        if self._poller is not None and self._poller.is_alive():
            return
        self._stop.clear()
        self._poller = threading.Thread(target=self._poll, name="neurowave-position", daemon=True)
        self._poller.start()

    def stop_polling(self) -> None:
        self._stop.set()
        if self._poller is not None:
            self._poller.join(timeout=2.0)
            self._poller = None

    def _poll(self) -> None:
        while not self._stop.wait(self.poll_interval):
            try:
                self.set_position(self.player.backend.position_ms())
            except Exception:
                # mpv мог закрыться: позиция просто не обновляется.
                continue

    # --- снимок --------------------------------------------------------
    def snapshot(self) -> UiState:
        player = self.player
        track = player.current_track
        status = getattr(player.state.status, "value", str(player.state.status))
        dsp_status = getattr(player.dsp, "status", None)
        profile = getattr(dsp_status, "profile", None)
        quality = player.current_quality
        settings = player.settings
        output_id = settings.audio_output or "auto"
        duration_ms, duration_known = _duration(track)
        return UiState(
            status=status,
            is_playing=status == "playing",
            title=track.title if track else "NeuroWave",
            subtitle=_subtitle(track),
            position_ms=self.position_ms(),
            duration_ms=duration_ms,
            duration_known=duration_known,
            volume=settings.volume,
            applied_volume=settings.effective_volume(),
            volume_limited=settings.volume_limited(),
            profile_id=profile.id if profile else settings.dsp_profile,
            profile_name=profile.name if profile else "Без обработки",
            profile_mode=getattr(dsp_status, "mode", None).value
            if getattr(dsp_status, "mode", None) is not None
            else "pure",
            output_id=output_id,
            output_name=self._output_name(output_id),
            quality=quality.label() if quality else "",
            last_event=self._last_event,
            dsp_confirmed=bool(getattr(dsp_status, "confirmed", False)),
            dsp_reason=getattr(dsp_status, "reason", None) or "",
        )

    _last_event: str = ""

    def _output_name(self, output_id: str) -> str:
        try:
            for output in self.player.outputs():
                if output.id == output_id:
                    return output.label()
        except Exception:
            pass
        return output_id

    # --- команды -------------------------------------------------------
    def _nav(self, name: str, action: Callable[[], Any]) -> None:
        """Навигация: новое поколение отменяет ещё не начатые команды."""
        generation = self.dispatcher.bump_generation()
        self.dispatcher.submit(name, action, generation=generation)

    def _cmd(self, name: str, action: Callable[[], Any]) -> None:
        self.dispatcher.submit(name, action, generation=self.dispatcher.generation)

    def start_wave(self) -> None:
        self._nav("wave", self.player.start_wave)

    def toggle(self) -> None:
        self._cmd("toggle", self.player.toggle_pause)

    def next(self) -> None:
        self._nav("next", self.player.next_track)

    def previous(self) -> None:
        self._nav("previous", self.player.previous_track)

    def stop(self) -> None:
        self._nav("stop", self.player.stop)

    def seek(self, position_ms: int) -> None:
        position = max(0, int(position_ms))
        self.set_position(position)  # слайдер не должен «прыгать» назад
        self._cmd("seek", lambda: self.player.seek(position))

    def set_volume(self, volume: int) -> None:
        self._cmd("volume", lambda: self.player.set_volume(volume))

    def adjust_volume(self, delta: int) -> None:
        self.set_volume(self.player.volume + int(delta))

    def profiles(self) -> list[tuple[str, str, bool]]:
        """(id, имя, встроенный) для списка в Sound Lab."""
        return list(self.player.profiles.describe())

    def set_profile(self, profile_id: str) -> None:
        self._cmd("profile", lambda: self.player.set_dsp_profile(profile_id))

    def apply_profile(self, profile: Any) -> None:
        """Применить собранный профиль (в том числе несохранённый)."""
        self._cmd("apply-profile", lambda: self.player.set_dsp_profile(profile))

    def save_profile(self, profile: Any) -> None:
        self._cmd("save-profile", lambda: self.player.save_profile(profile))

    def delete_profile(self, profile_id: str) -> None:
        self._cmd("delete-profile", lambda: self.player.delete_profile(profile_id))

    def set_mode(self, mode: str) -> None:
        self._cmd("mode", lambda: self.player.set_dsp_mode(mode))

    def toggle_pure(self) -> None:
        enhanced = getattr(self.player.dsp.status, "mode", None)
        mode = "pure" if getattr(enhanced, "value", "pure") == "enhanced" else "enhanced"
        self.set_mode(mode)

    def set_output(self, output_id: str) -> None:
        self._cmd("output", lambda: self.player.set_output(output_id))

    # --- жизненный цикл ------------------------------------------------
    def close(self) -> None:
        """Отписаться от ошибок и остановить фоновые потоки."""
        self._closed = True
        remove_listener = getattr(self.dispatcher, "remove_error_listener", None)
        if callable(remove_listener):
            remove_listener(self._on_command_error)
        self.stop_polling()
        self.dispatcher.shutdown()
