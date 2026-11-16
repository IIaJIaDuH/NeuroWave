"""TinyTag migration: fields, kbps units, optional dependency and fallback."""

from __future__ import annotations

import sys
import types
import wave

from neurowave.local.adapter import LocalMusicAdapter, _read_tags


def test_tinytag_metadata_and_bitrate_are_in_kbps(tmp_path, monkeypatch) -> None:
    class FakeTinyTag:
        @staticmethod
        def get(_path):
            return types.SimpleNamespace(
                title="  Song  ",
                artist=" Primary ",
                albumartist="Fallback",
                album=" Album ",
                duration=123.456,
                bitrate=320.0,  # TinyTag gives kbps, NOT bits per second
                other={"artist": ["Second", "Primary"]},
            )

    fake_module = types.ModuleType("tinytag")
    fake_module.TinyTag = FakeTinyTag
    monkeypatch.setitem(sys.modules, "tinytag", fake_module)
    result = _read_tags(tmp_path / "example.mp3")
    assert result == {
        "title": "Song",
        "artists": ("Primary", "Second"),
        "album": "Album",
        "duration_ms": 123456,
        "bitrate_kbps": 320,
    }


def test_tinytag_missing_fields_and_invalid_numbers(tmp_path, monkeypatch) -> None:
    class FakeTinyTag:
        @staticmethod
        def get(_path):
            return types.SimpleNamespace(
                title=None,
                artist=None,
                albumartist=" Album Artist ",
                album=None,
                duration=float("nan"),
                bitrate=float("inf"),
                other={},
            )

    fake_module = types.ModuleType("tinytag")
    fake_module.TinyTag = FakeTinyTag
    monkeypatch.setitem(sys.modules, "tinytag", fake_module)
    assert _read_tags(tmp_path / "example.mp3") == {
        "artists": ("Album Artist",),
    }


def test_missing_optional_tinytag_uses_filename(tmp_path, monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "tinytag", None)
    root = tmp_path / "music"
    root.mkdir()
    (root / "Artist - Filename.mp3").write_bytes(b"invalid-audio")
    song = LocalMusicAdapter(root).tracks()[0]
    assert song.title == "Filename"
    assert song.artists == ("Artist",)


def test_tinytag_parse_exception_does_not_break_scan(tmp_path, monkeypatch) -> None:
    class BrokenTinyTag:
        @staticmethod
        def get(_path):
            raise ValueError("corrupt tags")

    fake_module = types.ModuleType("tinytag")
    fake_module.TinyTag = BrokenTinyTag
    monkeypatch.setitem(sys.modules, "tinytag", fake_module)
    root = tmp_path / "music"
    root.mkdir()
    (root / "Artist - Damaged.mp3").write_bytes(b"bad")
    assert LocalMusicAdapter(root).tracks()[0].title == "Damaged"


def test_real_tinytag_reads_wav_duration_and_kbps(tmp_path) -> None:
    import pytest

    pytest.importorskip("tinytag")
    path = tmp_path / "tone.wav"
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(48000)
        audio.writeframes(b"\x00\x00" * 48000)
    info = _read_tags(path)
    assert info["duration_ms"] == 1000
    assert info["bitrate_kbps"] == 768  # 48k mono * 16 bits
