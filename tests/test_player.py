from __future__ import annotations

import pytest

from neurowave.audio import NullBackend
from neurowave.config import MAX_VOLUME, SAFE_VOLUME
from neurowave.domain import Track
from neurowave.errors import PlayerError, TrackUnavailable
from neurowave.player import PlayerService
from neurowave.state import Overlay, PlayerStatus
from tests.conftest import FakeAdapter, make_track


def test_start_wave_plays_first_track(player, backend, adapter):
    track = player.start_wave()
    assert track is not None and track.id == "1"
    assert backend.loaded_url == "https://audio.test/1.mp3"
    assert player.state.status is PlayerStatus.PLAYING
    assert ("started", "1") in adapter.feedback


def test_track_end_advances(player, backend, adapter):
    player.start_wave()
    backend.finish()
    assert backend.loaded_url == "https://audio.test/2.mp3"
    assert ("finished", "1") in adapter.feedback
    assert player.current_track.id == "2"


def test_repeat_one_replays_same_track(player, backend):
    player.queue.set_repeat("one")
    player.start_wave()
    backend.finish()
    assert backend.loaded_url == "https://audio.test/1.mp3"
    assert player.state.status is PlayerStatus.PLAYING


def test_queue_end_stops(player, backend):
    player.play_tracks([make_track(1)])
    backend.finish()
    assert player.state.status is PlayerStatus.STOPPED


def test_unavailable_track_is_skipped(player, backend, adapter):
    adapter.fail_ids = {"2"}
    player.start_wave()
    backend.finish()
    assert backend.loaded_url == "https://audio.test/3.mp3"
    assert player.current_track.id == "3"


def test_two_failures_stop_with_overlay(player, backend, adapter):
    adapter.fail_ids = {"2", "3"}
    player.start_wave()
    backend.finish()
    assert player.state.status is PlayerStatus.STOPPED
    assert player.state.overlay is Overlay.TRACK_UNAVAILABLE


def test_unavailable_first_track_raises(player, adapter):
    adapter.fail_ids = {"1"}
    with pytest.raises(TrackUnavailable):
        player.start_wave()


def test_pause_and_resume(player, backend):
    player.start_wave()
    assert player.toggle_pause() is PlayerStatus.PAUSED
    assert backend.is_playing() is False
    assert player.toggle_pause() is PlayerStatus.PLAYING
    assert backend.is_playing() is True
    player.stop()
    with pytest.raises(PlayerError):
        player.toggle_pause()


def test_volume_is_clamped_and_saved(player, backend):
    # Запрошенное значение хранится (шкала до 130 %), но в звук без
    # проверенного лимитера уходит не больше 100 % — см. test_volume_safety.
    assert player.set_volume(500) == SAFE_VOLUME
    assert backend.volume == SAFE_VOLUME
    assert player.settings.volume == MAX_VOLUME
    assert player.set_volume(-10) == 0
    assert backend.volume == 0


def test_play_tracks_skips_unavailable_and_starts(player, backend):
    tracks = [make_track(1, available=False), make_track(2)]
    player.play_tracks(tracks)
    assert player.current_track.id == "2"
    assert backend.loaded_url == "https://audio.test/2.mp3"


def test_play_tracks_rejects_empty(player):
    with pytest.raises(TrackUnavailable):
        player.play_tracks([make_track(1, available=False)])


def test_previous_returns_to_previous_track(player, backend):
    player.start_wave()
    player.next_track()
    assert player.current_track.id == "2"
    assert player.previous_track().id == "1"
    assert backend.loaded_url == "https://audio.test/1.mp3"


def test_cached_track_is_preferred(tmp_path, adapter, backend):
    from neurowave.config import Settings
    from neurowave.storage.cache import MediaCache

    cache = MediaCache(tmp_path / "cache", quota_bytes=10**6)
    cache.put_bytes("1", b"fake-audio")
    player = PlayerService(
        adapter=adapter,
        backend=backend,
        settings=Settings(volume=50),
        cache=cache,
        prefetch=False,
    )
    try:
        player.start_wave()
        assert backend.loaded_url == str(cache.get("1"))
        assert adapter.stream_calls == []
    finally:
        player.close()


def test_wave_feedback_only_for_wave(player, adapter):
    player.play_tracks([make_track(1), make_track(2)])
    player.next_track()
    assert adapter.feedback == []
