"""Ошибки фоновых команд в реальном окне Qt (offscreen).

D-05: исключение в рабочем потоке ``CommandDispatcher`` раньше оставалось
внутри диспетчера — строка статуса не менялась, и пользователь не понимал,
почему выбранный аудиовыход «не работает». Здесь проверяется весь путь:
команда → ошибка → очередь событий → статусная строка.
"""

from __future__ import annotations

import logging
import os
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6.QtWidgets", reason="нет PySide6")

from neurowave.audio import NullBackend  # noqa: E402
from neurowave.config import Settings  # noqa: E402
from neurowave.dispatcher import CommandDispatcher  # noqa: E402
from neurowave.errors import PlayerError  # noqa: E402
from neurowave.player import PlayerService  # noqa: E402
from neurowave.ui.app import ERROR_COLOR, _build_window  # noqa: E402
from neurowave.ui.viewmodel import PlayerViewModel  # noqa: E402
from tests.conftest import FakeAdapter, make_track  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def window(qapp):
    backend = NullBackend()
    backend.is_running = True  # окно работает с «живым» mpv-двойником
    player = PlayerService(
        adapter=FakeAdapter(), backend=backend, settings=Settings(), prefetch=False
    )
    dispatcher = CommandDispatcher()
    model = PlayerViewModel(player, dispatcher=dispatcher)
    window = _build_window(model)
    yield window, player, backend, model
    window.close()
    model.close()


def _labels(window) -> list[str]:
    from PySide6.QtWidgets import QLabel

    return [label.text() for label in window.findChildren(QLabel)]


def _button(window, text: str):
    from PySide6.QtWidgets import QPushButton

    for button in window.findChildren(QPushButton):
        if button.text() == text:
            return button
    raise AssertionError(f"нет кнопки {text!r}")


def _seek_slider(window):
    from PySide6.QtWidgets import QSlider

    sliders = [item for item in window.findChildren(QSlider) if item.maximum() == 1000]
    assert len(sliders) == 1
    return sliders[0]


def _status(window) -> str:
    return window._status.text()


def _fail_with(player, name: str, error: BaseException) -> None:
    def boom(*_args, **_kwargs):
        raise error

    setattr(player, name, boom)


# --- A. Ошибка выбора аудиовыхода --------------------------------------
def test_output_error_is_shown_in_status(window) -> None:
    window, player, _backend, model = window
    player.play_tracks([make_track(1)])

    model.set_output("pulse/dead")  # устройства нет в списке доступных
    assert model.dispatcher.wait_idle(3)
    window._refresh()

    text = _status(window)
    assert text.startswith("Ошибка: ")
    assert "не удалось выбрать аудиовыход" in text
    assert "pulse/dead" in text
    assert ERROR_COLOR.lower() in window._status.styleSheet().lower()


def test_error_survives_background_events(window) -> None:
    window, player, _backend, model = window
    player.play_tracks([make_track(1)])
    model.set_output("pulse/dead")
    assert model.dispatcher.wait_idle(3)
    window._refresh()
    assert "Ошибка:" in _status(window)

    # Фоновое событие (например, автопереход трека) не должно стирать ошибку.
    player.play_tracks([make_track(2)])
    window._refresh()
    window._refresh()

    assert "Ошибка:" in _status(window)
    assert "аудиовыход" in _status(window)


# --- B. Ошибка перемотки ------------------------------------------------
def test_seek_error_from_slider_is_shown(window) -> None:
    window, player, _backend, model = window
    player.play_tracks([make_track(1)])
    window._refresh()
    slider = _seek_slider(window)
    assert slider.isEnabled() is True

    _fail_with(player, "seek", PlayerError("mpv не отвечает"))
    slider.setValue(500)
    slider.sliderReleased.emit()
    assert model.dispatcher.wait_idle(3)
    window._refresh()

    assert "не удалось перемотать" in _status(window)
    assert "mpv не отвечает" in _status(window)


# --- C. Ошибка DSP-профиля ----------------------------------------------
def test_profile_error_is_visible_and_window_stays_alive(window) -> None:
    window, player, _backend, model = window
    player.play_tracks([make_track(1)])
    _fail_with(player, "set_dsp_profile", PlayerError("профиль отклонён"))

    model.set_profile("night")
    assert model.dispatcher.wait_idle(3)
    window._refresh()
    window._refresh()

    assert "профиль" in _status(window)
    assert model.snapshot().status == "playing"
    assert window.windowTitle() == "NeuroWave"


# --- D. Ошибка воспроизведения ------------------------------------------
def test_playback_error_is_visible_and_qt_keeps_running(window) -> None:
    window, player, _backend, model = window
    player.play_tracks([make_track(1)])
    window._refresh()
    _fail_with(player, "next_track", PlayerError("очередь пуста"))

    _button(window, "⏭").click()
    assert model.dispatcher.wait_idle(3)
    window._refresh()

    assert "не удалось переключить трек" in _status(window)
    # Окно продолжает работать: обычная команда проходит.
    _button(window, "⏸").click()
    assert model.dispatcher.wait_idle(3)
    window._refresh()
    assert "Ошибка" not in _status(window)


# --- F. Успех после ошибки ----------------------------------------------
def test_user_action_restores_normal_status(window) -> None:
    window, player, _backend, model = window
    player.play_tracks([make_track(1)])
    model.set_output("pulse/dead")
    assert model.dispatcher.wait_idle(3)
    window._refresh()
    assert "Ошибка:" in _status(window)

    _button(window, "⏸").click()  # пауза проходит успешно
    assert model.dispatcher.wait_idle(3)
    window._refresh()

    assert "Ошибка" not in _status(window)
    assert ERROR_COLOR.lower() not in window._status.styleSheet().lower()
    assert model.snapshot().is_playing is False


# --- G. Несколько ошибок подряд -----------------------------------------
def test_latest_error_replaces_previous_in_status(window) -> None:
    window, player, _backend, model = window
    player.play_tracks([make_track(1)])
    _fail_with(player, "seek", PlayerError("mpv не отвечает"))

    model.set_output("pulse/dead")
    assert model.dispatcher.wait_idle(3)
    model.seek(60_000)
    assert model.dispatcher.wait_idle(3)
    window._refresh()

    assert "перемотать" in _status(window)
    assert "аудиовыход" not in _status(window)
    assert "перемотать" in (model.last_error or "")


# --- H. Закрытие приложения ---------------------------------------------
def test_refresh_stays_fast_while_error_is_shown(window) -> None:
    window, player, _backend, model = window
    model.set_output("pulse/dead")
    assert model.dispatcher.wait_idle(3)
    window._refresh()

    started = time.monotonic()
    for _ in range(20):
        window._refresh()
    assert time.monotonic() - started < 2.0


def test_close_with_pending_error_is_safe(window) -> None:
    window, player, _backend, model = window
    _fail_with(player, "set_output", PlayerError("устройство недоступно"))
    model.set_output("pulse/dead")
    assert model.dispatcher.wait_idle(3)

    window.close()
    model.close()  # повторное закрытие не должно падать

    assert model.dispatcher._thread.is_alive() is False
    assert model.dispatcher._error_listeners == []


# --- J. Независимость от D-06 -------------------------------------------
def test_unknown_duration_and_error_are_shown_together(window) -> None:
    window, player, _backend, model = window
    player.play_tracks([make_track(1, duration_ms=None)])
    model.set_position(15_000)
    model.set_output("pulse/dead")
    assert model.dispatcher.wait_idle(3)
    window._refresh()

    texts = _labels(window)
    assert "Ошибка:" in _status(window)
    assert "0:15 / --:--" in texts
    assert _button(window, "⏸") is not None  # Play/Pause продолжает обновляться


# --- D-05-R1: жизненный цикл ошибки и защита секретов ------------------
def test_error_is_kept_until_next_command_really_finishes(window) -> None:
    """Постановка команды B не снимает ошибку A до завершения B."""
    window, player, _backend, model = window
    player.play_tracks([make_track(1)])
    original = player.toggle_pause
    _fail_with(player, "toggle_pause", PlayerError("плеер занят"))
    model.toggle()
    assert model.dispatcher.wait_idle(3)
    window._refresh()
    assert "Ошибка:" in _status(window)
    assert "плеер занят" in _status(window)

    gate = threading.Event()
    started = threading.Event()

    def slow_pause() -> None:
        started.set()
        gate.wait(5)

    player.toggle_pause = slow_pause
    _button(window, "⏸").click()
    assert started.wait(2)

    # B ещё выполняется: ошибка A остаётся видимой, а не мигает.
    window._refresh()
    window._refresh()
    assert "Ошибка:" in _status(window)
    assert "плеер занят" in _status(window)
    assert model.last_error is not None

    gate.set()
    assert model.dispatcher.wait_idle(3)
    window._refresh()

    assert "Ошибка" not in _status(window)
    assert ERROR_COLOR.lower() not in window._status.styleSheet().lower()
    assert model.last_error is None
    player.toggle_pause = original


def test_new_error_replaces_old_one_and_stays_after_background_event(window) -> None:
    window, player, _backend, model = window
    player.play_tracks([make_track(1)])
    _fail_with(player, "toggle_pause", PlayerError("плеер занят"))
    model.toggle()
    assert model.dispatcher.wait_idle(3)
    window._refresh()
    assert "плеер занят" in _status(window)

    _fail_with(player, "seek", PlayerError("mpv не отвечает"))
    model.seek(30_000)
    assert model.dispatcher.wait_idle(3)
    player.play_tracks([make_track(2)])  # фоновое событие не стирает ошибку
    window._refresh()

    assert "перемотать" in _status(window)
    assert "mpv не отвечает" in _status(window)
    assert "плеер занят" not in _status(window)


def test_unknown_command_error_hides_secrets_in_log_and_status(window, caplog) -> None:
    window, player, _backend, model = window
    player.play_tracks([make_track(1)])
    _fail_with(
        player,
        "set_output",
        RuntimeError(
            "OAuth failed: Authorization: Bearer TOKEN-SECRET-777, "
            "cookie: session=COOKIE-SECRET-888"
        ),
    )

    with caplog.at_level(logging.DEBUG, logger="neurowave.ui.viewmodel"):
        model.set_output("pulse/dead")
        assert model.dispatcher.wait_idle(3)
    window._refresh()

    assert _status(window) == "Ошибка: не удалось выбрать аудиовыход"
    for secret in ("TOKEN-SECRET-777", "COOKIE-SECRET-888", "OAuth failed", "Bearer"):
        assert secret not in caplog.text
        assert secret not in _status(window)
    assert "RuntimeError" in caplog.text
