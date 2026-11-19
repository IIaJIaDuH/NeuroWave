"""View-model окна: снимки состояния, очередь событий и команды.

Проверяется без Qt: слой специально отделён, чтобы поведение интерфейса можно
было тестировать обычным pytest, а не «глазами».
"""

from __future__ import annotations

import logging
import threading
import time

import pytest

from neurowave.audio import NullBackend
from neurowave.config import Settings
from neurowave.dispatcher import Command, CommandDispatcher
from neurowave.errors import PlayerError
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


# --- D-05: ошибки асинхронных команд -----------------------------------
def _fail_with(player, name: str, error: BaseException) -> None:
    """Проваливать одну команду плеера, как отключённое устройство."""

    def boom(*_args, **_kwargs):
        raise error

    setattr(player, name, boom)


def _error_events(model) -> list:
    return [event for event in model.drain() if event.name == "command_error"]


def test_command_error_reaches_viewmodel_and_queue(vm) -> None:
    model, player, _ = vm
    _fail_with(player, "set_output", PlayerError("Нет такого аудиовыхода: pulse/dead"))

    model.set_output("pulse/dead")
    assert model.dispatcher.wait_idle(3)

    errors = _error_events(model)
    assert len(errors) == 1
    payload = errors[0].payload
    assert set(payload) == {"command", "message", "command_id"}
    assert payload["command"] == "output"
    assert isinstance(payload["command_id"], int)
    assert payload["message"] == (
        "не удалось выбрать аудиовыход — Нет такого аудиовыхода: pulse/dead"
    )
    assert model.last_error == payload["message"]
    assert model.dispatcher.stats["failed"] == 1


def test_unknown_command_error_hides_details_but_keeps_text_safe(vm) -> None:
    model, player, _ = vm
    _fail_with(player, "set_output", RuntimeError("внутренний сбой token=SECRET123"))

    model.set_output("pulse/dead")
    assert model.dispatcher.wait_idle(3)

    message = _error_events(model)[0].payload["message"]
    assert message == "не удалось выбрать аудиовыход"
    assert "SECRET123" not in message
    assert "Traceback" not in message
    assert model.last_error == message


def test_expected_error_text_is_redacted(vm) -> None:
    model, player, _ = vm
    _fail_with(
        player,
        "set_output",
        PlayerError("401: Bearer abcdef123456, access_token=deadbeef"),
    )

    model.set_output("pulse/dead")
    assert model.dispatcher.wait_idle(3)

    message = _error_events(model)[0].payload["message"]
    assert "не удалось выбрать аудиовыход" in message
    assert "abcdef123456" not in message
    assert "deadbeef" not in message
    assert "***" in message


def test_several_errors_are_delivered_in_order(vm) -> None:
    model, player, _ = vm
    _fail_with(player, "seek", PlayerError("mpv не отвечает"))
    _fail_with(player, "set_output", PlayerError("устройство недоступно"))

    model.seek(1000)
    model.set_output("pulse/dead")
    assert model.dispatcher.wait_idle(3)

    errors = _error_events(model)
    assert [event.payload["command"] for event in errors] == ["seek", "output"]
    assert model.last_error == errors[-1].payload["message"]
    assert model.dispatcher.stats["failed"] == 2


def test_successful_command_after_error_is_not_reported(vm) -> None:
    model, player, backend = vm
    player.play_tracks([make_track(1)])
    original = player.toggle_pause
    _fail_with(player, "toggle_pause", PlayerError("плеер занят"))

    model.toggle()
    assert model.dispatcher.wait_idle(3)
    assert _error_events(model)

    player.toggle_pause = original
    model.toggle()
    assert model.dispatcher.wait_idle(3)
    assert _error_events(model) == []
    assert backend.is_playing() is False


def test_cancelled_navigation_does_not_report_error(vm) -> None:
    model, player, _ = vm
    player.play_tracks([make_track(1), make_track(2)])
    gate = threading.Event()
    started = threading.Event()

    def blocker() -> None:
        started.set()
        gate.wait(5)

    running = model.dispatcher.submit("blocker", blocker)
    assert started.wait(2)
    model.next()
    model.next()
    gate.set()

    assert running.done.wait(3)
    assert model.dispatcher.wait_idle(3)
    assert model.dispatcher.stats["cancelled"] >= 1
    assert _error_events(model) == []
    assert model.last_error is None


def test_external_dispatcher_keeps_its_own_error_handler() -> None:
    legacy: list[str] = []
    dispatcher = CommandDispatcher(on_error=lambda command: legacy.append(command.name))
    player = PlayerService(
        adapter=FakeAdapter(), backend=NullBackend(), settings=Settings(), prefetch=False
    )
    model = PlayerViewModel(player, dispatcher=dispatcher)
    try:
        _fail_with(player, "toggle_pause", PlayerError("плеер занят"))
        model.toggle()
        assert dispatcher.wait_idle(3)
        assert legacy == ["toggle"]
        errors = _error_events(model)
        assert len(errors) == 1
        assert "не удалось переключить воспроизведение" in errors[0].payload["message"]
    finally:
        model.close()


def test_close_releases_error_subscription() -> None:
    class RecordingDispatcher(CommandDispatcher):
        def __init__(self) -> None:
            super().__init__()
            self.added: list = []
            self.removed: list = []

        def add_error_listener(self, listener) -> None:
            self.added.append(listener)
            super().add_error_listener(listener)

        def remove_error_listener(self, listener) -> None:
            self.removed.append(listener)
            super().remove_error_listener(listener)

    dispatcher = RecordingDispatcher()
    player = PlayerService(
        adapter=FakeAdapter(), backend=NullBackend(), settings=Settings(), prefetch=False
    )
    model = PlayerViewModel(player, dispatcher=dispatcher)
    assert len(dispatcher.added) == 1

    model.close()

    assert dispatcher.removed == dispatcher.added
    assert dispatcher._error_listeners == []
    assert dispatcher._thread.is_alive() is False


# --- D-05-R1: секреты в логах и жизненный цикл ошибки ------------------
def _fake_command(command_id: int, name: str, seq: int, error=None) -> Command:
    return Command(
        id=command_id,
        name=name,
        action=lambda: None,
        generation=0,
        seq=seq,
        error=error,
    )


def test_unknown_error_reaches_log_without_exception_text(vm, caplog) -> None:
    """DEBUG-лог не должен публиковать текст неизвестного исключения."""
    model, player, _ = vm
    _fail_with(
        player,
        "set_output",
        RuntimeError(
            "OAuth failed: Authorization: Bearer TOKEN-SECRET-123, "
            "access_token=ACCESS-SECRET-456 cookie: session=COOKIE-SECRET-789"
        ),
    )

    with caplog.at_level(logging.DEBUG, logger="neurowave.ui.viewmodel"):
        model.set_output("pulse/dead")
        assert model.dispatcher.wait_idle(3)

    message = _error_events(model)[0].payload["message"]
    assert message == "не удалось выбрать аудиовыход"

    captured = caplog.text
    for secret in (
        "TOKEN-SECRET-123",
        "ACCESS-SECRET-456",
        "COOKIE-SECRET-789",
        "OAuth failed",
        "Traceback",
    ):
        assert secret not in captured
    # Диагностическая ценность сохранена: тип, имя и идентификатор команды.
    assert "RuntimeError" in captured
    assert "output" in captured
    assert "id=" in captured


def test_known_error_redacts_credentials_in_ui_and_log(vm, caplog) -> None:
    model, player, _ = vm
    _fail_with(
        player,
        "set_output",
        PlayerError(
            "401: Authorization: Bearer abcdef123456, access_token=deadbeef, "
            "password=hunter2, https://user:pa55@audio.example/dev"
        ),
    )

    with caplog.at_level(logging.DEBUG, logger="neurowave.ui.viewmodel"):
        model.set_output("pulse/dead")
        assert model.dispatcher.wait_idle(3)

    message = _error_events(model)[0].payload["message"]
    assert "не удалось выбрать аудиовыход" in message
    for secret in ("abcdef123456", "deadbeef", "hunter2", "pa55"):
        assert secret not in message
        assert secret not in caplog.text
    assert "***" in message


def test_error_clears_only_after_next_command_succeeds(vm) -> None:
    """Отправка команды — не успех: ошибка живёт до её завершения."""
    model, player, _ = vm
    player.play_tracks([make_track(1)])
    original = player.toggle_pause
    _fail_with(player, "toggle_pause", PlayerError("плеер занят"))
    model.toggle()
    assert model.dispatcher.wait_idle(3)
    assert model.last_error is not None
    assert _error_events(model)

    gate = threading.Event()
    started = threading.Event()

    def slow_pause() -> None:
        started.set()
        gate.wait(5)

    player.toggle_pause = slow_pause
    model.toggle()
    assert started.wait(2)

    # Команда уже в работе, но ещё не завершилась: ошибка не снимается.
    assert model.last_error is not None
    assert model.drain() == []

    gate.set()
    assert model.dispatcher.wait_idle(3)
    assert [event.name for event in model.drain()] == ["command_ok"]
    assert model.last_error is None
    player.toggle_pause = original


def test_second_error_replaces_the_first_cause(vm) -> None:
    model, player, _ = vm
    _fail_with(player, "toggle_pause", PlayerError("плеер занят"))
    _fail_with(player, "set_output", PlayerError("устройство недоступно"))

    model.toggle()
    model.set_output("pulse/dead")
    assert model.dispatcher.wait_idle(3)

    errors = _error_events(model)
    assert [event.payload["command"] for event in errors] == ["toggle", "output"]
    assert model.last_error is not None
    assert "устройство недоступно" in model.last_error
    assert "плеер занят" not in model.last_error


def test_delayed_error_while_next_command_is_queued(vm) -> None:
    """Порядок «ошибка A → успех B» даёт нормальный конечный статус."""
    model, player, _ = vm
    player.play_tracks([make_track(1)])
    release = threading.Event()
    started = threading.Event()

    def failing_pause() -> None:
        started.set()
        release.wait(5)
        raise PlayerError("плеер занят")

    player.toggle_pause = failing_pause
    model.toggle()  # A: упадёт после задержки
    assert started.wait(2)
    model.set_output("auto")  # B уже стоит в очереди
    release.set()

    assert model.dispatcher.wait_idle(3)
    names = [
        event.name
        for event in model.drain()
        if event.name in ("command_error", "command_ok")
    ]
    assert names == ["command_error", "command_ok"]
    assert model.last_error is None


def test_cancelled_command_reports_neither_error_nor_success(vm) -> None:
    model, player, _ = vm
    player.play_tracks([make_track(1), make_track(2)])
    model.drain()  # события запуска воспроизведения здесь не проверяются
    gate = threading.Event()
    started = threading.Event()

    def blocker() -> None:
        started.set()
        gate.wait(5)

    running = model.dispatcher.submit("blocker", blocker)
    assert started.wait(2)
    model.next()
    model.next()
    gate.set()

    assert running.done.wait(3)
    assert model.dispatcher.wait_idle(3)
    assert model.dispatcher.stats["cancelled"] >= 1
    names = [event.name for event in model.drain()]
    assert "command_error" not in names
    assert "command_ok" not in names
    assert model.last_error is None


def test_stale_success_does_not_clear_newer_error(vm) -> None:
    """Запоздавший успех старой команды не стирает свежую ошибку."""
    model, _, _ = vm
    model._on_command_error(
        _fake_command(2, "output", seq=7, error=PlayerError("свежая ошибка"))
    )
    model._on_command_done(_fake_command(1, "toggle", seq=5))

    assert model.last_error is not None
    assert "свежая ошибка" in model.last_error
    assert [event.name for event in model.drain()] == ["command_error"]


def test_external_dispatcher_keeps_completion_and_error_callbacks() -> None:
    legacy: list[str] = []
    external: list[str] = []
    dispatcher = CommandDispatcher(on_error=lambda command: legacy.append(command.name))
    dispatcher.add_completion_listener(lambda command: external.append(command.name))
    player = PlayerService(
        adapter=FakeAdapter(), backend=NullBackend(), settings=Settings(), prefetch=False
    )
    model = PlayerViewModel(player, dispatcher=dispatcher)
    try:
        model.set_output("auto")
        assert dispatcher.wait_idle(3)
        assert external == ["output"]
        assert legacy == []
        assert model.last_error is None
    finally:
        model.close()


def test_close_releases_completion_subscription_and_ignores_late_events() -> None:
    dispatcher = CommandDispatcher()
    player = PlayerService(
        adapter=FakeAdapter(), backend=NullBackend(), settings=Settings(), prefetch=False
    )
    model = PlayerViewModel(player, dispatcher=dispatcher)

    model.close()

    assert dispatcher._completion_listeners == []
    # Поздние вызовы после закрытия не должны ни падать, ни менять состояние.
    model._on_command_error(
        _fake_command(1, "output", seq=1, error=PlayerError("поздняя ошибка"))
    )
    model._on_command_done(_fake_command(2, "toggle", seq=2))
    assert model.last_error is None
    assert model.drain() == []
