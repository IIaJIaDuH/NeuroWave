"""USB-07: Qt-независимый мост USB Mixtape на fake-USB, без реальной записи.

Проверяются: список томов, требование подтверждения прав и записи, preview с
байтами и причинами пропуска, фоновая работа (не в потоке вызова), прогресс,
отмена, обнаружение изменённого плана, verify только для complete и извлечение
только после завершения worker'а.
"""

from __future__ import annotations

import json
import math
import struct
import threading
import wave
from pathlib import Path

import pytest

from neurowave.mixtape.bridge import (
    BridgeStatus,
    MixtapeBridge,
    describe_skips,
)
from neurowave.mixtape.devices import FakeUsbDeviceService
from neurowave.mixtape.jobs import JobStore
from neurowave.mixtape.models import DeviceIdentity, MixtapeRequest

OBJECT = "/org/freedesktop/UDisks2/block_devices/sdb1"
OTHER = "/org/freedesktop/UDisks2/block_devices/sdc1"
SUB = "MUSIC/NeuroWave"


def make_wav(path: Path, *, duration_ms: int = 5_000, rate: int = 8_000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(rate * duration_ms / 1000)
    data = b"".join(
        struct.pack("<h", int(1_000 * math.sin(index / 10))) for index in range(frames)
    )
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(data)
    return path


def identity(mountpoint: Path, *, object_path: str = OBJECT) -> DeviceIdentity:
    return DeviceIdentity(
        udisks_object_path=object_path,
        block_device="/dev/sdb1",
        mountpoint=mountpoint.resolve(),
        filesystem_type="vfat",
        filesystem_uuid="1234-ABCD",
        drive_id="USB_Stick",
        removable=True,
        writable=True,
        capacity_bytes=16_000_000_000,
        free_bytes=8_000_000_000,
    )


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "Music"
    make_wav(root / "Artist One - Song One.wav")
    make_wav(root / "Artist Two - Song Two.wav")
    return root


@pytest.fixture
def usb(tmp_path):
    root = tmp_path / "usb"
    root.mkdir()
    (root / "sentinel.txt").write_text("не трогать", encoding="utf-8")
    return root


@pytest.fixture
def service(usb):
    return FakeUsbDeviceService([identity(usb)])


@pytest.fixture
def store(tmp_path):
    return JobStore(tmp_path / "jobs")


@pytest.fixture
def bridge(service, store, library):
    instance = MixtapeBridge(device_service=service, job_store=store, library=library)
    yield instance
    instance.close()


def plan_bridge(bridge: MixtapeBridge, *, request: MixtapeRequest | None = None) -> None:
    assert bridge.request_plan(request=request or MixtapeRequest(target_duration_ms=3_600_000), rights_confirmed=True)
    assert bridge.wait(10)


# --- устройства и план ---------------------------------------------------


def test_devices_and_selection(bridge, service):
    devices = bridge.refresh_devices()
    state = bridge.snapshot()

    assert [device.object_path for device in devices] == [OBJECT]
    assert state.devices[0].mountpoint == str(service.devices[0].mountpoint)
    assert state.devices[0].free_bytes == 8_000_000_000
    assert state.selected == OBJECT
    assert state.can_plan is True


def test_plan_requires_confirmed_rights(bridge):
    started = bridge.request_plan(rights_confirmed=False)
    state = bridge.snapshot()

    assert started is False
    assert state.preview is None
    assert state.error_code == "rights_not_confirmed"
    assert state.can_export is False


def test_plan_preview_shows_tracks_bytes_and_skips(bridge):
    bridge.refresh_devices()
    plan_bridge(bridge, request=MixtapeRequest(target_duration_ms=3_600_000, excluded_artists=("Artist One",)))
    state = bridge.snapshot()

    assert state.status is BridgeStatus.PLANNED
    preview = state.preview
    assert preview is not None
    assert preview.item_count == 1
    assert preview.candidate_count == 2
    assert preview.estimated_bytes > 0
    assert preview.shortage_ms > 0  # 5 секунд из часа
    assert preview.tracks[0].destination.startswith(SUB)
    assert [skip.reason for skip in preview.skipped] == ["excluded_artist"]
    assert "excluded_artist: 1" in describe_skips(preview)
    assert state.can_export is True


def test_plan_without_library_file_is_reported(tmp_path, service, store):
    missing = tmp_path / "nope"
    bridge = MixtapeBridge(device_service=service, job_store=store, library=missing)
    try:
        assert bridge.request_plan(rights_confirmed=True) is False
        state = bridge.snapshot()
        assert state.status is BridgeStatus.FAILED
        assert state.error_code == "library_missing"
        assert state.preview is None
    finally:
        bridge.close()


def test_empty_library_plan_cannot_be_exported(tmp_path, service, store):
    empty = tmp_path / "Empty"
    empty.mkdir()
    bridge = MixtapeBridge(device_service=service, job_store=store, library=empty)
    try:
        assert bridge.request_plan(rights_confirmed=True) is True
        assert bridge.wait(10)
        state = bridge.snapshot()
        assert state.status is BridgeStatus.PLANNED
        assert state.preview is not None and state.preview.item_count == 0
        assert state.can_export is False
        assert bridge.start_export(confirm=True) is False
    finally:
        bridge.close()


def test_snapshot_is_json_ready(bridge):
    bridge.refresh_devices()
    plan_bridge(bridge)

    payload = json.dumps(bridge.snapshot().to_dict(), ensure_ascii=False)

    assert '"status": "planned"' in payload


# --- экспорт --------------------------------------------------------------


def test_export_requires_explicit_confirm(bridge, usb):
    bridge.refresh_devices()
    plan_bridge(bridge)

    assert bridge.start_export(confirm=False) is False
    state = bridge.snapshot()

    assert state.error_code == "confirmation_required"
    assert state.status is BridgeStatus.PLANNED
    assert not (usb / SUB).exists()


def test_export_requires_selected_device(bridge, usb):
    plan_bridge(bridge)
    bridge.select_device("")

    assert bridge.start_export(confirm=True) is False
    assert bridge.snapshot().error_code == "target_not_found"
    assert not (usb / SUB).exists()


def test_export_requires_preview(bridge, usb):
    bridge.refresh_devices()

    assert bridge.start_export(confirm=True) is False
    assert bridge.snapshot().error_code == "plan_required"
    assert not (usb / SUB).exists()


def test_export_runs_in_background_thread(bridge, usb, monkeypatch):
    from neurowave.mixtape.export import SafeWriter

    caller = threading.get_ident()
    seen: list[int] = []
    real_write = SafeWriter.write

    def spy(self, *args, **kwargs):
        seen.append(threading.get_ident())
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(SafeWriter, "write", spy)
    bridge.refresh_devices()
    plan_bridge(bridge)

    assert bridge.start_export(confirm=True) is True
    assert bridge.snapshot().status is BridgeStatus.EXPORTING
    assert bridge.snapshot().busy is True
    assert bridge.wait(15)

    assert seen and seen[0] != caller
    assert (usb / SUB).is_dir()


def test_export_progress_summary_and_journal(bridge, usb, store):
    bridge.refresh_devices()
    plan_bridge(bridge)
    job_id = bridge.snapshot().preview.plan_id

    assert bridge.start_export(confirm=True) is True
    assert bridge.wait(20)
    state = bridge.snapshot()

    assert state.status is BridgeStatus.DONE
    assert state.summary is not None
    assert state.summary.verified == 2
    assert state.summary.job_id == job_id
    assert state.progress is not None
    assert state.progress.bytes_done > 0
    assert state.progress.percent == 100.0
    assert state.progress.items_done == 2
    assert state.can_verify is True
    assert len(list((usb / SUB).glob("*.wav"))) == 2
    job = store.load(job_id)
    assert job is not None and job["status"] == "complete"
    assert job["object_path"] == OBJECT
    assert (usb / "sentinel.txt").read_text(encoding="utf-8") == "не трогать"


def test_export_detects_changed_plan(bridge, usb, library):
    bridge.refresh_devices()
    plan_bridge(bridge)
    make_wav(library / "Artist Three - Song Three.wav")

    assert bridge.start_export(confirm=True) is True
    assert bridge.wait(20)
    state = bridge.snapshot()

    assert state.status is BridgeStatus.FAILED
    assert state.error_code == "plan_changed"
    assert not (usb / SUB).exists()


def test_cancel_stops_export_and_marks_job(bridge, usb, store, monkeypatch):
    from neurowave.mixtape.export import SafeWriter

    real_write = SafeWriter.write

    def cancel_then_write(self, *args, **kwargs):
        bridge.request_cancel()
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(SafeWriter, "write", cancel_then_write)
    bridge.refresh_devices()
    plan_bridge(bridge)
    job_id = bridge.snapshot().preview.plan_id

    assert bridge.start_export(confirm=True) is True
    assert bridge.wait(20)
    state = bridge.snapshot()

    assert state.status is BridgeStatus.CANCELLED
    assert state.summary is not None and state.summary.status == "cancelled"
    job = store.load(job_id)
    assert job is not None and job["status"] == "cancelled"
    assert job["verified_count"] == 0


def test_verify_requires_complete_job(bridge, store):
    bridge.refresh_devices()
    plan_bridge(bridge)
    job_id = bridge.snapshot().preview.plan_id
    bridge.start_export(confirm=True)
    assert bridge.wait(20)

    assert bridge.request_verify() is True
    assert bridge.wait(20)
    state = bridge.snapshot()
    assert state.status is BridgeStatus.VERIFIED
    assert state.verification is not None and state.verification.ok is True

    job = store.load(job_id)
    assert job is not None
    job["status"] = "partial"
    job["items"][0]["status"] = "failed"
    store.save(job_id, job)

    assert bridge.request_verify() is True
    assert bridge.wait(20)
    state = bridge.snapshot()
    assert state.status is BridgeStatus.FAILED
    assert state.error_code == "verify_failed"
    assert state.verification is not None
    assert state.verification.ok is False
    assert state.verification.mismatches[0].error == "not_verified:failed"


# --- извлечение -----------------------------------------------------------


def test_eject_requires_confirm(bridge, service):
    bridge.refresh_devices()

    assert bridge.eject(confirm=False) is False
    assert bridge.snapshot().error_code == "confirmation_required"
    assert service.ejected == []


def test_eject_refused_while_exporting(bridge, service, monkeypatch):
    from neurowave.mixtape.export import SafeWriter

    started = threading.Event()
    release = threading.Event()
    real_write = SafeWriter.write

    def blocked(self, *args, **kwargs):
        started.set()
        release.wait(10)
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(SafeWriter, "write", blocked)
    bridge.refresh_devices()
    plan_bridge(bridge)
    bridge.start_export(confirm=True)
    try:
        assert started.wait(10)
        assert bridge.snapshot().can_eject is False
        assert bridge.eject(confirm=True) is False
        assert bridge.snapshot().error_code == "job_running"
        assert service.ejected == []
    finally:
        release.set()
        assert bridge.wait(20)

    assert bridge.eject(confirm=True) is True
    assert bridge.wait(10)
    state = bridge.snapshot()
    assert state.status is BridgeStatus.DONE
    assert state.selected == ""
    assert state.devices == ()  # список обновлён самим worker'ом
    assert "отключён" in state.message
    assert service.ejected == [OBJECT]


def test_eject_checks_journal_of_running_job(service, store, library):
    store.save("mixtape-other", {"status": "running", "object_path": OBJECT})
    bridge = MixtapeBridge(device_service=service, job_store=store, library=library)
    try:
        bridge.refresh_devices()
        assert bridge.eject(confirm=True) is True
        assert bridge.wait(10)
        assert bridge.snapshot().error_code == "job_running"
        assert service.ejected == []
    finally:
        bridge.close()


def test_close_cancels_running_export(bridge, monkeypatch):
    from neurowave.mixtape.export import SafeWriter

    started = threading.Event()
    release = threading.Event()
    real_write = SafeWriter.write

    def blocked(self, *args, **kwargs):
        started.set()
        release.wait(10)
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(SafeWriter, "write", blocked)
    bridge.refresh_devices()
    plan_bridge(bridge)
    bridge.start_export(confirm=True)
    assert started.wait(10)
    release.set()
    bridge.close()

    assert bridge.wait(10)
    assert bridge.snapshot().busy is False
