"""Сервис плеера: связывает очередь, сервис, кеш и аудиовыход.

Правила:
* аудиопоток не ждёт сеть дольше, чем нужно для загрузки текущего трека;
* ошибка одного трека не останавливает сессию сразу;
* ИИ и UI не участвуют в воспроизведении.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Iterable, Protocol

from .audio.backend import AudioBackend
from .config import Settings
from .domain import Quality, RepeatMode, StreamInfo, Track
from .errors import NeuroWaveError, PlayerError, TrackUnavailable
from .queue import Queue
from .state import Overlay, PlayerStateMachine, PlayerStatus
from .storage.cache import MediaCache, download_to_cache
from .storage.db import Database

EventListener = Callable[[str, dict[str, Any]], None]

MAX_CONSECUTIVE_FAILURES = 2


class MusicAdapterProtocol(Protocol):
    def wave_tracks(self) -> list[Track]: ...
    def wave_feedback(
        self, kind: str, track_id: str | None = ..., total_played_seconds: float | None = ...
    ) -> bool: ...
    def stream_url(self, track_id: str) -> StreamInfo: ...


class PlayerService:
    def __init__(
        self,
        adapter: MusicAdapterProtocol,
        backend: AudioBackend,
        state: PlayerStateMachine | None = None,
        queue: Queue | None = None,
        settings: Settings | None = None,
        store: Database | None = None,
        cache: MediaCache | None = None,
        listener: EventListener | None = None,
        router: Any | None = None,
        prefetch: bool = True,
    ):
        self.adapter = adapter
        self._router = router
        self.backend = backend
        self.settings = settings or Settings()
        self.state = state or PlayerStateMachine()
        # Не `queue or …`: пустая очередь ложна из-за __len__, и переданная
        # очередь с repeat/shuffle молча терялась бы.
        self.queue = (
            queue
            if queue is not None
            else Queue(repeat=self.settings.repeat, shuffle=self.settings.shuffle)
        )
        self.store = store
        self.cache = cache
        self._listener = listener
        self._prefetch_enabled = prefetch and cache is not None
        self._prefetch_thread: threading.Thread | None = None
        self._failures = 0
        self._wave_active = False
        # Автопереход идёт из потока аудиособытий, а команды — из CLI/MPRIS.
        # Старт трека должен выполняться целиком, иначе события перепутаются.
        self._lock = threading.RLock()
        self._current_history_id: int | None = None
        self._current_quality: Quality | None = None
        self._started_at: float | None = None
        backend.on_end = self._handle_end
        backend.set_volume(self.settings.volume)

    # --- свойства -----------------------------------------------------
    @property
    def current_track(self) -> Track | None:
        return self.queue.current

    @property
    def current_quality(self) -> Quality | None:
        return self._current_quality

    @property
    def volume(self) -> int:
        return self.settings.volume

    def add_listener(self, listener: EventListener) -> None:
        self._listener = listener

    # --- запуск -------------------------------------------------------
    def start_wave(self) -> Track | None:
        if self.state.status in (PlayerStatus.IDLE, PlayerStatus.STOPPED):
            self.state.transition(PlayerStatus.READY)
        tracks = self._router.wave_tracks() if self._router is not None else self.adapter.wave_tracks()
        if not tracks:
            raise TrackUnavailable("«Моя волна» не вернула треки")
        self._wave_active = True
        self.queue.clear()
        self.queue.extend(tracks)
        self.queue.jump(0)
        self._play_current()
        return self.current_track

    def play_tracks(self, tracks: Iterable[Track], start: int = 0) -> Track | None:
        items = [track for track in tracks if track.available]
        if not items:
            raise TrackUnavailable("Нет доступных треков для воспроизведения")
        if not 0 <= start < len(items):
            raise PlayerError(f"Некорректный индекс старта: {start}")
        if self.state.status in (PlayerStatus.IDLE, PlayerStatus.STOPPED):
            self.state.transition(PlayerStatus.READY)
        self._wave_active = False
        self.queue.clear()
        self.queue.extend(items)
        self.queue.jump(start)
        self._play_current()
        return self.current_track

    def play_track(self, track: Track) -> Track | None:
        return self.play_tracks([track])

    # --- управление ---------------------------------------------------
    def toggle_pause(self) -> PlayerStatus:
        if self.state.status is PlayerStatus.PLAYING:
            self.backend.pause()
            self.state.transition(PlayerStatus.PAUSED)
            self._emit("paused", track=self.current_track)
        elif self.state.status is PlayerStatus.PAUSED:
            self.backend.play()
            self.state.transition(PlayerStatus.PLAYING)
            self._emit("resumed", track=self.current_track)
        else:
            raise PlayerError(f"Пауза недоступна в состоянии {self.state.status.value}")
        return self.state.status

    def set_volume(self, volume: int) -> int:
        value = self.settings.clamp_volume(volume)
        self.settings.volume = value
        self.backend.set_volume(value)
        if self.store is not None:
            self.store.set_setting("volume", value)
        self._emit("volume", volume=value)
        return value

    def seek(self, position_ms: int) -> None:
        self.backend.seek(position_ms)

    def next_track(self, automatic: bool = False) -> Track | None:
        self._send_feedback("finished" if automatic else "skip")
        track = self.queue.next(automatic=automatic)
        if track is None:
            self._finish()
            return None
        self._advance(track)
        return track

    def previous_track(self) -> Track | None:
        track = self.queue.previous()
        if track is None:
            return None
        self._play_current()
        return track

    def stop(self) -> None:
        self.backend.stop()
        self.state.clear_overlay()
        if self.state.status is not PlayerStatus.STOPPED:
            self.state.transition(PlayerStatus.STOPPED)
        self._emit("stopped", track=self.current_track)

    def close(self) -> None:
        try:
            self.backend.shutdown()
        finally:
            if self.store is not None:
                self.store.save_queue(self.queue.snapshot())

    # --- внутреннее ---------------------------------------------------
    def _play_current(self) -> None:
        with self._lock:
            self._start_current()

    def _start_current(self) -> None:
        track = self.queue.current
        if track is None:
            raise PlayerError("Очередь пуста")
        if not track.available:
            raise TrackUnavailable(f"Трек «{track.title}» недоступен")
        self.state.transition(PlayerStatus.BUFFERING)
        self._current_quality = None
        url, from_cache = self._resolve_url(track)
        quality = self._current_quality  # снимок: диспетчер может уже уйти вперёд
        self.backend.load(url)
        self.backend.set_volume(self.settings.volume)
        self.backend.play()
        self.state.transition(PlayerStatus.PLAYING)
        self.state.clear_overlay(Overlay.TRACK_UNAVAILABLE)
        self._failures = 0
        self._started_at = time.monotonic()
        if self.store is not None:
            self._current_history_id = self.store.record_play(track)
        self._send_feedback("started")
        self._emit(
            "track",
            track=track,
            quality=quality,
            from_cache=from_cache,
        )
        self._prefetch_next()

    def _resolve_url(self, track: Track) -> tuple[str, bool]:
        if self.cache is not None and self._cache_allowed(track):
            cached = self.cache.get(track.key)
            if cached is not None:
                self._emit("cache_hit", track=track)
                return str(cached), True
        stream = self._stream_url(track)
        self._current_quality = stream.quality
        return stream.url, False

    def _stream_url(self, track: Track) -> StreamInfo:
        """Ссылка у того провайдера, которому принадлежит трек."""
        if self._router is not None:
            return self._router.stream_url(track)
        return self.adapter.stream_url(track.id)

    def _cache_allowed(self, track: Track) -> bool:
        if self._router is not None:
            return self._router.cache_allowed(track)
        return True

    def _send_feedback(self, kind: str) -> None:
        """Обратная связь «Моей волне» по провайдеру трека; сбой не ломает звук."""
        if not self._wave_active:
            return
        track_key = self._current_id()
        if track_key is None:
            return
        try:
            if self._router is not None:
                self._router.wave_feedback(kind, track_key)
            else:
                self.adapter.wave_feedback(kind, track_key)
        except Exception:
            return

    def _advance(self, track: Track) -> None:
        try:
            self._play_current()
        except NeuroWaveError as exc:
            self._handle_failure(exc)

    def _handle_failure(self, exc: NeuroWaveError) -> None:
        self._failures += 1
        self._emit("error", error=str(exc))
        self.state.set_overlay(Overlay.TRACK_UNAVAILABLE, str(exc))
        if self._failures >= MAX_CONSECUTIVE_FAILURES:
            self._emit("failed", error=str(exc))
            self._finish()
            return
        track = self.queue.next(automatic=True)
        if track is None:
            self._finish()
            return
        try:
            self._play_current()
        except NeuroWaveError as nested:
            self._handle_failure(nested)

    def _finish(self) -> None:
        self.backend.stop()
        self._current_quality = None
        if self.state.status is not PlayerStatus.STOPPED:
            self.state.transition(PlayerStatus.STOPPED)
        self._emit("finished", track=self.queue.current)

    def _handle_end(self, reason: str = "eof") -> None:
        if self.state.status in (PlayerStatus.PLAYING, PlayerStatus.BUFFERING, PlayerStatus.PAUSED):
            self.next_track(automatic=True)

    def _current_id(self) -> str | None:
        """С ``router`` — квалифицированный ключ, иначе сырой id адаптера."""
        track = self.queue.current
        if track is None:
            return None
        return track.key if self._router is not None else track.id

    def _prefetch_next(self) -> None:
        if not self._prefetch_enabled or self.cache is None:
            return
        if self._prefetch_thread is not None and self._prefetch_thread.is_alive():
            return
        upcoming = self.queue.preview_next()
        if upcoming is None or not self._cache_allowed(upcoming):
            return
        if self.cache.has(upcoming.key):
            return

        def worker() -> None:
            try:
                stream = self._stream_url(upcoming)
                download_to_cache(self.cache, upcoming.key, stream.url, timeout=20.0)
                self._emit("prefetched", track=upcoming)
            except Exception:
                return  # предзагрузка необязательна и не должна шуметь

        self._prefetch_thread = threading.Thread(target=worker, daemon=True)
        self._prefetch_thread.start()

    def _emit(self, event: str, **payload: Any) -> None:
        if self._listener is not None:
            try:
                self._listener(event, payload)
            except Exception:
                pass
