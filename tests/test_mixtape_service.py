"""USB-07: общий сервис плана и записи — один для CLI и Qt-моста.

Проверяются контракт и коды ошибок сервиса: план без библиотеки, preview без
прав, запись только с подтверждёнными правами и совпавшим `plan_id`, журнал
задачи и verify. Всё офлайн: fake-USB и временная библиотека.
"""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

import pytest

from neurowave.mixtape.devices import FakeUsbDeviceService
from neurowave.mixtape.jobs import JobStore
from neurowave.mixtape.models import DeviceIdentity, MixtapeRequest
from neurowave.mixtape.service import MixtapeService, MixtapeServiceError

OBJECT = "/org/freedesktop/UDisks2/block_devices/sdb1"
SUB = "MUSIC/NeuroWave"
REQUEST = MixtapeRequest(target_duration_ms=3_600_000)


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
def service(usb, tmp_path):
    identity = DeviceIdentity(
        udisks_object_path=OBJECT,
        block_device="/dev/sdb1",
        mountpoint=usb.resolve(),
        filesystem_type="vfat",
        filesystem_uuid="1234-ABCD",
        drive_id="USB_Stick",
        removable=True,
        writable=True,
        capacity_bytes=16_000_000_000,
        free_bytes=8_000_000_000,
    )
    return MixtapeService(
        device_service=FakeUsbDeviceService([identity]),
        job_store=JobStore(tmp_path / "jobs"),
    )


def test_plan_is_side_effect_free(service, library):
    build = service.plan(library, REQUEST, rights_confirmed=True)

    assert build.rights_confirmed is True
    assert len(build.tracks) == 2
    assert len(build.assets) == 2
    assert len(build.plan.items) == 2
    assert build.plan.estimated_bytes > 0
    assert not (library / SUB).exists()


def test_plan_without_rights_has_no_files(service, library):
    build = service.plan(library, REQUEST, rights_confirmed=False)

    assert build.rights_confirmed is False
    assert build.plan.items == ()
    assert build.plan.skipped


def test_plan_missing_library_is_coded_error(service, tmp_path):
    with pytest.raises(MixtapeServiceError) as error:
        service.plan(tmp_path / "nope", REQUEST, rights_confirmed=True)

    assert error.value.code == "library_missing"


def test_export_requires_confirmed_rights(service, library, usb):
    build = service.plan(library, REQUEST, rights_confirmed=False)

    with pytest.raises(MixtapeServiceError) as error:
        service.export(build, object_path=OBJECT)

    assert error.value.code == "rights_not_confirmed"
    assert not (usb / SUB).exists()


def test_export_rejects_stale_plan_id(service, library, usb):
    """CLI/мост пересобирают план и сравнивают его с сохранённым plan_id."""

    build = service.plan(library, REQUEST, rights_confirmed=True)
    make_wav(library / "Artist Three - Song Three.wav")
    fresh = service.plan(library, REQUEST, rights_confirmed=True)

    with pytest.raises(MixtapeServiceError) as error:
        service.export(
            fresh, object_path=OBJECT, expected_plan_id=build.plan.plan_id
        )

    assert error.value.code == "plan_changed"
    assert fresh.plan.plan_id != build.plan.plan_id
    assert not (usb / SUB).exists()


def test_export_unknown_target(service, library, usb):
    build = service.plan(library, REQUEST, rights_confirmed=True)

    with pytest.raises(MixtapeServiceError) as error:
        service.export(build, object_path="/org/freedesktop/UDisks2/block_devices/sdz1")

    assert error.value.code == "target_not_found"
    assert not (usb / SUB).exists()


def test_export_writes_files_and_journal(service, library, usb, tmp_path):
    build = service.plan(library, REQUEST, rights_confirmed=True)
    started: list[tuple[str, str]] = []
    store = JobStore(tmp_path / "jobs")

    outcome = service.export(
        build,
        object_path=OBJECT,
        expected_plan_id=build.plan.plan_id,
        on_job_started=lambda job_id: started.append(
            (job_id, (store.load(job_id) or {}).get("status", ""))
        ),
    )

    assert outcome.report.status.value == "complete"
    assert outcome.report.verified_count == 2
    assert outcome.job_id == build.plan.plan_id
    assert started == [(outcome.job_id, "running")]  # журнал уже пишется
    assert outcome.record["object_path"] == OBJECT
    assert outcome.record["library"] == str(build.library)
    assert len(list((usb / SUB).glob("*.wav"))) == 2
    assert (usb / SUB / "manifest.json").is_file()
    assert (usb / "sentinel.txt").read_text(encoding="utf-8") == "не трогать"
    saved = store.load(outcome.job_id)
    assert saved is not None and saved["status"] == "complete"


def test_verify_after_export(service, library, usb):
    build = service.plan(library, REQUEST, rights_confirmed=True)
    outcome = service.export(build, object_path=OBJECT)

    report = service.verify(outcome.job_id)

    assert report.ok is True
    assert report.checked == 2
    assert report.total == 2


def test_verify_unknown_job(service):
    with pytest.raises(MixtapeServiceError) as error:
        service.verify("mixtape-nope")

    assert error.value.code == "job_not_found"


def test_eject_uses_revalidated_target(service, usb):
    identity = service.eject_target(OBJECT)

    assert identity.udisks_object_path == OBJECT
    assert service.device_service.ejected == [OBJECT]
