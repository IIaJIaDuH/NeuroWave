"""USB-06: headless CLI mixtape на fake-устройстве, без реальной записи.

Проверяются JSON-команды devices/plan/export/status/cancel/verify/eject,
явное подтверждение записи и извлечения, отказ при изменённом плане и
проверка sha256 записанных файлов.
"""

from __future__ import annotations

import json
import math
import struct
import wave
from pathlib import Path

import pytest

from neurowave.cli import build_parser
from neurowave.mixtape.cli import run
from neurowave.mixtape.devices import FakeUsbDeviceService
from neurowave.mixtape.jobs import CancelSignal, JobStore
from neurowave.mixtape.models import DeviceIdentity

OBJECT = "/org/freedesktop/UDisks2/block_devices/sdb1"
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


def identity(usb: Path) -> DeviceIdentity:
    return DeviceIdentity(
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


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "Music"
    root.mkdir()
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


def parse(*argv):
    return build_parser().parse_args(list(argv))


def call(args, capsys, *, service, store):
    capsys.readouterr()
    code = run(args, device_service=service, job_store=store)
    captured = capsys.readouterr()
    data = None
    if captured.out.strip().startswith("{"):
        data = json.loads(captured.out)
    return code, data, captured.out


def plan_args(library, *, save: Path | None = None, confirm: bool = True):
    argv = ["mixtape", "plan", "--library", str(library), "--hours", "1", "--json"]
    if confirm:
        argv.append("--confirm-local-rights")
    if save is not None:
        argv += ["--save", str(save)]
    return parse(*argv)


def export_args(plan_file: Path, *, target: str = OBJECT, confirm: bool = True):
    argv = ["mixtape", "export", "--plan", str(plan_file), "--target", target, "--json"]
    if confirm:
        argv.append("--confirm")
    return parse(*argv)


def saved_plan(library, path: Path, *, confirm: bool = True) -> dict:
    assert run(plan_args(library, save=path, confirm=confirm), job_store=JobStore(path.parent / "jobs")) == 0
    return json.loads(path.read_text(encoding="utf-8"))


# --- парсер и devices ---------------------------------------------------


def test_parser_registers_mixtape_commands():
    args = parse("mixtape", "devices", "--json")

    assert args.command == "mixtape"
    assert args.mixtape_command == "devices"
    assert args.mixtape_json is True


def test_devices_json(service, store, capsys):
    code, data, _ = call(parse("mixtape", "devices", "--json"), capsys, service=service, store=store)

    assert code == 0
    assert data["ok"] is True
    assert data["devices"][0]["object_path"] == OBJECT
    assert data["devices"][0]["mountpoint"] == str(service.devices[0].mountpoint)


def test_devices_without_volumes(capsys, store):
    code, data, _ = call(
        parse("mixtape", "devices", "--json"),
        capsys,
        service=FakeUsbDeviceService([]),
        store=store,
    )

    assert code == 0
    assert data["devices"] == []


# --- plan ---------------------------------------------------------------


def test_plan_without_rights_is_preview_only(library, service, store, capsys):
    code, data, _ = call(plan_args(library, confirm=False), capsys, service=service, store=store)

    assert code == 0
    assert data["rights_confirmed"] is False
    assert data["tracks_found"] == 2
    assert data["plan"]["items"] == []
    assert data["plan"]["skipped"]


def test_plan_with_rights_and_save(library, tmp_path, service, store, capsys):
    plan_file = tmp_path / "plan.json"
    args = plan_args(library, save=plan_file)

    code, data, _ = call(args, capsys, service=service, store=store)

    assert code == 0
    assert data["rights_confirmed"] is True
    assert len(data["plan"]["items"]) == 2
    saved = json.loads(plan_file.read_text(encoding="utf-8"))
    assert saved["plan_id"] == data["plan"]["plan_id"]
    assert saved["rights_confirmed"] is True
    assert len(saved["items"]) == 2


def test_plan_missing_library_is_json_error(tmp_path, service, store, capsys):
    args = parse("mixtape", "plan", "--library", str(tmp_path / "nope"), "--json")

    code, data, _ = call(args, capsys, service=service, store=store)

    assert code == 2
    assert data["ok"] is False
    assert data["error"] == "library_missing"


# --- export -------------------------------------------------------------


def test_export_requires_confirm(library, tmp_path, usb, service, store, capsys):
    plan_file = tmp_path / "plan.json"
    saved_plan(library, plan_file)

    code, data, _ = call(
        export_args(plan_file, confirm=False), capsys, service=service, store=store
    )

    assert code == 2
    assert data["error"] == "confirmation_required"
    assert not (usb / SUB).exists()


def test_export_rejects_plan_without_rights(library, tmp_path, service, store, capsys):
    plan_file = tmp_path / "plan.json"
    saved_plan(library, plan_file, confirm=False)

    code, data, _ = call(export_args(plan_file), capsys, service=service, store=store)

    assert code == 2
    assert data["error"] == "rights_not_confirmed"


def test_export_detects_changed_plan(library, tmp_path, service, store, capsys):
    plan_file = tmp_path / "plan.json"
    saved = saved_plan(library, plan_file)
    saved["plan_id"] = "mixtape-changed"
    plan_file.write_text(json.dumps(saved), encoding="utf-8")

    code, data, _ = call(export_args(plan_file), capsys, service=service, store=store)

    assert code == 2
    assert data["error"] == "plan_changed"


def test_export_unknown_target(library, tmp_path, service, store, capsys):
    plan_file = tmp_path / "plan.json"
    saved_plan(library, plan_file)

    code, data, _ = call(
        export_args(plan_file, target="/org/freedesktop/UDisks2/block_devices/sdz1"),
        capsys,
        service=service,
        store=store,
    )

    assert code == 2
    assert data["error"] == "target_not_found"


def test_export_writes_files_manifest_and_job(library, tmp_path, usb, service, store, capsys):
    plan_file = tmp_path / "plan.json"
    saved = saved_plan(library, plan_file)

    code, data, _ = call(export_args(plan_file), capsys, service=service, store=store)

    assert code == 0
    assert data["ok"] is True
    assert data["status"] == "complete"
    assert data["verified_count"] == 2
    files = sorted((usb / SUB).glob("*.wav"))
    assert len(files) == 2
    assert (usb / SUB / "playlist.m3u8").is_file()
    assert (usb / SUB / "manifest.json").is_file()
    job = store.load(saved["plan_id"])
    assert job is not None and job["status"] == "complete"
    assert (usb / "sentinel.txt").read_text(encoding="utf-8") == "не трогать"


def test_repeat_export_does_not_duplicate(library, tmp_path, usb, service, store, capsys):
    plan_file = tmp_path / "plan.json"
    saved = saved_plan(library, plan_file)
    first = call(export_args(plan_file), capsys, service=service, store=store)
    second = call(export_args(plan_file), capsys, service=service, store=store)

    assert first[0] == 0 and second[0] == 0
    assert second[1]["verified_count"] == 2
    assert len(list((usb / SUB).glob("*.wav"))) == 2
    job = store.load(saved["plan_id"])
    assert job is not None and job["status"] == "complete"


# --- status / cancel / verify -------------------------------------------


def test_status_after_export(library, tmp_path, service, store, capsys):
    plan_file = tmp_path / "plan.json"
    saved = saved_plan(library, plan_file)
    call(export_args(plan_file), capsys, service=service, store=store)

    code, data, _ = call(
        parse("mixtape", "status", "--job", saved["plan_id"], "--json"),
        capsys,
        service=service,
        store=store,
    )

    assert code == 0
    assert data["job"] == saved["plan_id"]
    assert data["status"] == "complete"
    assert data["cancel_requested"] is False


def test_status_unknown_job(service, store, capsys):
    code, data, _ = call(
        parse("mixtape", "status", "--job", "no-such-job", "--json"),
        capsys,
        service=service,
        store=store,
    )

    assert code == 2
    assert data["error"] == "job_not_found"


def test_cancel_running_and_finished_job(service, store, capsys):
    store.save("mixtape-run", {"status": "running"})
    code, data, _ = call(
        parse("mixtape", "cancel", "--job", "mixtape-run", "--json"),
        capsys,
        service=service,
        store=store,
    )
    assert code == 0
    assert data["cancel_requested"] is True
    assert store.cancel_requested("mixtape-run") is True

    store.save("mixtape-done", {"status": "complete"})
    code, data, _ = call(
        parse("mixtape", "cancel", "--job", "mixtape-done", "--json"),
        capsys,
        service=service,
        store=store,
    )
    assert code == 0
    assert data["cancel_requested"] is False


def test_cancel_signal_reads_store_marker(store):
    store.save("mixtape-run", {"status": "running"})
    signal = CancelSignal(store, "mixtape-run")

    assert signal.is_set() is False
    store.request_cancel("mixtape-run")
    assert signal.is_set() is True


def test_verify_detects_corruption(library, tmp_path, usb, service, store, capsys):
    plan_file = tmp_path / "plan.json"
    saved = saved_plan(library, plan_file)
    call(export_args(plan_file), capsys, service=service, store=store)

    code, data, _ = call(
        parse("mixtape", "verify", "--job", saved["plan_id"], "--json"),
        capsys,
        service=service,
        store=store,
    )
    assert code == 0
    assert data["ok"] is True
    assert data["checked"] == 2

    [file] = sorted((usb / SUB).glob("*.wav"))[:1]
    file.write_bytes(b"corrupted")
    code, data, _ = call(
        parse("mixtape", "verify", "--job", saved["plan_id"], "--json"),
        capsys,
        service=service,
        store=store,
    )
    assert code == 1
    assert data["ok"] is False
    assert data["mismatches"]


def test_verify_requires_complete_status(library, tmp_path, usb, service, store, capsys):
    plan_file = tmp_path / "plan.json"
    saved = saved_plan(library, plan_file)
    call(export_args(plan_file), capsys, service=service, store=store)
    job = store.load(saved["plan_id"])
    assert job is not None
    job["status"] = "partial"
    job["items"][0]["status"] = "failed"
    job["items"][0]["error"] = "no_space"
    store.save(saved["plan_id"], job)

    code, data, _ = call(
        parse("mixtape", "verify", "--job", saved["plan_id"], "--json"),
        capsys,
        service=service,
        store=store,
    )

    assert code == 1
    assert data["ok"] is False
    assert data["status"] == "partial"
    assert data["complete"] is False
    assert data["checked"] == 1
    assert data["total"] == 2
    assert data["mismatches"] == [
        {"destination": job["items"][0]["destination"], "error": "not_verified:failed"}
    ]


def test_verify_counts_skipped_as_mismatch(library, tmp_path, usb, service, store, capsys):
    plan_file = tmp_path / "plan.json"
    saved = saved_plan(library, plan_file)
    call(export_args(plan_file), capsys, service=service, store=store)
    job = store.load(saved["plan_id"])
    assert job is not None
    job["status"] = "complete"
    job["items"][1]["status"] = "skipped"
    store.save(saved["plan_id"], job)

    code, data, _ = call(
        parse("mixtape", "verify", "--job", saved["plan_id"], "--json"),
        capsys,
        service=service,
        store=store,
    )

    assert code == 1
    assert data["ok"] is False
    assert [item["error"] for item in data["mismatches"]] == ["not_verified:skipped"]


# --- eject --------------------------------------------------------------


def test_eject_requires_confirm(service, store, capsys):
    code, data, _ = call(
        parse("mixtape", "eject", "--target", OBJECT, "--json"),
        capsys,
        service=service,
        store=store,
    )

    assert code == 2
    assert data["error"] == "confirmation_required"
    assert service.ejected == []


def test_eject_keeps_its_own_target_message(service, store, library, tmp_path, capsys):
    """Тексты CLI не меняются: у eject и export разная формулировка."""

    code, data, _ = call(
        parse(
            "mixtape", "eject", "--target", "/org/freedesktop/UDisks2/block_devices/sdz1", "--confirm", "--json"
        ),
        capsys,
        service=service,
        store=store,
    )
    assert code == 2
    assert data["error"] == "target_not_found"
    assert data["message"] == "Выбранный USB-том не найден"

    plan_file = tmp_path / "plan.json"
    saved_plan(library, plan_file)
    code, data, _ = call(
        export_args(plan_file, target="/org/freedesktop/UDisks2/block_devices/sdz1"),
        capsys,
        service=service,
        store=store,
    )
    assert code == 2
    assert data["message"] == "Выбранный USB-том не найден среди writable-томов"


def test_eject_confirmed(service, store, capsys):
    code, data, _ = call(
        parse("mixtape", "eject", "--target", OBJECT, "--confirm", "--json"),
        capsys,
        service=service,
        store=store,
    )

    assert code == 0
    assert data["ejected"] is True
    assert service.ejected == [OBJECT]
    assert service.list_removable_writable_filesystems() == ()


def test_export_marks_job_failed_on_writer_error(
    library, tmp_path, service, store, capsys, monkeypatch
):
    from neurowave.mixtape.export import ExportError, ExportErrorCode

    plan_file = tmp_path / "plan.json"
    saved = saved_plan(library, plan_file)

    def boom(*args, **kwargs):
        raise ExportError(ExportErrorCode.ENOSPC, "нет места")

    monkeypatch.setattr("neurowave.mixtape.service.SafeWriter.write", boom)
    code, data, _ = call(export_args(plan_file), capsys, service=service, store=store)

    assert code == 2
    assert data["error"] == ExportErrorCode.ENOSPC.value
    job = store.load(saved["plan_id"])
    assert job is not None and job["status"] == "failed"
    assert "нет места" in job["error"]


def test_eject_refuses_any_running_job_for_target(service, store, capsys):
    store.save("mixtape-other", {"status": "running", "object_path": OBJECT})

    code, data, _ = call(
        parse("mixtape", "eject", "--target", OBJECT, "--confirm", "--json"),
        capsys,
        service=service,
        store=store,
    )

    assert code == 2
    assert data["error"] == "job_running"
    assert service.ejected == []


def test_eject_refuses_running_job(service, store, capsys):
    store.save("mixtape-run", {"status": "running", "object_path": OBJECT})

    code, data, _ = call(
        parse(
            "mixtape", "eject", "--target", OBJECT, "--job", "mixtape-run", "--confirm", "--json"
        ),
        capsys,
        service=service,
        store=store,
    )

    assert code == 2
    assert data["error"] == "job_running"
    assert service.ejected == []


def test_eject_job_bound_to_object_path(service, store, capsys):
    store.save(
        "mixtape-other-target",
        {
            "status": "complete",
            "object_path": "/org/freedesktop/UDisks2/block_devices/sdc1",
        },
    )

    code, data, _ = call(
        parse(
            "mixtape",
            "eject",
            "--target",
            OBJECT,
            "--job",
            "mixtape-other-target",
            "--confirm",
            "--json",
        ),
        capsys,
        service=service,
        store=store,
    )

    assert code == 2
    assert data["error"] == "target_mismatch"
    assert service.ejected == []


def test_eject_job_without_object_path_is_refused(service, store, capsys):
    store.save("mixtape-legacy", {"status": "complete"})

    code, data, _ = call(
        parse(
            "mixtape",
            "eject",
            "--target",
            OBJECT,
            "--job",
            "mixtape-legacy",
            "--confirm",
            "--json",
        ),
        capsys,
        service=service,
        store=store,
    )

    assert code == 2
    assert data["error"] == "target_mismatch"
    assert service.ejected == []


def test_eject_with_matching_job(service, store, capsys):
    store.save("mixtape-done", {"status": "complete", "object_path": OBJECT})

    code, data, _ = call(
        parse(
            "mixtape",
            "eject",
            "--target",
            OBJECT,
            "--job",
            "mixtape-done",
            "--confirm",
            "--json",
        ),
        capsys,
        service=service,
        store=store,
    )

    assert code == 0
    assert data["ejected"] is True
    assert service.ejected == [OBJECT]
