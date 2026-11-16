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
import subprocess
import threading
import time
import wave
from pathlib import Path

import pytest

from neurowave.audio.dsp_profiles import BUILTIN_PROFILES
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


def make_wav(path: Path, seconds: float, freq: int = 440, level: int = 3000) -> Path:
    """Короткий тон на стандартной библиотеке: ffmpeg в CI не нужен.

    ``level`` — амплитуда в отсчётах (3000 ≈ −20.8 dBFS, 31000 ≈ −0.5 dBFS):
    для проверки лимитера нужен сигнал, который заведомо упрётся в потолок.
    """
    frames = b"".join(
        struct.pack("<h", int(level * math.sin(2 * math.pi * freq * i / RATE)))
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


@pytest.mark.parametrize("profile_id", sorted(BUILTIN_PROFILES))
def test_real_mpv_accepts_every_builtin_dsp_graph(backend: MpvBackend, tmp_path: Path, profile_id: str) -> None:
    """Граф профиля должен собираться настоящим libavfilter, а не только нами.

    Это ловит опечатки в параметрах (`t=q`, `level=0`, `dB`) и фильтры,
    которых нет в сборке mpv, — двойник такое не проверяет.
    """
    from neurowave.audio.dsp_graph import build_filter_graph

    profile = BUILTIN_PROFILES[profile_id]
    graph = build_filter_graph(profile)
    audio = make_wav(tmp_path / f"tone-{profile_id}.wav", 0.6)
    try:
        backend.start()
        backend.load(str(audio))
        backend.set_volume(100)
        if graph is not None:
            backend.set_audio_filter(graph)  # PlayerError, если mpv отверг
            applied = backend.audio_filter()
            assert applied is not None
            assert "alimiter" in applied if profile.limiter.enabled else True
        else:
            assert backend.audio_filter() is None
    finally:
        backend.shutdown()


def test_real_mpv_plays_through_enhanced_graph(tmp_path: Path) -> None:
    """Не только «фильтр принят», но и звук идёт и позиция растёт."""
    from neurowave.audio.dsp_graph import build_filter_graph

    audio = make_wav(tmp_path / "tone-graph.wav", 1.2)
    backend = MpvBackend(extra_args=["--ao=null", "--volume=100"])
    try:
        backend.load(str(audio))
        backend.set_audio_filter(build_filter_graph(BUILTIN_PROFILES["bluetooth"]))
        backend.play()
        assert wait_until(lambda: backend.position_ms() > 300, timeout=5.0)
        assert backend.is_playing() is True
    finally:
        backend.shutdown()


def _pcm(path: Path, graph: str | None, volume: int, out: Path) -> list[float]:
    """Выгрузить PCM через mpv и вернуть сэмплы в [-1, 1]."""
    args = [
        "mpv", "--no-video", "--no-terminal", "--really-quiet",
        "--ao=pcm", f"--ao-pcm-file={out}", "--audio-format=s16",
        f"--audio-samplerate={RATE}", "--audio-channels=mono", f"--volume={volume}",
    ]
    if graph:
        args.append(f"--af={graph}")
    args.append(str(path))
    subprocess.run(args, check=True, capture_output=True)
    with wave.open(str(out)) as handle:
        data = handle.readframes(handle.getnframes())
    return [sample / 32768.0 for sample in struct.unpack(f"<{len(data) // 2}h", data)]


def test_mpv_volume_is_applied_after_filters(tmp_path: Path) -> None:
    """Громкость mpv стоит ПОСЛЕ `af`: лимитер не защищает цифровой boost.

    Проверка фиксирует реальный порядок обработки, а не ожидание: с потолком
    −3 dBFS сигнал на выходе лимитера равен −3 dBFS, а volume 130 % поднимает
    его уже после фильтра. Кривая громкости mpv кубическая, поэтому 130 %
    это не +2.3 дБ, а +6.8 дБ — лимитер такого усиления не видит.
    Именно поэтому `Settings.MAX_VOLUME` не применяется выше 100 %.
    """
    audio = make_wav(tmp_path / "tone-order.wav", 0.6, freq=997)
    ceiling = 10 ** (-3 / 20)
    graph = f"lavfi=[alimiter=limit={ceiling:.4f}:attack=5:release=50:level=0]"

    quiet = _pcm(audio, graph, 100, tmp_path / "q.wav")
    loud = _pcm(audio, graph, 130, tmp_path / "l.wav")
    peak_quiet = max(abs(sample) for sample in quiet)
    peak_loud = max(abs(sample) for sample in loud)
    gain = 20 * math.log10(peak_loud / peak_quiet)

    expected = 60 * math.log10(1.3)  # кубическая кривая mpv: 1.3³
    assert gain == pytest.approx(expected, abs=0.15), "volume обязан применяться после af"
    assert sum(1 for sample in loud if abs(sample) >= 0.999) >= 0, "шум не должен ломать тест"


def test_safe_boost_is_gain_inside_graph_before_limiter(tmp_path: Path) -> None:
    """Безопасный путь для будущего boost: усиление в графе, лимитер последним."""
    audio = make_wav(tmp_path / "tone-boost.wav", 0.6, freq=997, level=31000)
    ceiling = 10 ** (-3 / 20)
    graph = (
        "lavfi=[volume=volume=9dB:precision=float,"
        f"alimiter=limit={ceiling:.4f}:attack=5:release=50:level=0]"
    )

    samples = _pcm(audio, graph, 100, tmp_path / "boost.wav")
    peak = max(abs(sample) for sample in samples)
    assert sum(1 for sample in samples if abs(sample) >= 0.999) == 0
    assert 20 * math.log10(peak) == pytest.approx(-3.0, abs=0.35)
