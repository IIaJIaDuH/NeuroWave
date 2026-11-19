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


def test_player_skips_unresolvable_first_track_with_real_mpv(tmp_path: Path) -> None:
    """QA D-04 на настоящем mpv: трек без потока не блокирует очередь."""
    from neurowave.errors import TrackUnavailable

    good = make_wav(tmp_path / "b-good.wav", seconds=3, freq=523)
    bridge = MpvBackend(
        binary=str(MPV),
        socket_path=tmp_path / "recovery.sock",
        extra_args=["--ao=null", "--volume=20"],
    )

    class BrokenFirstAdapter(LocalFilesAdapter):
        def wave_tracks(self) -> list[Track]:
            return [Track(id="broken", title="broken", provider="local")] + super(
            ).wave_tracks()

        def stream_url(self, track_id: str) -> StreamInfo:
            if track_id == "broken":
                raise TrackUnavailable("поток недоступен")
            return super().stream_url(track_id)

    played: list[str] = []

    def listener(event: str, payload: dict) -> None:
        if event == "track":
            played.append(str(payload["track"].id))

    player = PlayerService(
        adapter=BrokenFirstAdapter([good]),
        backend=bridge,
        queue=Queue(),
        settings=Settings(volume=20),
        listener=listener,
        prefetch=False,
    )
    try:
        track = player.play_tracks([
            Track(id="broken", title="broken", provider="local"),
            Track(id="b-good.wav", title="good", provider="local"),
        ])

        assert track is not None and track.id == "b-good.wav"
        assert player.state.status is PlayerStatus.PLAYING
        assert wait_until(lambda: bridge.position_ms() > 200, timeout=8.0)
        assert played == ["b-good.wav"]
    finally:
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


def test_real_mpv_lists_and_switches_audio_device(tmp_path: Path) -> None:
    """Список устройств настоящего mpv и переключение без остановки звука.

    Тест требует живого звукового сервера (pactl + sink по умолчанию): в CI
    без PipeWire/PulseAudio он пропускается, потому что mpv с реальным AO
    там запустить нельзя, а `--ao=null` не показывает настоящие устройства.
    """
    from neurowave.audio.devices import probe_default_sink

    if probe_default_sink() is None:
        pytest.skip("нет звукового сервера: нечего переключать")

    backend = MpvBackend(extra_args=["--volume=6"])
    audio = make_wav(tmp_path / "tone-device.wav", 8.0)
    try:
        backend.load(str(audio))
        devices = backend.audio_devices()
        assert any(item.get("name") == "auto" for item in devices)
        candidates = [item["name"] for item in devices if item.get("name") != "auto"]
        if not candidates:
            pytest.skip("в окружении нет реальных аудиовыходов кроме auto")
        assert wait_until(lambda: backend.position_ms() > 200, timeout=8.0)
        target = candidates[0]
        backend.set_audio_device(target)
        assert backend.audio_device() == target
        before = backend.position_ms()
        assert wait_until(lambda: backend.position_ms() > before, timeout=8.0), (
            "переключение выхода не должно останавливать воспроизведение"
        )
    finally:
        backend.shutdown()


def test_real_mpv_applies_seek_right_after_load(backend: MpvBackend, tmp_path: Path) -> None:
    """QA D-01: seek сразу после loadfile не должен отклоняться mpv."""
    source = make_wav(tmp_path / "seek-load.wav", seconds=20)

    backend.load(str(source))
    backend.seek(6000)  # раньше здесь было «error running command»

    assert wait_until(lambda: backend.position_ms() >= 5000, timeout=10.0), (
        "перемотка после loadfile не применилась"
    )
    assert backend.position_ms() < 12_000, "позиция ушла дальше запрошенной"


def test_real_mpv_applies_last_seek_while_file_loads(backend: MpvBackend, tmp_path: Path) -> None:
    source = make_wav(tmp_path / "seek-last.wav", seconds=25)

    backend.load(str(source))
    for position in (2000, 9000, 16000):
        backend.seek(position)

    assert wait_until(lambda: backend.position_ms() >= 15000, timeout=10.0), (
        "применилась не последняя перемотка"
    )
    assert backend.position_ms() < 20_000


def test_real_mpv_seek_after_next_track(tmp_path: Path) -> None:
    """Сквозной сценарий дефекта: Next, сразу перемотка — и позиция нового трека."""
    first = make_wav(tmp_path / "a-first.wav", seconds=20, freq=440)
    second = make_wav(tmp_path / "b-second.wav", seconds=20, freq=523)
    adapter = LocalFilesAdapter([first, second])
    bridge = MpvBackend(
        binary=str(MPV),
        socket_path=tmp_path / "seek-next.sock",
        extra_args=["--ao=null", "--volume=20"],
    )
    player = PlayerService(
        adapter=adapter,
        backend=bridge,
        queue=Queue(),
        settings=Settings(volume=20),
        prefetch=False,
    )
    try:
        player.play_tracks(adapter.wave_tracks())
        player.next_track()
        player.seek(7000)

        assert wait_until(lambda: bridge.position_ms() >= 6000, timeout=10.0), (
            "перемотка после Next не применилась"
        )
        assert player.current_track is not None
        assert player.current_track.id == "b-second.wav"
        assert bridge.position_ms() < 14_000
    finally:
        try:
            player.stop()
        finally:
            player.close()


def test_real_mpv_seek_after_stop_is_ignored(backend: MpvBackend, tmp_path: Path) -> None:
    """После stop файла нет: seek — контролируемый no-op, а не ошибка IPC."""
    backend.load(str(make_wav(tmp_path / "seek-stop.wav", seconds=20)))
    assert wait_until(lambda: backend.position_ms() > 200, timeout=8.0)

    backend.stop()
    backend.seek(3000)

    assert backend.position_ms() < 1000


def test_real_mpv_single_end_file_after_seek(tmp_path: Path) -> None:
    """Перемотка не порождает двойных end-file."""
    received: list[str] = []
    done = threading.Event()
    bridge = MpvBackend(
        binary=str(MPV),
        socket_path=tmp_path / "seek-end.sock",
        extra_args=["--ao=null", "--volume=20"],
        on_end=lambda reason: (received.append(reason), done.set()),
    )
    try:
        bridge.load(str(make_wav(tmp_path / "seek-end.wav", seconds=1.5)))
        bridge.seek(1000)

        assert done.wait(15.0), "трек не доиграл после перемотки"
        time.sleep(0.6)
        assert received == ["eof"], f"лишние события завершения: {received}"
    finally:
        bridge.shutdown()


def test_real_mpv_accepts_custom_profile_graph(tmp_path: Path) -> None:
    """Пользовательский профиль (4 полосы + лимитер) должен собрать живой lavfi."""
    from neurowave.audio.dsp_controller import DspController
    from neurowave.audio.dsp_profiles import AudioProfile, EqBand, Limiter
    from neurowave.config import Settings

    profile = AudioProfile(
        id="custom-live",
        name="Проверка",
        preamp_db=-5.0,
        bands=(
            EqBand("lowshelf", 90.0, -2.0, 0.7),
            EqBand("peaking", 450.0, 1.5, 1.4),
            EqBand("peaking", 3200.0, -1.0, 2.0),
            EqBand("highshelf", 10500.0, -2.5, 0.7),
        ),
        limiter=Limiter(enabled=True, ceiling_dbfs=-3.5, lookahead_ms=6.0, release_ms=70.0),
    )
    backend = MpvBackend(extra_args=["--ao=null", "--volume=100"])
    controller = DspController(backend, settings=Settings(dsp_profile="flat"))
    audio = make_wav(tmp_path / "tone-custom.wav", 2.5, freq=450, level=26000)
    try:
        backend.load(str(audio))
        status = controller.apply(profile)
        assert status.confirmed is True, status.reason
        assert "lowshelf=f=90" in (backend.audio_filter() or "")
        assert wait_until(lambda: backend.position_ms() > 300, timeout=5.0)
    finally:
        backend.shutdown()
