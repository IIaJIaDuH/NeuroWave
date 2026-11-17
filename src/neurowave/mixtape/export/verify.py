"""Проверка записанных файлов по журналу задачи.

``verify`` — это не «файл на месте», а «задача действительно завершилась и
каждый её файл совпал по sha256 и размеру». Поэтому отчёт ``ok`` требует
статуса ``complete``: ``partial``/``failed``/``cancelled`` и пункты в статусе
``failed``/``skipped`` считаются расхождением, а не успехом.

Модуль только читает: он не пишет на носитель и не меняет журнал.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

VERIFIED = "verified"
COMPLETE = "complete"


@dataclass(frozen=True)
class VerifyMismatch:
    destination: str
    error: str

    def to_dict(self) -> dict[str, str]:
        return {"destination": self.destination, "error": self.error}


@dataclass(frozen=True)
class VerificationReport:
    """Итог проверки без абсолютных путей: их показывает только явный вызов."""

    job_id: str
    status: str
    complete: bool
    checked: int
    total: int
    mismatches: tuple[VerifyMismatch, ...] = ()

    @property
    def ok(self) -> bool:
        return self.complete and self.total > 0 and not self.mismatches

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "job": self.job_id,
            "status": self.status,
            "complete": self.complete,
            "checked": self.checked,
            "total": self.total,
            "mismatches": [item.to_dict() for item in self.mismatches],
        }


def verify_job(job_id: str, job: Mapping[str, Any]) -> VerificationReport:
    """Сверить файлы из журнала задачи; абсолютные/выходящие пути отклоняются."""

    status = str(job.get("status") or "")
    target = Path(str(job.get("target") or ""))
    items = list(job.get("items") or [])
    mismatches: list[VerifyMismatch] = []
    checked = 0
    for item in items:
        destination = str(item.get("destination") or "")
        item_status = str(item.get("status") or "")
        if item_status != VERIFIED:
            mismatches.append(
                VerifyMismatch(
                    destination=destination,
                    error=f"not_verified:{item_status or 'unknown'}",
                )
            )
            continue
        checked += 1
        relative = Path(destination)
        if not destination or relative.is_absolute() or ".." in relative.parts:
            mismatches.append(VerifyMismatch(destination=destination, error="unsafe_path"))
            continue
        path = target / relative
        try:
            if path.is_symlink() or not path.is_file():
                raise OSError("файл отсутствует")
            digest = hash_file(path)
            if digest != item.get("sha256") or path.stat().st_size != int(
                item.get("size_bytes") or 0
            ):
                mismatches.append(
                    VerifyMismatch(destination=destination, error="hash_mismatch")
                )
        except OSError as error:
            mismatches.append(VerifyMismatch(destination=destination, error=str(error)))
    return VerificationReport(
        job_id=str(job_id),
        status=status,
        complete=status == COMPLETE,
        checked=checked,
        total=len(items),
        mismatches=tuple(mismatches),
    )


def hash_file(path: Path) -> str:
    """sha256 файла без перехода по симлинку."""

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
