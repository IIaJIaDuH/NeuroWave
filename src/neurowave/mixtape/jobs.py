"""Локальный журнал задач USB Mixtape и флаги отмены.

Файлы лежат в XDG data с правами 0600. Это не USB-manifest: здесь только
отчёт, цель и статус, без токенов и содержимого треков.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Mapping

from neurowave.config import atomic_write_json
from neurowave.mixtape.export.writer import ExportReport

JOB_SCHEMA_VERSION = 1
MAX_JOB_BYTES = 1_048_576
_JOB_ID = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


class JobStore:
    def __init__(self, root: Path | str) -> None:
        self._root = Path(root).expanduser()

    @property
    def root(self) -> Path:
        return self._root

    def _path(self, job_id: str) -> Path:
        if not _JOB_ID.match(str(job_id or "")):
            raise ValueError("Некорректный id задачи")
        return self._root / f"{job_id}.json"

    def _cancel_path(self, job_id: str) -> Path:
        if not _JOB_ID.match(str(job_id or "")):
            raise ValueError("Некорректный id задачи")
        return self._root / f"{job_id}.cancel"

    def save(self, job_id: str, data: Mapping[str, Any]) -> None:
        payload = dict(data)
        payload["schema_version"] = JOB_SCHEMA_VERSION
        payload["updated_at"] = int(time.time())
        atomic_write_json(self._path(job_id), payload)

    def load(self, job_id: str) -> dict[str, Any] | None:
        path = self._path(job_id)
        try:
            stat = path.lstat()
        except FileNotFoundError:
            return None
        if path.is_symlink() or not path.is_file() or stat.st_size > MAX_JOB_BYTES:
            raise ValueError("Журнал задачи повреждён")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("Журнал задачи не читается") from error
        if not isinstance(data, dict) or data.get("schema_version") != JOB_SCHEMA_VERSION:
            raise ValueError("Неизвестная схема журнала задачи")
        return data

    def list_ids(self) -> list[str]:
        if not self._root.is_dir():
            return []
        ids: list[str] = []
        for path in sorted(self._root.glob("*.json")):
            if _JOB_ID.match(path.stem):
                ids.append(path.stem)
        return ids

    def request_cancel(self, job_id: str) -> None:
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self._cancel_path(job_id)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, b"cancel\n")
            os.fsync(fd)
        finally:
            os.close(fd)

    def cancel_requested(self, job_id: str) -> bool:
        try:
            return self._cancel_path(job_id).exists()
        except ValueError:
            return False

    def clear_cancel(self, job_id: str) -> None:
        try:
            self._cancel_path(job_id).unlink(missing_ok=True)
        except (OSError, ValueError):
            pass


class CancelSignal:
    """Флаг отмены задачи, который читает writer между чанками."""

    def __init__(self, store: JobStore, job_id: str) -> None:
        self._store = store
        self._job_id = job_id

    def is_set(self) -> bool:
        return self._store.cancel_requested(self._job_id)


def job_record(
    report: ExportReport,
    *,
    target: str,
    object_path: str,
    library: str,
) -> dict[str, Any]:
    """Запись журнала по отчёту writer'а (общая для CLI и GUI)."""

    return {
        "status": report.status.value,
        "plan_id": report.plan_id,
        "target": str(target),
        "object_path": str(object_path),
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
