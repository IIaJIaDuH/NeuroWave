"""Квалифицированные ID ``provider:track_id`` и маршрутизация по провайдеру."""

from __future__ import annotations

import pytest

from neurowave.audio import NullBackend
from neurowave.config import Settings
from neurowave.domain import Quality, RepeatMode, StreamInfo, Track
from neurowave.local import LocalMusicAdapter
from neurowave.player import PlayerService
from neurowave.providers.registry import ProviderNotImplemented
from neurowave.providers.router import ProviderRouter
from neurowave.queue import Queue
from neurowave.storage.cache import MediaCache
from neurowave.state import PlayerStatus


class FakeAdapter:
    def __init__(self, name: str):
        self.name = name
        self.stream_calls: list[str] = []
        self.feedback: list[tuple[str, str | None]] = []

    def wave_tracks(self) -> list[Track]:
        return [Track(id="1", title=f"{self.name} one", provider=self.name)]

    def wave_feedback(self, kind, track_id=None, total_played_seconds=None) -> bool:
        self.feedback.append((kind, track_id))
        return True

    def stream_url(self, track_id: str) -> StreamInfo:
        self.stream_calls.append(track_id)
        return StreamInfo(f"{self.name}://{track_id}", Quality("mp3", 320))


# --- модель -------------------------------------------------------------
def test_track_key_is_provider_qualified() -> None:
    track = Track(id="123", title="t", provider="yandex")
    assert track.key == "yandex:123"
    assert track.to_dict()["provider"] == "yandex"
    assert Track.from_dict(track.to_dict()) == track


def test_track_key_without_provider_is_raw_id() -> None:
    track = Track(id="123", title="t")
    assert track.key == "123"
    assert Track.from_dict(track.to_dict()).provider is None


def test_split_key_handles_colons_and_plain_ids() -> None:
    assert Track.split_key("local:Album/Track - One.flac") == ("local", "Album/Track - One.flac")
    assert Track.split_key("123") == (None, "123")
    assert Track.split_key("") == (None, "")


# --- очередь ------------------------------------------------------------
def test_queue_keeps_same_id_from_different_providers() -> None:
    queue = Queue(repeat=RepeatMode.OFF)
    queue.extend(
        [
            Track(id="1", title="яндекс", provider="yandex"),
            Track(id="1", title="локальный", provider="local"),
        ]
    )
    queue.jump(0)
    assert queue.remove("local:1") is True
    assert len(queue) == 1
    assert queue.current.key == "yandex:1"


def test_queue_history_and_snapshot_are_qualified() -> None:
    queue = Queue()
    queue.extend(
        [
            Track(id="1", title="a", provider="yandex"),
            Track(id="1", title="b", provider="local"),
        ]
    )
    queue.jump(0)
    queue.next()
    assert queue.current.key == "local:1"
    assert queue.previous().key == "yandex:1"

    restored = Queue.restore(queue.snapshot())
    assert [track.key for track in restored.items] == ["yandex:1", "local:1"]
    assert restored.position == queue.position


# --- роутер -------------------------------------------------------------
def make_router(**adapters) -> ProviderRouter:
    return ProviderRouter({name: (lambda adapter=adapter: adapter) for name, adapter in adapters.items()}, default="yandex")


def test_stream_url_routes_by_track_provider() -> None:
    yandex, local = FakeAdapter("yandex"), FakeAdapter("local")
    router = make_router(yandex=yandex, local=local)

    stream = router.stream_url(Track(id="7", title="t", provider="local"))
    assert stream.url == "local://7"
    assert local.stream_calls == ["7"]
    assert yandex.stream_calls == []

    # Без провайдера — адаптер по умолчанию и сырой id.
    router.stream_url(Track(id="8", title="t"))
    assert yandex.stream_calls == ["8"]


def test_unknown_provider_is_a_named_error() -> None:
    router = make_router(yandex=FakeAdapter("yandex"))
    with pytest.raises(ProviderNotImplemented) as exc:
        router.stream_url(Track(id="1", title="t", provider="soundcloud"))
    assert "soundcloud" in str(exc.value)


def test_wave_and_feedback_are_routed() -> None:
    yandex, local = FakeAdapter("yandex"), FakeAdapter("local")
    router = make_router(yandex=yandex, local=local)

    assert [track.provider for track in router.wave_tracks()] == ["yandex"]
    assert router.wave_feedback("started", "local:3") is True
    assert router.wave_feedback("skip", "3") is True
    assert local.feedback == [("started", "3")]
    assert yandex.feedback == [("skip", "3")]


def test_cache_policy_comes_from_provider_info() -> None:
    router = make_router(yandex=FakeAdapter("yandex"))
    assert router.cache_allowed(Track(id="1", title="t", provider="yandex")) is True
    assert router.cache_allowed(Track(id="1", title="t", provider="local")) is False
    assert router.cache_allowed(None) is False


# --- плеер --------------------------------------------------------------
def make_player(tmp_path, router: ProviderRouter, cache: MediaCache | None = None) -> PlayerService:
    return PlayerService(
        adapter=router.adapter(router.default),
        backend=NullBackend(),
        settings=Settings(volume=50),
        cache=cache,
        router=router,
        prefetch=False,
    )


def test_player_caches_by_qualified_key(tmp_path) -> None:
    cache = MediaCache(tmp_path / "cache")
    yandex = FakeAdapter("yandex")
    player = make_player(tmp_path, make_router(yandex=yandex), cache=cache)

    yandex_track = Track(id="5", title="five", provider="yandex")
    local_track = Track(id="5", title="five-local", provider="local")
    cache.put_bytes("yandex:5", b"cached-yandex")

    player.play_track(yandex_track)
    assert yandex.stream_calls == []  # взят из кеша
    assert player.backend.loaded_url.endswith(".cache")
    player.close()

    # Тот же id у другого провайдера не должен попадать в чужой кеш.
    yandex2, local2 = FakeAdapter("yandex"), FakeAdapter("local")
    player2 = make_player(tmp_path, make_router(yandex=yandex2, local=local2), cache=cache)
    data = cache.put_bytes("local:5", b"cached-local")
    player2.play_track(local_track)
    assert local2.stream_calls == ["5"]  # локальный провайдер кеш не использует
    assert player2.backend.loaded_url != str(data)
    player2.close()


def test_player_sends_feedback_for_current_provider(tmp_path) -> None:
    yandex = FakeAdapter("yandex")
    player = make_player(tmp_path, make_router(yandex=yandex))
    try:
        player.start_wave()
        assert yandex.feedback == [("started", "1")]
        player.next_track()
        assert yandex.feedback[-1][0] in {"skip", "finished"}
    finally:
        player.close()


def test_player_plays_local_file_through_router(tmp_path) -> None:
    music = tmp_path / "Music"
    music.mkdir()
    audio = music / "Artist - Song.mp3"
    audio.write_bytes(b"audio")
    local = LocalMusicAdapter(music)
    router = make_router(yandex=FakeAdapter("yandex"), local=local)
    player = make_player(tmp_path, router)

    try:
        track = local.tracks()[0]
        player.play_track(track)
        assert player.backend.loaded_url == str(audio.resolve())
        assert player.state.status is PlayerStatus.PLAYING
        assert player.current_quality is not None
        assert player.current_quality.codec == "mp3"
    finally:
        player.close()


def test_player_without_router_still_uses_single_adapter(tmp_path) -> None:
    """Старый путь (один адаптер) не сломан — CLI его больше не использует."""
    yandex = FakeAdapter("yandex")

    class Single(FakeAdapter):
        pass

    adapter = Single("yandex")
    player = PlayerService(adapter=adapter, backend=NullBackend(), settings=Settings(), prefetch=False)
    try:
        player.start_wave()
        assert adapter.stream_calls == ["1"]
        assert adapter.feedback == [("started", "1")]
    finally:
        player.close()
