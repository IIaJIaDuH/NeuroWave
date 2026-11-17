"""Локальный источник экспортируемых файлов для USB Mixtape.

Разрешён только явно одобренный root. Каждый путь проверяется на абсолютность,
``..``, скрытые сегменты, NUL, симлинк за пределы root и не-аудио формат.
Stream-only провайдеры (Yandex, SoundCloud, Spotify) сюда не попадают: источник
смотрит только на локальные треки и реальные файлы.

Модуль не ходит в сеть и не трогает USB. Файлы не читаются: размер берётся из
``stat``, длительность и теги — из каталога. Повторное открытие с защитой от
TOCTOU делает writer на следующем этапе.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Sequence

from neurowave.domain import Track
from neurowave.local.adapter import AUDIO_EXTENSIONS, LOSSLESS_EXTENSIONS
from neurowave.mixtape.errors import MixtapeError
from neurowave.mixtape.models import ExportAsset, RightsStatus
from neurowave.mixtape.rights import ExportRightsPolicy, LocalExportRights

SOURCE_ID = "local"


def _cache_root() -> Path:
    configured = os.environ.get("XDG_CACHE_HOME", "").strip()
    base = Path(configured).expanduser() if configured else Path.home() / ".cache"
    return base.resolve()


class LocalExportSource:
    """Превращает локальные треки в :class:`ExportAsset` внутри approved root."""

    source_id = SOURCE_ID

    def __init__(
        self,
        root: Path | str,
        *,
        rights: ExportRightsPolicy | None = None,
    ) -> None:
        raw = Path(root).expanduser()
        try:
            resolved = raw.resolve()
        except (OSError, RuntimeError) as error:
            raise MixtapeError(f"Не удалось разрешить музыкальную папку: {error}") from error
        if resolved == Path(resolved.anchor):
            raise MixtapeError("Корень файловой системы не может быть библиотекой экспорта")
        cache = _cache_root()
        if resolved.is_relative_to(cache):
            raise MixtapeError(
                "Кеш приложения не является источником экспорта: выберите музыкальную папку"
            )
        self._root = resolved
        self._rights: ExportRightsPolicy = rights or LocalExportRights(confirmed=False)
        self._issued: dict[str, Path] = {}

    @property
    def root(self) -> Path:
        return self._root

    @property
    def available(self) -> bool:
        return self._root.is_dir()

    @property
    def rights(self) -> ExportRightsPolicy:
        return self._rights

    def list_eligible(self, candidates: Sequence[Track]) -> Sequence[ExportAsset]:
        """Вернуть assets с их правами; только ``PERMITTED`` реально eligible.

        Отклонённые и неизвестные права тоже возвращаются — планировщик должен
        объяснить пользователю причину пропуска, а не молча потерять трек.
        """

        if not self.available:
            return ()
        assets: list[ExportAsset] = []
        seen: set[str] = set()
        for track in candidates:
            if track.provider not in (None, self.source_id):
                continue
            if track.key in seen:
                continue
            resolved = self._resolve(track.id)
            if resolved is None:
                continue
            try:
                stat = resolved.stat()
            except OSError:
                continue
            if not stat.st_size:
                continue
            right = self._rights.right_for(resolved, self._root)
            seen.add(track.key)
            suffix = resolved.suffix.lower()
            asset_id = _asset_id(resolved.relative_to(self._root).as_posix())
            self._issued[asset_id] = resolved
            assets.append(
                ExportAsset(
                    asset_id=asset_id,
                    track_key=track.key,
                    source_id=self.source_id,
                    media_format=suffix.lstrip("."),
                    size_bytes=int(stat.st_size),
                    rights=right.status,
                    rights_basis=right.basis,
                    duration_ms=track.duration_ms,
                    lossless=suffix in LOSSLESS_EXTENSIONS,
                    local_path=resolved,
                    eligible=right.status is RightsStatus.PERMITTED,
                )
            )
        return tuple(assets)

    def rights_status(self, asset_id: str) -> RightsStatus:
        """Только по-настоящему выданный этим источником asset получает статус.

        Произвольная строка ``local-...`` (например, из чужого manifest) не
        считается доказательством права и получает ``UNKNOWN``.
        """

        path = self._issued.get(str(asset_id))
        if path is None:
            return RightsStatus.UNKNOWN
        return self._rights.right_for(path, self._root).status

    def _resolve(self, track_id: str) -> Path | None:
        text = str(track_id or "")
        if not text or "\x00" in text:
            return None
        relative = Path(text)
        if relative.is_absolute() or ".." in relative.parts:
            return None
        if any(part.startswith(".") for part in relative.parts):
            return None
        if relative.suffix.lower() not in AUDIO_EXTENSIONS:
            return None
        try:
            resolved = (self._root / relative).resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            return None
        if not resolved.is_relative_to(self._root):
            return None
        if any(part.startswith(".") for part in resolved.relative_to(self._root).parts):
            return None
        try:
            if not resolved.is_file():
                return None
        except OSError:
            return None
        if resolved.suffix.lower() not in AUDIO_EXTENSIONS:
            return None
        return resolved


def _asset_id(relative_posix: str) -> str:
    digest = hashlib.sha256(relative_posix.encode("utf-8")).hexdigest()[:16]
    return f"local-{digest}"
