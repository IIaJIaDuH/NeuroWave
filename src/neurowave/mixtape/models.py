"""Неизменяемые модели USB Mixtape.

Модуль не выполняет файловых, сетевых и USB-операций: здесь только данные,
которые планировщик получает от источников и отдаёт наружу. ``local_path``
хранится для будущего writer, но планировщик его не открывает и не проверяет.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


class RightsStatus(str, Enum):
    """Право на автономный файл, а не на прослушивание потока."""

    PERMITTED = "permitted"
    DENIED = "denied"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ExportRight:
    """Решение политики прав: статус плюс основание для manifest/preview."""

    status: RightsStatus
    basis: str = "unknown"
    detail: str = ""


class ExportOperation(str, Enum):
    COPY = "copy"
    TRANSCODE = "transcode"
    SKIP = "skip"


class SkipReason(str, Enum):
    """Стабильные причины пропуска: их показывает preview и CLI --json."""

    EXCLUDED_TRACK = "excluded_track"
    EXCLUDED_ARTIST = "excluded_artist"
    UNAVAILABLE = "unavailable"
    NO_ASSET = "no_asset"
    RIGHTS_DENIED = "rights_denied"
    RIGHTS_UNKNOWN = "rights_unknown"
    NO_DURATION = "no_duration"
    NOT_LOSSLESS = "not_lossless"
    DUPLICATE = "duplicate"
    ARTIST_LIMIT = "artist_limit"
    OVER_DURATION = "over_duration"
    OVER_BYTES = "over_bytes"


@dataclass(frozen=True)
class ExportAsset:
    """Конкретный файл, который источник разрешает скопировать.

    ``track_key`` связывает файл с каталоговым треком, но право на файл
    определяется только :attr:`rights` и :attr:`eligible`; ``DIRECT_STREAM``
    и кеш плеера сюда не превращаются.
    """

    asset_id: str
    track_key: str | None
    source_id: str
    media_format: str
    size_bytes: int
    rights: RightsStatus = RightsStatus.UNKNOWN
    rights_basis: str = ""
    duration_ms: int | None = None
    lossless: bool = False
    local_path: Path | None = None
    eligible: bool = True

    @property
    def exportable(self) -> bool:
        return self.eligible and self.rights is RightsStatus.PERMITTED


@dataclass(frozen=True)
class TasteSnapshot:
    """Снимок локальных сигналов вкуса; сеть и БД не нужны."""

    liked_keys: frozenset[str] = frozenset()
    play_counts: Mapping[str, int] = field(default_factory=dict)
    last_played_ms: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "liked_keys", frozenset(self.liked_keys))
        object.__setattr__(self, "play_counts", MappingProxyType(dict(self.play_counts)))
        object.__setattr__(self, "last_played_ms", MappingProxyType(dict(self.last_played_ms)))

    @classmethod
    def empty(cls) -> "TasteSnapshot":
        return cls()


@dataclass(frozen=True)
class ProfilePolicy:
    """Шаблон профиля: данные, а не ветвления в коде планировщика."""

    name: str
    description: str
    liked_weight: float
    plays_weight: float
    recency_weight: float
    max_per_artist: int | None
    frequent_plays: int = 5


PROFILES: Mapping[str, ProfilePolicy] = MappingProxyType(
    {
        "car": ProfilePolicy(
            name="car",
            description="Car USB: совместимая папка, немного повторов исполнителя",
            liked_weight=2.0,
            plays_weight=1.0,
            recency_weight=0.5,
            max_per_artist=3,
        ),
        "road": ProfilePolicy(
            name="road",
            description="Дорога: баланс любимого и разнообразия",
            liked_weight=3.0,
            plays_weight=1.0,
            recency_weight=1.0,
            max_per_artist=4,
        ),
        "party": ProfilePolicy(
            name="party",
            description="Вечеринка: шире по исполнителям, выше вес прослушиваний",
            liked_weight=2.0,
            plays_weight=1.5,
            recency_weight=1.0,
            max_per_artist=6,
        ),
        "chill": ProfilePolicy(
            name="chill",
            description="Спокойное: любимое и недавнее, меньше одного исполнителя",
            liked_weight=3.0,
            plays_weight=1.0,
            recency_weight=1.5,
            max_per_artist=3,
        ),
        "favorites": ProfilePolicy(
            name="favorites",
            description="Только избранное, без ограничения на исполнителя",
            liked_weight=5.0,
            plays_weight=1.0,
            recency_weight=0.5,
            max_per_artist=None,
        ),
    }
)


@dataclass(frozen=True)
class MixtapeRequest:
    """Параметры подборки. Значения проверяет планировщик."""

    profile: str = "road"
    target_duration_ms: int = 4 * 60 * 60 * 1000
    max_bytes: int | None = None
    output_format: str = "original"
    strict_unique: bool = True
    seed: int = 0
    excluded_track_keys: frozenset[str] = frozenset()
    excluded_artists: tuple[str, ...] = ()
    destination_subdir: str = "MUSIC/NeuroWave"
    reference_time_ms: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "excluded_track_keys", frozenset(self.excluded_track_keys))
        object.__setattr__(self, "excluded_artists", tuple(self.excluded_artists))

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "target_duration_ms": self.target_duration_ms,
            "max_bytes": self.max_bytes,
            "output_format": self.output_format,
            "strict_unique": self.strict_unique,
            "seed": self.seed,
            "excluded_track_keys": sorted(self.excluded_track_keys),
            "excluded_artists": list(self.excluded_artists),
            "destination_subdir": self.destination_subdir,
            "reference_time_ms": self.reference_time_ms,
        }


@dataclass(frozen=True)
class PlannedItem:
    source_asset_id: str
    source_id: str
    track_key: str
    title: str
    artists: tuple[str, ...]
    destination_relative: str
    operation: ExportOperation
    estimated_bytes: int
    duration_ms: int
    score: float
    reason: str
    source_hash: str | None = None


@dataclass(frozen=True)
class SkippedCandidate:
    track_key: str
    title: str
    reason: SkipReason
    detail: str = ""


@dataclass(frozen=True)
class MixtapePlan:
    plan_id: str
    request: MixtapeRequest
    items: tuple[PlannedItem, ...]
    candidate_count: int
    eligible_count: int
    skipped: tuple[SkippedCandidate, ...]
    estimated_bytes: int
    estimated_duration_ms: int
    shortage_ms: int
    revision: int = 1

    @property
    def is_complete(self) -> bool:
        """Цель по времени достигнута; причины пропусков остаются в отчёте."""

        return self.shortage_ms == 0

    def to_dict(self) -> dict[str, Any]:
        """Preview для CLI/JSON; абсолютные пути и права сюда не попадают."""

        return {
            "plan_id": self.plan_id,
            "revision": self.revision,
            "request": self.request.to_dict(),
            "candidate_count": self.candidate_count,
            "eligible_count": self.eligible_count,
            "estimated_bytes": self.estimated_bytes,
            "estimated_duration_ms": self.estimated_duration_ms,
            "shortage_ms": self.shortage_ms,
            "is_complete": self.is_complete,
            "items": [
                {
                    "asset_id": item.source_asset_id,
                    "source": item.source_id,
                    "track_key": item.track_key,
                    "title": item.title,
                    "artists": list(item.artists),
                    "destination": item.destination_relative,
                    "operation": item.operation.value,
                    "estimated_bytes": item.estimated_bytes,
                    "duration_ms": item.duration_ms,
                    "score": item.score,
                    "reason": item.reason,
                }
                for item in self.items
            ],
            "skipped": [
                {
                    "track_key": skip.track_key,
                    "title": skip.title,
                    "reason": skip.reason.value,
                    "detail": skip.detail,
                }
                for skip in self.skipped
            ],
        }
