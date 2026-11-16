"""Интеграционные тесты против настоящего mpv (без звуковой карты).

Двойник `tests/fake_mpv.py` проверяет протокол и потоки, но не может поймать
рассинхрон с реальным поведением mpv. Здесь mpv настоящий, а вывод звука
уведён в `--ao=null`: тест не зависит от PipeWire/PulseAudio, не шумит и
работает в CI.

Тесты пропускаются, если `mpv` не установлен: локально это обычная ситуация,
в CI mpv ставится отдельным шагом.
"""

from __future__ import annotations

import math
import shutil
import struct
import threading
import time
import wave
from pathlib import Path

import pytest

from neurowave.audio.mpv import MpvBackend
from neurowave.config import Settings
from neurowave.domain import Quality, StreamInfo, Track
from neurowave.player import PlayerService
from neurowave.queue import Queue
from neurowave.state import PlayerStatus

MPV = shutil.which("mpv")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(MPV is None, reason="mpv не установлен"),
]

RATE = 8000


def make_wav(path: Path, seconds: float, freq: int = 440) -> Path:
    """Короткий тон на стандартной библиотеке: ffmpeg в CI не нужен."""
    frames = b"".join(
        struct.pack("<h", int(3000 * math.sin(2 * math.pi * freq * i / RATE)))
        for i in range(int(RATE * seconds))
    )
    with wave.open(str(path), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(frames)
    return path


@pytest.fixture
def backend(tmp_path: Path) -> MpvBackend:
    bridge = MpvBackend(
        binary=str(MPV),
        socket_path=tmp_path / "mpv.sock",
        extra_args=["--ao=null", "--volume=20"],
    )
    yield bridge
    bridge.shutdown()


def wait_until(predicate, timeout: float, step: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


def test_real_mpv_plays_and_reports_position(backend: MpvBackend, tmp_path: Path) -> None:
    source = make_wav(tmp_path / "tone.wav", seconds=3)

    backend.load(str(source))

    assert wait_until(lambda: backend.position_ms() > 200, timeout=8.0)
    assert backend.is_playing() is True
    assert backend.is_running is True


def test_real_mpv_reports_end_of_file(backend: MpvBackend, tmp_path: Path) -> None:
    """Быстрый случай: файл короче таймаута команд."""
    received: list[str] = []
    done = threading.Event()
    backend.on_end = lambda reason: (received.append(reason), done.set())
    source = make_wav(tmp_path / "short.wav", seconds=0.4)

    backend.load(str(source))

    assert done.wait(15.0), "mpv доиграл файл, но событие завершения не пришло"
    assert received == ["eof"]


def test_real_mpv_delivers_end_after_long_track(tmp_path: Path) -> None:
    """Регрессия: тишина в сокете дольше таймаута команд не должна рвать IPC.

    Между загрузкой и естественным концом трека длиной 4.5 с команд нет —
    на прежнем коде читатель умирал через 3 с, и `eof` терялся.
    """
    received: list[str] = []
    done = threading.Event()
    bridge = MpvBackend(
        binary=str(MPV),
        socket_path=tmp_path / "long.sock",
        extra_args=["--ao=null", "--volume=20"],
        on_end=lambda reason: (received.append(reason), done.set()),
    )
    try:
        bridge.load(str(make_wav(tmp_path / "long.wav", seconds=4.5)))

        assert done.wait(20.0), "за 4.5 с тишины событие завершения потерялось"
        assert received == ["eof"]
        assert bridge.position_ms() >= 0  # IPC всё ещё отвечает
    finally:
        bridge.shutdown()


def test_real_mpv_pauses_and_resumes(backend: MpvBackend, tmp_path: Path) -> None:
    source = make_wav(tmp_path / "tone.wav", seconds=5)
    backend.load(str(source))
    assert wait_until(lambda: backend.position_ms() > 100, timeout=8.0)

    backend.pause()

    assert backend.is_playing() is False
    frozen = backend.position_ms()
    time.sleep(0.5)
    assert backend.position_ms() <= frozen + 200

    backend.play()
    assert wait_until(lambda: backend.is_playing() is True, timeout=5.0)


def test_real_mpv_shutdown_leaves_no_process(backend: MpvBackend, tmp_path: Path) -> None:
    source = make_wav(tmp_path / "tone.wav", seconds=2)
    backend.load(str(source))
    process = backend._process

    backend.shutdown()

    assert process is not None and process.poll() is not None
    assert not backend._socket_path.exists()


class LocalFilesAdapter:
    """Адаптер «локальные файлы» для сквозной проверки плеера."""

    def __init__(self, files: list[Path]):
        self._files = {path.name: path for path in files}

    def wave_tracks(self) -> list[Track]:
        return [
            Track(id=name, title=name, provider="local") for name in sorted(self._files)
        ]

    def stream_url(self, track_id: str) -> StreamInfo:
        return StreamInfo(url=str(self._files[track_id]), quality=Quality(codec="pcm"))

    def wave_feedback(self, kind, track_id=None, total_played_seconds=None) -> bool:
        return True


def test_player_advances_to_next_track_with_real_mpv(tmp_path: Path) -> None:
    """Сквозная регрессия: трек доиграл → следующий трек реально заиграл."""
    first = make_wav(tmp_path / "a-first.wav", seconds=0.4, freq=440)
    second = make_wav(tmp_path / "b-second.wav", seconds=3, freq=523)
    adapter = LocalFilesAdapter([first, second])
    bridge = MpvBackend(
        binary=str(MPV),
        socket_path=tmp_path / "player.sock",
        extra_args=["--ao=null", "--volume=20"],
    )
    played: list[str] = []
    advanced = threading.Event()

    def listener(event: str, payload: dict) -> None:
        if event != "track":
            return
        played.append(str(payload["track"].id))
        if len(played) >= 2:
            advanced.set()

    player = PlayerService(
        adapter=adapter,
        backend=bridge,
        queue=Queue(),
        settings=Settings(volume=20),
        listener=listener,
        prefetch=False,
    )
    try:
        player.play_tracks(adapter.wave_tracks())
        assert advanced.wait(15.0), f"автопереход не случился, играли: {played}"
        assert played == ["a-first.wav", "b-second.wav"]
        assert wait_until(lambda: bridge.position_ms() > 100, timeout=8.0)
        assert player.state.status is PlayerStatus.PLAYING
    finally:
        # stop() может бросить (mpv не ответил), но close() обязан выполниться:
        # иначе процесс mpv остаётся сиротой.
        try:
            player.stop()
        finally:
            player.close()
