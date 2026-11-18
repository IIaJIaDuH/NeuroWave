"""Перемотка сразу после смены трека (QA D-01, PR #32).

Настоящий mpv отклоняет ``seek``, пока файл не загружен («error running
command»). Двойник mpv (``tests/fake_mpv.py``) воспроизводит это планом
``slow-load``: до ``file-loaded`` он отвечает на ``seek`` ошибкой. Поэтому
тесты ниже падают на коде, который отправляет перемотку немедленно.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from neurowave.audio.mpv import MpvBackend
from neurowave.config import Settings
from neurowave.domain import Quality, StreamInfo
from neurowave.errors import PlayerError
from neurowave.player import PlayerService
from tests.conftest import make_track


def make_backend(fake_mpv: Path, tmp_path: Path, plan: str, log: Path | None = None) -> MpvBackend:
    extra = [f"--fake-plan={plan}"]
    if log is not None:
        extra.append(f"--fake-log={log}")
    # Короткое имя сокета: AF_UNIX ограничен ~108 байтами на весь путь.
    socket_name = f"seek-{time.monotonic_ns() % 1_000_000_000}.sock"
    return MpvBackend(
        binary=str(fake_mpv),
        socket_path=tmp_path / socket_name,
        extra_args=extra,
    )


def wait_until(predicate, timeout: float = 3.0, step: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


def read_log(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line]


def seeks_in(log: Path) -> list[str]:
    return [line for line in read_log(log) if line.startswith("seek ")]


class _Adapter:
    """Локальные «файлы» без сети: id трека превращается в fake:// URL."""

    def __init__(self, ids: tuple[str, ...] = ("1", "2", "3")):
        self._ids = list(ids)

    def wave_tracks(self):
        return [make_track(int(track_id)) for track_id in self._ids]

    def stream_url(self, track_id: str) -> StreamInfo:
        return StreamInfo(url=f"fake://{track_id}", quality=Quality(codec="wav"))

    def wave_feedback(self, kind, track_id=None, total_played_seconds=None) -> bool:
        return True


def make_player(
    fake_mpv: Path, tmp_path: Path, plan: str, log: Path | None = None
) -> tuple[PlayerService, MpvBackend]:
    backend = make_backend(fake_mpv, tmp_path, plan, log)
    player = PlayerService(
        adapter=_Adapter(),
        backend=backend,
        settings=Settings(volume=20),
        prefetch=False,
    )
    return player, backend


# --- бэкенд ---------------------------------------------------------------


def test_seek_right_after_loadfile_is_deferred_until_file_loaded(fake_mpv, tmp_path) -> None:
    """Раньше здесь был PlayerError: «mpv отклонил seek»."""
    log = tmp_path / "mpv.log"
    backend = make_backend(fake_mpv, tmp_path, "slow-load", log)
    try:
        backend.load("fake://first")

        backend.seek(5000)  # файл ещё грузится, но ошибки быть не должно

        assert wait_until(lambda: backend.position_ms() == 5000), "перемотка не применилась"
        assert seeks_in(log) == ["seek 5.0"], "seek ушёл в mpv до готовности файла"
    finally:
        backend.shutdown()


def test_several_seeks_during_load_apply_only_the_last(fake_mpv, tmp_path) -> None:
    log = tmp_path / "mpv.log"
    backend = make_backend(fake_mpv, tmp_path, "slow-load", log)
    try:
        backend.load("fake://first")
        for position in (1000, 2000, 3000):
            backend.seek(position)

        assert wait_until(lambda: backend.position_ms() == 3000)
        assert seeks_in(log) == ["seek 3.0"], "должен примениться только последний запрос"
    finally:
        backend.shutdown()


def test_seek_on_ready_file_is_sent_immediately(fake_mpv, tmp_path) -> None:
    log = tmp_path / "mpv.log"
    backend = make_backend(fake_mpv, tmp_path, "idle", log)
    try:
        backend.load("fake://first")
        assert wait_until(lambda: not backend._loading)

        backend.seek(4000)

        assert backend.position_ms() == 4000
        assert seeks_in(log) == ["seek 4.0"]
    finally:
        backend.shutdown()


def test_pending_seek_is_not_carried_to_the_next_track(fake_mpv, tmp_path) -> None:
    """Next до готовности прошлого файла: перемотка не должна достаться новому."""
    log = tmp_path / "mpv.log"
    backend = make_backend(fake_mpv, tmp_path, "slow-load", log)
    try:
        backend.load("fake://first")
        backend.seek(6000)

        backend.load("fake://second")
        assert wait_until(lambda: not backend._loading)
        time.sleep(0.3)  # прошлый файл успел бы «догрузиться»

        assert backend.position_ms() == 0
        assert seeks_in(log) == [], "отложенный seek прошлого трека ушёл в новый"
    finally:
        backend.shutdown()


def test_stop_cancels_pending_seek(fake_mpv, tmp_path) -> None:
    log = tmp_path / "mpv.log"
    backend = make_backend(fake_mpv, tmp_path, "slow-load", log)
    try:
        backend.load("fake://first")
        backend.seek(7000)

        backend.stop()
        time.sleep(0.5)

        assert seeks_in(log) == []
        assert backend.position_ms() == 0
    finally:
        backend.shutdown()


def test_failed_load_clears_pending_seek(fake_mpv, tmp_path) -> None:
    """end-file/error до готовности: ожидание завершается, а не висит."""
    log = tmp_path / "mpv.log"
    backend = make_backend(fake_mpv, tmp_path, "load-error", log)
    try:
        backend.load("fake://broken")
        backend.seek(5000)

        assert wait_until(lambda: not backend._loading)
        time.sleep(0.3)
        assert seeks_in(log) == []
        assert backend.position_ms() == 0

        # Файла нет: следующий seek не висит и не поднимает старую ошибку.
        backend.seek(1000)
        assert backend.position_ms() == 0
    finally:
        backend.shutdown()


def test_seek_after_stop_is_ignored(fake_mpv, tmp_path) -> None:
    """После stop перематывать нечего: это no-op, а не ошибка IPC (D-01)."""
    log = tmp_path / "mpv.log"
    backend = make_backend(fake_mpv, tmp_path, "idle", log)
    try:
        backend.load("fake://first")
        assert wait_until(lambda: not backend._loading)

        backend.stop()
        backend.seek(2000)

        assert backend.position_ms() == 0
        assert seeks_in(log) == []
    finally:
        backend.shutdown()


def test_seek_after_pause_and_resume_is_immediate(fake_mpv, tmp_path) -> None:
    log = tmp_path / "mpv.log"
    backend = make_backend(fake_mpv, tmp_path, "idle", log)
    try:
        backend.load("fake://first")
        assert wait_until(lambda: not backend._loading)

        backend.pause()
        backend.seek(7000)
        assert backend.position_ms() == 7000

        backend.play()
        backend.seek(8000)
        assert backend.position_ms() == 8000
    finally:
        backend.shutdown()


def test_pending_seek_does_not_wait_forever_without_file_loaded(
    fake_mpv, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("neurowave.audio.mpv.SEEK_READY_TIMEOUT", 0.25)
    log = tmp_path / "mpv.log"
    backend = make_backend(fake_mpv, tmp_path, "no-file-loaded", log)
    try:
        backend.load("fake://stuck")
        backend.seek(9000)

        assert wait_until(lambda: backend._pending_seek is None, timeout=2.0), (
            "ожидание готовности файла не завершилось"
        )
        assert seeks_in(log) == ["seek 9.0"], "попытка должна быть ровно одна"
    finally:
        backend.shutdown()


# --- PlayerService --------------------------------------------------------


def test_player_seek_right_after_play_is_applied(fake_mpv, tmp_path) -> None:
    player, backend = make_player(fake_mpv, tmp_path, "slow-load")
    try:
        player.play_tracks(_Adapter().wave_tracks())

        player.seek(3000)

        assert wait_until(lambda: backend.position_ms() == 3000)
        assert player.current_track is not None and player.current_track.id == "1"
    finally:
        player.close()


def test_player_seek_right_after_next_applies_to_new_track(fake_mpv, tmp_path) -> None:
    log = tmp_path / "mpv.log"
    player, backend = make_player(fake_mpv, tmp_path, "slow-load", log)
    try:
        player.play_tracks(_Adapter().wave_tracks())

        player.next_track()
        player.seek(4000)

        assert wait_until(lambda: backend.position_ms() == 4000)
        assert player.current_track is not None and player.current_track.id == "2"
        assert [line for line in read_log(log) if line.startswith("loadfile")][-1] == (
            "loadfile fake://2"
        )
        assert seeks_in(log) == ["seek 4.0"]
    finally:
        player.close()


def test_player_seek_right_after_previous_applies_to_previous_track(fake_mpv, tmp_path) -> None:
    log = tmp_path / "mpv.log"
    player, backend = make_player(fake_mpv, tmp_path, "slow-load", log)
    try:
        player.play_tracks(_Adapter().wave_tracks())
        player.next_track()
        assert wait_until(lambda: not backend._loading)

        player.previous_track()
        player.seek(6000)

        assert wait_until(lambda: backend.position_ms() == 6000)
        assert player.current_track is not None and player.current_track.id == "1"
    finally:
        player.close()


def test_player_rapid_next_twice_then_seek_lands_on_latest_track(fake_mpv, tmp_path) -> None:
    log = tmp_path / "mpv.log"
    player, backend = make_player(fake_mpv, tmp_path, "slow-load", log)
    try:
        player.play_tracks(_Adapter().wave_tracks())

        player.next_track()
        player.next_track()
        player.seek(5000)

        assert wait_until(lambda: backend.position_ms() == 5000)
        assert player.current_track is not None and player.current_track.id == "3"
        assert seeks_in(log) == ["seek 5.0"]
    finally:
        player.close()


def test_player_seek_does_not_block_while_file_loads(fake_mpv, tmp_path) -> None:
    player, _backend = make_player(fake_mpv, tmp_path, "slow-load")
    try:
        player.play_tracks(_Adapter().wave_tracks())

        started = time.monotonic()
        for position in (1000, 2000, 3000):
            player.seek(position)
        elapsed = time.monotonic() - started

        assert elapsed < 0.2, f"seek заблокировал вызывающий поток на {elapsed:.2f} с"
    finally:
        player.close()


def test_player_seek_after_stop_is_ignored(fake_mpv, tmp_path) -> None:
    player, backend = make_player(fake_mpv, tmp_path, "idle")
    try:
        player.play_tracks(_Adapter().wave_tracks())
        assert wait_until(lambda: not backend._loading)

        player.stop()
        player.seek(2000)  # раньше: PlayerError

        assert backend.position_ms() == 0
    finally:
        player.close()


def test_player_seek_after_failed_load_does_not_hang(fake_mpv, tmp_path) -> None:
    player, backend = make_player(fake_mpv, tmp_path, "deny-loadfile")
    try:
        with pytest.raises(PlayerError):
            player.play_tracks(_Adapter().wave_tracks())

        player.seek(1000)  # файла нет: контролируемый no-op

        assert backend.position_ms() == 0
    finally:
        player.close()
