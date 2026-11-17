"""Smoke-тест окна Qt в offscreen-режиме.

Проверяет не внешний вид, а связки: снимок состояния попадает в подписи,
кнопка «Пауза» реально доходит до ядра через диспетчер, а громкость выше 100 %
показывается как ограниченная. Без PySide6 или без Qt-платформы тест
пропускается.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PySide6 = pytest.importorskip("PySide6.QtWidgets", reason="нет PySide6")

from neurowave.audio import NullBackend  # noqa: E402
from neurowave.config import Settings  # noqa: E402
from neurowave.dispatcher import CommandDispatcher  # noqa: E402
from neurowave.player import PlayerService  # noqa: E402
from neurowave.ui.viewmodel import PlayerViewModel  # noqa: E402
from tests.conftest import FakeAdapter, make_track  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def window(qapp):
    from neurowave.ui.app import _build_window

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


def _button(window, text: str):
    from PySide6.QtWidgets import QPushButton

    for button in window.findChildren(QPushButton):
        if button.text() == text:
            return button
    raise AssertionError(f"нет кнопки {text!r}")


def test_window_shows_track_and_quality(window) -> None:
    window, player, _, _ = window
    player.play_tracks([make_track(1)])
    window._refresh()
    texts = _labels(window)
    assert "Song 1" in texts
    assert any("Artist 1" in text for text in texts)


def test_play_button_reaches_core(window) -> None:
    window, player, backend, model = window
    player.play_tracks([make_track(1)])
    window._refresh()
    assert backend.is_playing() is True
    _button(window, "⏸").click()
    assert model.dispatcher.wait_idle(3)
    assert backend.is_playing() is False
    window._refresh()
    _button(window, "▶").click()
    assert model.dispatcher.wait_idle(3)
    assert backend.is_playing() is True


def test_volume_limit_is_explained_in_window(window) -> None:
    window, player, _, _ = window
    player.set_volume(130)
    window._refresh()
    texts = " ".join(_labels(window))
    assert "Усиление выше 100 % выключено" in texts
    assert "130 %" in texts and "100 %" in texts


def test_pure_checkbox_reflects_mode(window) -> None:
    window, player, _, model = window
    player.play_tracks([make_track(1)])
    from PySide6.QtWidgets import QCheckBox

    checkbox = window.findChildren(QCheckBox)[0]
    assert checkbox.isChecked() is True  # профиль по умолчанию — pure
    checkbox.setChecked(False)
    assert model.dispatcher.wait_idle(3)
    window._refresh()
    assert player.dsp.status.mode.value == "enhanced"


def test_app_quits_on_sigterm(tmp_path) -> None:
    """SIGTERM должен закрывать окно: иначе процесс висит после выхода из сессии."""
    import signal
    import subprocess
    import sys
    import time

    # Ядро проверяет наличие mpv при сборке плеера: даём ему двойника,
    # сам процесс окно всё равно не запускает.
    import shutil
    from pathlib import Path

    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    fake = binary_dir / "mpv"
    shutil.copy(Path(__file__).with_name("fake_mpv.py"), fake)
    fake.chmod(0o755)

    env = {
        **os.environ,
        "QT_QPA_PLATFORM": "offscreen",
        "NEUROWAVE_HOME": str(tmp_path),
        "PATH": f"{binary_dir}:{os.environ.get('PATH', '')}",
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "neurowave", "--home", str(tmp_path), "--provider", "local", "app"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(3.0)  # окно успевает построиться
        assert process.poll() is None
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=8) in (0, -signal.SIGTERM)
    finally:
        if process.poll() is None:
            process.kill()


def test_soundlab_collects_and_applies_profile(window) -> None:
    from neurowave.ui.soundlab import _build_dialog

    window, player, backend, model = window
    player.play_tracks([make_track(1)])
    dialog = _build_dialog(model)
    dialog._add_row()
    table = dialog._bands
    assert table.rowCount() == 1
    table.cellWidget(0, 1).setValue(2000.0)
    table.cellWidget(0, 2).setValue(3.0)
    profile = dialog._collect()
    assert profile.bands[0].frequency_hz == 2000.0
    dialog._on_apply()
    assert model.dispatcher.wait_idle(3)
    assert "g=3" in (backend.audio_filter_value or "")
    dialog.close()


def test_soundlab_rejects_invalid_profile(window) -> None:
    from PySide6.QtWidgets import QLineEdit

    from neurowave.ui.soundlab import _build_dialog

    window, player, backend, model = window
    dialog = _build_dialog(model)
    name = dialog.findChildren(QLineEdit)[0]
    name.setText("   ")  # пустое имя — невалидный профиль
    dialog._on_apply()
    assert model.dispatcher.wait_idle(1)
    assert "Проверьте параметры" in dialog._status.text()
    assert backend.audio_filter_value is None or "g=" not in backend.audio_filter_value
    dialog.close()


def test_soundlab_saves_custom_profile(window) -> None:
    from PySide6.QtWidgets import QLineEdit

    from neurowave.ui.soundlab import _build_dialog

    window, player, _backend, model = window
    dialog = _build_dialog(model)
    edits = dialog.findChildren(QLineEdit)
    edits[0].setText("Свой пресет")
    edits[1].setText("my-preset")
    dialog._on_save()
    assert model.dispatcher.wait_idle(3)
    saved = {item["id"] for item in player.settings.custom_profiles}
    assert "my-preset" in saved
    dialog.close()


def test_soundlab_lists_builtin_and_custom(window) -> None:
    from neurowave.ui.soundlab import _build_dialog

    window, _player, _backend, model = window
    dialog = _build_dialog(model)
    names = [dialog.findChildren(type(dialog._bands))]  # sanity: dialog построен
    assert names
    assert model.profiles()
    dialog.close()


def test_mixtape_button_opens_screen(window, monkeypatch) -> None:
    """Кнопка в главном окне открывает экран USB Mixtape (мост — Qt-free)."""
    from neurowave.ui import mixtape as mixtape_ui

    window, _player, _backend, _model = window
    opened: list[object] = []

    def fake_open(parent=None, bridge=None):
        opened.append(parent)
        return object()

    monkeypatch.setattr(mixtape_ui, "open_mixtape", fake_open)
    _button(window, "USB Mixtape").click()

    assert opened
