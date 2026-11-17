"""Manifest задачи на USB: схема, безопасный разбор и атомарная запись.

Содержимое manifest с чужого носителя недоверенное: ограничиваются размер
файла и число записей, проверяются версия, типы и относительные пути. Ошибка
разбора не приводит к «оптимистичному» resume — writer отказывается работать.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from neurowave.mixtape.export.errors import ExportError, ExportErrorCode
from neurowave.mixtape.export.filenames import validate_relative

MANIFEST_NAME = "manifest.json"
SCHEMA_VERSION = 1
MAX_MANIFEST_BYTES = 1_048_576
MAX_ENTRIES = 10_000


@dataclass(frozen=True)
class ManifestEntry:
    track_key: str
    destination: str
    status: str
    size_bytes: int = 0
    sha256: str = ""
    error: str = ""


@dataclass(frozen=True)
class Manifest:
    job_id: str
    plan_id: str
    revision: int
    destination_subdir: str
    status: str
    entries: tuple[ManifestEntry, ...]
    device: Mapping[str, Any] | None = None
    playlist: str = ""
    schema_version: int = SCHEMA_VERSION
    updated_at: int = field(default_factory=lambda: int(time.time()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "job_id": self.job_id,
            "plan_id": self.plan_id,
            "revision": self.revision,
            "destination_subdir": self.destination_subdir,
            "device": dict(self.device) if self.device is not None else None,
            "status": self.status,
            "playlist": self.playlist,
            "updated_at": self.updated_at,
            "entries": [
                {
                    "track_key": entry.track_key,
                    "destination": entry.destination,
                    "status": entry.status,
                    "size_bytes": entry.size_bytes,
                    "sha256": entry.sha256,
                    "error": entry.error,
                }
                for entry in self.entries
            ],
        }


def load_manifest(path: Path) -> Manifest | None:
    """Прочитать manifest; отсутствие — ``None``, порча — ошибка."""

    try:
        stat = path.lstat()
    except FileNotFoundError:
        return None
    if not path.is_file() or path.is_symlink():
        raise ExportError(ExportErrorCode.MANIFEST_INVALID, "manifest не является обычным файлом")
    if stat.st_size > MAX_MANIFEST_BYTES:
        raise ExportError(ExportErrorCode.MANIFEST_INVALID, "manifest слишком большой")
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ExportError(ExportErrorCode.MANIFEST_INVALID, "manifest не читается") from error

    if not isinstance(data, dict):
        raise ExportError(ExportErrorCode.MANIFEST_INVALID, "manifest должен быть объектом")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ExportError(ExportErrorCode.MANIFEST_INVALID, "неизвестная версия manifest")
    subdir = data.get("destination_subdir")
    if not isinstance(subdir, str) or not subdir:
        raise ExportError(ExportErrorCode.MANIFEST_INVALID, "в manifest нет целевого каталога")

    raw_entries = data.get("entries")
    if not isinstance(raw_entries, list) or len(raw_entries) > MAX_ENTRIES:
        raise ExportError(ExportErrorCode.MANIFEST_INVALID, "некорректный список файлов")
    entries: list[ManifestEntry] = []
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            raise ExportError(ExportErrorCode.MANIFEST_INVALID, "запись manifest не объект")
        track_key = raw_entry.get("track_key")
        destination = raw_entry.get("destination")
        status = raw_entry.get("status")
        if not isinstance(track_key, str) or not isinstance(destination, str):
            raise ExportError(ExportErrorCode.MANIFEST_INVALID, "некорректная запись manifest")
        try:
            validate_relative(destination, subdir)
        except ValueError as error:
            raise ExportError(
                ExportErrorCode.MANIFEST_INVALID, "небезопасный путь в manifest"
            ) from error
        if not isinstance(status, str) or status not in {"verified", "failed", "skipped"}:
            raise ExportError(ExportErrorCode.MANIFEST_INVALID, "некорректный статус записи")
        size = raw_entry.get("size_bytes", 0)
        if not isinstance(size, int) or size < 0:
            raise ExportError(ExportErrorCode.MANIFEST_INVALID, "некорректный размер")
        sha = raw_entry.get("sha256", "")
        if not isinstance(sha, str) or (sha and not _is_hex_digest(sha)):
            raise ExportError(ExportErrorCode.MANIFEST_INVALID, "некорректный sha256")
        entries.append(
            ManifestEntry(
                track_key=track_key,
                destination=destination,
                status=status,
                size_bytes=size,
                sha256=sha,
                error=str(raw_entry.get("error") or ""),
            )
        )

    device = data.get("device")
    if device is not None and not isinstance(device, dict):
        raise ExportError(ExportErrorCode.MANIFEST_INVALID, "некорректное описание устройства")
    return Manifest(
        job_id=str(data.get("job_id") or ""),
        plan_id=str(data.get("plan_id") or ""),
        revision=int(data.get("revision") or 0),
        destination_subdir=subdir,
        status=str(data.get("status") or ""),
        entries=tuple(entries),
        device=device,
        playlist=str(data.get("playlist") or ""),
        schema_version=SCHEMA_VERSION,
        updated_at=int(data.get("updated_at") or 0),
    )


def write_manifest_atomic(path: Path, manifest: Manifest) -> None:
    """tmp → fsync → rename; права 0600, без секретов и абсолютных путей."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.tmp-{os.getpid()}"
    payload = json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2) + "\n"
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        os.replace(temporary, path)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise ExportError(ExportErrorCode.IO, f"manifest не записан: {error}") from error


def device_fingerprint(identity: Any) -> dict[str, Any] | None:
    if identity is None:
        return None
    return {
        "object_path": str(identity.udisks_object_path),
        "block_device": str(identity.block_device),
        "mountpoint": str(identity.mountpoint),
        "filesystem_uuid": identity.filesystem_uuid,
        "filesystem_type": identity.filesystem_type,
        "capacity_bytes": int(identity.capacity_bytes),
    }


def _is_hex_digest(value: str) -> bool:
    if len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def path_is_within(root: Path, candidate: Path) -> bool:
    try:
        return candidate.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def manifest_destination(subdir: str, relative: str) -> str:
    return str(PurePosixPath(subdir) / relative)
