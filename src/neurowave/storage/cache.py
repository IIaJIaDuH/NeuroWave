"""Ограниченный дисковый кеш: LRU + TTL + квота.

Кеш — только техническая буферизация прослушивания. Он не является
офлайн-библиотекой и не должен хранить контент дольше разрешённого срока.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from ..errors import CacheError

INDEX_NAME = ".index.json"


@dataclass(frozen=True)
class CacheStats:
    count: int
    bytes_used: int
    quota_bytes: int

    @property
    def megabytes_used(self) -> float:
        return round(self.bytes_used / 1024**2, 1)

    @property
    def megabytes_quota(self) -> float:
        return round(self.quota_bytes / 1024**2, 1)


@dataclass
class _Entry:
    key: str
    filename: str
    size: int
    created_at: float
    last_used: float


class MediaCache:
    def __init__(
        self,
        directory: Path | str,
        quota_bytes: int = 2 * 1024**3,
        ttl_seconds: int = 7 * 24 * 3600,
        clock: Callable[[], float] = time.time,
    ):
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._quota = max(0, int(quota_bytes))
        self._ttl = max(0, int(ttl_seconds))
        self._clock = clock
        self._entries: dict[str, _Entry] = {}
        self._index_file = self._dir / INDEX_NAME
        self._load_index()

    # --- публичное ----------------------------------------------------
    def has(self, key: str) -> bool:
        entry = self._entries.get(key)
        return entry is not None and not self._expired(entry)

    def get(self, key: str) -> Path | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if self._expired(entry):
            self._drop(key)
            return None
        path = self._dir / entry.filename
        if not path.exists():
            self._drop(key)
            return None
        entry.last_used = self._clock()
        self._save_index()
        return path

    def put_bytes(self, key: str, data: bytes) -> Path:
        if not data:
            raise CacheError("Нельзя кешировать пустые данные")
        size = len(data)
        if size > self._quota:
            raise CacheError("Трек больше разрешённой квоты кеша")
        self._make_room(size, protect=key)
        filename = self._filename_for(key)
        path = self._dir / filename
        fd, tmp_name = tempfile.mkstemp(prefix=".part-", dir=str(self._dir))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        now = self._clock()
        self._entries[key] = _Entry(
            key=key,
            filename=filename,
            size=size,
            created_at=now,
            last_used=now,
        )
        self._save_index()
        return path

    def store_file(self, key: str, source: Path | str) -> Path:
        source_path = Path(source)
        try:
            return self.put_bytes(key, source_path.read_bytes())
        except OSError as exc:
            raise CacheError(f"Не удалось прочитать {source_path}: {exc}") from exc

    def evict_expired(self) -> int:
        expired = [key for key, entry in self._entries.items() if self._expired(entry)]
        for key in expired:
            self._drop(key)
        if expired:
            self._save_index()
        return len(expired)

    def clear(self) -> None:
        for entry in list(self._entries.values()):
            try:
                (self._dir / entry.filename).unlink()
            except OSError:
                pass
        self._entries.clear()
        self._save_index()

    def stats(self) -> CacheStats:
        return CacheStats(
            count=len(self._entries),
            bytes_used=sum(entry.size for entry in self._entries.values()),
            quota_bytes=self._quota,
        )

    def set_quota(self, quota_bytes: int) -> None:
        self._quota = max(0, int(quota_bytes))
        self._evict_to_quota()
        self._save_index()

    # --- внутреннее ---------------------------------------------------
    def _expired(self, entry: _Entry) -> bool:
        if self._ttl <= 0:
            return False
        return self._clock() - entry.created_at > self._ttl

    def _drop(self, key: str) -> None:
        entry = self._entries.pop(key, None)
        if entry is None:
            return
        try:
            (self._dir / entry.filename).unlink()
        except OSError:
            pass

    def _filename_for(self, key: str) -> str:
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:40] + ".cache"

    def _make_room(self, incoming: int, protect: str) -> None:
        self.evict_expired()
        total = sum(entry.size for entry in self._entries.values())
        if total + incoming <= self._quota:
            return
        for entry in sorted(
            (e for k, e in self._entries.items() if k != protect),
            key=lambda e: e.last_used,
        ):
            self._drop(entry.key)
            total -= entry.size
            if total + incoming <= self._quota:
                break

    def _evict_to_quota(self) -> None:
        total = sum(entry.size for entry in self._entries.values())
        if total <= self._quota:
            return
        for entry in sorted(self._entries.values(), key=lambda e: e.last_used):
            self._drop(entry.key)
            total -= entry.size
            if total <= self._quota:
                break

    def _load_index(self) -> None:
        if not self._index_file.exists():
            return
        try:
            raw = json.loads(self._index_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for item in raw.get("entries", []) if isinstance(raw, dict) else []:
            try:
                entry = _Entry(**item)
            except TypeError:
                continue
            path = self._dir / entry.filename
            if not path.exists():
                continue
            entry.size = int(entry.size) if entry.size else path.stat().st_size
            self._entries[entry.key] = entry

    def _save_index(self) -> None:
        payload: dict[str, Any] = {
            "version": 1,
            "entries": [asdict(entry) for entry in self._entries.values()],
        }
        fd, tmp_name = tempfile.mkstemp(prefix=".index-", dir=str(self._dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            os.replace(tmp_name, self._index_file)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise


def download_to_cache(
    cache: MediaCache,
    key: str,
    url: str,
    timeout: float = 20.0,
    max_bytes: int | None = None,
) -> Path:
    """Скачать поток в кеш (для предзагрузки следующего трека)."""
    limit = max_bytes if max_bytes is not None else cache.stats().quota_bytes
    request = urllib.request.Request(url, headers={"User-Agent": "NeuroWave/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            data = response.read(limit + 1)
    except OSError as exc:
        raise CacheError(f"Не удалось загрузить трек в кеш: {exc}") from exc
    if len(data) > limit:
        raise CacheError("Ответ больше разрешённой квоты кеша")
    return cache.put_bytes(key, data)
