"""Детерминированный планировщик USB-сборки.

Планировщик работает только с неизменяемыми моделями и метаданными источников:
он не открывает файлы, не ходит в сеть, не читает БД и не трогает USB.
Одинаковые кандидаты, seed и веса профиля дают одинаковый план.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
from dataclasses import dataclass
from typing import Sequence

from neurowave.domain import Track
from neurowave.mixtape.errors import MixtapeError
from neurowave.mixtape.match import normalize_text, recording_identity, same_recording
from neurowave.mixtape.models import (
    PROFILES,
    ExportAsset,
    ExportOperation,
    MixtapePlan,
    MixtapeRequest,
    PlannedItem,
    ProfilePolicy,
    RightsStatus,
    SkipReason,
    SkippedCandidate,
    TasteSnapshot,
)
from neurowave.mixtape.sources.base import ExportSource

MIN_TARGET_DURATION_MS = 60 * 60 * 1000
MAX_TARGET_DURATION_MS = 12 * 60 * 60 * 1000
OUTPUT_FORMATS = frozenset({"original", "mp3", "flac"})
MP3_ESTIMATE_BITRATE_BPS = 320_000
RECENCY_HALF_LIFE_DAYS = 14.0
PLAN_REVISION = 1

_INVALID_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
_SPACES = re.compile(r"\s+")


@dataclass(frozen=True)
class _Resolved:
    track: Track
    asset: ExportAsset
    duration_ms: int
    operation: ExportOperation
    estimated_bytes: int
    score: float
    reason: str
    identity: tuple[str, str]


class MixtapePlanner:
    """Строит :class:`MixtapePlan` из кандидатов и разрешённых файлов."""

    def __init__(self, sources: Sequence[ExportSource] = ()) -> None:
        self._sources = tuple(sources)

    def plan(
        self,
        request: MixtapeRequest,
        candidates: Sequence[Track],
        taste: TasteSnapshot | None = None,
    ) -> MixtapePlan:
        policy = _validate(request)
        taste = taste or TasteSnapshot.empty()
        candidates = tuple(candidates)
        assets_by_key = self._collect_assets(candidates)

        skipped: list[SkippedCandidate] = []
        resolved: list[_Resolved] = []
        seen_keys: set[str] = set()
        excluded_artists = {
            normalize_text(name) for name in request.excluded_artists if normalize_text(name)
        }

        for track in candidates:
            if track.key in seen_keys:
                _skip(skipped, track, SkipReason.DUPLICATE, "повтор в списке кандидатов")
                continue
            seen_keys.add(track.key)

            if track.key in request.excluded_track_keys:
                _skip(skipped, track, SkipReason.EXCLUDED_TRACK, "исключён пользователем")
                continue
            if any(normalize_text(artist) in excluded_artists for artist in track.artists):
                _skip(skipped, track, SkipReason.EXCLUDED_ARTIST, "исполнитель исключён")
                continue
            if not track.available:
                _skip(skipped, track, SkipReason.UNAVAILABLE, "трек недоступен")
                continue

            asset, reason, detail = _pick_asset(assets_by_key.get(track.key, ()))
            if asset is None:
                _skip(skipped, track, reason or SkipReason.NO_ASSET, detail)
                continue

            duration_ms = asset.duration_ms or track.duration_ms
            if not duration_ms or duration_ms <= 0:
                _skip(skipped, track, SkipReason.NO_DURATION, "длительность неизвестна")
                continue

            media_format = (asset.media_format or "").strip().lower().lstrip(".")
            if request.output_format == "flac" and not asset.lossless:
                _skip(
                    skipped,
                    track,
                    SkipReason.NOT_LOSSLESS,
                    f"FLAC-only, исходник {media_format or 'unknown'}",
                )
                continue

            operation, estimated_bytes = _estimate(asset, media_format, request, duration_ms)
            score, reason_text = _score(track, policy, taste, request.reference_time_ms)
            resolved.append(
                _Resolved(
                    track=track,
                    asset=asset,
                    duration_ms=int(duration_ms),
                    operation=operation,
                    estimated_bytes=estimated_bytes,
                    score=score,
                    reason=reason_text,
                    identity=recording_identity(track.title, track.artists),
                )
            )

        ranked = _rank(resolved, request.seed)
        unique = self._deduplicate(ranked, request, skipped)
        items, total_duration, total_bytes = _select(unique, request, policy, skipped)

        shortage_ms = max(0, request.target_duration_ms - total_duration)
        plan_id = _plan_id(request, items)
        skipped.sort(key=lambda item: (item.track_key, item.reason.value, item.detail))

        return MixtapePlan(
            plan_id=plan_id,
            request=request,
            items=tuple(items),
            candidate_count=len(candidates),
            eligible_count=len(resolved),
            skipped=tuple(skipped),
            estimated_bytes=total_bytes,
            estimated_duration_ms=total_duration,
            shortage_ms=shortage_ms,
            revision=PLAN_REVISION,
        )

    def _collect_assets(self, candidates: Sequence[Track]) -> dict[str, list[ExportAsset]]:
        wanted = {track.key for track in candidates}
        by_key: dict[str, list[ExportAsset]] = {}
        for source in self._sources:
            for asset in source.list_eligible(candidates):
                if asset.track_key and asset.track_key in wanted:
                    by_key.setdefault(asset.track_key, []).append(asset)
        return by_key

    def _deduplicate(
        self,
        ranked: Sequence[_Resolved],
        request: MixtapeRequest,
        skipped: list[SkippedCandidate],
    ) -> list[_Resolved]:
        unique: list[_Resolved] = []
        seen_assets: set[str] = set()
        for candidate in ranked:
            if candidate.asset.asset_id in seen_assets:
                _skip(
                    skipped,
                    candidate.track,
                    SkipReason.DUPLICATE,
                    f"тот же файл: {candidate.asset.asset_id}",
                )
                continue
            if request.strict_unique:
                duplicate = next(
                    (
                        kept
                        for kept in unique
                        if kept.identity == candidate.identity
                        and same_recording(kept.duration_ms, candidate.duration_ms)
                    ),
                    None,
                )
                if duplicate is not None:
                    _skip(
                        skipped,
                        candidate.track,
                        SkipReason.DUPLICATE,
                        f"та же запись: {duplicate.track.key}",
                    )
                    continue
            seen_assets.add(candidate.asset.asset_id)
            unique.append(candidate)
        return unique


def _validate(request: MixtapeRequest) -> ProfilePolicy:
    policy = PROFILES.get(str(request.profile))
    if policy is None:
        available = ", ".join(sorted(PROFILES))
        raise MixtapeError(f"неизвестный профиль {request.profile!r}; доступны: {available}")
    target = int(request.target_duration_ms)
    if not MIN_TARGET_DURATION_MS <= target <= MAX_TARGET_DURATION_MS:
        raise MixtapeError(
            "длительность подборки должна быть от 1 до 12 часов, "
            f"получено {target} мс"
        )
    if request.max_bytes is not None and int(request.max_bytes) <= 0:
        raise MixtapeError("max_bytes должен быть положительным или None")
    if request.output_format not in OUTPUT_FORMATS:
        raise MixtapeError(
            f"неизвестный формат {request.output_format!r}; "
            f"доступны: {', '.join(sorted(OUTPUT_FORMATS))}"
        )
    subdir = str(request.destination_subdir or "").strip("/")
    if not subdir or str(request.destination_subdir).startswith("/") or ".." in subdir.split("/"):
        raise MixtapeError("destination_subdir должен быть относительным путём без '..'")
    return policy


def _pick_asset(
    assets: Sequence[ExportAsset],
) -> tuple[ExportAsset | None, SkipReason | None, str]:
    for asset in assets:
        if asset.exportable:
            return asset, None, ""
    for asset in assets:
        if asset.rights is RightsStatus.DENIED:
            return None, SkipReason.RIGHTS_DENIED, f"источник {asset.source_id} запретил файл"
    for asset in assets:
        if asset.rights is RightsStatus.UNKNOWN:
            return None, SkipReason.RIGHTS_UNKNOWN, f"права источника {asset.source_id} неизвестны"
    return None, SkipReason.NO_ASSET, "нет разрешённого файла в источниках"


def _estimate(
    asset: ExportAsset,
    media_format: str,
    request: MixtapeRequest,
    duration_ms: int,
) -> tuple[ExportOperation, int]:
    if request.output_format == "mp3" and media_format != "mp3":
        # Предварительная оценка для резерва места; точный размер даст транскодер.
        estimated = max(1, int(duration_ms) * MP3_ESTIMATE_BITRATE_BPS // 8 // 1000)
        return ExportOperation.TRANSCODE, estimated
    return ExportOperation.COPY, max(0, int(asset.size_bytes))


def _score(
    track: Track,
    policy: ProfilePolicy,
    taste: TasteSnapshot,
    reference_time_ms: int | None,
) -> tuple[float, str]:
    plays = max(0, int(taste.play_counts.get(track.key, 0) or 0))
    liked = bool(track.liked or track.key in taste.liked_keys)
    recency = 0.0
    last_played = taste.last_played_ms.get(track.key)
    if reference_time_ms is not None and last_played is not None:
        age_days = max(0, int(reference_time_ms) - int(last_played)) / 86_400_000
        recency = math.exp(-age_days / RECENCY_HALF_LIFE_DAYS)

    score = (
        policy.liked_weight * (1.0 if liked else 0.0)
        + policy.plays_weight * math.log1p(plays)
        + policy.recency_weight * recency
    )
    if liked:
        reason = "лайк"
    elif plays >= policy.frequent_plays:
        reason = f"часто слушаешь ({plays} раз)"
    elif recency >= 0.5:
        reason = "недавно слушал"
    else:
        reason = "из локальной библиотеки"
    return round(score, 6), reason


def _rank(resolved: Sequence[_Resolved], seed: int) -> list[_Resolved]:
    """Score по убыванию; seed задаёт только детерминированный tie-break."""

    rng = random.Random(int(seed))
    tiebreak = {
        candidate.track.key: rng.random()
        for candidate in sorted(resolved, key=lambda item: item.track.key)
    }
    return sorted(
        resolved,
        key=lambda item: (-item.score, tiebreak[item.track.key], item.track.key),
    )


def _select(
    ranked: Sequence[_Resolved],
    request: MixtapeRequest,
    policy: ProfilePolicy,
    skipped: list[SkippedCandidate],
) -> tuple[list[PlannedItem], int, int]:
    items: list[PlannedItem] = []
    total_duration = 0
    total_bytes = 0
    artist_counts: dict[str, int] = {}
    subdir = str(request.destination_subdir).strip("/")

    for candidate in ranked:
        if total_duration + candidate.duration_ms > request.target_duration_ms:
            _skip(
                skipped,
                candidate.track,
                SkipReason.OVER_DURATION,
                "не помещается в целевую длительность",
            )
            continue
        if (
            request.max_bytes is not None
            and total_bytes + candidate.estimated_bytes > request.max_bytes
        ):
            _skip(skipped, candidate.track, SkipReason.OVER_BYTES, "не помещается в лимит байт")
            continue

        artist = normalize_text(candidate.track.artists[0]) if candidate.track.artists else ""
        if policy.max_per_artist is not None and artist_counts.get(artist, 0) >= policy.max_per_artist:
            _skip(
                skipped,
                candidate.track,
                SkipReason.ARTIST_LIMIT,
                f"профиль {policy.name}: не больше {policy.max_per_artist} треков исполнителя",
            )
            continue

        index = len(items) + 1
        items.append(
            PlannedItem(
                source_asset_id=candidate.asset.asset_id,
                source_id=candidate.asset.source_id,
                track_key=candidate.track.key,
                title=candidate.track.title,
                artists=candidate.track.artists,
                destination_relative=_destination(subdir, index, candidate),
                operation=candidate.operation,
                estimated_bytes=candidate.estimated_bytes,
                duration_ms=candidate.duration_ms,
                score=candidate.score,
                reason=candidate.reason,
                source_hash=None,
            )
        )
        total_duration += candidate.duration_ms
        total_bytes += candidate.estimated_bytes
        artist_counts[artist] = artist_counts.get(artist, 0) + 1

    return items, total_duration, total_bytes


def _destination(subdir: str, index: int, candidate: _Resolved) -> str:
    artist = _safe_name(candidate.track.artists[0] if candidate.track.artists else "", "Неизвестный исполнитель")
    title = _safe_name(candidate.track.title, "Без названия")
    extension = _extension(candidate.asset, candidate.operation)
    return f"{subdir}/{index:03d} - {artist} - {title}.{extension}"


def _safe_name(value: str | None, fallback: str) -> str:
    cleaned = _INVALID_FILENAME.sub(" ", str(value or ""))
    cleaned = _SPACES.sub(" ", cleaned).strip(" .")
    return cleaned[:80] or fallback


def _extension(asset: ExportAsset, operation: ExportOperation) -> str:
    if operation is ExportOperation.TRANSCODE:
        return "mp3"
    extension = re.sub(r"[^a-z0-9]+", "", (asset.media_format or "").strip().lower().lstrip("."))
    return extension[:5] or "bin"


def _plan_id(request: MixtapeRequest, items: Sequence[PlannedItem]) -> str:
    payload = {
        "revision": PLAN_REVISION,
        "request": request.to_dict(),
        "items": [
            [item.source_asset_id, item.track_key, item.destination_relative, item.estimated_bytes]
            for item in items
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return f"mixtape-{digest[:16]}"


def _skip(
    skipped: list[SkippedCandidate],
    track: Track,
    reason: SkipReason,
    detail: str = "",
) -> None:
    skipped.append(
        SkippedCandidate(
            track_key=track.key,
            title=track.title,
            reason=reason,
            detail=detail,
        )
    )
