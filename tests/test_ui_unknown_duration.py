"""D-06: неизвестная длительность не должна останавливать обновление окна.

Раньше ``duration_ms=None`` попадал в ``_mmss()`` и каждое обновление падало с
``TypeError``: подписи, ползунок и кнопка «Пауза» замирали, хотя звук играл.
Здесь проверяется контракт снимка, форматирование, безопасность перемотки и
реальный Qt-таймер в offscreen-режиме. Без PySide6 тесты пропускаются.
"""

from __future__ import annotations

import os
import sys
import threading

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6.QtWidgets", reason="нет PySide6")

from neurowave.audio import NullBackend  # noqa: E402
from neurowave.config import Settings  # noqa: E402
from neurowave.dispatcher import CommandDispatcher  # noqa: E402
from neurowave.player import PlayerService  # noqa: E402
from neurowave.ui.app import _build_window, _mmss  # noqa: E402
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
    player = PlayerService(adapter=FakeAdapter(), backend=backend, settings=Settings(), prefetch=False)
    dispatcher = CommandDispatcher()
    model = PlayerViewModel(player, dispatcher=dispatcher)
    window = _build_window(model)
    yield window, player, backend, model
    window.close()
    model.close()


def _labels(window) -> list[str]:
    from PySide6.QtWidgets import QLabel

    return [label.text() for label in window.findChildren(QLabel)]


def _time_text(window) -> str:
    for text in _labels(window):
        if " / " in text:
            return text
    raise AssertionError("нет подписи времени")


def _seek_slider(window):
    from PySide6.QtWidgets import QSlider

    for slider in window.findChildren(QSlider):
        if slider.maximum() == 1000:
            return slider
    raise AssertionError("нет ползунка перемотки")


def _button(window, text: str):
    from PySide6.QtWidgets import QPushButton

    for button in window.findChildren(QPushButton):
        if button.text() == text:
            return button
    raise AssertionError(f"нет кнопки {text!r}")


def _seeks(backend) -> list[int]:
    return [position for name, position in backend.calls if name == "seek"]


# --- формат времени (раздел 5) -----------------------------------------
@pytest.mark.parametrize(
    ("milliseconds", "expected"),
    [
        (None, "--:--"),
        (0, "0:00"),
        (61_000, "1:01"),
        (3_600_000, "60:00"),
        (6 * 3_600_000, "360:00"),
    ],
)
def test_mmss_formats_unknown_and_known(milliseconds, expected) -> None:
    assert _mmss(milliseconds) == expected


# --- A, G: неизвестная длительность и стресс обновлений ----------------
def test_unknown_duration_keeps_window_refreshing(window) -> None:
    win, player, _, model = window
    player.play_tracks([make_track(1, duration_ms=None)])
    model.set_position(15_000)
    for _ in range(100):
        win._refresh()
    assert _time_text(win) == "0:15 / --:--"
    assert _seek_slider(win).isEnabled() is False
    assert "Song 1" in _labels(win)


# --- B: Pause / Resume --------------------------------------------------
def test_pause_resume_with_unknown_duration(window) -> None:
    win, player, backend, model = window
    player.play_tracks([make_track(1, duration_ms=None)])
    win._refresh()
    _button(win, "⏸").click()
    assert model.dispatcher.wait_idle(3)
    win._refresh()
    assert backend.is_playing() is False
    _button(win, "▶")
    _button(win, "▶").click()
    assert model.dispatcher.wait_idle(3)
    win._refresh()
    assert backend.is_playing() is True
    _button(win, "⏸")


# --- C: Known → Unknown → Known ----------------------------------------
def test_switching_tracks_updates_duration_and_slider(window) -> None:
    win, player, _, model = window
    player.play_tracks(
        [make_track(1), make_track(2, duration_ms=None), make_track(3)]
    )
    win._refresh()
    assert _time_text(win).endswith("/ 3:00")
    assert _seek_slider(win).isEnabled() is True

    model.next()
    assert model.dispatcher.wait_idle(3)
    win._refresh()
    assert _time_text(win).endswith("/ --:--")
    assert _seek_slider(win).isEnabled() is False

    model.next()
    assert model.dispatcher.wait_idle(3)
    win._refresh()
    assert _time_text(win).endswith("/ 3:00")
    assert _seek_slider(win).isEnabled() is True


# --- D: нулевая длительность -------------------------------------------
def test_zero_duration_shows_zero_and_disables_seek(window) -> None:
    win, player, backend, model = window
    player.play_tracks([make_track(1, duration_ms=0)])
    win._refresh()
    assert _time_text(win) == "0:00 / 0:00"
    assert _seek_slider(win).isEnabled() is False
    backend.calls.clear()
    slider = _seek_slider(win)
    slider.setValue(500)
    slider.sliderReleased.emit()
    assert model.dispatcher.wait_idle(3)
    assert _seeks(backend) == []


# --- E: очень длинный трек ---------------------------------------------
def test_six_hour_track_shows_360_minutes(window) -> None:
    win, player, _, _model = window
    player.play_tracks([make_track(1, duration_ms=6 * 3_600_000)])
    win._refresh()
    assert _time_text(win) == "0:00 / 360:00"
    assert _seek_slider(win).isEnabled() is True


# --- F: пустая очередь --------------------------------------------------
def test_empty_queue_stays_alive(window) -> None:
    win, _player, _backend, _model = window
    win._refresh()
    assert _time_text(win) == "0:00 / 0:00"
    assert _seek_slider(win).isEnabled() is False
    _button(win, "▶")  # кнопка на месте, исключений нет


# --- H: реальный Qt timer ----------------------------------------------
def test_real_qt_timer_survives_unknown_duration(window) -> None:
    from PySide6.QtTest import QTest

    win, player, _, model = window
    player.play_tracks([make_track(1, duration_ms=None)])
    captured: list[tuple[type[BaseException], BaseException]] = []
    old_hook = sys.excepthook
    old_thread_hook = threading.excepthook
    sys.excepthook = lambda kind, value, tb: captured.append((kind, value))
    threading.excepthook = lambda args: captured.append((args.exc_type, args.exc_value))
    try:
        QTest.qWait(700)
        model.set_position(15_000)
        QTest.qWait(700)
        assert _time_text(win) == "0:15 / --:--"
        model.set_position(16_000)
        QTest.qWait(700)
        assert _time_text(win).startswith("0:16 / ")
        QTest.qWait(3_200)  # всего больше пяти секунд живого таймера
        assert captured == []
        assert _seek_slider(win).isEnabled() is False
    finally:
        sys.excepthook = old_hook
        threading.excepthook = old_thread_hook


# --- I: команды и события ----------------------------------------------
def test_transport_and_events_with_unknown_duration(window) -> None:
    win, player, _backend, model = window
    player.play_tracks([make_track(1, duration_ms=None), make_track(2)])
    win._refresh()

    _button(win, "⏭").click()
    assert model.dispatcher.wait_idle(3)
    win._refresh()
    assert "Song 2" in _labels(win)

    _button(win, "⏮").click()
    assert model.dispatcher.wait_idle(3)
    win._refresh()
    assert "Song 1" in _labels(win)

    model.on_event("started", {})
    win._refresh()
    assert "started" in _labels(win)

    _button(win, "⏹").click()
    assert model.dispatcher.wait_idle(3)
    win._refresh()
    _button(win, "▶")


# --- J: перемотка и безопасность ---------------------------------------
def test_unknown_duration_does_not_send_seek_on_release(window) -> None:
    win, player, backend, model = window
    player.play_tracks([make_track(1, duration_ms=None)])
    assert model.dispatcher.wait_idle(3)
    win._refresh()
    backend.calls.clear()
    slider = _seek_slider(win)
    slider.setValue(700)
    slider.sliderReleased.emit()
    assert model.dispatcher.wait_idle(3)
    assert _seeks(backend) == []
    assert model.position_ms() == 0


def test_known_duration_seek_still_works(window) -> None:
    win, player, backend, model = window
    player.play_tracks([make_track(1)])
    assert model.dispatcher.wait_idle(3)
    win._refresh()
    backend.calls.clear()
    slider = _seek_slider(win)
    slider.setValue(500)
    slider.sliderReleased.emit()
    assert model.dispatcher.wait_idle(3)
    assert _seeks(backend) == [90_000]
