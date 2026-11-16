"""MPRIS-логика без D-Bus: отображение состояния и диспетчеризация команд."""

from __future__ import annotations

import pytest

from neurowave.audio import NullBackend
from neurowave.config import Settings
from neurowave.domain import Quality, StreamInfo, Track
from neurowave.integrations.mpris import (
    BUS_NAME,
    MAX_VOLUME_FRACTION,
    MprisController,
    MprisSnapshot,
    PlayerTarget,
    metadata,
    playback_status,
    properties,
    track_object_path,
    volume_fraction,
    volume_percent,
)
from neurowave.player import PlayerService
from neurowave.state import PlayerStatus

TRACK = Track(
    id="12345",
    title="Проверочный трек",
    artists=("Исполнитель",),
    album="Альбом",
    duration_ms=180_500,
)


def snapshot(**overrides) -> MprisSnapshot:
    defaults = dict(
        status=PlayerStatus.PLAYING,
        track=TRACK,
        quality=Quality("aac", 320, False),
        volume_percent=80,
        position_us=42_000_000,
    )
    defaults.update(overrides)
    return MprisSnapshot(**defaults)


class RecordingTarget:
    """Заглушка MprisTarget, запоминающая вызовы."""

    def __init__(self, state: MprisSnapshot | None = None):
        self.state = state or snapshot()
        self.calls: list[tuple[str, object]] = []

    def snapshot(self) -> MprisSnapshot:
        return self.state

    def play(self) -> None:
        self.calls.append(("play", None))
        self.state = snapshot(status=PlayerStatus.PLAYING, track=self.state.track)

    def pause(self) -> None:
        self.calls.append(("pause", None))
        self.state = snapshot(status=PlayerStatus.PAUSED, track=self.state.track)

    def play_pause(self) -> None:
        self.calls.append(("play_pause", None))

    def next(self) -> None:
        self.calls.append(("next", None))

    def previous(self) -> None:
        self.calls.append(("previous", None))

    def stop(self) -> None:
        self.calls.append(("stop", None))
        self.state = snapshot(status=PlayerStatus.STOPPED, track=self.state.track)

    def seek_us(self, offset_us: int) -> None:
        self.calls.append(("seek_us", offset_us))

    def set_position_us(self, position_us: int) -> None:
        self.calls.append(("set_position_us", position_us))

    def set_volume_fraction(self, fraction: float) -> None:
        self.calls.append(("set_volume_fraction", fraction))


# --- отображение -------------------------------------------------------
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (PlayerStatus.PLAYING, "Playing"),
        (PlayerStatus.PAUSED, "Paused"),
        (PlayerStatus.BUFFERING, "Stopped"),
        (PlayerStatus.STOPPED, "Stopped"),
        (PlayerStatus.IDLE, "Stopped"),
    ],
)
def test_playback_status(status: PlayerStatus, expected: str) -> None:
    assert playback_status(status) == expected


@pytest.mark.parametrize("track_id", ["12345", None, "", "abc/def", "..", "трек"])
def test_track_path_is_valid_object_path(track_id: str | None) -> None:
    path = track_object_path(track_id)
    assert path.startswith("/org/neurowave/Track/")
    assert path.count("/") >= 3
    for segment in path.strip("/").split("/"):
        assert segment, f"пустой сегмент в {path}"
        assert all(char.isalnum() or char == "_" for char in segment), path


def test_metadata_reports_only_real_fields() -> None:
    data = metadata(snapshot())
    assert data["mpris:trackid"] == "/org/neurowave/Track/t12345"
    assert data["xesam:title"] == "Проверочный трек"
    assert data["xesam:artist"] == ["Исполнитель"]
    assert data["xesam:album"] == "Альбом"
    assert data["mpris:length"] == 180_500_000
    assert data["neurowave:quality"] == "AAC 320 kbps"
    assert data["neurowave:lossless"] is False


def test_metadata_without_optional_fields() -> None:
    bare = Track(id="1", title="Без метаданных")
    data = metadata(snapshot(track=bare, quality=Quality()))
    assert "mpris:length" not in data
    assert "xesam:album" not in data
    assert "neurowave:quality" not in data
    assert data["xesam:artist"] == ["Неизвестный исполнитель"]


def test_metadata_empty_without_track() -> None:
    assert metadata(snapshot(track=None, quality=None)) == {}


@pytest.mark.parametrize(
    ("percent", "fraction"),
    [(0, 0.0), (80, 0.8), (100, 1.0), (130, MAX_VOLUME_FRACTION), (999, MAX_VOLUME_FRACTION), (-5, 0.0)],
)
def test_volume_fraction_clamps(percent: int, fraction: float) -> None:
    assert volume_fraction(percent) == pytest.approx(fraction)


@pytest.mark.parametrize("fraction", [0.0, 0.5, 1.0, 1.3, 4.0, -1.0])
def test_volume_round_trip(fraction: float) -> None:
    percent = volume_percent(fraction)
    assert volume_fraction(percent) == pytest.approx(min(MAX_VOLUME_FRACTION, max(0.0, fraction)), abs=0.01)


def test_properties_reflect_state() -> None:
    playing = properties(snapshot())
    assert playing["PlaybackStatus"] == "Playing"
    assert playing["CanPause"] is True
    assert playing["CanSeek"] is True
    assert playing["CanControl"] is True
    assert playing["Volume"] == pytest.approx(0.8)

    paused = properties(snapshot(status=PlayerStatus.PAUSED))
    assert paused["PlaybackStatus"] == "Paused"
    assert paused["CanPause"] is False
    assert paused["CanSeek"] is True

    stopped = properties(snapshot(status=PlayerStatus.STOPPED))
    assert stopped["CanPlay"] is False
    assert stopped["CanSeek"] is False

    empty = properties(snapshot(track=None, status=PlayerStatus.IDLE))
    assert empty["CanGoNext"] is False
    assert empty["CanGoPrevious"] is False


def test_bus_name_matches_mpris_convention() -> None:
    assert BUS_NAME == "org.mpris.MediaPlayer2.neurowave"


# --- контроллер --------------------------------------------------------
def test_controller_dispatches_commands() -> None:
    target = RecordingTarget()
    controller = MprisController(target, ttl=0)
    controller.play()
    controller.pause()
    controller.play_pause()
    controller.next()
    controller.previous()
    controller.stop()
    assert [name for name, _ in target.calls] == [
        "play",
        "pause",
        "play_pause",
        "next",
        "previous",
        "stop",
    ]


def test_controller_seek_ignored_when_not_active() -> None:
    target = RecordingTarget(snapshot(status=PlayerStatus.STOPPED))
    controller = MprisController(target, ttl=0)
    assert controller.seek(5_000_000) is None
    assert target.calls == []


def test_controller_seek_relative_when_playing() -> None:
    target = RecordingTarget(snapshot(status=PlayerStatus.PLAYING))
    controller = MprisController(target, ttl=0)
    controller.seek(3_000_000)
    assert ("seek_us", 3_000_000) in target.calls


def test_controller_set_position_checks_track_id() -> None:
    target = RecordingTarget()
    controller = MprisController(target, ttl=0)
    controller.set_position("/org/neurowave/Track/t999", 1_000_000)
    controller.set_position("/org/neurowave/Track/t12345", -10)
    assert target.calls == []
    controller.set_position("/org/neurowave/Track/t12345", 12_000_000)
    assert ("set_position_us", 12_000_000) in target.calls


def test_controller_volume_and_root_properties() -> None:
    target = RecordingTarget()
    controller = MprisController(target)
    controller.set_volume(0.8)
    assert ("set_volume_fraction", 0.8) in target.calls
    root = controller.root_properties()
    assert root["Identity"] == "NeuroWave"
    assert root["DesktopEntry"] == "neurowave"
    assert root["CanRaise"] is False
    assert controller.can_quit is False


def test_controller_quit_requires_callback() -> None:
    quit_calls: list[bool] = []
    controller = MprisController(RecordingTarget(), quit_callback=lambda: quit_calls.append(True))
    assert controller.can_quit is True
    controller.quit()
    controller2 = MprisController(RecordingTarget())
    controller2.quit()
    assert quit_calls == [True]


def test_controller_snapshot_is_cached() -> None:
    calls: list[int] = []
    target = RecordingTarget()
    original = target.snapshot
    target.snapshot = lambda: (calls.append(1), original())[1]  # type: ignore[method-assign]
    controller = MprisController(target, ttl=60)
    controller.snapshot(force=True)
    controller.snapshot()
    controller.snapshot()
    controller.snapshot(force=True)
    assert len(calls) == 2


# --- адаптер к PlayerService ------------------------------------------
class _Adapter:
    def __init__(self, tracks: list[Track]):
        self.tracks = tracks

    def wave_tracks(self) -> list[Track]:
        return list(self.tracks)

    def wave_feedback(self, kind, track_id=None, total_played_seconds=None) -> bool:
        return True

    def stream_url(self, track_id: str) -> StreamInfo:
        return StreamInfo(f"https://audio.test/{track_id}.mp3", Quality("mp3", 320))


def make_player() -> PlayerService:
    player = PlayerService(
        adapter=_Adapter([TRACK]),
        backend=NullBackend(),
        settings=Settings(volume=60),
        prefetch=False,
    )
    player.start_wave()
    return player


def test_player_target_snapshot_and_volume() -> None:
    player = make_player()
    try:
        target = PlayerTarget(player)
        snap = target.snapshot()
        assert snap.track is not None and snap.track.id == TRACK.id
        assert snap.status is PlayerStatus.PLAYING
        assert snap.volume_percent == 60
        assert snap.quality is not None and snap.quality.bitrate_kbps == 320

        target.set_volume_fraction(0.9)
        assert player.volume == 90
        target.set_volume_fraction(5.0)
        assert player.volume == 130  # кламп ядра
    finally:
        player.close()


def test_player_target_play_pause_round_trip() -> None:
    player = make_player()
    try:
        target = PlayerTarget(player)
        target.pause()
        assert player.state.status is PlayerStatus.PAUSED
        target.play()
        assert player.state.status is PlayerStatus.PLAYING
        target.play()  # уже играет — без ошибок
        assert player.state.status is PlayerStatus.PLAYING
    finally:
        player.close()


def test_player_target_seek_relative() -> None:
    player = make_player()
    try:
        backend = player.backend
        target = PlayerTarget(player)
        target.seek_us(5_000_000)
        seeks = [value for name, value in backend.calls if name == "seek"]
        assert len(seeks) == 1
        assert 4_990 <= int(seeks[0]) <= 5_100
    finally:
        player.close()
