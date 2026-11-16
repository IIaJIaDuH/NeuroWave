"""Локальный провайдер: файлы на диске, без сети и без выдуманных данных."""

from __future__ import annotations

import pytest

from neurowave.config import Settings
from neurowave.errors import TrackUnavailable
from neurowave.local import LocalMusicAdapter, default_root


@pytest.fixture
def music(tmp_path):
    root = tmp_path / "Music"
    (root / "Album").mkdir(parents=True)
    (root / "Artist - Song One.mp3").write_bytes(b"ID3fake")
    (root / "Album" / "Second Track.flac").write_bytes(b"fLaCfake")
    (root / ".hidden.mp3").write_bytes(b"hidden")
    (root / "readme.txt").write_text("не музыка", encoding="utf-8")
    return root


def test_scan_ignores_hidden_and_non_audio(music) -> None:
    adapter = LocalMusicAdapter(music)
    tracks = adapter.tracks()
    assert [track.id for track in tracks] == ["Album/Second Track.flac", "Artist - Song One.mp3"]
    assert all(track.provider == "local" for track in tracks)
    assert all(track.available for track in tracks)


def test_metadata_from_filenames_and_folders(music) -> None:
    adapter = LocalMusicAdapter(music)
    by_id = {track.id: track for track in adapter.tracks()}
    song = by_id["Artist - Song One.mp3"]
    assert song.title == "Song One"
    assert song.artists == ("Artist",)

    nested = by_id["Album/Second Track.flac"]
    assert nested.title == "Second Track"
    assert nested.album == "Album"


def test_search_is_case_insensitive_and_matches_album(music) -> None:
    adapter = LocalMusicAdapter(music)
    assert [track.title for track in adapter.search("artist").tracks] == ["Song One"]
    assert [track.id for track in adapter.search("ALBUM").tracks] == ["Album/Second Track.flac"]
    assert adapter.search("ничего-такого").tracks == ()


def test_stream_url_returns_real_path_and_quality(music) -> None:
    adapter = LocalMusicAdapter(music)
    flac = adapter.stream_url("Album/Second Track.flac")
    assert flac.url == str((music / "Album" / "Second Track.flac").resolve())
    assert flac.quality.lossless is True
    assert flac.quality.codec == "flac"

    mp3 = adapter.stream_url("Artist - Song One.mp3")
    assert mp3.quality.lossless is False
    assert mp3.quality.codec == "mp3"


def test_stream_url_rejects_escape_from_music_root(music, tmp_path) -> None:
    adapter = LocalMusicAdapter(music)
    outside = tmp_path / "secret.txt"
    outside.write_text("секрет", encoding="utf-8")

    for bad in ("../secret.txt", str(outside), "Album/../../secret.txt"):
        with pytest.raises(TrackUnavailable):
            adapter.stream_url(bad)


def test_stream_url_reports_missing_file(music) -> None:
    adapter = LocalMusicAdapter(music)
    with pytest.raises(TrackUnavailable):
        adapter.stream_url("Album/nope.mp3")


def test_wave_is_random_sample_of_library(music) -> None:
    adapter = LocalMusicAdapter(music)
    wave = adapter.wave_tracks()
    assert len(wave) == 2
    assert {track.id for track in wave} == {track.id for track in adapter.tracks()}


def test_empty_and_missing_directory(tmp_path) -> None:
    empty = tmp_path / "Empty"
    empty.mkdir()
    adapter = LocalMusicAdapter(empty)
    assert adapter.tracks() == []
    assert adapter.wave_tracks() == []
    assert adapter.exists is True

    missing = LocalMusicAdapter(tmp_path / "Missing")
    assert missing.exists is False
    assert missing.tracks() == []


def test_account_and_unsupported_actions(music) -> None:
    adapter = LocalMusicAdapter(music)
    account = adapter.account()
    assert "Локальная музыка" in account.title
    assert account.plus is None
    assert adapter.like("whatever") is False
    assert adapter.unlike("whatever") is False
    assert adapter.liked_tracks() == []
    assert adapter.playlists() == []


def test_refresh_sees_new_files(music) -> None:
    adapter = LocalMusicAdapter(music)
    assert len(adapter.tracks()) == 2
    (music / "Third.mp3").write_bytes(b"x")
    assert len(adapter.tracks()) == 2  # кеш каталога
    adapter.refresh()
    assert len(adapter.tracks()) == 3


def test_default_root_follows_settings(tmp_path) -> None:
    assert default_root(Settings(local_music_dir=str(tmp_path))) == tmp_path
    assert default_root(Settings()) == default_root()


# --- CLI на локальном провайдере --------------------------------------
def prepare_home(tmp_path):
    from neurowave.config import ConfigStore, Paths

    music = tmp_path / "Music"
    music.mkdir()
    (music / "Artist - Song.mp3").write_bytes(b"audio")
    (music / "Album").mkdir()
    (music / "Album" / "Second.flac").write_bytes(b"audio")
    home = tmp_path / "home"
    paths = Paths.resolve({"NEUROWAVE_HOME": str(home)}).ensure()
    ConfigStore(paths).save(Settings(local_music_dir=str(music)))
    return home, music


def test_cli_plays_local_library_without_login(tmp_path, capsys) -> None:
    from neurowave.cli import main

    home, _ = prepare_home(tmp_path)
    code = main(
        [
            "--home", str(home),
            "--provider", "local",
            "--backend", "null",
            "play", "Song",
            "--seconds", "1",
            "--no-interactive",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "▶" in out
    assert "Artist - Song" in out or "Song" in out


def test_cli_local_wave_is_shuffle_not_personal_radio(tmp_path, capsys) -> None:
    from neurowave.cli import main

    home, _ = prepare_home(tmp_path)
    code = main(
        ["--home", str(home), "--provider", "local", "--backend", "null",
         "wave", "--seconds", "1", "--no-interactive"]
    )
    captured = capsys.readouterr()
    assert code == 0
    assert "случайная выборка" in captured.err


def test_cli_local_library_command_explains_gap(tmp_path, capsys) -> None:
    from neurowave.cli import main

    home, _ = prepare_home(tmp_path)
    code = main(["--home", str(home), "--provider", "local", "library"])
    assert code == 2
    assert "не поддерживает" in capsys.readouterr().err


def test_cli_local_login_is_not_required(tmp_path, capsys) -> None:
    from neurowave.cli import main

    home, _ = prepare_home(tmp_path)
    assert main(["--home", str(home), "--provider", "local", "login"]) == 0
    assert "Вход не требуется" in capsys.readouterr().out


def test_cli_local_provider_listed_as_connected(tmp_path, capsys) -> None:
    from neurowave.cli import main

    home, _ = prepare_home(tmp_path)
    assert main(["--home", str(home), "providers"]) == 0
    out = capsys.readouterr().out
    assert "local" in out
    assert "вход не требуется" in out
