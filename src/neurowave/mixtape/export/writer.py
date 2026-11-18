"""Безопасный writer USB-сборки: copy → sha256 → fsync → no-clobber commit.

Writer не форматирует и не монтирует носитель и не пишет в ``/dev``. Все
операции идут относительно открытого каталога назначения с ``O_NOFOLLOW``,
поэтому подмена каталога симлинком после проверки не выводит запись наружу.
Существующие файлы никогда не перезаписываются: только новый файл или
продолжение собственной verified-записи из manifest.

Копирование потоковое (ограниченный буфер), с sha256 на входе и проверкой
записанного файла до commit. Ошибки ENOSPC/EROFS/EIO/cancel оставляют чужие
файлы и уже проверенные записи нетронутыми, а manifest — пригодным для resume.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import platform
import secrets
import stat
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from neurowave.mixtape.devices.base import UsbDeviceError
from neurowave.mixtape.export.errors import ExportError, ExportErrorCode
from neurowave.mixtape.export.filenames import (
    portable_component,
    portable_destination,
    validate_relative,
)
from neurowave.mixtape.export.manifest import (
    MANIFEST_NAME,
    Manifest,
    ManifestEntry,
    ManifestRead,
    device_fingerprint,
    manifest_payload,
    path_is_within,
    read_manifest,
)
from neurowave.mixtape.export.playlist import PLAYLIST_NAME, PlaylistEntry, render_m3u8
from neurowave.mixtape.export.transcoder import TranscoderError, TranscoderUnavailable
from neurowave.mixtape.models import ExportAsset, ExportOperation, MixtapePlan, PlannedItem

BUFFER_SIZE = 1024 * 1024
TEMP_PREFIX = ".neurowave-part-"
# Версии плейлиста: обновление никогда не перезаписывает существующий файл.
PLAYLIST_MAX_ATTEMPTS = 100
_ABORT_CODES = {
    ExportErrorCode.ENOSPC,
    ExportErrorCode.READ_ONLY,
    ExportErrorCode.IO,
    ExportErrorCode.DEVICE,
    # Файловая система не даёт атомарного no-replace: продолжать нельзя.
    ExportErrorCode.NO_ATOMIC_REPLACE,
}

# renameat2(2) с RENAME_NOREPLACE: атомарный no-clobber там, где hardlink
# недоступен (FAT32/exFAT и т.п.). Номера системных вызовов на поддерживаемых
# архитектурах; на неизвестной архитектуре writer отказывается писать.
_RENAME_NOREPLACE = 1
_RENAMEAT2_SYSCALL = {
    "x86_64": 316,
    "amd64": 316,
    "aarch64": 276,
    "arm64": 276,
    "armv7l": 382,
    "i386": 353,
    "i686": 353,
    "ppc64le": 357,
    "s390x": 347,
}
_LIBC = ctypes.CDLL(None, use_errno=True)


class ItemStatus(str, Enum):
    VERIFIED = "verified"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class ExportStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True)
class ItemReport:
    track_key: str
    destination: str
    status: ItemStatus
    size_bytes: int = 0
    sha256: str = ""
    error: str = ""


@dataclass(frozen=True)
class ExportReport:
    plan_id: str
    status: ExportStatus
    items: tuple[ItemReport, ...]
    verified_count: int
    failed_count: int
    skipped_count: int
    cancelled: bool
    manifest_path: str
    playlist_path: str = ""
    playlist_error: str = ""

    @property
    def verified(self) -> tuple[ItemReport, ...]:
        return tuple(item for item in self.items if item.status is ItemStatus.VERIFIED)


@dataclass(frozen=True)
class ProgressUpdate:
    """Снимок прогресса записи для UI: только числа, без путей за пределы USB."""

    index: int
    total: int
    destination: str
    bytes_done: int
    bytes_total: int
    items_done: int


class CancelSignal(Protocol):
    def is_set(self) -> bool: ...


class ProgressSink(Protocol):
    """Наблюдатель прогресса; ошибки наблюдателя запись не прерывают."""

    def __call__(self, update: ProgressUpdate) -> None: ...


class _Meter:
    """Считает байты и проверенные файлы, отдавая снимки наружу."""

    def __init__(self, sink: ProgressSink, total: int, bytes_total: int) -> None:
        self._sink = sink
        self._total = total
        self._bytes_total = max(0, int(bytes_total))
        self._committed = 0
        self._item_bytes = 0
        self._items_done = 0
        self._index = 0
        self._destination = ""

    @property
    def bytes_done(self) -> int:
        return self._committed + self._item_bytes

    def start_item(self, index: int, destination: str) -> None:
        self._index = int(index)
        self._destination = destination
        self._item_bytes = 0
        self._emit()

    def advance(self, delta: int) -> None:
        self._item_bytes += max(0, int(delta))
        self._emit()

    def finish_item(self, report: ItemReport) -> None:
        if report.status is ItemStatus.VERIFIED:
            self._committed += int(report.size_bytes) or self._item_bytes
            self._items_done += 1
        self._item_bytes = 0
        self._emit()

    def _emit(self) -> None:
        try:
            self._sink(
                ProgressUpdate(
                    index=self._index,
                    total=self._total,
                    destination=self._destination,
                    bytes_done=self.bytes_done,
                    bytes_total=self._bytes_total,
                    items_done=self._items_done,
                )
            )
        except Exception:
            # Наблюдатель (UI) не должен ломать запись и манифест.
            pass


class _Cancelled(Exception):
    pass


class SafeWriter:
    def __init__(
        self,
        target: Path | str,
        *,
        device_service: Any | None = None,
        expected_device: Any | None = None,
        buffer_size: int = BUFFER_SIZE,
        transcoder: Any | None = None,
        job_id: str = "",
    ) -> None:
        self._target = Path(target).expanduser()
        self._device = device_service
        self._expected = expected_device
        self._buffer = max(1, int(buffer_size))
        self._transcoder = transcoder
        # Конкретный запуск для USB-manifest; пусто — fallback на plan_id
        # ради старых вызовов и manifest без job_id.
        self._job_id = str(job_id or "")

    def write(
        self,
        plan: MixtapePlan,
        assets: Sequence[ExportAsset],
        *,
        cancel: CancelSignal | None = None,
        resume: bool = True,
        progress: ProgressSink | None = None,
    ) -> ExportReport:
        # USB-FIX-03: ни одного побочного эффекта до перепроверки тома.
        # Иначе при выдернутой флешке каталог на системном диске остался бы
        # доступен, и writer создал бы там подкаталог и manifest.
        self._ensure_device()
        self._assert_mountpoint()
        try:
            target = self._target.resolve(strict=True)
        except OSError as error:
            raise ExportError(
                ExportErrorCode.TARGET_MISSING, "Целевой каталог USB не найден"
            ) from error
        if not target.is_dir():
            raise ExportError(ExportErrorCode.TARGET_MISSING, "Целевой каталог USB не найден")
        # Дальше работаем только с разрешённым путём: он же сверяется с открытым
        # fd перед каждой записью manifest, чтобы размонтирование не увело
        # запись в каталог на хостовом диске.
        self._target = target
        subdir = str(plan.request.destination_subdir or "").strip("/")
        subdir_parts = tuple(part for part in subdir.split("/") if part)
        if not subdir_parts:
            raise ExportError(ExportErrorCode.UNSAFE_PATH, "Пустой целевой каталог")
        subdir_path = target.joinpath(*subdir_parts)
        if not path_is_within(target, subdir_path):
            raise ExportError(ExportErrorCode.PATH_ESCAPE, "Целевой каталог вне USB")
        manifest_path = subdir_path / MANIFEST_NAME

        target_fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            manifest_state = self._read_manifest(target_fd, subdir_parts)
            manifest_identity: tuple[int, int] | None = None
            if manifest_state is not None:
                if not resume:
                    raise ExportError(
                        ExportErrorCode.MANIFEST_EXISTS, "manifest уже существует"
                    )
                if not self._compatible(manifest_state.manifest, plan, subdir):
                    raise ExportError(
                        ExportErrorCode.FOREIGN_MANIFEST,
                        "manifest принадлежит другой задаче или устройству",
                    )
                existing = manifest_state.manifest
                manifest_identity = (manifest_state.st_dev, manifest_state.st_ino)
            else:
                existing = None
            # Владельцем playlist считаемся только тогда, когда в каталоге наш
            # manifest (он совпал по плану/устройству). Manifest старого формата
            # без поля ``playlist`` тоже даёт право на версионирование: файл всё
            # равно не перезаписывается, а только рядом появляется новый.
            playlist_owned = existing is not None
            playlist_record = existing.playlist if playlist_owned else ""

            entries: dict[str, ManifestEntry] = {
                entry.track_key: entry for entry in existing.entries
            } if existing else {}
            seen = {
                entry.destination.casefold()
                for entry in entries.values()
                if entry.status == ItemStatus.VERIFIED.value
            }
            assets_by_id = {asset.asset_id: asset for asset in assets}
            meter = (
                _Meter(
                    progress,
                    len(plan.items),
                    sum(max(0, int(item.estimated_bytes)) for item in plan.items),
                )
                if progress is not None
                else None
            )
            results: list[ItemReport] = []
            order: list[str] = []
            verified = 0
            cancelled = False
            aborted = False

            playlist_path = ""
            playlist_error = ""
            if _cancelled(cancel):
                cancelled = True
            else:
                for index, item in enumerate(plan.items, start=1):
                    if _cancelled(cancel):
                        cancelled = True
                        break
                    report, error_code = self._process(
                        target_fd,
                        index,
                        item,
                        assets_by_id.get(item.source_asset_id),
                        entries,
                        seen,
                        cancel,
                        subdir,
                        meter,
                    )
                    if meter is not None:
                        meter.finish_item(report)
                    results.append(report)
                    order.append(item.track_key)
                    if report.status is ItemStatus.VERIFIED:
                        verified += 1
                        seen.add(report.destination.casefold())
                    entries[item.track_key] = _entry(report)
                    manifest_identity = self._flush(
                        target_fd,
                        subdir_parts,
                        plan,
                        entries,
                        order,
                        identity=manifest_identity,
                        playlist=playlist_record,
                    )
                    if report.status is ItemStatus.CANCELLED:
                        cancelled = True
                        break
                    if error_code in _ABORT_CODES:
                        aborted = True
                        break
            if verified:
                try:
                    playlist_path, playlist_error = self._write_playlist(
                        target_fd, subdir, plan, results, owned=playlist_owned
                    )
                except ExportError as error:
                    playlist_error = str(error)
                else:
                    if playlist_path:
                        playlist_record = playlist_path.rsplit("/", 1)[-1]
            status = self._status(plan, verified, cancelled, aborted)
            self._flush(
                target_fd,
                subdir_parts,
                plan,
                entries,
                order,
                identity=manifest_identity,
                status=status,
                playlist=playlist_record,
            )
        finally:
            os.close(target_fd)
        failed = sum(1 for item in results if item.status is ItemStatus.FAILED)
        skipped = sum(1 for item in results if item.status is ItemStatus.SKIPPED)
        return ExportReport(
            plan_id=plan.plan_id,
            status=status,
            items=tuple(results),
            verified_count=verified,
            failed_count=failed,
            skipped_count=skipped,
            cancelled=cancelled,
            manifest_path=str(manifest_path),
            playlist_path=playlist_path,
            playlist_error=playlist_error,
        )

    # --- один файл ----------------------------------------------------
    def _process(
        self,
        target_fd: int,
        index: int,
        item: PlannedItem,
        asset: ExportAsset | None,
        entries: Mapping[str, ManifestEntry],
        seen: set[str],
        cancel: CancelSignal | None,
        subdir: str,
        meter: _Meter | None = None,
    ) -> tuple[ItemReport, ExportErrorCode | None]:
        try:
            destination = self._destination(item.destination_relative, subdir)
        except ExportError as error:
            if meter is not None:
                meter.start_item(index, "")
            return _failed(item, "", error.code), error.code
        if meter is not None:
            meter.start_item(index, destination)

        try:
            # Перед каждой записью: том всё ещё смонтирован туда, куда открыт fd.
            self._reassert_mount(target_fd)
            previous = entries.get(item.track_key)
            if previous is not None and previous.status == ItemStatus.VERIFIED.value:
                existing = Path(self._target.resolve(), destination)
                if previous.destination == destination and existing.exists():
                    if self._matches_existing(existing, previous):
                        return (
                            ItemReport(
                                track_key=item.track_key,
                                destination=destination,
                                status=ItemStatus.VERIFIED,
                                size_bytes=previous.size_bytes,
                                sha256=previous.sha256,
                            ),
                            None,
                        )
                    raise ExportError(
                        ExportErrorCode.VERIFY,
                        "Файл на носителе не совпадает с manifest",
                        path=destination,
                    )
                if previous.destination == destination:
                    seen.discard(destination.casefold())

            if destination.casefold() in seen:
                raise ExportError(
                    ExportErrorCode.COLLISION, "Имя уже занято другим треком", path=destination
                )
            if asset is None or asset.local_path is None or not asset.exportable:
                raise ExportError(ExportErrorCode.RIGHTS, "Нет права на этот файл", path=destination)
            source = asset.local_path
            if not source.is_file():
                raise ExportError(
                    ExportErrorCode.SOURCE_MISSING, "Исходный файл пропал", path=destination
                )

            if item.operation is ExportOperation.TRANSCODE:
                size, digest = self._transcode(
                    target_fd, destination, source, asset, cancel
                )
            else:
                size, digest = self._copy(
                    target_fd, destination, source, asset, cancel, meter
                )
            return (
                ItemReport(
                    track_key=item.track_key,
                    destination=destination,
                    status=ItemStatus.VERIFIED,
                    size_bytes=size,
                    sha256=digest,
                ),
                None,
            )
        except _Cancelled:
            return (
                ItemReport(
                    track_key=item.track_key,
                    destination=destination,
                    status=ItemStatus.CANCELLED,
                    error="cancelled",
                ),
                None,
            )
        except ExportError as error:
            return _failed(item, destination, error.code, str(error)), error.code
        except OSError as error:
            mapped = _os_error(error)
            return _failed(item, destination, mapped.code, str(mapped)), mapped.code

    def _copy(
        self,
        target_fd: int,
        destination: str,
        source: Path,
        asset: ExportAsset,
        cancel: CancelSignal | None,
        meter: _Meter | None = None,
    ) -> tuple[int, str]:
        parts = tuple(destination.split("/"))
        dir_fd = _ensure_dir_fd(target_fd, parts[:-1])
        name = parts[-1]
        temporary = TEMP_PREFIX + secrets.token_hex(8)
        source_fd = -1
        temp_fd = -1
        committed = False
        hasher = hashlib.sha256()
        size = 0
        try:
            try:
                os.lstat(name, dir_fd=dir_fd)
            except FileNotFoundError:
                pass
            else:
                raise ExportError(
                    ExportErrorCode.EXISTS, "Файл уже существует, перезапись запрещена", path=destination
                )

            source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
            temp_fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=dir_fd,
            )
            while True:
                if _cancelled(cancel):
                    raise _Cancelled
                chunk = _read_bytes(source_fd, self._buffer)
                if not chunk:
                    break
                _write_bytes(temp_fd, chunk)
                hasher.update(chunk)
                size += len(chunk)
                if meter is not None:
                    meter.advance(len(chunk))
            os.fsync(temp_fd)
            os.close(temp_fd)
            temp_fd = -1
            os.close(source_fd)
            source_fd = -1

            digest = hasher.hexdigest()
            if int(asset.size_bytes) > 0 and size != int(asset.size_bytes):
                raise ExportError(
                    ExportErrorCode.SOURCE_CHANGED,
                    "Исходный файл изменился после планирования",
                    path=destination,
                )
            self._verify_temp(dir_fd, temporary, size, digest, destination)
            _commit(dir_fd, temporary, name, destination)
            committed = True
            return size, digest
        finally:
            for fd in (temp_fd, source_fd):
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            if not committed:
                try:
                    os.unlink(temporary, dir_fd=dir_fd)
                except OSError:
                    pass
            os.close(dir_fd)

    def _transcode(
        self,
        target_fd: int,
        destination: str,
        source: Path,
        asset: ExportAsset,
        cancel: CancelSignal | None,
    ) -> tuple[int, str]:
        if self._transcoder is None or not getattr(self._transcoder, "available", False):
            raise ExportError(
                ExportErrorCode.TRANSCODE_UNAVAILABLE,
                "Транскодирование недоступно: нет ffmpeg",
                path=destination,
            )
        parts = tuple(destination.split("/"))
        dir_fd = _ensure_dir_fd(target_fd, parts[:-1])
        name = parts[-1]
        temporary = TEMP_PREFIX + secrets.token_hex(8)
        temporary_path = Path(self._target.resolve(), *parts[:-1], temporary)
        fd = -1
        committed = False
        try:
            if _cancelled(cancel):
                raise _Cancelled
            try:
                os.lstat(name, dir_fd=dir_fd)
            except FileNotFoundError:
                pass
            else:
                raise ExportError(
                    ExportErrorCode.EXISTS,
                    "Файл уже существует, перезапись запрещена",
                    path=destination,
                )
            try:
                self._transcoder.transcode(
                    source, temporary_path, target_format="mp3", bitrate_kbps=320
                )
            except TranscoderUnavailable as error:
                raise ExportError(
                    ExportErrorCode.TRANSCODE_UNAVAILABLE, str(error), path=destination
                ) from error
            except TranscoderError as error:
                raise ExportError(
                    ExportErrorCode.TRANSCODE_FAILED, str(error), path=destination
                ) from error

            fd = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
            os.fsync(fd)
            size = os.fstat(fd).st_size
            if size <= 0:
                raise ExportError(
                    ExportErrorCode.TRANSCODE_FAILED, "Транскодер создал пустой файл"
                )
            try:
                info = self._transcoder.probe(temporary_path)
            except (TranscoderError, TranscoderUnavailable) as error:
                raise ExportError(
                    ExportErrorCode.PROBE_FAILED,
                    f"Не удалось проверить выход: {error}",
                    path=destination,
                ) from error
            if info.media_format and "mp3" not in info.media_format:
                raise ExportError(
                    ExportErrorCode.PROBE_FAILED,
                    f"Ожидался MP3, получен {info.media_format}",
                    path=destination,
                )
            if asset.duration_ms and info.duration_ms:
                tolerance = max(3_000, int(asset.duration_ms) // 20)
                if abs(int(info.duration_ms) - int(asset.duration_ms)) > tolerance:
                    raise ExportError(
                        ExportErrorCode.PROBE_FAILED,
                        "Длительность результата не совпадает с исходником",
                        path=destination,
                    )
            os.lseek(fd, 0, os.SEEK_SET)
            _, digest = _hash_fd(fd)
            _commit(dir_fd, temporary, name, destination)
            committed = True
            return size, digest
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if not committed:
                try:
                    os.unlink(temporary, dir_fd=dir_fd)
                except OSError:
                    pass
            os.close(dir_fd)

    def _write_playlist(
        self,
        target_fd: int,
        subdir: str,
        plan: MixtapePlan,
        results: Sequence[ItemReport],
        *,
        owned: bool,
    ) -> tuple[str, str]:
        """Записать M3U8, никогда не перезаписывая существующий файл.

        Атомарного «заменить, если inode тот же» в POSIX нет, поэтому обновление
        своей записи идёт версионированием: первый файл — ``playlist.m3u8``,
        следующие — ``playlist-2.m3u8`` и далее, каждый публикуется no-clobber.
        Чужой файл и его symlink не трогаются, а гонка на выбранное имя
        разрешается следующим свободным.
        """

        by_key = {item.track_key: item for item in plan.items}
        entries: list[PlaylistEntry] = []
        for result in results:
            if result.status is not ItemStatus.VERIFIED:
                continue
            item = by_key.get(result.track_key)
            if item is None:
                continue
            entries.append(
                PlaylistEntry(
                    destination=result.destination,
                    duration_ms=item.duration_ms,
                    artist=", ".join(item.artists),
                    title=item.title,
                )
            )
        if not entries:
            return "", ""
        text = render_m3u8(entries, subdir)
        parts = tuple(part for part in subdir.split("/") if part)
        try:
            dir_fd = _ensure_dir_fd(target_fd, parts)
        except OSError as error:
            raise ExportError(ExportErrorCode.PATH_ESCAPE, str(error)) from error
        temporary = ".neurowave-playlist-" + secrets.token_hex(8)
        try:
            self._reassert_mount(target_fd)
            name = PLAYLIST_NAME
            if _lexists(dir_fd, PLAYLIST_NAME):
                if not owned:
                    raise ExportError(
                        ExportErrorCode.PLAYLIST_EXISTS,
                        f"{subdir}/{PLAYLIST_NAME} уже существует и не принадлежит этой задаче",
                        path=f"{subdir}/{PLAYLIST_NAME}",
                    )
                name = self._free_playlist_name(dir_fd, subdir)
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=dir_fd,
            )
            try:
                _write_bytes(fd, text.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            for _ in range(PLAYLIST_MAX_ATTEMPTS):
                destination = f"{subdir}/{name}"
                try:
                    _commit(dir_fd, temporary, name, destination)
                except ExportError as error:
                    if error.code is not ExportErrorCode.EXISTS:
                        raise
                    if name == PLAYLIST_NAME:
                        raise ExportError(
                            ExportErrorCode.PLAYLIST_EXISTS,
                            f"{destination} появился во время записи: файл не тронут",
                            path=destination,
                        ) from error
                    name = self._free_playlist_name(dir_fd, subdir)
                    continue
                return destination, ""
            raise ExportError(
                ExportErrorCode.PLAYLIST_EXISTS,
                "Не удалось выбрать свободное имя плейлиста: файл не записан",
            )
        except ExportError:
            raise
        except OSError as error:
            raise ExportError(ExportErrorCode.IO, f"playlist не записан: {error}") from error
        finally:
            try:
                os.unlink(temporary, dir_fd=dir_fd)
            except OSError:
                pass
            os.close(dir_fd)

    @staticmethod
    def _free_playlist_name(dir_fd: int, subdir: str) -> str:
        """Следующее имя версии, которого ещё нет; symlink тоже считается занятым."""

        for version in range(2, PLAYLIST_MAX_ATTEMPTS + 2):
            candidate = f"playlist-{version}.m3u8"
            if not _lexists(dir_fd, candidate):
                return candidate
        raise ExportError(
            ExportErrorCode.PLAYLIST_EXISTS,
            f"В {subdir} слишком много версий плейлиста: файл не записан",
        )

    def _verify_temp(
        self, dir_fd: int, temporary: str, size: int, digest: str, destination: str
    ) -> None:
        fd = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
        try:
            actual_size, actual_digest = _hash_fd(fd)
        finally:
            os.close(fd)
        if actual_size != size or actual_digest != digest:
            raise ExportError(
                ExportErrorCode.VERIFY, "Проверка записанного файла не прошла", path=destination
            )

    def _destination(self, relative: str, subdir: str) -> str:
        try:
            parts = validate_relative(relative, subdir)
        except ValueError as error:
            raise ExportError(ExportErrorCode.UNSAFE_PATH, str(error)) from error
        prefix = tuple(part for part in subdir.split("/") if part)
        safe = (*prefix, *portable_destination(parts[len(prefix) :]))
        destination = "/".join(safe)
        if any(part in ("", ".", "..") for part in destination.split("/")):
            raise ExportError(ExportErrorCode.UNSAFE_PATH, "Небезопасный путь назначения")
        return destination

    def _ensure_device(self) -> None:
        if self._device is None:
            return
        if self._expected is None:
            raise ExportError(ExportErrorCode.DEVICE, "Устройство не выбрано")
        try:
            self._device.revalidate(self._expected)
        except UsbDeviceError as error:
            raise ExportError(ExportErrorCode.DEVICE, str(error)) from error

    def _assert_mountpoint(self) -> None:
        """Каталог назначения — всё ещё точка монтирования подтверждённого тома."""

        if self._expected is None:
            return
        try:
            target = Path(self._target).resolve()
            expected = Path(self._expected.mountpoint).resolve()
        except OSError as error:
            raise ExportError(ExportErrorCode.DEVICE, "Точка монтирования недоступна") from error
        if target != expected:
            raise ExportError(
                ExportErrorCode.DEVICE,
                "Точка монтирования изменилась после подтверждения тома",
            )
        checker = getattr(self._device, "assert_mountpoint", None)
        if checker is None:
            return
        try:
            checker(self._expected)
        except UsbDeviceError as error:
            raise ExportError(ExportErrorCode.DEVICE, str(error)) from error

    def _matches_existing(self, path: Path, entry: ManifestEntry) -> bool:
        try:
            if path.is_symlink() or not path.is_file():
                return False
            if path.stat().st_size != entry.size_bytes:
                return False
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                _, digest = _hash_fd(fd)
            finally:
                os.close(fd)
            return digest == entry.sha256
        except OSError:
            return False

    def _compatible(self, manifest: Manifest, plan: MixtapePlan, subdir: str) -> bool:
        if manifest.plan_id != plan.plan_id or manifest.revision != plan.revision:
            return False
        if manifest.destination_subdir != subdir:
            return False
        return manifest.device == device_fingerprint(self._expected)

    def _read_manifest(
        self, target_fd: int, parts: Sequence[str]
    ) -> ManifestRead | None:
        """Прочитать manifest через открытый каталог, а не через путь."""

        try:
            dir_fd = _ensure_dir_fd(target_fd, parts)
        except OSError as error:
            raise ExportError(
                ExportErrorCode.PATH_ESCAPE, "Целевой каталог подменён симлинком"
            ) from error
        try:
            return read_manifest(dir_fd)
        finally:
            os.close(dir_fd)

    def _reassert_mount(self, target_fd: int) -> None:
        """Том всё ещё смонтирован в тот же каталог, который открыт для записи.

        Вызывается перед каждым файлом, записью manifest и плейлиста: после
        размонтирования путь уже смотрит в каталог хостовой системы, а fd —
        в отсоединённый inode, поэтому сверяются и устройство, и inode.
        """

        self._ensure_device()
        self._assert_mountpoint()
        try:
            current = os.stat(self._target)
        except OSError as error:
            raise ExportError(
                ExportErrorCode.DEVICE, "Точка монтирования недоступна"
            ) from error
        held = os.fstat(target_fd)
        if (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
            raise ExportError(
                ExportErrorCode.DEVICE,
                "Каталог назначения больше не является точкой монтирования",
            )

    def _flush(
        self,
        target_fd: int,
        subdir_parts: Sequence[str],
        plan: MixtapePlan,
        entries: Mapping[str, ManifestEntry],
        order: Sequence[str],
        *,
        identity: tuple[int, int] | None,
        status: str = "writing",
        playlist: str = "",
    ) -> tuple[int, int]:
        """Обновить manifest только через открытый каталог.

        Пока файла нет, публикация идёт no-clobber: параллельно созданный чужой
        manifest не перезаписывается. Дальше заменяется только файл с
        подтверждённой identity — исчезновение или подмена означают отказ.
        """

        self._reassert_mount(target_fd)
        ordered = [
            entries[key]
            for key in order
            if key in entries and entries[key].destination
        ]
        payload = manifest_payload(
            Manifest(
                job_id=self._job_id or plan.plan_id,
                plan_id=plan.plan_id,
                revision=plan.revision,
                destination_subdir="/".join(subdir_parts),
                status=status,
                entries=tuple(ordered),
                device=device_fingerprint(self._expected),
                playlist=playlist,
            )
        )
        try:
            dir_fd = _ensure_dir_fd(target_fd, subdir_parts)
        except OSError as error:
            raise ExportError(
                ExportErrorCode.PATH_ESCAPE, "Каталог manifest подменён симлинком"
            ) from error
        temporary = f".{MANIFEST_NAME}.tmp-{secrets.token_hex(8)}"
        try:
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=dir_fd,
            )
            try:
                _write_bytes(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)
            if identity is None:
                try:
                    _commit(dir_fd, temporary, MANIFEST_NAME, MANIFEST_NAME)
                except ExportError as error:
                    if error.code is ExportErrorCode.EXISTS:
                        raise ExportError(
                            ExportErrorCode.MANIFEST_EXISTS,
                            "manifest создан параллельно: файл не тронут",
                        ) from error
                    if error.code is not ExportErrorCode.NO_ATOMIC_REPLACE:
                        raise
                    # ФС без link/renameat2: создаём эксклюзивно, без перезаписи.
                    # Цена — manifest может остаться недописанным при аварии;
                    # на такой ФС writer и так отказывается писать аудио.
                    _create_manifest_exclusive(dir_fd, payload)
            else:
                _require_manifest_identity(dir_fd, identity)
                os.replace(
                    temporary,
                    MANIFEST_NAME,
                    src_dir_fd=dir_fd,
                    dst_dir_fd=dir_fd,
                )
            stat_result = os.lstat(MANIFEST_NAME, dir_fd=dir_fd)
        except OSError as error:
            raise ExportError(
                ExportErrorCode.IO, f"manifest не записан: {error}"
            ) from error
        finally:
            try:
                os.unlink(temporary, dir_fd=dir_fd)
            except OSError:
                pass
            os.close(dir_fd)
        return stat_result.st_dev, stat_result.st_ino

    def _status(
        self, plan: MixtapePlan, verified: int, cancelled: bool, aborted: bool
    ) -> ExportStatus:
        if cancelled:
            return ExportStatus.CANCELLED
        if not plan.items:
            # 0 == 0 не означает успех: записывать было нечего.
            return ExportStatus.FAILED
        if verified == len(plan.items):
            return ExportStatus.COMPLETE
        if verified:
            return ExportStatus.PARTIAL
        return ExportStatus.FAILED


def _ensure_dir_fd(root_fd: int, parts: Sequence[str]) -> int:
    fd = os.dup(root_fd)
    try:
        for part in parts:
            try:
                os.mkdir(part, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            new_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = new_fd
        return fd
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _lexists(dir_fd: int, name: str) -> bool:
    """Существует ли имя в каталоге, включая symlink (за ним не следуем)."""

    try:
        os.lstat(name, dir_fd=dir_fd)
    except FileNotFoundError:
        return False
    return True


def _require_manifest_identity(dir_fd: int, identity: tuple[int, int]) -> None:
    """Убедиться, что заменяем именно свой manifest, а не чужой файл."""

    try:
        current = os.lstat(MANIFEST_NAME, dir_fd=dir_fd)
    except FileNotFoundError as error:
        raise ExportError(
            ExportErrorCode.FOREIGN_MANIFEST,
            "manifest исчез во время записи: обновление отменено",
        ) from error
    if not stat.S_ISREG(current.st_mode):
        raise ExportError(
            ExportErrorCode.FOREIGN_MANIFEST,
            "manifest подменён симлинком: обновление отменено",
        )
    if (current.st_dev, current.st_ino) != identity:
        raise ExportError(
            ExportErrorCode.FOREIGN_MANIFEST,
            "manifest подменён во время записи: чужой файл не тронут",
        )


def _create_manifest_exclusive(dir_fd: int, payload: bytes) -> None:
    """Создать manifest без перезаписи там, где нет link/renameat2."""

    try:
        fd = os.open(
            MANIFEST_NAME,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=dir_fd,
        )
    except FileExistsError as error:
        raise ExportError(
            ExportErrorCode.MANIFEST_EXISTS,
            "manifest создан параллельно: файл не тронут",
        ) from error
    try:
        _write_bytes(fd, payload)
        os.fsync(fd)
    except BaseException:
        try:
            os.unlink(MANIFEST_NAME, dir_fd=dir_fd)
        except OSError:
            pass
        raise
    finally:
        os.close(fd)


def _commit(dir_fd: int, temporary: str, name: str, destination: str) -> None:
    """Атомарно опубликовать файл без права перезаписи.

    Сначала пробуем ``link`` (атомарный no-clobber на обычных ФС). Если он
    недоступен (FAT32/exFAT, запрет hardlink), используем
    ``renameat2(RENAME_NOREPLACE)``. Проверка «файла нет» перед ``rename``
    больше не используется: она не атомарна. Если ФС не даёт ни одной
    гарантии — fail-closed, а не оптимистичная перезапись.
    """

    try:
        os.link(temporary, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
    except FileExistsError as error:
        raise ExportError(
            ExportErrorCode.EXISTS, "Файл уже существует, перезапись запрещена", path=destination
        ) from error
    except OSError as error:
        if error.errno not in (
            errno.EPERM,
            errno.EOPNOTSUPP,
            getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
            errno.EXDEV,
            errno.EMLINK,
        ):
            raise
        _rename_noreplace(dir_fd, temporary, name, destination)
    else:
        os.unlink(temporary, dir_fd=dir_fd)


def _rename_noreplace(dir_fd: int, temporary: str, name: str, destination: str) -> None:
    syscall_no = _RENAMEAT2_SYSCALL.get(platform.machine().lower())
    if syscall_no is None:
        raise ExportError(
            ExportErrorCode.NO_ATOMIC_REPLACE,
            "Платформа не поддерживает атомарную запись без перезаписи",
            path=destination,
        )
    result, error_number = _renameat2_call(syscall_no, dir_fd, temporary, name)
    if result == 0:
        return
    if error_number == errno.EEXIST:
        raise ExportError(
            ExportErrorCode.EXISTS, "Файл уже существует, перезапись запрещена", path=destination
        )
    if error_number in (
        errno.ENOSYS,
        errno.EINVAL,
        errno.EOPNOTSUPP,
        getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
    ):
        raise ExportError(
            ExportErrorCode.NO_ATOMIC_REPLACE,
            "Файловая система не поддерживает атомарную запись без перезаписи",
            path=destination,
        )
    raise OSError(error_number, os.strerror(error_number))


def _renameat2_call(
    syscall_no: int, dir_fd: int, temporary: str, name: str
) -> tuple[int, int]:
    """Вызвать ``renameat2(RENAME_NOREPLACE)``; вынесено ради тестов.

    Возвращает ``(результат, errno)``: 0 — успех, иначе номер ошибки из libc.
    """

    result = _LIBC.syscall(
        ctypes.c_long(syscall_no),
        ctypes.c_int(dir_fd),
        ctypes.c_char_p(temporary.encode("utf-8")),
        ctypes.c_int(dir_fd),
        ctypes.c_char_p(name.encode("utf-8")),
        ctypes.c_uint(_RENAME_NOREPLACE),
    )
    if result == 0:
        return 0, 0
    return -1, ctypes.get_errno()


def _hash_fd(fd: int) -> tuple[int, str]:
    hasher = hashlib.sha256()
    size = 0
    while True:
        chunk = _read_bytes(fd, BUFFER_SIZE)
        if not chunk:
            break
        hasher.update(chunk)
        size += len(chunk)
    return size, hasher.hexdigest()


def _read_bytes(fd: int, size: int) -> bytes:
    return os.read(fd, size)


def _write_bytes(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _cancelled(cancel: CancelSignal | None) -> bool:
    return bool(cancel is not None and cancel.is_set())


def _os_error(error: OSError) -> ExportError:
    if error.errno in (errno.ELOOP, errno.ENOTDIR):
        return ExportError(ExportErrorCode.PATH_ESCAPE, "Каталог назначения подменён симлинком")
    if error.errno == errno.ENOSPC:
        return ExportError(ExportErrorCode.ENOSPC, "На носителе закончилось место")
    if error.errno == errno.EROFS:
        return ExportError(ExportErrorCode.READ_ONLY, "Носитель только для чтения")
    return ExportError(ExportErrorCode.IO, f"Ошибка ввода-вывода: {error}")


def _failed(
    item: PlannedItem, destination: str, code: ExportErrorCode, message: str = ""
) -> ItemReport:
    return ItemReport(
        track_key=item.track_key,
        destination=destination,
        status=ItemStatus.SKIPPED
        if code in {ExportErrorCode.COLLISION, ExportErrorCode.EXISTS}
        else ItemStatus.FAILED,
        error=code.value,
    )


def _entry(report: ItemReport) -> ManifestEntry:
    return ManifestEntry(
        track_key=report.track_key,
        destination=report.destination,
        status=report.status.value
        if report.status is not ItemStatus.CANCELLED
        else ItemStatus.SKIPPED.value,
        size_bytes=report.size_bytes,
        sha256=report.sha256,
        error=report.error,
    )
