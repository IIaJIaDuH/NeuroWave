"""USB-05: транскод, M3U8 и Car-профиль на синтетическом аудио.

Реальный ffmpeg в CI не требуется: команда и разбор проверяются через
внедряемый runner, а writer — через fake-транскодер. Отдельный тест на
настоящий ffmpeg пропускается, если его нет в окружении.
"""

from __future__ import annotations

import math
import shutil
import struct
import subprocess
import wave
from pathlib import Path

import pytest

from neurowave.mixtape import (
    DeviceIdentity,
    ExportAsset,
    MixtapePlan,
    MixtapeRequest,
    PlannedItem,
    RightsStatus,
)
from neurowave.mixtape.car import (
    CAR_FOLDER,
    car_destination,
    car_filename,
    transliterate,
)
from neurowave.mixtape.devices import FakeUsbDeviceService
from neurowave.mixtape.export import (
    ExportStatus,
    ItemStatus,
    SafeWriter,
)
from neurowave.mixtape.export.playlist import PlaylistEntry, render_m3u8
from neurowave.mixtape.export.transcoder import (
    FfmpegTranscoder,
    MediaInfo,
    TranscoderError,
    TranscoderUnavailable,
)
from neurowave.mixtape.models import ExportOperation

SUB = "MUSIC/NeuroWave"
TRACK = "local:1"


def synth_wav(path: Path, *, duration_ms: int = 500, rate: int = 8_000, freq: int = 440) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = max(1, int(rate * duration_ms / 1000))
    data = b"".join(
        struct.pack("<h", int(0.2 * 32767 * math.sin(2 * math.pi * freq * index / rate)))
        for index in range(frames)
    )
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(data)
    return path


class FakeTranscoder:
    available = True

    def __init__(
        self,
        *,
        duration_ms: int = 180_000,
        media_format: str = "mp3",
        payload: bytes = b"ID3-fake-mp3",
        fail: Exception | None = None,
        probe_error: Exception | None = None,
    ) -> None:
        self.duration_ms = duration_ms
        self.media_format = media_format
        self.payload = payload
        self.fail = fail
        self.probe_error = probe_error
        self.calls: list[dict] = []

    def transcode(self, source, destination, *, target_format="mp3", bitrate_kbps=320):
        self.calls.append(
            {"source": Path(source), "destination": Path(destination), "format": target_format}
        )
        if self.fail is not None:
            raise self.fail
        Path(destination).write_bytes(self.payload)

    def probe(self, path):
        if self.probe_error is not None:
            raise self.probe_error
        return MediaInfo(self.media_format, self.duration_ms)


def make_item(number: int = 1, *, operation: ExportOperation = ExportOperation.TRANSCODE) -> PlannedItem:
    return PlannedItem(
        source_asset_id=f"asset-{number}",
        source_id="local",
        track_key=f"local:{number}",
        title=f"Song {number}",
        artists=("Artist",),
        destination_relative=f"{SUB}/{number:03d} - Artist - Song {number}.mp3",
        operation=operation,
        estimated_bytes=0,
        duration_ms=180_000,
        score=0.0,
        reason="лайк",
    )


def make_asset(path: Path, number: int = 1, *, size_bytes: int | None = None) -> ExportAsset:
    return ExportAsset(
        asset_id=f"asset-{number}",
        track_key=f"local:{number}",
        source_id="local",
        media_format="flac",
        size_bytes=size_bytes if size_bytes is not None else path.stat().st_size,
        rights=RightsStatus.PERMITTED,
        duration_ms=180_000,
        lossless=True,
        local_path=path,
        eligible=True,
    )


def make_plan(items, *, subdir: str = SUB, plan_id: str = "mixtape-test") -> MixtapePlan:
    return MixtapePlan(
        plan_id=plan_id,
        request=MixtapeRequest(
            target_duration_ms=3_600_000,
            destination_subdir=subdir,
            output_format="mp3",
        ),
        items=tuple(items),
        candidate_count=len(items),
        eligible_count=len(items),
        skipped=(),
        estimated_bytes=0,
        estimated_duration_ms=0,
        shortage_ms=0,
        revision=1,
    )


def identity_for(root: Path) -> DeviceIdentity:
    return DeviceIdentity(
        udisks_object_path="/org/freedesktop/UDisks2/block_devices/sdb1",
        block_device="/dev/sdb1",
        mountpoint=root.resolve(),
        filesystem_type="vfat",
        filesystem_uuid="1234-ABCD",
        drive_id="USB_Stick",
        removable=True,
        writable=True,
        capacity_bytes=16_000_000_000,
        free_bytes=8_000_000_000,
    )


@pytest.fixture
def usb(tmp_path):
    root = tmp_path / "usb"
    root.mkdir()
    return root


@pytest.fixture
def source(tmp_path):
    return synth_wav(tmp_path / "library" / "tone.wav")


def writer_for(usb: Path, transcoder) -> SafeWriter:
    device = identity_for(usb)
    return SafeWriter(
        usb,
        device_service=FakeUsbDeviceService([device]),
        expected_device=device,
        transcoder=transcoder,
    )


# --- writer + транскодер ------------------------------------------------


def test_transcode_commits_mp3_and_playlist(usb, source):
    transcoder = FakeTranscoder()
    writer = writer_for(usb, transcoder)

    report = writer.write(make_plan([make_item()]), [make_asset(source)])

    destination = usb / SUB / "001 - Artist - Song 1.mp3"
    assert report.status is ExportStatus.COMPLETE
    assert report.items[0].status is ItemStatus.VERIFIED
    assert destination.read_bytes() == b"ID3-fake-mp3"
    assert report.playlist_path == f"{SUB}/playlist.m3u8"
    playlist = (usb / SUB / "playlist.m3u8").read_text(encoding="utf-8")
    assert "#EXTM3U" in playlist
    assert "#EXTINF:180,Artist - Song 1" in playlist
    assert "001 - Artist - Song 1.mp3" in playlist
    assert transcoder.calls[0]["format"] == "mp3"


def test_transcode_is_unavailable_without_ffmpeg(usb, source):
    writer = writer_for(usb, None)

    report = writer.write(make_plan([make_item()]), [make_asset(source)])

    assert report.items[0].error == "transcode_unavailable"
    assert report.verified_count == 0
    assert not (usb / SUB / "001 - Artist - Song 1.mp3").exists()
    assert report.playlist_path == ""


def test_transcoder_failure_leaves_no_file(usb, source):
    writer = writer_for(usb, FakeTranscoder(fail=TranscoderError("boom")))

    report = writer.write(make_plan([make_item()]), [make_asset(source)])

    assert report.items[0].error == "transcode_failed"
    assert not (usb / SUB / "001 - Artist - Song 1.mp3").exists()
    assert not list((usb / SUB).glob(".neurowave-part-*"))


def test_probe_duration_mismatch_is_rejected(usb, source):
    writer = writer_for(usb, FakeTranscoder(duration_ms=10_000))

    report = writer.write(make_plan([make_item()]), [make_asset(source)])

    assert report.items[0].error == "probe_failed"
    assert not (usb / SUB / "001 - Artist - Song 1.mp3").exists()


def test_probe_wrong_format_is_rejected(usb, source):
    writer = writer_for(usb, FakeTranscoder(media_format="flac"))

    report = writer.write(make_plan([make_item()]), [make_asset(source)])

    assert report.items[0].error == "probe_failed"


def test_missing_probe_is_reported(usb, source):
    writer = writer_for(usb, FakeTranscoder(probe_error=TranscoderUnavailable("нет ffprobe")))

    report = writer.write(make_plan([make_item()]), [make_asset(source)])

    assert report.items[0].error == "probe_failed"
    assert not list((usb / SUB).glob(".neurowave-part-*"))


# --- FfmpegTranscoder без реального ffmpeg -------------------------------


class Runner:
    def __init__(self, *, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0, write=None):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.write = write
        self.command: list[str] | None = None
        self.kwargs: dict = {}

    def __call__(self, command, **kwargs):
        self.command = list(command)
        self.kwargs = kwargs
        if self.write is not None:
            self.write(command)
        return subprocess.CompletedProcess(command, self.returncode, self.stdout, self.stderr)


def test_ffmpeg_command_is_bounded_and_has_no_shell(tmp_path, source):
    destination = tmp_path / "out.mp3"
    runner = Runner(write=lambda command: Path(command[-1]).write_bytes(b"mp3"))

    transcoder = FfmpegTranscoder(
        ffmpeg="/usr/bin/ffmpeg", ffprobe="/usr/bin/ffprobe", runner=runner
    )
    transcoder.transcode(source, destination, target_format="mp3", bitrate_kbps=320)

    assert transcoder.available is True
    assert runner.command[0] == "/usr/bin/ffmpeg"
    assert "-nostdin" in runner.command
    assert "libmp3lame" in runner.command
    assert runner.command[-5:-3] == ["-b:a", "320k"]
    assert runner.command[-3:-1] == ["-f", "mp3"]
    assert runner.kwargs["shell"] is False
    assert destination.read_bytes() == b"mp3"


def test_ffprobe_json_is_parsed(tmp_path):
    runner = Runner(stdout=b'{"format": {"format_name": "mp3", "duration": "180.5"}}')
    path = tmp_path / "x.mp3"
    path.write_bytes(b"x")

    info = FfmpegTranscoder(
        ffmpeg="/usr/bin/ffmpeg", ffprobe="/usr/bin/ffprobe", runner=runner
    ).probe(path)

    assert info == MediaInfo(media_format="mp3", duration_ms=180_500)
    assert "-print_format" in runner.command


def test_ffmpeg_nonzero_exit_is_an_error(tmp_path, source):
    destination = tmp_path / "out.mp3"
    runner = Runner(returncode=1, stderr=b"encoder failed")

    transcoder = FfmpegTranscoder(
        ffmpeg="/usr/bin/ffmpeg", ffprobe="/usr/bin/ffprobe", runner=runner
    )
    with pytest.raises(TranscoderError) as error:
        transcoder.transcode(source, destination)

    assert "encoder failed" in str(error.value)
    assert not destination.exists()


def test_transcoder_without_binary_is_unavailable(tmp_path, source):
    transcoder = FfmpegTranscoder(ffmpeg="")

    assert transcoder.available is False
    with pytest.raises(TranscoderUnavailable):
        transcoder.transcode(source, tmp_path / "out.mp3")


def test_unsupported_target_format_is_rejected(tmp_path, source):
    transcoder = FfmpegTranscoder(ffmpeg="/usr/bin/ffmpeg", runner=Runner())

    with pytest.raises(TranscoderError):
        transcoder.transcode(source, tmp_path / "out.ogg", target_format="ogg")


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe не установлены",
)
def test_real_ffmpeg_transcodes_synthetic_wav(tmp_path):
    wav = synth_wav(tmp_path / "tone.wav", duration_ms=500)
    mp3 = tmp_path / "tone.mp3"
    transcoder = FfmpegTranscoder()

    transcoder.transcode(wav, mp3, target_format="mp3", bitrate_kbps=128)
    info = transcoder.probe(mp3)

    assert mp3.stat().st_size > 0
    assert "mp3" in info.media_format
    assert abs(info.duration_ms - 500) <= 250


# --- M3U8 ---------------------------------------------------------------


def test_m3u8_uses_relative_paths_and_utf8():
    entries = [
        PlaylistEntry(f"{SUB}/001 - Артист - Трек.mp3", 233_400, "Артист", "Трек"),
        PlaylistEntry(f"{SUB}/002 - A - B.mp3", 59_000, "A", "B"),
    ]

    text = render_m3u8(entries, SUB)

    lines = text.splitlines()
    assert lines[0] == "#EXTM3U"
    assert lines[1] == "#EXTINF:233,Артист - Трек"
    assert lines[2] == "001 - Артист - Трек.mp3"
    assert "002 - A - B.mp3" in lines
    assert str(Path("/").anchor) not in (lines[2], lines[4])


def test_playlist_contains_only_verified_files(usb, tmp_path):
    good = synth_wav(tmp_path / "library" / "good.wav")
    missing = tmp_path / "library" / "missing.wav"
    items = [make_item(1, operation=ExportOperation.COPY), make_item(2, operation=ExportOperation.COPY)]
    assets = [
        ExportAsset(
            asset_id="asset-1",
            track_key="local:1",
            source_id="local",
            media_format="wav",
            size_bytes=good.stat().st_size,
            rights=RightsStatus.PERMITTED,
            local_path=good,
            eligible=True,
        ),
        ExportAsset(
            asset_id="asset-2",
            track_key="local:2",
            source_id="local",
            media_format="wav",
            size_bytes=10,
            rights=RightsStatus.PERMITTED,
            local_path=missing,
            eligible=True,
        ),
    ]
    writer = writer_for(usb, FakeTranscoder())

    report = writer.write(make_plan(items), assets)

    assert report.status is ExportStatus.PARTIAL
    playlist = (usb / SUB / "playlist.m3u8").read_text(encoding="utf-8")
    assert "001 - Artist - Song 1.mp3" in playlist or "001 - Artist - Song 1.wav" in playlist
    assert "002 - Artist - Song 2" not in playlist


# --- Car profile --------------------------------------------------------


def test_car_filename_numbering_and_ascii():
    assert car_filename(7, "Кино", "Группа крови") == "007 - Кино - Группа крови.mp3"
    ascii_name = car_filename(7, "Кино", "Группа крови", ascii_names=True)
    assert ascii_name == "007 - Kino - Gruppa krovi.mp3"
    assert car_filename(1, "AC/DC", "Back: in Black", "flac") == "001 - AC_DC - Back_ in Black.flac"


def test_car_destination_uses_car_folder():
    destination = car_destination(3, "Artist", "Title", ascii_names=True)

    assert destination == f"{CAR_FOLDER}/003 - Artist - Title.mp3"
    assert transliterate("Ёлка") == "Elka"


def test_car_names_are_length_limited():
    name = car_filename(1, "Artist", "Ж" * 400, ascii_names=True)

    assert len(name.encode("utf-8")) <= 240
    assert name.startswith("001 - Artist - ")
    assert name.endswith(".mp3")
