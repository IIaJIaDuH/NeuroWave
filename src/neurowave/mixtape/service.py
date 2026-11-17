"""Один сервис плана и записи для CLI и Qt-моста.

Сервис исполняет уже разрешённую операцию: собрать план из локальной библиотеки
или записать подтверждённый план на выбранный том. Ни CLI, ни мост не обходят
подтверждение — `--confirm`/диалог, `--confirm-local-rights`/отметка прав и
выбор UDisks2 object path остаются в вызывающем слое, а сервис только повторяет
одни и те же шаги и кодирует ошибки стабильными кодами (`library_missing`,
`rights_not_confirmed`, `plan_changed`, `target_not_found`).

Побочных эффектов вне `plan()`/`export()` нет: `plan()` ничего не пишет, а
`export()` пишет только на выбранный том и в журнал задач.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from neurowave.config import Paths
from neurowave.domain import Track
from neurowave.local import LocalMusicAdapter
from neurowave.mixtape.devices import UDisks2DeviceService
from neurowave.mixtape.errors import MixtapeError
from neurowave.mixtape.export import (
    ExportReport,
    ProgressSink,
    SafeWriter,
    VerificationReport,
    verify_job,
)
from neurowave.mixtape.export.transcoder import FfmpegTranscoder
from neurowave.mixtape.jobs import CancelSignal, JobStore, job_record
from neurowave.mixtape.models import (
    DeviceIdentity,
    ExportAsset,
    MixtapePlan,
    MixtapeRequest,
)
from neurowave.mixtape.planner import MixtapePlanner
from neurowave.mixtape.rights import LocalExportRights
from neurowave.mixtape.sources.local import LocalExportSource

LIBRARY_MISSING = "library_missing"
RIGHTS_NOT_CONFIRMED = "rights_not_confirmed"
PLAN_CHANGED = "plan_changed"
TARGET_NOT_FOUND = "target_not_found"
JOB_NOT_FOUND = "job_not_found"


def make_job_id(plan_id: str) -> str:
    """Уникальный id запуска: план — содержимое, задача — конкретная запись.

    Один и тот же план на двух флешках должен получить разные задачи, иначе
    журнал и флаг отмены одному экспорту мешают другому.
    """

    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    return f"{plan_id}-{stamp}-{secrets.token_hex(3)}"


class MixtapeServiceError(MixtapeError):
    """Отказ сервиса со стабильным кодом для CLI --json и снимка моста."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = str(code)


@dataclass(frozen=True)
class PlanBuild:
    """Готовый план вместе с кандидатами и разрешёнными файлами."""

    library: Path
    request: MixtapeRequest
    rights_confirmed: bool
    tracks: tuple[Track, ...]
    assets: tuple[ExportAsset, ...]
    plan: MixtapePlan


@dataclass(frozen=True)
class ExportOutcome:
    """Результат записи: отчёт writer'а, том, id задачи и запись журнала."""

    report: ExportReport
    identity: DeviceIdentity
    job_id: str
    record: Mapping[str, Any]


class MixtapeService:
    """План и запись USB Mixtape; устройства и журнал подставляются извне."""

    def __init__(
        self,
        *,
        device_service: Any | None = None,
        transcoder: Any | None = None,
        job_store: JobStore | None = None,
        track_source: Callable[[Path], Any] | None = None,
    ) -> None:
        self._device_service = device_service
        self._transcoder = transcoder
        self._job_store = job_store
        self._track_source = track_source or (lambda root: LocalMusicAdapter(root))

    # --- инфраструктура --------------------------------------------------
    @property
    def device_service(self) -> Any:
        if self._device_service is None:
            self._device_service = UDisks2DeviceService()
        return self._device_service

    @property
    def job_store(self) -> JobStore:
        if self._job_store is None:
            root = Paths.resolve().ensure().data_dir / "mixtape" / "jobs"
            self._job_store = JobStore(root)
        return self._job_store

    # --- план -------------------------------------------------------------
    def plan(
        self,
        library: Path | str,
        request: MixtapeRequest,
        *,
        rights_confirmed: bool,
        tracks: Iterable[Track] | None = None,
    ) -> PlanBuild:
        """Построить план; без библиотеки — ``library_missing``.

        Без подтверждения прав план всё равно строится, но с пустым списком
        файлов: это preview, а не разрешение на запись.
        """

        root = Path(library).expanduser()
        if not root.is_dir():
            raise MixtapeServiceError(LIBRARY_MISSING, f"Папка музыки не найдена: {root}")
        candidates = (
            tuple(tracks)
            if tracks is not None
            else tuple(self._track_source(root).tracks())
        )
        source = LocalExportSource(
            root, rights=LocalExportRights(confirmed=bool(rights_confirmed))
        )
        # Один проход по библиотеке: планировщик возвращает те же assets, что сам
        # использовал, поэтому повторный list_eligible (лишние stat) не нужен.
        assets: list[ExportAsset] = []
        plan = MixtapePlanner([source]).plan(request, candidates, assets_out=assets)
        return PlanBuild(
            library=root,
            request=request,
            rights_confirmed=bool(rights_confirmed),
            tracks=candidates,
            assets=tuple(assets),
            plan=plan,
        )

    # --- устройство -------------------------------------------------------
    def require_target(self, object_path: str) -> DeviceIdentity:
        """Найти writable-том по object path и перепроверить его identity."""

        devices = self.device_service.list_removable_writable_filesystems()
        identity = next(
            (device for device in devices if device.udisks_object_path == object_path),
            None,
        )
        if identity is None:
            raise MixtapeServiceError(
                TARGET_NOT_FOUND, "Выбранный USB-том не найден среди writable-томов"
            )
        return self.device_service.revalidate(identity)

    def eject_target(self, object_path: str) -> DeviceIdentity:
        """Размонтировать уже перепроверенный том; вызывающий отвечает за согласие."""

        identity = self.require_target(object_path)
        self.device_service.safely_eject(identity)
        return identity

    # --- запись -----------------------------------------------------------
    def export(
        self,
        build: PlanBuild,
        *,
        object_path: str,
        expected_plan_id: str | None = None,
        progress: ProgressSink | None = None,
        on_job_started: Callable[[str], None] | None = None,
    ) -> ExportOutcome:
        """Записать подтверждённый план; ошибки writer'а сохраняются в журнал."""

        if not build.rights_confirmed:
            raise MixtapeServiceError(
                RIGHTS_NOT_CONFIRMED,
                "План создан без --confirm-local-rights: перепланируйте и подтвердите права",
            )
        plan = build.plan
        if expected_plan_id is not None and plan.plan_id != expected_plan_id:
            raise MixtapeServiceError(
                PLAN_CHANGED,
                "Библиотека изменилась после плана: постройте новый план и подтвердите его",
            )
        identity = self.require_target(object_path)
        store = self.job_store
        # USB-FIX-05: fresh job id per run; plan_id остаётся id содержимого плана.
        job_id = make_job_id(plan.plan_id)
        while store.load(job_id) is not None:  # pragma: no cover - коллизия имён
            job_id = make_job_id(plan.plan_id)
        store.clear_cancel(job_id)
        store.save(
            job_id,
            {
                "status": "running",
                "job_id": job_id,
                "plan_id": plan.plan_id,
                "target": str(identity.mountpoint),
                "object_path": identity.udisks_object_path,
                "library": str(build.library),
            },
        )
        if on_job_started is not None:
            on_job_started(job_id)
        writer = SafeWriter(
            identity.mountpoint,
            device_service=self.device_service,
            expected_device=identity,
            transcoder=self._transcoder if self._transcoder is not None else FfmpegTranscoder(),
        )
        try:
            report = writer.write(
                plan,
                build.assets,
                cancel=CancelSignal(store, job_id),
                progress=progress,
            )
        except Exception as error:
            store.save(
                job_id,
                {
                    "status": "failed",
                    "job_id": job_id,
                    "plan_id": plan.plan_id,
                    "target": str(identity.mountpoint),
                    "object_path": identity.udisks_object_path,
                    "library": str(build.library),
                    "error": str(error),
                },
            )
            raise
        record = job_record(
            report,
            target=str(identity.mountpoint),
            object_path=identity.udisks_object_path,
            library=str(build.library),
            job_id=job_id,
        )
        store.save(job_id, record)
        return ExportOutcome(
            report=report, identity=identity, job_id=job_id, record=record
        )

    # --- проверка ---------------------------------------------------------
    def verify(self, job_id: str) -> VerificationReport:
        """Проверить записанные файлы по журналу задачи."""

        job = self.job_store.load(job_id)
        if job is None:
            raise MixtapeServiceError(JOB_NOT_FOUND, f"Задача не найдена: {job_id}")
        return verify_job(job_id, job)


__all__ = [
    "ExportOutcome",
    "MixtapeService",
    "MixtapeServiceError",
    "make_job_id",
        "PlanBuild",
]

