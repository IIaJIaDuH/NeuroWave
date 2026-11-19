"""View-model окна: снимки состояния, очередь событий и команды.

Проверяется без Qt: слой специально отделён, чтобы поведение интерфейса можно
было тестировать обычным pytest, а не «глазами».
"""

from __future__ import annotations

import time

import pytest

from neurowave.audio import NullBackend
from neurowave.config import Settings
from neurowave.dispatcher import CommandDispatcher
from neurowave.player import PlayerService
from neurowave.ui.viewmodel import PlayerViewModel, UiState
from tests.conftest import FakeAdapter, make_track


@pytest.fixture
def vm():
    backend = NullBackend()
    player = PlayerService(adapter=FakeAdapter(), backend=backend, settings=Settings(), prefetch=False)
    dispatcher = CommandDispatcher()
    model = PlayerViewModel(player, dispatcher=dispatcher)
    yield model, player, backend
    model.close()


def test_snapshot_describes_idle_player(vm) -> None:
    model, _, _ = vm
    state = model.snapshot()
    assert state.title == "NeuroWave"
    assert state.status == "idle"
    assert state.is_playing is False
    assert state.progress == 0.0
    assert state.profile_mode == "pure"


def test_snapshot_follows_playback(vm) -> None:
    model, player, _ = vm
    player.play_tracks([make_track(1)])
    state = model.snapshot()
    assert state.title == "Song 1"
    assert state.subtitle.startswith("Artist 1")
    assert state.is_playing is True
    assert state.duration_ms == 180_000
    model.set_position(30_000)
    assert 0.16 < model.snapshot().progress < 0.17


def test_volume_warning_is_visible_in_state(vm) -> None:
    model, player, _ = vm
    player.set_volume(130)
    state = model.snapshot()
    assert state.volume == 130
    assert state.applied_volume == 100
    assert state.volume_limited is True


def test_events_are_queued_from_audio_thread(vm) -> None:
    model, player, backend = vm
    player.play_tracks([make_track(1)])
    backend.finish()  # событие приходит из «аудиопотока»
    deadline = time.monotonic() + 2
    events = []
    while time.monotonic() < deadline and not events:
        events = model.drain()
        time.sleep(0.02)
    names = {event.name for event in events}
    assert names & {"track", "started"}, names
    assert model.snapshot().last_event


def test_commands_go_through_dispatcher(vm) -> None:
    model, player, backend = vm
    player.play_tracks([make_track(1)])
    model.toggle()
    assert model.dispatcher.wait_idle(3)
    assert backend.is_playing() is False
    model.toggle()
    assert model.dispatcher.wait_idle(3)
    assert backend.is_playing() is True


def test_next_bumps_generation(vm) -> None:
    model, player, _ = vm
    player.play_tracks([make_track(1), make_track(2)])
    before = model.dispatcher.generation
    model.next()
    assert model.dispatcher.generation == before + 1
    assert model.dispatcher.wait_idle(3)
    assert player.current_track.id == "2"


def test_seek_moves_slider_immediately(vm) -> None:
    model, player, _ = vm
    player.play_tracks([make_track(1)])
    model.seek(60_000)
    assert model.position_ms() == 60_000  # без ожидания IPC
    assert model.dispatcher.wait_idle(3)


def test_set_profile_and_pure_switch(vm) -> None:
    model, player, backend = vm
    player.play_tracks([make_track(1)])
    model.set_profile("night")
    assert model.dispatcher.wait_idle(3)
    assert model.snapshot().profile_id == "night"
    assert model.snapshot().profile_mode == "enhanced"
    model.set_mode("pure")
    assert model.dispatcher.wait_idle(3)
    assert backend.audio_filter_value is None
    assert model.snapshot().profile_mode == "pure"


def test_position_polling_does_not_block_gui(vm) -> None:
    model, player, _ = vm
    player.play_tracks([make_track(1)])
    model.poll_interval = 0.05
    model.start_polling()
    time.sleep(0.25)
    model.stop_polling()
    assert model.position_ms() >= 0


def test_output_name_is_human_readable(vm) -> None:
    model, player, _ = vm
    player.settings.audio_output = "pulse/fake"
    state = model.snapshot()
    assert state.output_id == "pulse/fake"
    assert "pulse/fake" in state.output_name


def test_state_serializes_for_debugging(vm) -> None:
    model, _, _ = vm
    data = model.snapshot().to_dict()
    assert data["status"] == "idle"
    assert set(data) >= {"title", "volume", "profile_id", "output_id"}
    assert UiState().progress == 0.0


# --- D-06: неизвестная длительность -----------------------------------
def test_snapshot_marks_unknown_duration_and_keeps_numbers_safe(vm) -> None:
    model, player, _ = vm
    player.play_tracks([make_track(1, duration_ms=None)])
    model.set_position(15_000)
    state = model.snapshot()
    assert state.status == "playing"
    assert state.is_playing is True
    assert state.duration_known is False
    assert state.duration_ms == 0  # число, а не None: GUI не делит None
    assert state.progress == 0.0
    assert state.position_ms == 15_000


def test_snapshot_distinguishes_known_zero_duration(vm) -> None:
    model, player, _ = vm
    player.play_tracks([make_track(1, duration_ms=0)])
    state = model.snapshot()
    assert state.duration_known is True
    assert state.duration_ms == 0
    assert state.progress == 0.0


def test_snapshot_treats_empty_queue_as_known_zero(vm) -> None:
    model, _, _ = vm
    state = model.snapshot()
    assert state.duration_known is True
    assert state.duration_ms == 0
    assert state.progress == 0.0


def test_snapshot_keeps_long_duration(vm) -> None:
    model, player, _ = vm
    player.play_tracks([make_track(1, duration_ms=6 * 3_600_000)])
    model.set_position(3_600_000)
    state = model.snapshot()
    assert state.duration_known is True
    assert state.duration_ms == 21_600_000
    assert 0.16 < state.progress < 0.17


def test_state_dict_exposes_duration_known(vm) -> None:
    import json

    model, player, _ = vm
    player.play_tracks([make_track(1, duration_ms=None)])
    data = model.snapshot().to_dict()
    assert data["duration_known"] is False
    assert data["duration_ms"] == 0
    json.dumps(data)  # снимок остаётся сериализуемым для отладки


def test_hundred_snapshots_with_unknown_duration(vm) -> None:
    model, player, _ = vm
    player.play_tracks([make_track(1, duration_ms=None)])
    model.set_position(15_000)
    for _ in range(100):
        state = model.snapshot()
        assert state.title == "Song 1"
        assert state.duration_ms == 0
        assert state.progress == 0.0
        assert state.volume == 80
