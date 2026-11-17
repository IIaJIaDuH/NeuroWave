"""Headless CLI USB Mixtape: devices / plan / export / status / cancel / verify / eject.

Никакой обход подтверждения: ``export`` требует ``--confirm`` и план, в котором
пользователь явно подтвердил права на локальные файлы; ``eject`` требует
отдельного ``--confirm`` и не выполняется для незавершённой задачи. В тестах
сервис устройств и транскодер подменяются, реальная запись не выполняется.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from neurowave.config import Paths, atomic_write_json
from neurowave.local import LocalMusicAdapter, default_root
from neurowave.mixtape.devices import (
    UDisks2DeviceService,
    UsbDeviceError,
)
from neurowave.mixtape.errors import MixtapeError
from neurowave.mixtape.export import (
    ExportError,
    ExportReport,
    ExportStatus,
    SafeWriter,
)
from neurowave.mixtape.export.transcoder import FfmpegTranscoder
from neurowave.mixtape.jobs import JobStore
from neurowave.mixtape.models import PROFILES, MixtapeRequest
from neurowave.mixtape.planner import MixtapePlanner
from neurowave.mixtape.rights import LocalExportRights
from neurowave.mixtape.sources.local import LocalExportSource

PLAN_SCHEMA_VERSION = 1
MAX_PLAN_BYTES = 1_048_576


def build_mixtape_parser(subparsers: Any) -> None:
    mixtape = subparsers.add_parser(
        "mixtape", help="USB Mixtape: подборка, безопасная запись и проверка"
    )
    commands = mixtape.add_subparsers(dest="mixtape_command", required=True)

    devices = commands.add_parser("devices", help="съёмные writable-тома (UDisks2)")
    _add_json(devices)

    plan = commands.add_parser("plan", help="предпросмотр подборки, без записи")
    plan.add_argument("--library", metavar="DIR", help="папка музыки (по умолчанию ~/Music)")
    plan.add_argument("--profile", choices=sorted(PROFILES), default="road")
    plan.add_argument("--hours", type=float, default=4.0, help="длительность 1–12 часов")
    plan.add_argument(
        "--format",
        dest="output_format",
        choices=["original", "mp3", "flac"],
        default="original",
    )
    plan.add_argument("--seed", type=int, default=0)
    plan.add_argument("--max-bytes", dest="max_bytes", type=int, default=None)
    plan.add_argument("--exclude-artist", dest="exclude_artists", action="append", default=[])
    plan.add_argument("--exclude-track", dest="exclude_tracks", action="append", default=[])
    plan.add_argument(
        "--confirm-local-rights",
        dest="confirm_local_rights",
        action="store_true",
        help="подтвердить право копировать файлы своей локальной библиотеки",
    )
    plan.add_argument("--save", metavar="FILE", help="сохранить план для export")
    _add_json(plan)

    export = commands.add_parser("export", help="записать сохранённый план на USB")
    export.add_argument("--plan", dest="plan_file", required=True, metavar="FILE")
    export.add_argument("--target", required=True, metavar="OBJECT", help="UDisks2 object path")
    export.add_argument("--confirm", action="store_true", help="явное согласие на запись")
    _add_json(export)

    status = commands.add_parser("status", help="статус задачи")
    status.add_argument("--job", required=True)
    _add_json(status)

    cancel = commands.add_parser("cancel", help="запросить отмену задачи")
    cancel.add_argument("--job", required=True)
    _add_json(cancel)

    verify = commands.add_parser("verify", help="проверить записанные файлы по sha256")
    verify.add_argument("--job", required=True)
    _add_json(verify)

    eject = commands.add_parser("eject", help="безопасно размонтировать том")
    eject.add_argument("--target", required=True, metavar="OBJECT")
    eject.add_argument("--job", help="не извлекать, пока задача не завершена")
    eject.add_argument("--confirm", action="store_true", help="явное согласие на извлечение")
    _add_json(eject)


def _add_json(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", dest="mixtape_json", action="store_true", help="JSON-вывод")


def run(
    args: argparse.Namespace,
    *,
    device_service: Any | None = None,
    transcoder: Any | None = None,
    job_store: JobStore | None = None,
) -> int:
    handlers = {
        "devices": _cmd_devices,
        "plan": _cmd_plan,
        "export": _cmd_export,
        "status": _cmd_status,
        "cancel": _cmd_cancel,
        "verify": _cmd_verify,
        "eject": _cmd_eject,
    }
    handler = handlers.get(getattr(args, "mixtape_command", ""))
    if handler is None:
        return _fail(args, "unknown_command", "Неизвестная команда mixtape")
    try:
        return handler(
            args,
            device_service=device_service,
            transcoder=transcoder,
            job_store=job_store,
        )
    except (ExportError, UsbDeviceError, MixtapeError, ValueError, OSError) as error:
        code = getattr(error, "code", None)
        code = getattr(code, "value", code) or "error"
        return _fail(args, str(code), str(error))


# --- команды -------------------------------------------------------------


def _cmd_devices(args, *, device_service=None, transcoder=None, job_store=None) -> int:
    service = device_service or UDisks2DeviceService()
    devices = service.list_removable_writable_filesystems()
    payload = {
        "ok": True,
        "devices": [
            {
                "object_path": device.udisks_object_path,
                "block_device": device.block_device,
                "mountpoint": str(device.mountpoint),
                "filesystem": device.filesystem_type,
                "uuid": device.filesystem_uuid,
                "removable": device.removable,
                "writable": device.writable,
                "capacity_bytes": device.capacity_bytes,
                "free_bytes": device.free_bytes,
            }
            for device in devices
        ],
    }
    lines = [f"Найдено томов: {len(devices)}"] + [
        f"- {device.mountpoint} · {device.filesystem_type} · "
        f"{_gb(device.free_bytes)} свободно · {device.udisks_object_path}"
        for device in devices
    ]
    return _emit(args, payload, lines)


def _cmd_plan(args, *, device_service=None, transcoder=None, job_store=None) -> int:
    library = Path(args.library).expanduser() if args.library else default_root()
    if not library.is_dir():
        return _fail(args, "library_missing", f"Папка музыки не найдена: {library}")
    tracks = LocalMusicAdapter(library).tracks()
    confirmed = bool(getattr(args, "confirm_local_rights", False))
    source = LocalExportSource(library, rights=LocalExportRights(confirmed=confirmed))
    request = _request(args)
    plan = MixtapePlanner([source]).plan(request, tracks)
    payload: dict[str, Any] = {
        "ok": True,
        "library": str(library.resolve()),
        "rights_confirmed": confirmed,
        "tracks_found": len(tracks),
        "plan": plan.to_dict(),
    }
    if getattr(args, "save", None):
        path = Path(args.save).expanduser().resolve()
        _save_plan(path, plan, library, confirmed)
        payload["saved_to"] = str(path)
    lines = [
        f"План {plan.plan_id}: {len(plan.items)} файлов, "
        f"{_duration(plan.estimated_duration_ms)} из {_duration(request.target_duration_ms)}",
        f"Объём: {_mb(plan.estimated_bytes)} · кандидатов: {plan.candidate_count} · "
        f"с правом на файл: {plan.eligible_count}",
    ]
    if plan.shortage_ms:
        lines.append(f"Нехватка: {_duration(plan.shortage_ms)} — подборка неполная")
    if not confirmed:
        lines.append(
            "Права на локальные файлы не подтверждены: для экспорта добавьте "
            "--confirm-local-rights"
        )
    for skipped in plan.skipped[:10]:
        lines.append(f"  пропуск {skipped.track_key}: {skipped.reason.value}")
    if getattr(args, "save", None):
        lines.append(f"Сохранено: {args.save}")
    return _emit(args, payload, lines)


def _cmd_export(args, *, device_service=None, transcoder=None, job_store=None) -> int:
    if not args.confirm:
        return _fail(
            args,
            "confirmation_required",
            "Нужен явный --confirm: без него запись не начинается",
        )
    saved = _load_plan(Path(args.plan_file).expanduser())
    if not saved.get("rights_confirmed"):
        return _fail(
            args,
            "rights_not_confirmed",
            "План создан без --confirm-local-rights: перепланируйте и подтвердите права",
        )
    library = Path(str(saved["library"]))
    if not library.is_dir():
        return _fail(args, "library_missing", f"Папка музыки не найдена: {library}")
    request = _request_from_saved(saved)
    tracks = LocalMusicAdapter(library).tracks()
    source = LocalExportSource(library, rights=LocalExportRights(confirmed=True))
    plan = MixtapePlanner([source]).plan(request, tracks)
    if plan.plan_id != saved.get("plan_id"):
        return _fail(
            args,
            "plan_changed",
            "Библиотека изменилась после плана: постройте новый план и подтвердите его",
        )

    service = device_service or UDisks2DeviceService()
    devices = service.list_removable_writable_filesystems()
    identity = next(
        (device for device in devices if device.udisks_object_path == args.target), None
    )
    if identity is None:
        return _fail(
            args, "target_not_found", "Выбранный USB-том не найден среди writable-томов"
        )
    identity = service.revalidate(identity)

    store = _store(job_store)
    job_id = plan.plan_id
    store.clear_cancel(job_id)
    store.save(
        job_id,
        {
            "status": "running",
            "plan_id": plan.plan_id,
            "target": str(identity.mountpoint),
            "object_path": identity.udisks_object_path,
            "library": str(library),
        },
    )
    writer = SafeWriter(
        identity.mountpoint,
        device_service=service,
        expected_device=identity,
        transcoder=transcoder if transcoder is not None else FfmpegTranscoder(),
    )
    try:
        report = writer.write(
            plan,
            source.list_eligible(tracks),
            cancel=_CancelSignal(store, job_id),
        )
    except (ExportError, OSError, UsbDeviceError) as error:
        store.save(
            job_id,
            {
                "status": "failed",
                "plan_id": plan.plan_id,
                "target": str(identity.mountpoint),
                "object_path": identity.udisks_object_path,
                "library": str(library),
                "error": str(error),
            },
        )
        raise
    job = _report_job(report, identity, library)
    store.save(job_id, job)
    payload = {
        "ok": report.status in {ExportStatus.COMPLETE, ExportStatus.PARTIAL},
        "job": job_id,
        **job,
    }
    lines = [
        f"Задача {job_id}: {report.status.value}",
        f"Проверено файлов: {report.verified_count} · ошибок: {report.failed_count} · "
        f"пропущено: {report.skipped_count}",
    ]
    if report.playlist_path:
        lines.append(f"Плейлист: {report.playlist_path}")
    if report.playlist_error:
        lines.append(f"Плейлист не записан: {report.playlist_error}")
    code = 0 if payload["ok"] else 1
    return _emit(args, payload, lines, code=code)


def _cmd_status(args, *, device_service=None, transcoder=None, job_store=None) -> int:
    store = _store(job_store)
    job = store.load(args.job)
    if job is None:
        return _fail(args, "job_not_found", f"Задача не найдена: {args.job}")
    cancel = store.cancel_requested(args.job)
    payload = {"ok": True, "job": args.job, "cancel_requested": cancel, **job}
    lines = [
        f"Задача {args.job}: {job.get('status', '?')}",
        f"Цель: {job.get('target', '—')}",
        f"Файлов проверено: {job.get('verified_count', 0)}",
    ]
    return _emit(args, payload, lines)


def _cmd_cancel(args, *, device_service=None, transcoder=None, job_store=None) -> int:
    store = _store(job_store)
    job = store.load(args.job)
    if job is None:
        return _fail(args, "job_not_found", f"Задача не найдена: {args.job}")
    if job.get("status") != "running":
        payload = {"ok": True, "job": args.job, "cancel_requested": False, "status": job.get("status")}
        return _emit(args, payload, [f"Задача уже завершена: {job.get('status')}"])
    store.request_cancel(args.job)
    return _emit(
        args,
        {"ok": True, "job": args.job, "cancel_requested": True},
        [f"Отмена запрошена для {args.job}: writer остановится между чанками"],
    )


def _cmd_verify(args, *, device_service=None, transcoder=None, job_store=None) -> int:
    job = _store(job_store).load(args.job)
    if job is None:
        return _fail(args, "job_not_found", f"Задача не найдена: {args.job}")
    status = str(job.get("status") or "")
    target = Path(str(job.get("target") or ""))
    items = list(job.get("items") or [])
    mismatches: list[dict[str, str]] = []
    checked = 0
    for item in items:
        destination = str(item.get("destination") or "")
        item_status = str(item.get("status") or "")
        if item_status != "verified":
            mismatches.append(
                {
                    "destination": destination,
                    "error": f"not_verified:{item_status or 'unknown'}",
                }
            )
            continue
        checked += 1
        relative = Path(destination)
        if not destination or relative.is_absolute() or ".." in relative.parts:
            mismatches.append({"destination": destination, "error": "unsafe_path"})
            continue
        path = target / relative
        try:
            if path.is_symlink() or not path.is_file():
                raise OSError("файл отсутствует")
            digest = _hash_file(path)
            if digest != item.get("sha256") or path.stat().st_size != int(
                item.get("size_bytes") or 0
            ):
                mismatches.append({"destination": destination, "error": "hash_mismatch"})
        except OSError as error:
            mismatches.append({"destination": destination, "error": str(error)})
    complete = status == "complete"
    ok = complete and bool(items) and not mismatches
    payload = {
        "ok": ok,
        "job": args.job,
        "status": status,
        "complete": complete,
        "checked": checked,
        "total": len(items),
        "mismatches": mismatches,
    }
    lines = [
        f"Статус задачи: {status or '—'}",
        f"Проверено файлов: {checked} из {len(items)}",
    ]
    if not complete:
        lines.append("Задача не завершена: проверка требует статус complete")
    lines += [f"  {item['destination']}: {item['error']}" for item in mismatches]
    lines.append("Итог: OK" if ok else "Итог: есть расхождения")
    return _emit(args, payload, lines, code=0 if ok else 1)


def _cmd_eject(args, *, device_service=None, transcoder=None, job_store=None) -> int:
    if not args.confirm:
        return _fail(args, "confirmation_required", "Нужен явный --confirm для извлечения")
    store = _store(job_store)
    if args.job:
        job = store.load(args.job)
        if job is None:
            return _fail(args, "job_not_found", f"Задача не найдена: {args.job}")
        if job.get("object_path") != args.target:
            return _fail(
                args,
                "target_mismatch",
                f"Задача {args.job} относится к другому тому: извлечение запрещено",
            )
        if job.get("status") == "running":
            return _fail(args, "job_running", "Задача ещё пишет: извлечение запрещено")
    else:
        for job_id in store.list_ids():
            job = store.load(job_id)
            if job and job.get("status") == "running" and job.get("object_path") == args.target:
                return _fail(
                    args,
                    "job_running",
                    f"Задача {job_id} ещё пишет: извлечение запрещено",
                )
    service = device_service or UDisks2DeviceService()
    devices = service.list_removable_writable_filesystems()
    identity = next(
        (device for device in devices if device.udisks_object_path == args.target), None
    )
    if identity is None:
        return _fail(args, "target_not_found", "Выбранный USB-том не найден")
    identity = service.revalidate(identity)
    service.safely_eject(identity)
    return _emit(
        args,
        {"ok": True, "object_path": identity.udisks_object_path, "ejected": True},
        [f"Том отключён: {identity.mountpoint}"],
    )


# --- вспомогательное -----------------------------------------------------


class _CancelSignal:
    def __init__(self, store: JobStore, job_id: str) -> None:
        self._store = store
        self._job_id = job_id

    def is_set(self) -> bool:
        return self._store.cancel_requested(self._job_id)


def _request(args) -> MixtapeRequest:
    return MixtapeRequest(
        profile=args.profile,
        target_duration_ms=int(float(args.hours) * 3_600_000),
        max_bytes=args.max_bytes,
        output_format=args.output_format,
        seed=args.seed,
        excluded_track_keys=frozenset(getattr(args, "exclude_tracks", ()) or ()),
        excluded_artists=tuple(getattr(args, "exclude_artists", ()) or ()),
    )


def _request_from_saved(saved: dict[str, Any]) -> MixtapeRequest:
    raw = saved.get("request")
    if not isinstance(raw, dict):
        raise ValueError("В плане нет параметров запроса")
    try:
        return MixtapeRequest(**raw)
    except (TypeError, ValueError) as error:
        raise ValueError("Некорректные параметры плана") from error


def _save_plan(path: Path, plan, library: Path, confirmed: bool) -> None:
    payload = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "plan_id": plan.plan_id,
        "library": str(library.resolve()),
        "rights_confirmed": bool(confirmed),
        "request": plan.request.to_dict(),
        "items": [
            {
                "track_key": item.track_key,
                "source_asset_id": item.source_asset_id,
                "destination": item.destination_relative,
            }
            for item in plan.items
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write_json(path, payload)


def _load_plan(path: Path) -> dict[str, Any]:
    try:
        stat = path.lstat()
    except FileNotFoundError as error:
        raise ValueError(f"План не найден: {path}") from error
    if path.is_symlink() or not path.is_file() or stat.st_size > MAX_PLAN_BYTES:
        raise ValueError("Файл плана повреждён")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Файл плана не читается") from error
    if not isinstance(data, dict) or data.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise ValueError("Неизвестная схема плана")
    if not isinstance(data.get("library"), str) or not data["library"]:
        raise ValueError("В плане нет библиотеки")
    if not isinstance(data.get("plan_id"), str) or not data["plan_id"]:
        raise ValueError("В плане нет plan_id")
    return data


def _report_job(report: ExportReport, identity, library: Path) -> dict[str, Any]:
    return {
        "status": report.status.value,
        "plan_id": report.plan_id,
        "target": str(identity.mountpoint),
        "object_path": identity.udisks_object_path,
        "library": str(library),
        "manifest": report.manifest_path,
        "playlist": report.playlist_path,
        "playlist_error": report.playlist_error,
        "verified_count": report.verified_count,
        "failed_count": report.failed_count,
        "skipped_count": report.skipped_count,
        "cancelled": report.cancelled,
        "items": [
            {
                "track_key": item.track_key,
                "destination": item.destination,
                "status": item.status.value,
                "size_bytes": item.size_bytes,
                "sha256": item.sha256,
                "error": item.error,
            }
            for item in report.items
        ],
    }


def _store(job_store: JobStore | None) -> JobStore:
    if job_store is not None:
        return job_store
    root = Paths.resolve().ensure().data_dir / "mixtape" / "jobs"
    return JobStore(root)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.close(fd)
    return digest.hexdigest()


def _emit(args, payload: dict[str, Any], lines: Sequence[str], *, code: int = 0) -> int:
    if _json(args):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for line in lines:
            print(line)
    return code


def _fail(args, code: str, message: str) -> int:
    if _json(args):
        print(json.dumps({"ok": False, "error": code, "message": message}, ensure_ascii=False))
    else:
        print(f"Ошибка: {message}", file=sys.stderr)
    return 2


def _json(args) -> bool:
    return bool(getattr(args, "mixtape_json", False) or getattr(args, "json", False))


def _duration(ms: int) -> str:
    total = max(0, int(ms)) // 1000
    hours, rem = divmod(total, 3600)
    minutes = rem // 60
    return f"{hours} ч {minutes:02d} мин" if hours else f"{minutes} мин"


def _gb(value: int) -> str:
    return f"{max(0, int(value)) / (1024 ** 3):.1f} ГБ"


def _mb(value: int) -> str:
    return f"{max(0, int(value)) / (1024 ** 2):.1f} МБ"
