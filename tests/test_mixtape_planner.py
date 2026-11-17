"""USB-01: детерминированный планировщик без файлов, сети и USB.

Проверяются обещания этапа: одинаковый seed и библиотека дают одинаковый
план, бюджет времени/байт соблюдается, дубликаты между провайдерами не
превращаются в два файла, а нехватка разрешённых источников показывается
честно, без выдуманных треков.
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from neurowave.domain import Track
from neurowave.mixtape import (
    PROFILES,
    ExportAsset,
    MixtapeError,
    MixtapePlanner,
    MixtapeRequest,
    RightsStatus,
    SkipReason,
    TasteSnapshot,
)
from neurowave.mixtape.models import ExportOperation

HOUR = 3_600_000
MINUTE = 60_000


class FakeSource:
    """Фейковый источник: отдаёт заранее заданные assets и ничего не читает."""

    def __init__(self, assets, source_id: str = "local") -> None:
        self.source_id = source_id
        self.assets = tuple(assets)
        self.calls: list[tuple[str, ...]] = []

    def list_eligible(self, candidates):
        self.calls.append(tuple(track.key for track in candidates))
        wanted = {track.key for track in candidates}
        return tuple(asset for asset in self.assets if asset.track_key in wanted)

    def rights_status(self, asset_id: str) -> RightsStatus:
        for asset in self.assets:
            if asset.asset_id == asset_id:
                return asset.rights
        return RightsStatus.UNKNOWN


def track(
    number: int,
    *,
    provider: str = "local",
    title: str | None = None,
    artist: str | None = None,
    artists: tuple[str, ...] | None = None,
    duration_ms: int | None = 3 * MINUTE,
    liked: bool = False,
    available: bool = True,
) -> Track:
    return Track(
        id=str(number),
        title=title if title is not None else f"Song {number}",
        artists=artists if artists is not None else (artist or f"Artist {number}",),
        duration_ms=duration_ms,
        liked=liked,
        available=available,
        provider=provider,
    )


def asset(
    item: Track,
    *,
    asset_id: str | None = None,
    rights: RightsStatus = RightsStatus.PERMITTED,
    size_bytes: int = 5_000_000,
    media_format: str = "mp3",
    lossless: bool = False,
    duration_ms: int | None = None,
    eligible: bool = True,
    source_id: str = "local",
) -> ExportAsset:
    return ExportAsset(
        asset_id=asset_id or f"asset-{item.key}",
        track_key=item.key,
        source_id=source_id,
        media_format=media_format,
        size_bytes=size_bytes,
        rights=rights,
        duration_ms=item.duration_ms if duration_ms is None else duration_ms,
        lossless=lossless,
        local_path=Path("/nonexistent/library") / f"{item.id}.mp3",
        eligible=eligible,
    )


def request(**overrides) -> MixtapeRequest:
    defaults = dict(profile="road", target_duration_ms=HOUR, seed=7)
    defaults.update(overrides)
    return MixtapeRequest(**defaults)


def build(candidates, assets, *, taste=None, sources=None, **overrides):
    planner = MixtapePlanner(sources if sources is not None else [FakeSource(assets)])
    return planner.plan(request(**overrides), candidates, taste)


def keys(plan) -> list[str]:
    return [item.track_key for item in plan.items]


def skip_reason(plan, track_key: str) -> SkipReason:
    for skipped in plan.skipped:
        if skipped.track_key == track_key:
            return skipped.reason
    raise AssertionError(f"{track_key} не пропущен: {plan.skipped}")


# --- детерминизм и seed ---------------------------------------------------


def test_same_seed_and_library_produce_identical_plan():
    candidates = [track(i, duration_ms=10 * MINUTE) for i in range(1, 7)]
    assets = [asset(item, size_bytes=8_000_000) for item in candidates]

    first = build(candidates, assets)
    second = build(candidates, assets)

    assert first.plan_id == second.plan_id
    assert first == second


def test_plan_does_not_depend_on_candidate_order():
    candidates = [track(i, duration_ms=10 * MINUTE) for i in range(1, 7)]
    assets = [asset(item) for item in candidates]

    forward = build(candidates, assets)
    backward = build(list(reversed(candidates)), list(reversed(assets)))

    assert forward.plan_id == backward.plan_id
    assert keys(forward) == keys(backward)


def test_seed_changes_order_and_selection_between_equal_candidates():
    candidates = [track(i, duration_ms=15 * MINUTE) for i in range(1, 7)]
    assets = [asset(item) for item in candidates]

    first = build(candidates, assets, seed=1)
    second = build(candidates, assets, seed=2)

    assert len(first.items) == len(second.items) == 4
    assert keys(first) != keys(second)


def test_seed_is_reproducible_after_repeat():
    candidates = [track(i, duration_ms=15 * MINUTE) for i in range(1, 7)]
    assets = [asset(item) for item in candidates]

    assert build(candidates, assets, seed=42).plan_id == build(candidates, assets, seed=42).plan_id


# --- время и размер -------------------------------------------------------


def test_target_duration_is_respected_and_filled():
    candidates = [track(i, duration_ms=10 * MINUTE) for i in range(1, 9)]
    assets = [asset(item) for item in candidates]

    plan = build(candidates, assets)

    assert len(plan.items) == 6
    assert plan.estimated_duration_ms == HOUR
    assert plan.shortage_ms == 0
    assert plan.is_complete is True


def test_plan_never_exceeds_target_duration():
    candidates = [track(i, duration_ms=25 * MINUTE) for i in range(1, 4)]
    assets = [asset(item) for item in candidates]

    plan = build(candidates, assets)

    assert plan.estimated_duration_ms == 50 * MINUTE
    assert plan.shortage_ms == 10 * MINUTE
    assert skip_reason(plan, "local:3") is SkipReason.OVER_DURATION


def test_exact_boundary_is_included():
    candidates = [track(1, duration_ms=30 * MINUTE), track(2, duration_ms=30 * MINUTE)]
    assets = [asset(item) for item in candidates]

    plan = build(candidates, assets)

    assert len(plan.items) == 2
    assert plan.shortage_ms == 0


def test_shortage_is_reported_without_inventing_tracks():
    candidates = [track(1), track(2)]
    assets = [asset(item) for item in candidates]

    plan = build(candidates, assets)

    assert plan.estimated_duration_ms == 6 * MINUTE
    assert plan.shortage_ms == HOUR - 6 * MINUTE
    assert plan.is_complete is False
    assert len(plan.items) == 2
    assert plan.eligible_count == 2


def test_empty_library_reports_full_shortage():
    plan = build([], [])

    assert plan.items == ()
    assert plan.candidate_count == 0
    assert plan.shortage_ms == HOUR
    assert plan.is_complete is False


def test_max_bytes_limits_plan_and_is_reported():
    candidates = [track(i, duration_ms=10 * MINUTE) for i in range(1, 7)]
    assets = [asset(item, size_bytes=10_000_000) for item in candidates]

    plan = build(candidates, assets, target_duration_ms=4 * HOUR, max_bytes=25_000_000)

    assert len(plan.items) == 2
    assert plan.estimated_bytes == 20_000_000
    assert plan.estimated_bytes <= 25_000_000
    assert skip_reason(plan, "local:3") is SkipReason.OVER_BYTES


def test_estimated_bytes_is_sum_of_selected_items():
    candidates = [track(i, duration_ms=10 * MINUTE) for i in range(1, 4)]
    assets = [asset(item, size_bytes=1_234_567) for item in candidates]

    plan = build(candidates, assets)

    assert plan.estimated_bytes == sum(item.estimated_bytes for item in plan.items)
    assert plan.estimated_bytes == 3_703_701


# --- дедупликация между провайдерами --------------------------------------


def test_same_recording_from_two_providers_is_deduplicated():
    local = track(1, provider="local", title="Night Drive", artist="The Band")
    yandex = track(42, provider="yandex", title="Night Drive", artist="The Band")
    candidates = [local, yandex]
    assets = [asset(local), asset(yandex, source_id="local", size_bytes=7_000_000)]

    plan = build(candidates, assets)

    assert len(plan.items) == 1
    assert plan.eligible_count == 2
    duplicates = [skip for skip in plan.skipped if skip.reason is SkipReason.DUPLICATE]
    assert len(duplicates) == 1
    assert duplicates[0].track_key != plan.items[0].track_key
    assert plan.items[0].track_key in duplicates[0].detail


def test_remaster_with_different_duration_is_kept():
    original = track(1, provider="local", title="Night Drive", artist="The Band", duration_ms=180_000)
    remaster = track(2, provider="yandex", title="Night Drive", artist="The Band", duration_ms=245_000)

    plan = build([original, remaster], [asset(original), asset(remaster)], target_duration_ms=HOUR)

    assert len(plan.items) == 2
    assert not plan.skipped


def test_non_strict_mode_keeps_versions_from_different_assets():
    first = track(1, title="Night Drive", artist="The Band")
    second = track(2, provider="yandex", title="Night Drive", artist="The Band")

    plan = build([first, second], [asset(first), asset(second)], strict_unique=False)

    assert len(plan.items) == 2


def test_same_asset_id_is_used_only_once():
    first = track(1, title="One")
    second = track(2, title="Two")
    assets = [
        asset(first, asset_id="shared-asset"),
        asset(second, asset_id="shared-asset"),
    ]

    plan = build([first, second], assets, strict_unique=False)

    assert len(plan.items) == 1
    duplicates = [skip for skip in plan.skipped if skip.reason is SkipReason.DUPLICATE]
    assert len(duplicates) == 1
    assert duplicates[0].track_key != plan.items[0].track_key


def test_first_source_wins_for_same_track():
    item = track(1)
    first = FakeSource([asset(item, asset_id="first", size_bytes=1_000)], source_id="local")
    second = FakeSource([asset(item, asset_id="second", size_bytes=2_000)], source_id="other")

    plan = build([item], [], sources=[first, second])

    assert plan.items[0].source_asset_id == "first"
    assert plan.items[0].source_id == "local"


# --- права и причины пропуска ---------------------------------------------


def test_unknown_rights_are_never_guessed():
    item = track(1)

    plan = build([item], [asset(item, rights=RightsStatus.UNKNOWN)])

    assert plan.items == ()
    assert skip_reason(plan, item.key) is SkipReason.RIGHTS_UNKNOWN
    assert plan.shortage_ms == HOUR


def test_denied_rights_are_reported():
    item = track(1)

    plan = build([item], [asset(item, rights=RightsStatus.DENIED)])

    assert plan.items == ()
    assert skip_reason(plan, item.key) is SkipReason.RIGHTS_DENIED


def test_ineligible_asset_is_not_exported():
    item = track(1)

    plan = build([item], [asset(item, eligible=False)])

    assert plan.items == ()
    assert skip_reason(plan, item.key) is SkipReason.NO_ASSET


def test_missing_asset_is_reported():
    item = track(1)

    plan = build([item], [])

    assert plan.items == ()
    assert skip_reason(plan, item.key) is SkipReason.NO_ASSET


def test_unavailable_track_is_skipped():
    item = track(1, available=False)

    plan = build([item], [asset(item)])

    assert plan.items == ()
    assert skip_reason(plan, item.key) is SkipReason.UNAVAILABLE


def test_track_without_duration_is_skipped():
    item = track(1, duration_ms=None)

    plan = build([item], [asset(item, duration_ms=None)])

    assert plan.items == ()
    assert skip_reason(plan, item.key) is SkipReason.NO_DURATION


def test_excluded_track_key_is_skipped():
    item = track(1)

    plan = build([item], [asset(item)], excluded_track_keys=frozenset({item.key}))

    assert plan.items == ()
    assert skip_reason(plan, item.key) is SkipReason.EXCLUDED_TRACK
    assert plan.candidate_count == 1


def test_excluded_artist_is_case_insensitive():
    item = track(1, artist="BÄND")

    plan = build([item], [asset(item)], excluded_artists=("bänd",))

    assert plan.items == ()
    assert skip_reason(plan, item.key) is SkipReason.EXCLUDED_ARTIST


def test_profile_limits_tracks_per_artist():
    candidates = [track(i, artist="One Band", duration_ms=10 * MINUTE) for i in range(1, 7)]
    assets = [asset(item) for item in candidates]

    plan = build(candidates, assets, profile="car")

    assert len(plan.items) == 3
    assert sum(1 for s in plan.skipped if s.reason is SkipReason.ARTIST_LIMIT) == 3
    assert plan.estimated_duration_ms == 30 * MINUTE


def test_skipped_entries_are_sorted_for_stable_output():
    first = track(10)
    second = track(2)
    assets = [
        asset(first, rights=RightsStatus.UNKNOWN),
        asset(second, rights=RightsStatus.UNKNOWN),
    ]

    plan = build([first, second], assets)

    assert [skip.track_key for skip in plan.skipped] == ["local:10", "local:2"]


# --- форматы -------------------------------------------------------------


def test_flac_only_rejects_lossy_source():
    item = track(1)

    plan = build([item], [asset(item, media_format="mp3", lossless=False)], output_format="flac")

    assert plan.items == ()
    assert skip_reason(plan, item.key) is SkipReason.NOT_LOSSLESS


def test_flac_lossless_source_is_copied():
    item = track(1)

    plan = build([item], [asset(item, media_format="flac", lossless=True)], output_format="flac")

    assert plan.items[0].operation is ExportOperation.COPY
    assert plan.items[0].destination_relative.endswith(".flac")


def test_mp3_output_transcodes_lossless_source():
    item = track(1, duration_ms=240_000)
    source_asset = asset(item, media_format="flac", lossless=True, size_bytes=30_000_000)

    plan = build([item], [source_asset], output_format="mp3")

    planned = plan.items[0]
    assert planned.operation is ExportOperation.TRANSCODE
    assert planned.estimated_bytes == 9_600_000
    assert planned.destination_relative.endswith(".mp3")


def test_mp3_source_is_copied_without_transcode():
    item = track(1)

    plan = build([item], [asset(item, media_format="mp3", size_bytes=4_321_000)], output_format="mp3")

    assert plan.items[0].operation is ExportOperation.COPY
    assert plan.items[0].estimated_bytes == 4_321_000


# --- объяснимость и вкус --------------------------------------------------


def test_liked_track_goes_first_with_reason():
    plain = track(1, artist="A")
    liked = track(2, artist="B", liked=True)

    plan = build([plain, liked], [asset(plain), asset(liked)], target_duration_ms=HOUR)

    assert plan.items[0].track_key == liked.key
    assert plan.items[0].reason == "лайк"
    assert plan.items[1].reason == "из локальной библиотеки"


def test_play_count_and_recency_are_explained():
    ref = 1_800_000_000_000
    frequent = track(1, artist="A")
    recent = track(2, artist="B")
    taste = TasteSnapshot(
        play_counts={frequent.key: 10},
        last_played_ms={recent.key: ref - 86_400_000},
    )

    plan = build(
        [frequent, recent],
        [asset(frequent), asset(recent)],
        taste=taste,
        reference_time_ms=ref,
    )

    reasons = {item.track_key: item.reason for item in plan.items}
    assert reasons[frequent.key] == "часто слушаешь (10 раз)"
    assert reasons[recent.key] == "недавно слушал"


def test_recency_is_ignored_without_reference_time():
    ref = 1_800_000_000_000
    item = track(1)
    taste = TasteSnapshot(last_played_ms={item.key: ref - 86_400_000})

    plan = build([item], [asset(item)], taste=taste, reference_time_ms=None)

    assert plan.items[0].reason == "из локальной библиотеки"


def test_scores_are_stable_and_non_negative():
    candidates = [track(i) for i in range(1, 4)]
    assets = [asset(item) for item in candidates]

    first = build(candidates, assets)
    second = build(candidates, assets)

    assert [item.score for item in first.items] == [item.score for item in second.items]
    assert all(item.score >= 0 for item in first.items)


# --- пути, модели, сериализация -------------------------------------------


def test_destination_stays_inside_requested_subdir():
    item = track(1, artist="AC/DC", title="../../etc/passwd")

    plan = build([item], [asset(item, media_format="mp3")])

    destination = Path(plan.items[0].destination_relative)
    assert plan.items[0].destination_relative.startswith("MUSIC/NeuroWave/")
    assert ".." not in destination.parts
    assert destination.parts[2].endswith(".mp3")
    assert "/" not in destination.parts[2]


def test_destination_numbering_follows_plan_order():
    candidates = [track(i, duration_ms=10 * MINUTE) for i in range(1, 4)]
    assets = [asset(item) for item in candidates]

    plan = build(candidates, assets)

    assert plan.items[0].destination_relative.split("/")[-1].startswith("001 - ")
    assert plan.items[1].destination_relative.split("/")[-1].startswith("002 - ")
    assert plan.items[2].destination_relative.split("/")[-1].startswith("003 - ")


def test_plan_to_dict_is_preview_json():
    item = track(1)
    plan = build([item], [asset(item, rights=RightsStatus.UNKNOWN)])

    payload = plan.to_dict()
    encoded = json.dumps(payload, ensure_ascii=False)

    assert payload["shortage_ms"] == HOUR
    assert payload["is_complete"] is False
    assert payload["skipped"][0]["reason"] == "rights_unknown"
    assert "local_path" not in encoded
    assert "nonexistent" not in encoded


def test_models_are_immutable():
    item = track(1)
    plan = build([item], [asset(item)])

    with pytest.raises(FrozenInstanceError):
        plan.request.seed = 1  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        plan.items[0].title = "changed"  # type: ignore[misc]


def test_planner_does_not_require_existing_files():
    item = track(1)
    source_asset = asset(item)
    assert not source_asset.local_path.exists()

    plan = build([item], [source_asset])

    assert len(plan.items) == 1


# --- валидация запроса ----------------------------------------------------


@pytest.mark.parametrize("target", [0, 59 * MINUTE, 13 * HOUR])
def test_target_duration_outside_one_to_twelve_hours_is_rejected(target):
    with pytest.raises(MixtapeError):
        build([], [], target_duration_ms=target)


def test_unknown_profile_is_rejected():
    with pytest.raises(MixtapeError) as error:
        build([], [], profile="road-trip")

    assert "road-trip" in str(error.value)


def test_unknown_output_format_is_rejected():
    with pytest.raises(MixtapeError):
        build([], [], output_format="ogg")


def test_absolute_or_parent_subdir_is_rejected():
    with pytest.raises(MixtapeError):
        build([], [], destination_subdir="/media/usb")
    with pytest.raises(MixtapeError):
        build([], [], destination_subdir="../outside")


def test_non_positive_max_bytes_is_rejected():
    with pytest.raises(MixtapeError):
        build([], [], max_bytes=0)


def test_every_profile_has_positive_weights():
    assert set(PROFILES) == {"car", "road", "party", "chill", "favorites"}
    for policy in PROFILES.values():
        assert policy.liked_weight > 0
        assert policy.plays_weight > 0
        assert policy.recency_weight >= 0
        assert policy.max_per_artist is None or policy.max_per_artist > 0
