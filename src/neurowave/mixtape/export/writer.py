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

import errno
import hashlib
import os
import secrets
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
    device_fingerprint,
    load_manifest,
    path_is_within,
    write_manifest_atomic,
)
from neurowave.mixtape.export.playlist import PLAYLIST_NAME, PlaylistEntry, render_m3u8
from neurowave.mixtape.export.transcoder import TranscoderError, TranscoderUnavailable
from neurowave.mixtape.models import ExportAsset, ExportOperation, MixtapePlan, PlannedItem

BUFFER_SIZE = 1024 * 1024
TEMP_PREFIX = ".neurowave-part-"
_ABORT_CODES = {
    ExportErrorCode.ENOSPC,
    ExportErrorCode.READ_ONLY,
    ExportErrorCode.IO,
    ExportErrorCode.DEVICE,
}


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
    ) -> None:
        self._target = Path(target).expanduser()
        self._device = device_service
        self._expected = expected_device
        self._buffer = max(1, int(buffer_size))
        self._transcoder = transcoder

    def write(
        self,
        plan: MixtapePlan,
        assets: Sequence[ExportAsset],
        *,
        cancel: CancelSignal | None = None,
        resume: bool = True,
        progress: ProgressSink | None = None,
    ) -> ExportReport:
        target = self._target.resolve()
        if not target.is_dir():
            raise ExportError(ExportErrorCode.TARGET_MISSING, "Целевой каталог USB не найден")
        subdir = str(plan.request.destination_subdir or "").strip("/")
        subdir_parts = tuple(part for part in subdir.split("/") if part)
        if not subdir_parts:
            raise ExportError(ExportErrorCode.UNSAFE_PATH, "Пустой целевой каталог")
        subdir_path = target.joinpath(*subdir_parts)
        if not path_is_within(target, subdir_path):
            raise ExportError(ExportErrorCode.PATH_ESCAPE, "Целевой каталог вне USB")

        root_fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            _ensure_dir_fd(root_fd, subdir_parts)
        except OSError as error:
            os.close(root_fd)
            raise ExportError(
                ExportErrorCode.PATH_ESCAPE, "Целевой каталог подменён симлинком"
            ) from error
        finally:
            try:
                os.close(root_fd)
            except OSError:
                pass

        manifest_path = subdir_path / MANIFEST_NAME
        if manifest_path.exists() or manifest_path.is_symlink():
            if not resume:
                raise ExportError(ExportErrorCode.MANIFEST_EXISTS, "manifest уже существует")
            existing = load_manifest(manifest_path)
            if existing is None or not self._compatible(existing, plan, subdir):
                raise ExportError(
                    ExportErrorCode.FOREIGN_MANIFEST,
                    "manifest принадлежит другой задаче или устройству",
                )
        else:
            existing = None

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
        target_fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
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
                    self._flush(plan, subdir, entries, order, manifest_path)
                    if report.status is ItemStatus.CANCELLED:
                        cancelled = True
                        break
                    if error_code in _ABORT_CODES:
                        aborted = True
                        break
            if verified:
                try:
                    playlist_path, playlist_error = self._write_playlist(
                        target_fd, subdir, plan, results
                    )
                except ExportError as error:
                    playlist_error = str(error)
        finally:
            os.close(target_fd)

        status = self._status(plan, verified, cancelled, aborted)
        self._flush(plan, subdir, entries, order, manifest_path, status=status)
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
            self._ensure_device()
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
    ) -> tuple[str, str]:
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
        temporary = ".neurowave-playlist-" + secrets.token_hex(8)
        try:
            dir_fd = _ensure_dir_fd(target_fd, parts)
        except OSError as error:
            raise ExportError(ExportErrorCode.PATH_ESCAPE, str(error)) from error
        try:
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
            os.replace(temporary, PLAYLIST_NAME, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except OSError as error:
            raise ExportError(ExportErrorCode.IO, f"playlist не записан: {error}") from error
        finally:
            try:
                os.unlink(temporary, dir_fd=dir_fd)
            except OSError:
                pass
            os.close(dir_fd)
        return f"{subdir}/{PLAYLIST_NAME}", ""

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

    def _flush(
        self,
        plan: MixtapePlan,
        subdir: str,
        entries: Mapping[str, ManifestEntry],
        order: Sequence[str],
        manifest_path: Path,
        *,
        status: str = "writing",
    ) -> None:
        if not path_is_within(self._target, manifest_path.parent):
            raise ExportError(ExportErrorCode.PATH_ESCAPE, "Каталог manifest подменён симлинком")
        ordered = [
            entries[key]
            for key in order
            if key in entries and entries[key].destination
        ]
        write_manifest_atomic(
            manifest_path,
            Manifest(
                job_id=plan.plan_id,
                plan_id=plan.plan_id,
                revision=plan.revision,
                destination_subdir=subdir,
                status=status,
                entries=tuple(ordered),
                device=device_fingerprint(self._expected),
            ),
        )

    def _status(
        self, plan: MixtapePlan, verified: int, cancelled: bool, aborted: bool
    ) -> ExportStatus:
        if cancelled:
            return ExportStatus.CANCELLED
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


def _commit(dir_fd: int, temporary: str, name: str, destination: str) -> None:
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
        try:
            os.lstat(name, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
        else:
            raise ExportError(
                ExportErrorCode.EXISTS, "Файл уже существует, перезапись запрещена", path=destination
            ) from error
        os.rename(temporary, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    else:
        os.unlink(temporary, dir_fd=dir_fd)


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
