"""USB-04: safe writer на fake USB, без физической записи.

Проверяются T-006…T-015 и T-020…T-026: path traversal и симлинк-подмена,
коллизии имён, no-clobber, ENOSPC/EROFS, выдёргивание устройства, cancel,
повторный запуск и resume, чужой/битый manifest, проверка после записи.
"""

from __future__ import annotations

import errno
import json
import os
import threading
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
from neurowave.mixtape.devices import FakeUsbDeviceService
from neurowave.mixtape.export import (
    MANIFEST_NAME,
    ExportError,
    ExportErrorCode,
    ExportStatus,
    ItemStatus,
    SafeWriter,
)
from neurowave.mixtape.export import writer as writer_mod
from neurowave.mixtape.models import ExportOperation

SUB = "MUSIC/NeuroWave"
TRACK = "local:1"


def make_item(
    number: int = 1,
    *,
    name: str | None = None,
    destination: str | None = None,
    asset_id: str | None = None,
) -> PlannedItem:
    filename = name or f"{number:03d} - Artist - Song {number}.mp3"
    return PlannedItem(
        source_asset_id=asset_id or f"asset-{number}",
        source_id="local",
        track_key=f"local:{number}",
        title=f"Song {number}",
        artists=("Artist",),
        destination_relative=destination or f"{SUB}/{filename}",
        operation=ExportOperation.COPY,
        estimated_bytes=0,
        duration_ms=180_000,
        score=0.0,
        reason="лайк",
    )


def make_asset(
    path: Path,
    number: int = 1,
    *,
    rights: RightsStatus = RightsStatus.PERMITTED,
    eligible: bool = True,
    size_bytes: int | None = None,
) -> ExportAsset:
    return ExportAsset(
        asset_id=f"asset-{number}",
        track_key=f"local:{number}",
        source_id="local",
        media_format="mp3",
        size_bytes=size_bytes if size_bytes is not None else path.stat().st_size,
        rights=rights,
        duration_ms=180_000,
        local_path=path,
        eligible=eligible,
    )


def make_plan(items, *, plan_id: str = "mixtape-test", subdir: str = SUB) -> MixtapePlan:
    return MixtapePlan(
        plan_id=plan_id,
        request=MixtapeRequest(target_duration_ms=3_600_000, destination_subdir=subdir),
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
    (root / "sentinel.txt").write_text("не трогать", encoding="utf-8")
    return root


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    return root


def audio(path: Path, data: bytes = b"ID3-audio-data") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def writer_for(usb: Path) -> tuple[SafeWriter, FakeUsbDeviceService, DeviceIdentity]:
    device = identity_for(usb)
    service = FakeUsbDeviceService([device])
    return SafeWriter(usb, device_service=service, expected_device=device), service, device


def music_files(usb: Path) -> list[Path]:
    folder = usb / SUB
    return sorted(folder.glob("*.mp3")) if folder.is_dir() else []


def manifest_of(usb: Path) -> dict:
    return json.loads((usb / SUB / MANIFEST_NAME).read_text(encoding="utf-8"))


# --- T-006: path traversal ------------------------------------------------


@pytest.mark.parametrize(
    "destination",
    [
        "../evil.mp3",
        "/tmp/evil.mp3",
        f"{SUB}/../../evil.mp3",
        f"{SUB}/Album/\x00evil.mp3",
    ],
)
def test_t006_unsafe_destination_is_rejected(usb, library, destination):
    source = audio(library / "one.mp3")
    plan = make_plan([make_item(destination=destination)])
    writer, _, _ = writer_for(usb)

    report = writer.write(plan, [make_asset(source)])

    assert report.status is ExportStatus.FAILED
    assert report.items[0].status is ItemStatus.FAILED
    assert report.items[0].error in {"unsafe_path", "path_escape"}
    assert not (usb.parent / "evil.mp3").exists()


# --- T-007: symlink swap --------------------------------------------------


def test_t007_symlinked_subdir_is_rejected(usb, library, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (usb / "MUSIC").mkdir()
    (usb / "MUSIC" / "NeuroWave").symlink_to(outside, target_is_directory=True)
    source = audio(library / "one.mp3")
    writer, _, _ = writer_for(usb)

    with pytest.raises(ExportError) as error:
        writer.write(make_plan([make_item()]), [make_asset(source)])

    assert error.value.code is ExportErrorCode.PATH_ESCAPE
    assert list(outside.iterdir()) == []
    assert (usb / "sentinel.txt").read_text(encoding="utf-8") == "не трогать"


def test_t007_symlinked_album_directory_is_rejected(usb, library, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (usb / SUB).mkdir(parents=True)
    (usb / SUB / "Album").symlink_to(outside, target_is_directory=True)
    source = audio(library / "one.mp3")
    plan = make_plan([make_item(destination=f"{SUB}/Album/evil.mp3")])
    writer, _, _ = writer_for(usb)

    report = writer.write(plan, [make_asset(source)])

    assert report.status is ExportStatus.FAILED
    assert report.items[0].error == "path_escape"
    assert list(outside.iterdir()) == []


# --- T-008: case collision и no-clobber -----------------------------------


def test_t008_case_collision_is_skipped(usb, library):
    first = audio(library / "one.mp3", b"first")
    second = audio(library / "two.mp3", b"second")
    plan = make_plan(
        [
            make_item(1, name="001 - Artist - Song.mp3"),
            make_item(2, name="001 - Artist - song.mp3"),
        ]
    )
    writer, _, _ = writer_for(usb)

    report = writer.write(plan, [make_asset(first, 1), make_asset(second, 2)])

    assert report.status is ExportStatus.PARTIAL
    assert report.verified_count == 1
    assert report.items[1].status is ItemStatus.SKIPPED
    assert report.items[1].error == "collision"
    assert len(music_files(usb)) == 1


def test_existing_file_is_never_overwritten(usb, library):
    destination = usb / SUB / "001 - Artist - Song 1.mp3"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"user file")
    source = audio(library / "one.mp3")
    writer, _, _ = writer_for(usb)

    report = writer.write(make_plan([make_item()]), [make_asset(source)])

    assert report.items[0].error == "exists"
    assert destination.read_bytes() == b"user file"
    assert (usb / "sentinel.txt").exists()


# --- T-009: FAT-совместимые имена -----------------------------------------


def test_t009_long_filename_is_truncated_to_limit(usb, library):
    source = audio(library / "one.mp3")
    long_name = "x" * 300 + ".mp3"
    plan = make_plan([make_item(name=long_name)])
    writer, _, _ = writer_for(usb)

    report = writer.write(plan, [make_asset(source)])

    assert report.status is ExportStatus.COMPLETE
    [file] = music_files(usb)
    assert len(file.name.encode("utf-8")) <= 240
    assert file.name.endswith(".mp3")


# --- T-010/T-016: ENOSPC, EROFS -------------------------------------------


def test_t010_enospc_aborts_partial_and_cleans_temp(usb, library, monkeypatch):
    first = audio(library / "one.mp3", b"one")
    second = audio(library / "two.mp3", b"two")
    plan = make_plan([make_item(1), make_item(2)])
    writer, _, _ = writer_for(usb)

    def no_space(fd, data):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(writer_mod, "_write_bytes", no_space)

    report = writer.write(plan, [make_asset(first, 1), make_asset(second, 2)])

    assert report.status is ExportStatus.FAILED
    assert report.items[0].error == "no_space"
    assert music_files(usb) == []
    assert not list((usb / SUB).glob(".neurowave-part-*"))
    assert (usb / "sentinel.txt").read_text(encoding="utf-8") == "не трогать"


def test_eio_aborts_partial_and_cleans_temp(usb, library, monkeypatch):
    source = audio(library / "one.mp3")
    writer, _, _ = writer_for(usb)

    def io_error(fd, data):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(writer_mod, "_write_bytes", io_error)

    report = writer.write(make_plan([make_item()]), [make_asset(source)])

    assert report.items[0].error == "io_error"
    assert report.status is ExportStatus.FAILED
    assert music_files(usb) == []
    assert not list((usb / SUB).glob(".neurowave-part-*"))
    assert (usb / "sentinel.txt").exists()


def test_rename_race_never_overwrites_appearing_file(usb, library, monkeypatch):
    source = audio(library / "one.mp3", b"source")
    destination = usb / SUB / "001 - Artist - Song 1.mp3"
    real_link = os.link

    def racing_link(src, dst, *, src_dir_fd=None, dst_dir_fd=None, follow_symlinks=True):
        fd = os.open(
            dst,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=dst_dir_fd,
        )
        os.write(fd, b"user appeared")
        os.close(fd)
        return real_link(
            src,
            dst,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    writer, _, _ = writer_for(usb)
    monkeypatch.setattr(writer_mod.os, "link", racing_link)

    report = writer.write(make_plan([make_item()]), [make_asset(source)])

    assert report.items[0].error == "exists"
    assert destination.read_bytes() == b"user appeared"
    assert not list((usb / SUB).glob(".neurowave-part-*"))


def test_read_only_target_is_reported(usb, library, monkeypatch):
    source = audio(library / "one.mp3")
    writer, _, _ = writer_for(usb)

    def read_only(fd, data):
        raise OSError(errno.EROFS, "Read-only file system")

    monkeypatch.setattr(writer_mod, "_write_bytes", read_only)

    report = writer.write(make_plan([make_item()]), [make_asset(source)])

    assert report.items[0].error == "read_only"
    assert report.verified_count == 0


# --- T-011: устройство пропало -------------------------------------------


def test_t011_unplugged_device_stops_before_writing(usb, library):
    source = audio(library / "one.mp3")
    writer, service, _ = writer_for(usb)
    service.remove("/org/freedesktop/UDisks2/block_devices/sdb1")

    report = writer.write(make_plan([make_item()]), [make_asset(source)])

    assert report.items[0].error == "device_unverified"
    assert report.status is ExportStatus.FAILED
    assert music_files(usb) == []
    assert (usb / "sentinel.txt").exists()


# --- T-012/T-013/T-014: resume, повтор, изменившийся источник -------------


def test_t012_resume_after_partial_failure(usb, library):
    first = audio(library / "one.mp3", b"one")
    missing = library / "two.mp3"
    second_asset = ExportAsset(
        asset_id="asset-2",
        track_key="local:2",
        source_id="local",
        media_format="mp3",
        size_bytes=3,
        rights=RightsStatus.PERMITTED,
        local_path=missing,
    )
    plan = make_plan([make_item(1), make_item(2)])
    writer, _, _ = writer_for(usb)

    broken = writer.write(plan, [make_asset(first, 1), second_asset])
    assert broken.status is ExportStatus.PARTIAL
    assert broken.verified_count == 1
    assert broken.items[1].error == "source_missing"
    assert [file.name for file in music_files(usb)] == ["001 - Artist - Song 1.mp3"]

    audio(missing, b"two")
    writer, _, _ = writer_for(usb)
    resumed = writer.write(plan, [make_asset(first, 1), make_asset(missing, 2)])

    assert resumed.status is ExportStatus.COMPLETE
    assert resumed.verified_count == 2
    assert len(music_files(usb)) == 2


def test_t013_repeat_job_does_not_duplicate_or_overwrite(usb, library):
    source = audio(library / "one.mp3", b"one")
    plan = make_plan([make_item()])
    writer, _, _ = writer_for(usb)

    first = writer.write(plan, [make_asset(source)])
    second = SafeWriter(
        usb,
        device_service=FakeUsbDeviceService([identity_for(usb)]),
        expected_device=identity_for(usb),
    ).write(plan, [make_asset(source)])

    assert first.status is ExportStatus.COMPLETE
    assert second.status is ExportStatus.COMPLETE
    assert second.verified_count == 1
    assert len(music_files(usb)) == 1
    assert (usb / "sentinel.txt").exists()


def test_resume_recopies_missing_verified_file(usb, library):
    source = audio(library / "one.mp3", b"one")
    plan = make_plan([make_item()])
    writer, _, _ = writer_for(usb)
    writer.write(plan, [make_asset(source)])
    [file] = music_files(usb)
    file.unlink()

    writer, _, _ = writer_for(usb)
    report = writer.write(plan, [make_asset(source)])

    assert report.status is ExportStatus.COMPLETE
    assert [item.name for item in music_files(usb)] == ["001 - Artist - Song 1.mp3"]


def test_t014_changed_source_is_not_committed(usb, library):
    source = audio(library / "one.mp3", b"1234567890")
    writer, _, _ = writer_for(usb)

    report = writer.write(
        make_plan([make_item()]), [make_asset(source, size_bytes=5)]
    )

    assert report.items[0].error == "source_changed"
    assert music_files(usb) == []


# --- T-015/T-022: manifest -------------------------------------------------


def test_t015_invalid_manifest_refuses_to_resume(usb, library):
    (usb / SUB).mkdir(parents=True)
    (usb / SUB / MANIFEST_NAME).write_text("not json", encoding="utf-8")
    source = audio(library / "one.mp3")
    writer, _, _ = writer_for(usb)

    with pytest.raises(ExportError) as error:
        writer.write(make_plan([make_item()]), [make_asset(source)])

    assert error.value.code is ExportErrorCode.MANIFEST_INVALID
    assert music_files(usb) == []


def test_t015_manifest_with_traversal_is_rejected(usb, library):
    (usb / SUB).mkdir(parents=True)
    payload = {
        "schema_version": 1,
        "job_id": "other",
        "plan_id": "other",
        "revision": 1,
        "destination_subdir": SUB,
        "status": "writing",
        "entries": [
            {
                "track_key": "local:1",
                "destination": f"{SUB}/../../evil.mp3",
                "status": "verified",
                "size_bytes": 1,
                "sha256": "0" * 64,
            }
        ],
    }
    (usb / SUB / MANIFEST_NAME).write_text(json.dumps(payload), encoding="utf-8")
    source = audio(library / "one.mp3")
    writer, _, _ = writer_for(usb)

    with pytest.raises(ExportError) as error:
        writer.write(make_plan([make_item()]), [make_asset(source)])

    assert error.value.code is ExportErrorCode.MANIFEST_INVALID


def test_t022_foreign_manifest_is_not_touched(usb, library):
    source = audio(library / "one.mp3", b"one")
    first_plan = make_plan([make_item()], plan_id="mixtape-other")
    writer, _, _ = writer_for(usb)
    writer.write(first_plan, [make_asset(source)])
    before = (usb / SUB / MANIFEST_NAME).read_text(encoding="utf-8")

    other_plan = make_plan([make_item()], plan_id="mixtape-new")
    writer, _, _ = writer_for(usb)
    with pytest.raises(ExportError) as error:
        writer.write(other_plan, [make_asset(source)])

    assert error.value.code is ExportErrorCode.FOREIGN_MANIFEST
    assert (usb / SUB / MANIFEST_NAME).read_text(encoding="utf-8") == before
    assert (usb / "sentinel.txt").exists()


# --- T-020/T-025: cancel --------------------------------------------------


def test_t025_cancel_before_start_writes_no_audio(usb, library):
    source = audio(library / "one.mp3")
    cancel = threading.Event()
    cancel.set()
    writer, _, _ = writer_for(usb)

    report = writer.write(
        make_plan([make_item()]), [make_asset(source)], cancel=cancel
    )

    assert report.status is ExportStatus.CANCELLED
    assert report.verified_count == 0
    assert music_files(usb) == []


def test_t020_cancel_mid_file_removes_temp_and_keeps_previous(usb, library):
    first = audio(library / "one.mp3", b"one")
    second = audio(library / "two.mp3", b"x" * 4096)
    cancel = threading.Event()
    original = writer_mod._write_bytes
    calls = {"count": 0}

    def cancel_after_chunk(fd, data):
        original(fd, data)
        calls["count"] += 1
        if calls["count"] >= 2:
            cancel.set()

    writer = SafeWriter(
        usb,
        device_service=FakeUsbDeviceService([identity_for(usb)]),
        expected_device=identity_for(usb),
        buffer_size=512,
    )
    plan = make_plan([make_item(1), make_item(2)])

    import unittest.mock

    with unittest.mock.patch.object(writer_mod, "_write_bytes", cancel_after_chunk):
        report = writer.write(
            plan, [make_asset(first, 1), make_asset(second, 2)], cancel=cancel
        )

    assert report.status is ExportStatus.CANCELLED
    assert report.verified_count == 1
    assert report.items[1].status is ItemStatus.CANCELLED
    assert [file.name for file in music_files(usb)] == ["001 - Artist - Song 1.mp3"]
    assert not list((usb / SUB).glob(".neurowave-part-*"))


# --- T-023/T-024/T-026: unicode, длинная задача, verify -------------------


def test_t023_unicode_filename_is_committed(usb, library):
    source = audio(library / "one.mp3")
    plan = make_plan([make_item(name="001 - Артист — Трек 🎵.mp3")])
    writer, _, _ = writer_for(usb)

    report = writer.write(plan, [make_asset(source)])

    assert report.status is ExportStatus.COMPLETE
    assert (usb / SUB / "001 - Артист — Трек 🎵.mp3").exists()


def test_t024_many_files_with_small_buffer(usb, library):
    items = []
    assets = []
    for number in range(1, 41):
        source = audio(library / f"{number}.mp3", b"x" * 100 + bytes([number % 256]))
        items.append(make_item(number))
        assets.append(make_asset(source, number))
    writer = SafeWriter(
        usb,
        device_service=FakeUsbDeviceService([identity_for(usb)]),
        expected_device=identity_for(usb),
        buffer_size=64,
    )

    report = writer.write(make_plan(items), assets)

    assert report.status is ExportStatus.COMPLETE
    assert report.verified_count == 40
    assert len(music_files(usb)) == 40
    assert not list((usb / SUB).glob(".neurowave-part-*"))


def test_t026_verify_failure_does_not_commit(usb, library, monkeypatch):
    source = audio(library / "one.mp3")
    writer, _, _ = writer_for(usb)

    def wrong_hash(fd):
        return 0, "0" * 64

    monkeypatch.setattr(writer_mod, "_hash_fd", wrong_hash)

    report = writer.write(make_plan([make_item()]), [make_asset(source)])

    assert report.items[0].error == "verify_failed"
    assert music_files(usb) == []
    assert not list((usb / SUB).glob(".neurowave-part-*"))


# --- права и T-021 --------------------------------------------------------


def test_asset_without_rights_is_not_copied(usb, library):
    source = audio(library / "one.mp3")
    writer, _, _ = writer_for(usb)

    report = writer.write(
        make_plan([make_item()]),
        [make_asset(source, rights=RightsStatus.UNKNOWN, eligible=False)],
    )

    assert report.items[0].error == "rights_denied"
    assert music_files(usb) == []


def test_t021_foreign_files_are_never_deleted(usb, library):
    source = audio(library / "one.mp3")
    foreign = usb / SUB / "user-album" / "keep.mp3"
    foreign.parent.mkdir(parents=True)
    foreign.write_bytes(b"user music")
    writer, _, _ = writer_for(usb)

    report = writer.write(make_plan([make_item()]), [make_asset(source)])

    assert report.status is ExportStatus.COMPLETE
    assert foreign.read_bytes() == b"user music"
    assert (usb / "sentinel.txt").exists()
