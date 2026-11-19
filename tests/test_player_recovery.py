"""QA D-04: недоступный трек не должен останавливать всю очередь.

Аппаратный сценарий из QA PR #32 (`case_a_start_with_unavailable`):
первый трек формально доступен (`available=True`), но провайдер не может
разрешить поток. До исправления `play_tracks()` / `start_wave()` в этом
случае выбрасывали `TrackUnavailable`, состояние оставалось `buffering`,
следующий рабочий трек не запускался, а очередь выглядела «сломанной».

Тесты детерминированные: никаких `sleep`, фейковых таймеров и сети — только
фейковый адаптер и `NullBackend`, которые отвечают мгновенно.
"""

from __future__ import annotations

import random

import pytest

from neurowave.audio import NullBackend
from neurowave.config import Settings
from neurowave.domain import Quality, StreamInfo, Track
from neurowave.errors import PlayerError, TrackUnavailable
from neurowave.local import LocalMusicAdapter
from neurowave.player import MAX_CONSECUTIVE_FAILURES, PlayerService
from neurowave.providers.router import ProviderRouter
from neurowave.queue import Queue
from neurowave.state import Overlay, PlayerStatus
from tests.conftest import FakeAdapter, make_track


class EventRecorder:
    """Слушатель событий плеера: порядок и содержимое без потоков и времени."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def __call__(self, event: str, payload: dict) -> None:
        self.events.append((event, payload))

    def named(self, name: str) -> list[dict]:
        return [payload for event, payload in self.events if event == name]


class MemoryStore:
    """Хранилище истории: помнит, какие треки записаны как проигранные."""

    def __init__(self) -> None:
        self.played: list[str] = []
        self.saved_queue: dict | None = None

    def record_play(self, track: Track, played_at: float | None = None) -> int:
        self.played.append(track.key)
        return len(self.played)

    def save_queue(self, snapshot: dict) -> None:
        self.saved_queue = snapshot

    def set_setting(self, key: str, value: object) -> None:
        pass


class NamedAdapter:
    """Провайдер с отказывающими треками (для проверки изоляции провайдеров)."""

    def __init__(self, name: str, fail: set[str] | None = None) -> None:
        self.name = name
        self.fail = set(fail or ())
        self.stream_calls: list[str] = []
        self.feedback: list[tuple[str, str | None]] = []

    def wave_tracks(self) -> list[Track]:
        return []

    def stream_url(self, track_id: str) -> StreamInfo:
        self.stream_calls.append(track_id)
        if track_id in self.fail:
            raise TrackUnavailable(f"{self.name}: трек {track_id} недоступен")
        return StreamInfo(f"{self.name}://{track_id}", Quality("mp3", 320))

    def wave_feedback(
        self, kind: str, track_id: str | None = None, total_played_seconds: float | None = None
    ) -> bool:
        self.feedback.append((kind, track_id))
        return True


def make_player(
    adapter,
    *,
    backend: NullBackend | None = None,
    store: MemoryStore | None = None,
    queue: Queue | None = None,
    router: ProviderRouter | None = None,
) -> tuple[PlayerService, EventRecorder]:
    recorder = EventRecorder()
    player = PlayerService(
        adapter=adapter,
        backend=backend or NullBackend(),
        settings=Settings(volume=55),
        store=store,
        queue=queue,
        listener=recorder,
        router=router,
        prefetch=False,
    )
    return player, recorder


# --- путь A из QA-04: очередь начинается с недоступного трека -------------
def test_first_unresolvable_track_falls_through_to_next(adapter, backend):
    adapter.fail_ids = {"1"}
    player, events = make_player(adapter, backend=backend)
    try:
        playing = player.play_tracks([make_track(1), make_track(2), make_track(3)])

        assert playing is not None and playing.id == "2"
        assert player.current_track.id == "2"
        assert player.state.status is PlayerStatus.PLAYING
        assert backend.is_playing() is True
        assert backend.loaded_url == "https://audio.test/2.mp3"

        # Событие `track` — только про реально запущенный трек.
        assert [payload["track"].id for payload in events.named("track")] == ["2"]
        # Причина отказа не потеряна и сформулирована доменно.
        errors = events.named("error")
        assert len(errors) == 1
        assert "unavailable" in errors[0]["error"]
        assert events.named("failed") == []
        # Overlay снимается, как только играет рабочий трек.
        assert player.state.overlay is None
    finally:
        player.close()


def test_start_wave_recovers_from_unavailable_first_track(adapter, backend):
    adapter.fail_ids = {"1"}
    player, events = make_player(adapter, backend=backend)
    try:
        track = player.start_wave()

        assert track is not None and track.id == "2"
        assert player.state.status is PlayerStatus.PLAYING
        assert backend.loaded_url == "https://audio.test/2.mp3"
        # «Моя волна» получает feedback только за фактически запущенный трек.
        assert adapter.feedback == [("started", "2")]
        assert [payload["track"].id for payload in events.named("track")] == ["2"]
    finally:
        player.close()


def test_failed_track_is_not_recorded_as_played(adapter, backend):
    adapter.fail_ids = {"1"}
    store = MemoryStore()
    player, _ = make_player(adapter, backend=backend, store=store)
    try:
        player.play_tracks([make_track(1), make_track(2)])

        assert store.played == ["2"]
    finally:
        player.close()


def test_queue_keeps_skipped_items(adapter, backend):
    adapter.fail_ids = {"1"}
    player, _ = make_player(adapter, backend=backend)
    try:
        player.play_tracks([make_track(1), make_track(2), make_track(3)])

        assert [track.id for track in player.queue.items] == ["1", "2", "3"]
        assert player.queue.position == 1
    finally:
        player.close()


# --- путь B из QA-04: недоступный трек в середине очереди -----------------
def test_mid_queue_failure_keeps_previous_behaviour(adapter, backend):
    adapter.fail_ids = {"2"}
    player, events = make_player(adapter, backend=backend)
    try:
        player.play_tracks([make_track(1), make_track(2), make_track(3)])
        backend.finish()

        assert player.current_track.id == "3"
        assert player.state.status is PlayerStatus.PLAYING
        assert backend.loaded_url == "https://audio.test/3.mp3"
        assert [payload["track"].id for payload in events.named("track")] == ["1", "3"]
        assert len(events.named("error")) == 1
    finally:
        player.close()


# --- лимит последовательных отказов --------------------------------------
def test_two_consecutive_failures_stop_the_session(adapter, backend):
    adapter.fail_ids = {"1", "2"}
    player, events = make_player(adapter, backend=backend)
    try:
        with pytest.raises(TrackUnavailable) as exc:
            player.play_tracks([make_track(1), make_track(2), make_track(3)])

        assert "Не удалось запустить" in str(exc.value)
        assert adapter.stream_calls == ["1", "2"]
        assert player.state.status is PlayerStatus.STOPPED
        assert player.state.overlay is Overlay.TRACK_UNAVAILABLE
        assert backend.is_playing() is False
        assert len(events.named("error")) == MAX_CONSECUTIVE_FAILURES
        assert len(events.named("failed")) == 1
        assert events.named("track") == []
    finally:
        player.close()


def test_all_tracks_unresolvable_ends_in_stopped_state(adapter, backend):
    adapter.fail_ids = {"1", "2", "3"}
    player, _ = make_player(adapter, backend=backend)
    try:
        with pytest.raises(TrackUnavailable):
            player.play_tracks([make_track(1), make_track(2), make_track(3)])

        assert player.state.status is PlayerStatus.STOPPED
        assert player.state.status is not PlayerStatus.BUFFERING
        assert player.state.overlay is Overlay.TRACK_UNAVAILABLE
        assert backend.is_playing() is False
        # Лимит ошибок ограничивает перебор, очередь при этом не теряется.
        assert len(adapter.stream_calls) == MAX_CONSECUTIVE_FAILURES
        assert len(player.queue) == 3
    finally:
        player.close()


def test_start_wave_with_all_tracks_broken_ends_stopped(adapter, backend):
    adapter.fail_ids = {"1", "2", "3"}
    player, _ = make_player(adapter, backend=backend)
    try:
        with pytest.raises(TrackUnavailable):
            player.start_wave()

        assert player.state.status is PlayerStatus.STOPPED
        assert player.state.overlay is Overlay.TRACK_UNAVAILABLE
        assert backend.is_playing() is False
    finally:
        player.close()


# --- пустая очередь и пустой ввод: доменная ошибка сохраняется -----------
def test_empty_input_keeps_domain_error(adapter, backend):
    player, events = make_player(adapter, backend=backend)
    try:
        with pytest.raises(TrackUnavailable):
            player.play_tracks([])
        with pytest.raises(TrackUnavailable):
            player.play_tracks([make_track(1, available=False)])
        with pytest.raises(PlayerError):
            player.play_tracks([make_track(1)], start=5)

        adapter.tracks = []
        with pytest.raises(TrackUnavailable):
            player.start_wave()

        # Ни одна загрузка не начиналась: восстановлению нечего пропускать.
        assert backend.loaded_url is None
        assert events.named("track") == []
    finally:
        player.close()


# --- локальные файлы: сбой одного не ломает следующие --------------------
def test_local_file_failure_then_working_local_file(tmp_path):
    music = tmp_path / "Music"
    music.mkdir()
    broken = music / "01 - Broken.mp3"
    working = music / "02 - Working.mp3"
    broken.write_bytes(b"audio-1")
    working.write_bytes(b"audio-2")

    local = LocalMusicAdapter(music)
    tracks = local.tracks()
    assert [track.id for track in tracks] == ["01 - Broken.mp3", "02 - Working.mp3"]
    broken.unlink()  # файл исчез после сканирования — поток не разрешается

    backend = NullBackend()
    player, events = make_player(local, backend=backend)
    try:
        playing = player.play_tracks(tracks)

        assert playing is not None and playing.id == "02 - Working.mp3"
        assert player.state.status is PlayerStatus.PLAYING
        assert backend.loaded_url == str(working.resolve())
        assert [payload["track"].id for payload in events.named("track")] == [
            "02 - Working.mp3"
        ]
        assert len(events.named("error")) == 1
    finally:
        player.close()


def test_new_session_gets_a_fresh_failure_budget(adapter, backend):
    """Исчерпанный лимит прошлой сессии не блокирует новый старт."""
    adapter.fail_ids = {"1", "2"}
    player, _ = make_player(adapter, backend=backend)
    try:
        with pytest.raises(TrackUnavailable):
            player.play_tracks([make_track(1), make_track(2), make_track(3)])

        adapter.fail_ids = {"1"}
        playing = player.play_tracks([make_track(1), make_track(2), make_track(3)])

        assert playing is not None and playing.id == "2"
        assert player.state.status is PlayerStatus.PLAYING
    finally:
        player.close()


# --- repeat / shuffle: нет бесконечного повторного запуска ---------------
def test_repeat_one_does_not_loop_forever_on_unavailable_track(adapter, backend):
    adapter.fail_ids = {"1"}
    player, events = make_player(adapter, backend=backend, queue=Queue(repeat="one"))
    try:
        with pytest.raises(TrackUnavailable):
            player.play_tracks([make_track(1), make_track(2)])

        # Повторяется тот же трек, но лимит ошибок останавливает цикл.
        assert adapter.stream_calls == ["1", "1"]
        assert player.state.status is PlayerStatus.STOPPED
        assert len(events.named("error")) == MAX_CONSECUTIVE_FAILURES
    finally:
        player.close()


def test_shuffle_does_not_loop_forever_on_broken_tracks(adapter, backend):
    adapter.fail_ids = {"1", "2", "3"}
    queue = Queue(shuffle=True, rng=random.Random(7))
    player, _ = make_player(adapter, backend=backend, queue=queue)
    try:
        with pytest.raises(TrackUnavailable):
            player.play_tracks([make_track(1), make_track(2), make_track(3)])

        assert len(adapter.stream_calls) == MAX_CONSECUTIVE_FAILURES
        assert player.state.status is PlayerStatus.STOPPED
        assert player.state.overlay is Overlay.TRACK_UNAVAILABLE
    finally:
        player.close()


# --- отказ одного провайдера не затрагивает другой -----------------------
def test_provider_failure_does_not_affect_other_providers():
    yandex = NamedAdapter("yandex", fail={"1"})
    local = NamedAdapter("local")
    router = ProviderRouter(
        {"yandex": lambda: yandex, "local": lambda: local}, default="yandex"
    )
    backend = NullBackend()
    player, events = make_player(router.adapter("yandex"), backend=backend, router=router)
    try:
        broken = Track(id="1", title="яндекс", provider="yandex")
        good = Track(id="7", title="локальный", provider="local")

        playing = player.play_tracks([broken, good])

        assert playing is not None and playing.key == "local:7"
        assert player.current_track.key == "local:7"
        assert player.state.status is PlayerStatus.PLAYING
        assert backend.loaded_url == "local://7"
        assert yandex.stream_calls == ["1"]
        assert local.stream_calls == ["7"]
        assert [payload["track"].key for payload in events.named("track")] == ["local:7"]
    finally:
        player.close()
