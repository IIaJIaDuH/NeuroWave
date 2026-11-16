"""Регрессии слоя mpv: IPC-читатель, обработчик конца трека, жизненный цикл.

Живые прогоны показали, что именно здесь ломался автопереход: сессия играла
один трек и навсегда застревала в состоянии ``playing``. Тесты ниже работают
с двойником mpv (``tests/fake_mpv.py``), а не с ``NullBackend``, поэтому
проверяют реальный JSON IPC.
"""

from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path

import pytest

from neurowave.audio.mpv import COMMAND_TIMEOUT, MpvBackend


@pytest.fixture
def fake_mpv(tmp_path: Path) -> Path:
    """Исполняемый двойник mpv рядом с тестом."""
    binary = tmp_path / "mpv"
    shutil.copy(Path(__file__).with_name("fake_mpv.py"), binary)
    binary.chmod(0o755)
    return binary


def _backend(fake_mpv: Path, tmp_path: Path, plan: str, on_end=None) -> MpvBackend:
    return MpvBackend(
        binary=str(fake_mpv),
        socket_path=tmp_path / f"mpv-{plan}.sock",
        extra_args=[f"--fake-plan={plan}"],
        on_end=on_end,
    )


def test_is_playing_is_false_while_idle(fake_mpv, tmp_path) -> None:
    """В idle нет файла и играть нечего — прошлый код отвечал True."""
    backend = _backend(fake_mpv, tmp_path, "idle")
    try:
        backend.start()
        assert backend.is_playing() is False
        backend.load("fake://track")
        assert backend.is_playing() is True
    finally:
        backend.shutdown()


def test_command_after_long_silence_does_not_break_ipc(fake_mpv, tmp_path) -> None:
    """Тишина дольше таймаута команд не должна убивать читателя IPC."""
    backend = _backend(fake_mpv, tmp_path, "idle")
    try:
        backend.start()
        time.sleep(COMMAND_TIMEOUT + 0.7)  # раньше читатель здесь умирал
        assert backend.position_ms() == 0  # не PlayerError, файла нет
        assert backend._reader is not None and backend._reader.is_alive()
    finally:
        backend.shutdown()


def test_late_end_event_is_delivered_after_silence(fake_mpv, tmp_path) -> None:
    """end-file после 4 с тишины доходит: иначе очередь не переключается."""
    received: list[str] = []
    done = threading.Event()

    def on_end(reason: str) -> None:
        received.append(reason)
        done.set()

    backend = _backend(fake_mpv, tmp_path, "eof-late", on_end=on_end)
    try:
        backend.start()
        backend.load("fake://track")
        assert done.wait(COMMAND_TIMEOUT + 3.0), "событие завершения потеряно"
        assert received == ["eof"]
    finally:
        backend.shutdown()


def test_end_handler_may_send_commands(fake_mpv, tmp_path) -> None:
    """Из обработчика конца трека можно грузить следующий без дедлока."""
    handled = threading.Event()
    elapsed: list[float] = []
    backend: MpvBackend | None = None

    def on_end(reason: str) -> None:
        if handled.is_set():
            return
        started = time.monotonic()
        backend.load("fake://next")  # раньше ждало ответ 3 с и падало
        backend.stop()
        elapsed.append(time.monotonic() - started)
        handled.set()

    backend = _backend(fake_mpv, tmp_path, "eof", on_end=on_end)
    try:
        backend.start()
        backend.load("fake://first")
        assert handled.wait(COMMAND_TIMEOUT * 2), "обработчик не выполнился"
        assert elapsed and elapsed[0] < COMMAND_TIMEOUT / 2
    finally:
        backend.shutdown()


def test_shutdown_stops_process_and_removes_socket(fake_mpv, tmp_path) -> None:
    """Иначе mpv с --idle=yes остаётся сиротой после выхода из приложения."""
    backend = _backend(fake_mpv, tmp_path, "idle")
    backend.start()
    socket_path = backend._socket_path
    process = backend._process
    assert socket_path.exists()

    backend.shutdown()

    assert backend.is_running is False
    assert process is not None and process.poll() is not None
    assert not socket_path.exists()
