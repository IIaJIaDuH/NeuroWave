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
from .audio.dsp_controller import DspController, DspStatus
from .audio.devices import AudioOutput, build_outputs, classify, probe_default_sink, probe_sinks
from .audio.dsp_profiles import AudioProfile, resolve_profile
from .audio.profile_store import ProfileRegistry
from .config import SAFE_VOLUME, Settings
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
        dsp: DspController | None = None,
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
        self._boost_notice_shown = False
        backend.on_end = self._handle_end
        self.profiles = ProfileRegistry.from_settings(self.settings)
        self.dsp = dsp if dsp is not None else DspController(
            backend, settings=self.settings, listener=self._notify_dsp, registry=self.profiles
        )
        self.backend.set_volume(self._apply_volume_policy())

    # --- свойства -----------------------------------------------------
    @property
    def current_track(self) -> Track | None:
        return self.queue.current

    @property
    def current_quality(self) -> Quality | None:
        return self._current_quality

    @property
    def volume(self) -> int:
        """Запрошенная пользователем громкость (может быть больше применяемой)."""
        return self.settings.volume

    @property
    def applied_volume(self) -> int:
        """Фактически применяемая громкость с учётом политики безопасности."""
        return self.settings.effective_volume()

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
        requested = self.settings.clamp_volume(volume)
        self.settings.volume = requested
        applied = self._apply_volume_policy()
        self.backend.set_volume(applied)
        if self.store is not None:
            self.store.set_setting("volume", requested)
        self._emit("volume", volume=applied, requested=requested)
        return applied

    def _apply_volume_policy(self) -> int:
        """Вернуть безопасную громкость и один раз предупредить об ограничении.

        Пока в цепи нет проверенного лимитера, усиление выше 100 % не
        применяется: запись в настройках сохраняется, но фактический уровень
        ограничивается и пользователь узнаёт причину.
        """
        applied = self.settings.effective_volume()
        if applied != int(self.settings.volume) and not self._boost_notice_shown:
            self._boost_notice_shown = True
            self._emit(
                "volume_limited",
                requested=int(self.settings.volume),
                applied=applied,
                limit=SAFE_VOLUME,
            )
        return applied

    def seek(self, position_ms: int) -> None:
        """Перемотать трек.

        Пока mpv загружает файл, бэкенд откладывает команду до готовности:
        сам mpv такой seek отклоняет (QA D-01). Сигнатура и семантика для
        вызывающего не меняются.
        """
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

    def available_profiles(self) -> list[AudioProfile]:
        return self.profiles.all()

    def save_profile(self, profile: AudioProfile) -> AudioProfile:
        """Сохранить пользовательский профиль в настройках плеера."""
        existing = profile.id in self.profiles.custom
        saved = self.profiles.update(profile) if existing else self.profiles.add(profile)
        self.profiles.save(self.settings)
        return saved

    def delete_profile(self, profile_id: str) -> bool:
        removed = self.profiles.remove(profile_id)
        self.profiles.save(self.settings)
        if self.settings.dsp_profile == profile_id:
            self.settings.dsp_profile = "flat"
        return removed

    def set_dsp_profile(self, profile: AudioProfile | str) -> DspStatus:
        """Выбрать профиль звука: применить сейчас или сохранить до старта."""
        chosen = (
            profile
            if isinstance(profile, AudioProfile)
            else (self.profiles.get(profile) or resolve_profile(profile))
        )
        self.settings.dsp_profile = chosen.id
        if self.store is not None:
            self.store.set_setting("dsp_profile", chosen.id)
        if getattr(self.backend, "is_running", False):
            return self.dsp.apply(chosen)
        # mpv ещё не запущен: не поднимаем процесс ради фильтра, но интерфейс
        # должен видеть выбранный профиль, а не предыдущий.
        self.dsp.status = DspStatus(profile=chosen, mode=chosen.mode, graph=None, confirmed=False)
        return self.dsp.status

    # --- аудиовыход ---------------------------------------------------
    def outputs(self) -> list[AudioOutput]:
        """Выходы с типами и кодеками; без запущенного mpv — только настройка."""
        running = bool(getattr(self.backend, "is_running", False))
        devices = self.backend.audio_devices() if running else []
        current = self.backend.audio_device() if running else None
        return build_outputs(
            devices, probe_sinks(), current or (self.settings.audio_output or None), probe_default_sink()
        )

    def set_output(self, device_id: str) -> AudioOutput:
        """Выбрать выход. Несуществующий id отвергается до отправки в mpv.

        mpv молча принимает любой id и продолжает играть «в никуда», поэтому
        проверка по списку обязательна — иначе пользователь думает, что звук
        переключился.
        """
        device_id = (device_id or "auto").strip()
        running = bool(getattr(self.backend, "is_running", False))
        available = self.backend.audio_devices() if running else []
        known = {str(item.get("name")) for item in available if isinstance(item, dict)}
        if known and device_id not in known:
            options = ", ".join(sorted(known))
            raise PlayerError(f"Нет такого аудиовыхода: {device_id}. Доступны: {options}")
        previous = self.settings.audio_output
        self.settings.audio_output = device_id
        if self.store is not None:
            self.store.set_setting("audio_output", device_id)
        if not running:
            return AudioOutput(id=device_id, name=device_id, kind=classify(device_id), current=True)
        try:
            self.backend.set_audio_device(device_id)
            actual = self.backend.audio_device()
            if actual is not None and actual != device_id:
                raise PlayerError(f"mpv оставил другой выход: {actual}")
        except PlayerError:
            self.settings.audio_output = previous
            if previous:
                try:
                    self.backend.set_audio_device(previous)
                except PlayerError:
                    pass
            raise
        chosen = next((item for item in self.outputs() if item.id == device_id), None)
        output = chosen or AudioOutput(id=device_id, name=device_id, kind=classify(device_id), current=True)
        self._emit("output", output=output.to_dict())
        return output

    def _sync_output(self) -> None:
        """Применить сохранённый выход. Исчезнувшее устройство не ломает звук."""
        desired = (self.settings.audio_output or "").strip()
        if not desired or desired == "auto":
            return
        if self.backend.audio_device() == desired:
            return
        known = {str(item.get("name")) for item in self.backend.audio_devices() if isinstance(item, dict)}
        if desired not in known:
            return
        try:
            self.backend.set_audio_device(desired)
        except PlayerError:
            pass

    def set_dsp_mode(self, mode: str) -> DspStatus:
        from .audio.dsp_profiles import DspMode

        parsed = DspMode.parse(mode)
        if not getattr(self.backend, "is_running", False):
            self.settings.dsp_profile = (
                "flat" if parsed is DspMode.PURE else self.profiles.resolve(self.settings.dsp_profile).id
            )
            chosen = self.profiles.resolve(self.settings.dsp_profile)
            self.dsp.status = DspStatus(profile=chosen, mode=parsed)
            return self.dsp.status
        return self.dsp.set_mode(parsed)

    def close(self) -> None:
        try:
            self.dsp.invalidate()
            self.backend.shutdown()
        finally:
            if self.store is not None:
                self.store.save_queue(self.queue.snapshot())

    def _notify_dsp(self, event: str, payload: dict[str, Any]) -> None:
        self._emit(event, **payload)

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
        self.backend.set_volume(self._apply_volume_policy())
        # Фильтры и выход живут в mpv между треками, поэтому обычно no-op.
        self.dsp.sync()
        self._sync_output()
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
