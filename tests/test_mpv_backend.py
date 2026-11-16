"""Регрессии слоя mpv: IPC-читатель, обработчик конца трека, жизненный цикл.

Живые прогоны показали, что именно здесь ломался автопереход: сессия играла
один трек и навсегда застревала в состоянии ``playing``. Тесты ниже работают
с двойником mpv (``tests/fake_mpv.py``), а не с ``NullBackend``, поэтому
проверяют реальный JSON IPC.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from neurowave.audio.mpv import COMMAND_TIMEOUT, MpvBackend
from neurowave.errors import PlayerError


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


def test_mpv_error_reply_is_raised_and_recorded(fake_mpv, tmp_path) -> None:
    """Отказ mpv — это ошибка состояния, а не тихий no-op.

    На живом mpv 0.42 `af set` с неизвестным фильтром возвращает
    ``error running command``; раньше ответ никто не проверял, и Enhanced
    мог считаться включённым при пустой цепи.
    """
    backend = _backend(fake_mpv, tmp_path, "deny-af")
    try:
        with pytest.raises(PlayerError) as exc:
            backend.set_audio_filter("lavfi=[no_such_filter=1]")
        assert "af" in str(exc.value) and "error running command" in str(exc.value)
        assert backend.last_error == "error running command"
        assert backend.audio_filter() is None  # фильтр действительно не встал
    finally:
        backend.shutdown()


def test_loadfile_error_is_raised(fake_mpv, tmp_path) -> None:
    backend = _backend(fake_mpv, tmp_path, "deny-loadfile")
    try:
        with pytest.raises(PlayerError) as exc:
            backend.load("fake://broken")
        assert "loadfile" in str(exc.value)
        assert backend.last_error == "loading failed"
        assert backend.is_running is True  # сессия жива, можно продолжать
    finally:
        backend.shutdown()


def test_stale_end_file_does_not_advance_queue(fake_mpv, tmp_path) -> None:
    """end-file предыдущей загрузки после ручного next не должен двигать очередь."""
    received: list[str] = []
    backend = _backend(fake_mpv, tmp_path, "eof-stale", on_end=received.append)
    try:
        backend.start()
        backend.load("fake://first")
        assert backend._entry_id == 1
        time.sleep(0.5)  # событие с id 0 уже пришло, но относится к прошлому
        assert received == []
    finally:
        backend.shutdown()


def test_audio_filter_reports_actual_graph(fake_mpv, tmp_path) -> None:
    backend = _backend(fake_mpv, tmp_path, "idle")
    try:
        backend.start()
        assert backend.audio_filter() is None
        graph = "lavfi=[equalizer=f=1000:t=q:w=1:g=3]"
        backend.set_audio_filter(graph)
        # Настоящий mpv отдаёт граф без обёртки lavfi=[…], двойник повторяет это.
        assert backend.audio_filter() == "equalizer=f=1000:t=q:w=1:g=3"
    finally:
        backend.shutdown()


def test_snapshot_state_exposes_mpv_facts(fake_mpv, tmp_path) -> None:
    """Диагностика должна опираться на свойства mpv, а не на наши ожидания."""
    backend = _backend(fake_mpv, tmp_path, "idle")
    try:
        backend.start()
        backend.load("fake://track")
        snapshot = backend.snapshot_state()
        assert snapshot["running"] is True
        assert snapshot["entry_id"] == 1
        assert snapshot["af"] is None
        assert snapshot["volume"] is None  # двойник его не отдаёт — не выдумываем
    finally:
        backend.shutdown()


def test_backend_survives_failed_command(fake_mpv, tmp_path) -> None:
    backend = _backend(fake_mpv, tmp_path, "deny-af")
    backend.start()
    socket_path = backend._socket_path
    with pytest.raises(PlayerError):
        backend.set_audio_filter("lavfi=[bogus]")
    assert backend.position_ms() == 0  # следующая команда работает
    backend.shutdown()
    assert not socket_path.exists()


def test_stale_sockets_of_dead_processes_are_removed(tmp_path) -> None:
    """SIGKILL оставляет сокет: при следующем запуске его надо убрать."""
    from neurowave.audio.mpv import cleanup_stale_sockets

    dead = tmp_path / "neurowave-mpv-999999.sock"
    dead.write_text("")
    mine = tmp_path / f"neurowave-mpv-{__import__('os').getpid()}.sock"
    mine.write_text("")
    other = tmp_path / "unrelated.sock"
    other.write_text("")

    removed = cleanup_stale_sockets(tmp_path)

    assert removed == 1
    assert not dead.exists()
    assert mine.exists()  # свой сокет не трогаем
    assert other.exists()  # чужие файлы не наши
