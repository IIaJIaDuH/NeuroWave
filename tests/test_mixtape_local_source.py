"""USB-02: локальный источник и политика прав без сети и USB.

Проверяются границы approved root: абсолютные пути, ``..``, NUL, скрытые
сегменты, симлинки наружу, не-аудио файлы и stream-only провайдеры не
превращаются в ExportAsset. Доступ к файлам зависит от явного решения
политики прав, а не от одного лишь существования файла.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from neurowave.domain import Track
from neurowave.mixtape import (
    ExportRight,
    MixtapeError,
    MixtapePlanner,
    MixtapeRequest,
    RightsStatus,
    SkipReason,
)
from neurowave.mixtape.rights import LocalExportRights
from neurowave.mixtape.sources.local import LocalExportSource

HOUR = 3_600_000


class DeniedRights:
    """Политика, запрещающая экспорт независимо от файла."""

    default_status = RightsStatus.DENIED

    def right_for(self, path: Path, root: Path) -> ExportRight:
        return ExportRight(status=RightsStatus.DENIED, basis="TEST_DENIED", detail="запрещено")


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "Music"
    (root / "Album").mkdir(parents=True)
    (root / "Artist - Song.mp3").write_bytes(b"ID3" + b"a" * 1000)
    (root / "Album" / "Track.flac").write_bytes(b"fLaC" + b"b" * 2000)
    (root / ".hidden.mp3").write_bytes(b"hidden")
    (root / "notes.txt").write_text("не музыка", encoding="utf-8")
    return root


def local_track(relative: str, *, duration_ms: int | None = 180_000, provider: str = "local") -> Track:
    return Track(
        id=relative,
        title=Path(relative).stem,
        artists=("Artist",),
        duration_ms=duration_ms,
        provider=provider,
    )


def confirmed(root) -> LocalExportSource:
    return LocalExportSource(root, rights=LocalExportRights(confirmed=True))


def by_key(assets) -> dict[str, object]:
    return {asset.track_key: asset for asset in assets}


# --- approved root --------------------------------------------------------


def test_missing_root_is_unavailable_and_empty(tmp_path):
    source = LocalExportSource(tmp_path / "missing")

    assert source.available is False
    assert source.list_eligible([local_track("Artist - Song.mp3")]) == ()


def test_filesystem_root_is_rejected():
    with pytest.raises(MixtapeError):
        LocalExportSource(Path("/"))


def test_cache_directory_is_not_an_export_source(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    root = cache / "Music"
    root.mkdir(parents=True)
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))

    with pytest.raises(MixtapeError):
        LocalExportSource(root)


def test_root_is_resolved_and_available(library):
    source = LocalExportSource(library)

    assert source.root == library.resolve()
    assert source.available is True


# --- контракт ExportAsset -------------------------------------------------


def test_local_track_becomes_asset(library):
    source = confirmed(library)
    track = local_track("Album/Track.flac")

    assets = source.list_eligible([track])
    asset = by_key(assets)[track.key]

    assert asset.source_id == "local"
    assert asset.media_format == "flac"
    assert asset.lossless is True
    assert asset.size_bytes == 2004
    assert asset.duration_ms == 180_000
    assert asset.rights is RightsStatus.PERMITTED
    assert asset.rights_basis == "USER_OWNED"
    assert asset.eligible is True
    assert asset.local_path == (library / "Album" / "Track.flac").resolve()


def test_asset_id_is_stable_and_does_not_leak_absolute_path(library):
    track = local_track("Artist - Song.mp3")

    first = by_key(confirmed(library).list_eligible([track]))[track.key]
    second = by_key(confirmed(library).list_eligible([track]))[track.key]

    assert first.asset_id == second.asset_id
    assert first.asset_id.startswith("local-")
    assert str(library) not in first.asset_id


def test_duplicate_candidates_yield_one_asset(library):
    track = local_track("Artist - Song.mp3")

    assets = confirmed(library).list_eligible([track, track])

    assert len(assets) == 1


def test_stream_only_provider_is_ignored(library):
    source = confirmed(library)
    yandex = local_track("Artist - Song.mp3", provider="yandex")
    spotify = local_track("Album/Track.flac", provider="spotify")

    assert source.list_eligible([yandex, spotify]) == ()


def test_non_audio_file_is_ignored(library):
    assert confirmed(library).list_eligible([local_track("notes.txt")]) == ()


def test_hidden_file_is_ignored(library):
    assert confirmed(library).list_eligible([local_track(".hidden.mp3")]) == ()


def test_hidden_directory_is_ignored(library):
    hidden = library / ".private"
    hidden.mkdir()
    (hidden / "Secret.mp3").write_bytes(b"secret")

    assert confirmed(library).list_eligible([local_track(".private/Secret.mp3")]) == ()


def test_empty_file_is_ignored(library):
    (library / "Empty.mp3").write_bytes(b"")

    assert confirmed(library).list_eligible([local_track("Empty.mp3")]) == ()


# --- path traversal и symlink --------------------------------------------

TRAVERSAL = [
    "../outside.mp3",
    "../../etc/passwd",
    "Album/../../outside.mp3",
    "/etc/passwd",
    "Album//etc/passwd",
]


@pytest.mark.parametrize("track_id", TRAVERSAL)
def test_path_escape_is_rejected(library, track_id):
    assert confirmed(library).list_eligible([local_track(track_id)]) == ()


def test_nul_byte_is_rejected(library):
    assert confirmed(library).list_eligible([local_track("Artist - Song\x00.mp3")]) == ()


def test_missing_file_is_ignored(library):
    assert confirmed(library).list_eligible([local_track("Album/Missing.mp3")]) == ()


def test_symlink_escaping_root_is_rejected(library, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.mp3"
    secret.write_bytes(b"outside-secret")
    before = hashlib.sha256(secret.read_bytes()).hexdigest()
    (library / "evil.mp3").symlink_to(secret)

    assert confirmed(library).list_eligible([local_track("evil.mp3")]) == ()
    assert hashlib.sha256(secret.read_bytes()).hexdigest() == before


def test_symlinked_directory_escaping_root_is_rejected(library, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.mp3").write_bytes(b"outside")
    (library / "linked").symlink_to(outside, target_is_directory=True)

    assert confirmed(library).list_eligible([local_track("linked/secret.mp3")]) == ()


def test_symlink_inside_root_is_allowed(library):
    (library / "inside.mp3").symlink_to(library / "Artist - Song.mp3")

    assets = confirmed(library).list_eligible([local_track("inside.mp3")])

    assert len(assets) == 1
    assert assets[0].local_path == (library / "Artist - Song.mp3").resolve()


def test_broken_symlink_is_ignored(library):
    (library / "broken.mp3").symlink_to(library / "missing.mp3")

    assert confirmed(library).list_eligible([local_track("broken.mp3")]) == ()


# --- политика прав --------------------------------------------------------


def test_unconfirmed_rights_are_unknown_and_not_eligible(library):
    source = LocalExportSource(library)
    track = local_track("Artist - Song.mp3")

    asset = by_key(source.list_eligible([track]))[track.key]

    assert asset.rights is RightsStatus.UNKNOWN
    assert asset.rights_basis == "NOT_CONFIRMED"
    assert asset.eligible is False


def test_confirmed_rights_allow_export(library):
    asset = by_key(confirmed(library).list_eligible([local_track("Artist - Song.mp3")]))[
        "local:Artist - Song.mp3"
    ]

    assert asset.rights is RightsStatus.PERMITTED
    assert asset.eligible is True


def test_custom_policy_can_deny_export(library):
    source = LocalExportSource(library, rights=DeniedRights())

    asset = source.list_eligible([local_track("Artist - Song.mp3")])[0]

    assert asset.rights is RightsStatus.DENIED
    assert asset.eligible is False


def test_rights_status_reports_policy_for_local_assets(library):
    source = confirmed(library)
    asset = source.list_eligible([local_track("Artist - Song.mp3")])[0]

    assert source.rights_status(asset.asset_id) is RightsStatus.PERMITTED
    assert source.rights_status("yandex:123") is RightsStatus.UNKNOWN
    assert source.rights_status("local-forged") is RightsStatus.UNKNOWN
    assert LocalExportSource(library).rights_status(asset.asset_id) is RightsStatus.UNKNOWN


# --- отсутствие I/O и интеграция с планировщиком -------------------------


def test_listing_does_not_read_file_contents(library, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("источник не должен читать содержимое файлов")

    monkeypatch.setattr(Path, "open", forbidden)

    assets = confirmed(library).list_eligible([local_track("Artist - Song.mp3")])

    assert len(assets) == 1


def test_listing_does_not_modify_library(library):
    before = {
        path.relative_to(library).as_posix(): path.read_bytes()
        for path in library.rglob("*")
        if path.is_file()
    }

    confirmed(library).list_eligible(
        [local_track("Artist - Song.mp3"), local_track("Album/Track.flac")]
    )

    after = {
        path.relative_to(library).as_posix(): path.read_bytes()
        for path in library.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_planner_exports_confirmed_local_file(library):
    source = confirmed(library)
    tracks = [local_track("Artist - Song.mp3"), local_track("Album/Track.flac")]

    plan = MixtapePlanner([source]).plan(
        MixtapeRequest(profile="road", target_duration_ms=HOUR, seed=3), tracks
    )

    assert len(plan.items) == 2
    assert {item.source_id for item in plan.items} == {"local"}
    assert all(item.operation.value == "copy" for item in plan.items)


def test_planner_reports_unknown_rights_instead_of_exporting(library):
    source = LocalExportSource(library)
    track = local_track("Artist - Song.mp3")

    plan = MixtapePlanner([source]).plan(
        MixtapeRequest(profile="road", target_duration_ms=HOUR, seed=3), [track]
    )

    assert plan.items == ()
    assert plan.skipped[0].reason is SkipReason.RIGHTS_UNKNOWN
    assert plan.shortage_ms == HOUR


def test_planner_does_not_export_stream_only_candidate(library):
    source = confirmed(library)
    stream_only = local_track("Artist - Song.mp3", provider="yandex")

    plan = MixtapePlanner([source]).plan(
        MixtapeRequest(profile="road", target_duration_ms=HOUR, seed=3), [stream_only]
    )

    assert plan.items == ()
    assert plan.skipped[0].reason is SkipReason.NO_ASSET
