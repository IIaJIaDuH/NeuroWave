"""Мост между экраном USB Mixtape и ядром: без Qt, без блокировки UI.

Правила, ради которых мост отделён от виджетов:

* долгие операции (план, запись, проверка, извлечение) идут в отдельном
  потоке, а GUI-поток только читает неизменяемые снимки :meth:`MixtapeBridge.snapshot`;
* ни один путь не обходит согласие: экспорт требует явного ``confirm``,
  подтверждения прав на локальные файлы и выбранного object path;
* прогресс приходит из writer'а по байтам и превращается в проценты, скорость и
  оценку остатка; плеер и mpv мост не трогает вообще.

Все ошибки остаются в снимке состояния (§код + текст), чтобы окно показывало
причину, а не падало.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from neurowave.local import default_root
from neurowave.mixtape.devices import UsbDeviceError
from neurowave.mixtape.errors import MixtapeError
from neurowave.mixtape.export import (
    ExportError,
    ExportStatus,
    ProgressUpdate,
    VerificationReport,
)
from neurowave.mixtape.jobs import JobStore
from neurowave.mixtape.models import MixtapePlan, MixtapeRequest
from neurowave.mixtape.service import MixtapeService, MixtapeServiceError

MAX_SKIPPED_IN_PREVIEW = 200


class BridgeStatus(str, Enum):
    IDLE = "idle"
    DEVICES = "devices"
    PLANNING = "planning"
    PLANNED = "planned"
    EXPORTING = "exporting"
    VERIFYING = "verifying"
    EJECTING = "ejecting"
    DONE = "done"
    PARTIAL = "partial"
    VERIFIED = "verified"
    CANCELLED = "cancelled"
    FAILED = "failed"


_EXPORT_STATUS_TO_BRIDGE = {
    ExportStatus.COMPLETE: BridgeStatus.DONE,
    ExportStatus.PARTIAL: BridgeStatus.PARTIAL,
    ExportStatus.FAILED: BridgeStatus.FAILED,
    ExportStatus.CANCELLED: BridgeStatus.CANCELLED,
}


@dataclass(frozen=True)
class DeviceView:
    object_path: str
    label: str
    mountpoint: str
    filesystem: str
    capacity_bytes: int
    free_bytes: int
    removable: bool
    writable: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "object_path": self.object_path,
            "label": self.label,
            "mountpoint": self.mountpoint,
            "filesystem": self.filesystem,
            "capacity_bytes": self.capacity_bytes,
            "free_bytes": self.free_bytes,
            "removable": self.removable,
            "writable": self.writable,
        }


@dataclass(frozen=True)
class PreviewTrack:
    title: str
    artists: str
    destination: str
    operation: str
    estimated_bytes: int
    duration_ms: int
    reason: str


@dataclass(frozen=True)
class PreviewSkip:
    title: str
    reason: str
    detail: str


@dataclass(frozen=True)
class PlanPreview:
    plan_id: str
    request: MixtapeRequest
    tracks: tuple[PreviewTrack, ...]
    skipped: tuple[PreviewSkip, ...]
    candidate_count: int
    eligible_count: int
    estimated_bytes: int
    estimated_duration_ms: int
    target_duration_ms: int
    shortage_ms: int
    rights_confirmed: bool

    @property
    def item_count(self) -> int:
        return len(self.tracks)

    @property
    def is_complete(self) -> bool:
        return self.shortage_ms == 0

    def skip_reasons(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for skip in self.skipped:
            counts[skip.reason] = counts.get(skip.reason, 0) + 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "rights_confirmed": self.rights_confirmed,
            "candidate_count": self.candidate_count,
            "eligible_count": self.eligible_count,
            "item_count": self.item_count,
            "estimated_bytes": self.estimated_bytes,
            "estimated_duration_ms": self.estimated_duration_ms,
            "target_duration_ms": self.target_duration_ms,
            "shortage_ms": self.shortage_ms,
            "is_complete": self.is_complete,
            "tracks": [
                {
                    "title": track.title,
                    "artists": track.artists,
                    "destination": track.destination,
                    "operation": track.operation,
                    "estimated_bytes": track.estimated_bytes,
                    "duration_ms": track.duration_ms,
                    "reason": track.reason,
                }
                for track in self.tracks
            ],
            "skipped": [
                {"title": skip.title, "reason": skip.reason, "detail": skip.detail}
                for skip in self.skipped
            ],
        }


@dataclass(frozen=True)
class ExportProgress:
    percent: float
    bytes_done: int
    bytes_total: int
    items_done: int
    items_total: int
    current: str
    speed_bps: float
    eta_s: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "percent": self.percent,
            "bytes_done": self.bytes_done,
            "bytes_total": self.bytes_total,
            "items_done": self.items_done,
            "items_total": self.items_total,
            "current": self.current,
            "speed_bps": self.speed_bps,
            "eta_s": self.eta_s,
        }


@dataclass(frozen=True)
class ExportSummary:
    job_id: str
    status: str
    verified: int
    failed: int
    skipped: int
    target: str
    playlist: str = ""
    playlist_error: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "verified": self.verified,
            "failed": self.failed,
            "skipped": self.skipped,
            "target": self.target,
            "playlist": self.playlist,
            "playlist_error": self.playlist_error,
            "error": self.error,
        }


@dataclass(frozen=True)
class BridgeState:
    """Всё, что нужно экрану для одной перерисовки."""

    status: BridgeStatus = BridgeStatus.IDLE
    message: str = ""
    error: str = ""
    error_code: str = ""
    devices: tuple[DeviceView, ...] = ()
    selected: str = ""
    library: str = ""
    rights_confirmed: bool = False
    preview: PlanPreview | None = None
    progress: ExportProgress | None = None
    summary: ExportSummary | None = None
    verification: VerificationReport | None = None
    busy: bool = False

    @property
    def can_plan(self) -> bool:
        return not self.busy and bool(self.library)

    @property
    def can_export(self) -> bool:
        return (
            not self.busy
            and self.preview is not None
            and self.preview.rights_confirmed
            and bool(self.selected)
            and self.preview.item_count > 0
        )

    @property
    def can_cancel(self) -> bool:
        return self.busy and self.status is BridgeStatus.EXPORTING

    @property
    def can_verify(self) -> bool:
        return (
            not self.busy
            and self.summary is not None
            and self.summary.status in {ExportStatus.COMPLETE.value, ExportStatus.PARTIAL.value}
        )

    @property
    def can_eject(self) -> bool:
        return not self.busy and bool(self.selected)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "message": self.message,
            "error": self.error,
            "error_code": self.error_code,
            "devices": [device.to_dict() for device in self.devices],
            "selected": self.selected,
            "library": self.library,
            "rights_confirmed": self.rights_confirmed,
            "preview": self.preview.to_dict() if self.preview else None,
            "progress": self.progress.to_dict() if self.progress else None,
            "summary": self.summary.to_dict() if self.summary else None,
            "verification": (
                self.verification.to_dict() if self.verification else None
            ),
            "busy": self.busy,
            "can_plan": self.can_plan,
            "can_export": self.can_export,
            "can_cancel": self.can_cancel,
            "can_verify": self.can_verify,
            "can_eject": self.can_eject,
        }


def _device_view(device: Any) -> DeviceView:
    mountpoint = str(device.mountpoint)
    label = Path(mountpoint).name or mountpoint
    return DeviceView(
        object_path=device.udisks_object_path,
        label=label,
        mountpoint=mountpoint,
        filesystem=device.filesystem_type,
        capacity_bytes=int(device.capacity_bytes),
        free_bytes=int(device.free_bytes),
        removable=bool(device.removable),
        writable=bool(device.writable),
    )


def _preview(plan: MixtapePlan, rights_confirmed: bool) -> PlanPreview:
    return PlanPreview(
        plan_id=plan.plan_id,
        request=plan.request,
        tracks=tuple(
            PreviewTrack(
                title=item.title,
                artists=", ".join(item.artists),
                destination=item.destination_relative,
                operation=item.operation.value,
                estimated_bytes=int(item.estimated_bytes),
                duration_ms=int(item.duration_ms),
                reason=item.reason,
            )
            for item in plan.items
        ),
        skipped=tuple(
            PreviewSkip(title=skip.title, reason=skip.reason.value, detail=skip.detail)
            for skip in plan.skipped[:MAX_SKIPPED_IN_PREVIEW]
        ),
        candidate_count=int(plan.candidate_count),
        eligible_count=int(plan.eligible_count),
        estimated_bytes=int(plan.estimated_bytes),
        estimated_duration_ms=int(plan.estimated_duration_ms),
        target_duration_ms=int(plan.request.target_duration_ms),
        shortage_ms=int(plan.shortage_ms),
        rights_confirmed=bool(rights_confirmed),
    )


class MixtapeBridge:
    """Команды экрана и состояние; вся работа с носителем — в фоновом потоке."""

    def __init__(
        self,
        *,
        device_service: Any | None = None,
        transcoder: Any | None = None,
        job_store: JobStore | None = None,
        library: Path | str | None = None,
        track_source: Callable[[Path], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._mixtape = MixtapeService(
            device_service=device_service,
            transcoder=transcoder,
            job_store=job_store,
            track_source=track_source,
        )
        self._library = Path(library).expanduser() if library else default_root()
        self._clock = clock
        self._lock = threading.RLock()
        self._worker: threading.Thread | None = None
        self._status = BridgeStatus.IDLE
        self._message = "Готово"
        self._error = ""
        self._error_code = ""
        self._devices: tuple[DeviceView, ...] = ()
        self._selected = ""
        self._rights_confirmed = False
        self._preview: PlanPreview | None = None
        self._progress: ExportProgress | None = None
        self._summary: ExportSummary | None = None
        self._verification: VerificationReport | None = None
        self._job_id = ""
        self._export_started = 0.0
        self._plan_request = MixtapeRequest()
        self._closed = False

    # --- состояние ------------------------------------------------------
    @property
    def job_id(self) -> str:
        with self._lock:
            return self._job_id

    @property
    def library(self) -> Path:
        with self._lock:
            return self._library

    def snapshot(self) -> BridgeState:
        with self._lock:
            return BridgeState(
                status=self._status,
                message=self._message,
                error=self._error,
                error_code=self._error_code,
                devices=self._devices,
                selected=self._selected,
                library=str(self._library),
                rights_confirmed=self._rights_confirmed,
                preview=self._preview,
                progress=self._progress,
                summary=self._summary,
                verification=self._verification,
                busy=self._busy(),
            )

    def _busy(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def wait(self, timeout: float = 10.0) -> bool:
        """Дождаться фоновой операции (для тестов и корректного выхода)."""

        worker = self._worker
        if worker is None:
            return True
        worker.join(timeout)
        return not worker.is_alive()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            job_id = self._job_id if self._status is BridgeStatus.EXPORTING else ""
        if job_id:
            self._mixtape.job_store.request_cancel(job_id)
        self.wait(5.0)

    # --- устройства -----------------------------------------------------
    def refresh_devices(self) -> tuple[DeviceView, ...]:
        """Список writable-томов; ошибка сервиса остаётся в снимке."""

        service = self._mixtape.device_service
        with self._lock:
            if self._busy():
                return self._devices
            self._status = BridgeStatus.DEVICES
        return self._collect_devices(service, status=BridgeStatus.PLANNED)

    def _collect_devices(
        self,
        service: Any,
        *,
        status: BridgeStatus,
        message: str = "",
        force_status: bool = False,
    ) -> tuple[DeviceView, ...]:
        try:
            devices = tuple(
                _device_view(item) for item in service.list_removable_writable_filesystems()
            )
        except (UsbDeviceError, OSError) as error:
            with self._lock:
                self._devices = ()
                self._status = BridgeStatus.IDLE
                self._fail(error, getattr(getattr(error, "code", None), "value", "device_error"))
            return ()
        with self._lock:
            self._devices = devices
            objects = {device.object_path for device in devices}
            if self._selected not in objects:
                self._selected = devices[0].object_path if devices else ""
            if force_status or not self._busy():
                self._status = status if self._preview or force_status else BridgeStatus.IDLE
            self._message = message or f"Найдено томов: {len(devices)}"
            self._clear_error()
        return devices

    def select_device(self, object_path: str) -> None:
        with self._lock:
            self._selected = str(object_path or "")

    def set_library(self, library: Path | str) -> None:
        with self._lock:
            self._library = Path(library).expanduser()

    def set_rights_confirmed(self, confirmed: bool) -> None:
        with self._lock:
            self._rights_confirmed = bool(confirmed)

    # --- планирование ---------------------------------------------------
    def request_plan(
        self,
        *,
        library: Path | str | None = None,
        request: MixtapeRequest | None = None,
        rights_confirmed: bool | None = None,
    ) -> bool:
        """Построить план в фоне. Без подтверждения прав план пуст."""

        with self._lock:
            if self._closed or self._busy():
                return False
            if library is not None:
                self._library = Path(library).expanduser()
            if request is not None:
                self._plan_request = request
            if rights_confirmed is not None:
                self._rights_confirmed = bool(rights_confirmed)
            rights = self._rights_confirmed
            root = self._library
            params = self._plan_request
            self._preview = None
            self._summary = None
            self._verification = None
            self._progress = None
            self._status = BridgeStatus.PLANNING
            self._message = "Строим план…"
            self._clear_error()
            if not rights:
                self._status = BridgeStatus.IDLE
                self._error_code = "rights_not_confirmed"
                self._error = (
                    "Подтвердите право копировать файлы своей библиотеки: "
                    "без этого план не строится"
                )
                return False
            if not root.is_dir():
                self._status = BridgeStatus.FAILED
                self._error_code = "library_missing"
                self._error = f"Папка музыки не найдена: {root}"
                return False
            self._start(self._run_plan, root, params, rights)
            return True

    def _run_plan(self, root: Path, request: MixtapeRequest, rights: bool) -> None:
        try:
            build = self._mixtape.plan(root, request, rights_confirmed=rights)
        except (MixtapeError, OSError, ValueError) as error:
            with self._lock:
                self._status = BridgeStatus.FAILED
                self._fail(error, getattr(error, "code", "plan_failed") or "plan_failed")
            return
        plan = build.plan
        with self._lock:
            self._preview = _preview(plan, rights)
            self._status = BridgeStatus.PLANNED
            self._message = (
                f"План {plan.plan_id}: {len(plan.items)} файлов, "
                f"{len(plan.skipped)} пропусков"
            )
            self._clear_error()

    # --- экспорт --------------------------------------------------------
    def start_export(self, *, object_path: str | None = None, confirm: bool = False) -> bool:
        """Запись только после явного подтверждения и выбранного тома."""

        with self._lock:
            if object_path is not None:
                self._selected = str(object_path)
            if not confirm:
                self._error_code = "confirmation_required"
                self._error = "Нужно явное подтверждение записи: без него ничего не пишется"
                return False
            if self._closed or self._busy():
                return False
            preview = self._preview
            if preview is None:
                self._error_code = "plan_required"
                self._error = "Сначала постройте план"
                return False
            if not preview.rights_confirmed or not self._rights_confirmed:
                self._error_code = "rights_not_confirmed"
                self._error = "План построен без подтверждения прав: перепланируйте и подтвердите"
                return False
            if not self._selected:
                self._error_code = "target_not_found"
                self._error = "USB-том не выбран"
                return False
            root = self._library
            request = preview.request
            expected_plan_id = preview.plan_id
            object_path = self._selected
            self._progress = ExportProgress(
                percent=0.0,
                bytes_done=0,
                bytes_total=preview.estimated_bytes,
                items_done=0,
                items_total=preview.item_count,
                current="",
                speed_bps=0.0,
                eta_s=None,
            )
            self._summary = None
            self._verification = None
            self._status = BridgeStatus.EXPORTING
            self._message = "Записываем…"
            self._export_started = self._clock()
            self._clear_error()
            self._start(self._run_export, root, request, expected_plan_id, object_path)
            return True

    def _run_export(
        self,
        root: Path,
        request: MixtapeRequest,
        expected_plan_id: str,
        object_path: str,
    ) -> None:
        try:
            build = self._mixtape.plan(root, request, rights_confirmed=True)
            outcome = self._mixtape.export(
                build,
                object_path=object_path,
                expected_plan_id=expected_plan_id,
                progress=self._on_progress,
                on_job_started=self._set_job_id,
            )
        except (MixtapeError, ExportError, UsbDeviceError, OSError, ValueError) as error:
            code = getattr(error, "code", None)
            code = getattr(code, "value", code) or "export_failed"
            self._abort_export(error, str(code))
            return

        report = outcome.report
        record = dict(outcome.record)
        plan_items = len(build.plan.items)
        summary = ExportSummary(
            job_id=outcome.job_id,
            status=report.status.value,
            verified=report.verified_count,
            failed=report.failed_count,
            skipped=report.skipped_count,
            target=str(outcome.identity.mountpoint),
            playlist=report.playlist_path,
            playlist_error=report.playlist_error,
        )
        with self._lock:
            self._summary = summary
            done = sum(int(item.get("size_bytes") or 0) for item in record["items"])
            total = max(done, int(build.plan.estimated_bytes), 1)
            self._progress = ExportProgress(
                percent=round(min(1.0, done / total) * 100.0, 1),
                bytes_done=done,
                bytes_total=total,
                items_done=report.verified_count,
                items_total=plan_items,
                current="",
                speed_bps=self._speed(),
                eta_s=0.0,
            )
            # USB-FIX-04: GUI повторяет фактический итог, а не «всё хорошо».
            self._status = _EXPORT_STATUS_TO_BRIDGE.get(report.status, BridgeStatus.FAILED)
            counts = (
                f"Записано файлов: {report.verified_count} из {plan_items}"
                f" · ошибок: {report.failed_count}, пропущено: {report.skipped_count}"
            )
            suffix = {
                ExportStatus.COMPLETE: "",
                ExportStatus.PARTIAL: " · часть файлов не записана",
                ExportStatus.FAILED: " · экспорт не завершён",
                ExportStatus.CANCELLED: " · отменено",
            }[report.status]
            self._message = counts + suffix
            self._clear_error()

    def _set_job_id(self, job_id: str) -> None:
        """Сервис сообщает id задачи до начала записи — для кнопки «Отмена»."""

        with self._lock:
            self._job_id = job_id

    def request_cancel(self) -> bool:
        with self._lock:
            if self._status is not BridgeStatus.EXPORTING or not self._job_id:
                return False
            job_id = self._job_id
            self._message = "Отмена запрошена: writer остановится между чанками"
        self._mixtape.job_store.request_cancel(job_id)
        return True

    def _on_progress(self, update: ProgressUpdate) -> None:
        now = self._clock()
        with self._lock:
            started = self._export_started or now
            elapsed = max(1e-6, now - started)
            done = max(0, int(update.bytes_done))
            total = max(0, int(update.bytes_total))
            speed = done / elapsed if done else 0.0
            remaining = max(0, total - done)
            self._progress = ExportProgress(
                percent=round(min(1.0, done / total) * 100.0, 1) if total else 0.0,
                bytes_done=done,
                bytes_total=total,
                items_done=int(update.items_done),
                items_total=int(update.total),
                current=update.destination,
                speed_bps=round(speed, 1),
                eta_s=round(remaining / speed, 1) if speed > 0 and remaining else 0.0,
            )

    def _speed(self) -> float:
        progress = self._progress
        return progress.speed_bps if progress else 0.0

    def _abort_export(self, error: BaseException, code: str) -> None:
        with self._lock:
            self._status = BridgeStatus.FAILED
            self._fail(error, code)

    # --- проверка и извлечение -----------------------------------------
    def request_verify(self) -> bool:
        """Проверить записанное; запускается только по завершённой задаче."""

        with self._lock:
            if self._closed or self._busy():
                return False
            if not self._job_id:
                self._error_code = "job_not_found"
                self._error = "Нет задачи для проверки"
                return False
            self._status = BridgeStatus.VERIFYING
            self._message = "Проверяем sha256…"
            self._clear_error()
            job_id = self._job_id
            self._start(self._run_verify, job_id)
            return True

    def _run_verify(self, job_id: str) -> None:
        try:
            report = self._mixtape.verify(job_id)
        except MixtapeServiceError as error:
            with self._lock:
                self._status = BridgeStatus.FAILED
                self._fail(error, error.code)
            return
        except ValueError as error:
            with self._lock:
                self._status = BridgeStatus.FAILED
                self._fail(error, "job_corrupt")
            return
        with self._lock:
            self._verification = report
            self._status = BridgeStatus.VERIFIED if report.ok else BridgeStatus.FAILED
            if report.ok:
                self._message = f"Проверено файлов: {report.checked}"
                self._clear_error()
            else:
                self._error_code = "verify_failed"
                self._error = (
                    f"Расхождений: {len(report.mismatches)}"
                    if report.complete
                    else f"Задача не завершена: {report.status}"
                )

    def eject(self, *, object_path: str | None = None, confirm: bool = False) -> bool:
        """Извлечение только после завершения worker'а и с подтверждением."""

        with self._lock:
            if object_path is not None:
                self._selected = str(object_path)
            if not confirm:
                self._error_code = "confirmation_required"
                self._error = "Нужно явное подтверждение извлечения"
                return False
            if self._closed or self._busy():
                self._error_code = "job_running"
                self._error = "Операция ещё идёт: извлечение запрещено"
                return False
            target = self._selected
            if not target:
                self._error_code = "target_not_found"
                self._error = "USB-том не выбран"
                return False
            self._status = BridgeStatus.EJECTING
            self._message = "Отключаем том…"
            self._clear_error()
            self._start(self._run_eject, target)
            return True

    def _run_eject(self, target: str) -> None:
        store = self._mixtape.job_store
        for job_id in store.list_ids():
            try:
                job = store.load(job_id)
            except ValueError:
                continue
            if job and job.get("status") == "running" and job.get("object_path") == target:
                with self._lock:
                    self._status = BridgeStatus.FAILED
                    self._error_code = "job_running"
                    self._error = f"Задача {job_id} ещё пишет: извлечение запрещено"
                return
        service = self._mixtape.device_service
        try:
            self._mixtape.eject_target(target)
        except (UsbDeviceError, MixtapeError, OSError) as error:
            code = getattr(error, "code", None)
            with self._lock:
                self._status = BridgeStatus.FAILED
                self._fail(error, str(getattr(code, "value", code) or "eject_failed"))
            return
        with self._lock:
            self._selected = ""
        # Обновляем список прямо здесь: публичный refresh_devices() видит
        # собственный worker занятым и вернул бы старое значение.
        self._collect_devices(
            service,
            status=BridgeStatus.DONE,
            message="Том отключён: можно забирать",
            force_status=True,
        )

    # --- инфраструктура --------------------------------------------------
    def _start(self, target: Callable[..., None], *args: Any) -> None:
        worker = threading.Thread(
            target=self._guard, args=(target, *args), name="neurowave-mixtape", daemon=True
        )
        self._worker = worker
        worker.start()

    def _guard(self, target: Callable[..., None], *args: Any) -> None:
        try:
            target(*args)
        except (MixtapeError, ExportError, UsbDeviceError, OSError, ValueError) as error:
            with self._lock:
                self._status = BridgeStatus.FAILED
                self._fail(error, "bridge_error")
        except Exception as error:  # noqa: BLE001 — фоновая операция не должна убивать UI
            with self._lock:
                self._status = BridgeStatus.FAILED
                self._fail(error, "internal_error")

    def _fail(self, error: BaseException, code: str) -> None:
        self._error_code = str(code or "error")
        self._error = str(error) or self._error_code
        self._message = "Не удалось выполнить операцию"

    def _clear_error(self) -> None:
        self._error = ""
        self._error_code = ""


def describe_skips(preview: PlanPreview, limit: int = 6) -> str:
    """Короткая строка причин пропуска для экрана и статуса."""

    counts = preview.skip_reasons()
    if not counts:
        return ""
    parts = [f"{reason}: {count}" for reason, count in sorted(counts.items())]
    return ", ".join(parts[:limit])


__all__ = [
    "BridgeState",
    "BridgeStatus",
    "DeviceView",
    "ExportProgress",
    "ExportSummary",
    "MixtapeBridge",
    "PlanPreview",
    "PreviewSkip",
    "PreviewTrack",
    "describe_skips",
]
