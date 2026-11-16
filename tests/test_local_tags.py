"""Теги локальных файлов: tinytag (MIT) вместо GPL-mutagen.

Тесты не требуют ffmpeg: WAV с RIFF INFO-тегами собирается вручную, а tinytag
его читает. Отдельно проверяется, что GPL-зависимости в проекте не осталось и
что поломанный файл не делает трек недоступным.
"""

from __future__ import annotations

import struct
import sys
import wave
from pathlib import Path

import pytest

from neurowave.local.adapter import LocalMusicAdapter, _read_tags

ROOT = Path(__file__).resolve().parents[1]


def _info_chunk(fields: list[tuple[str, str]]) -> bytes:
    body = b"INFO"
    for chunk_id, text in fields:
        data = text.encode("utf-8") + b"\x00"
        body += chunk_id.encode() + struct.pack("<I", len(data)) + data
        if len(data) % 2:
            body += b"\x00"
    return b"LIST" + struct.pack("<I", len(body)) + body


def make_tagged_wav(path: Path, *, artist: str = "Artist A;Artist B") -> Path:
    import io

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x00" * 800)  # 0.1 с
    raw = buffer.getvalue()
    chunk = _info_chunk([("INAM", "Tone Title"), ("IART", artist), ("IPRD", "Album X")])
    out = raw[:12] + chunk + raw[12:]
    out = out[:4] + struct.pack("<I", len(out) - 8) + out[8:]
    path.write_bytes(out)
    return path


def test_tags_are_read_with_tinytag(tmp_path) -> None:
    source = make_tagged_wav(tmp_path / "Song.wav")
    tags = _read_tags(source)
    assert tags["title"] == "Tone Title"
    assert tags["artists"] == ("Artist A", "Artist B")  # «;» — разделитель
    assert tags["album"] == "Album X"
    assert tags["duration_ms"] == pytest.approx(100, abs=5)
    # tinytag отдаёт кбит/с, а не бит/с — единицы проверяем явно.
    assert tags["bitrate_kbps"] == 128


def test_single_artist_is_kept_whole(tmp_path) -> None:
    source = make_tagged_wav(tmp_path / "Solo.wav", artist="AC/DC")
    assert _read_tags(source)["artists"] == ("AC/DC",)


def test_adapter_uses_tags_for_track(tmp_path) -> None:
    root = tmp_path / "Music"
    root.mkdir()
    make_tagged_wav(root / "file-name.wav")
    track = LocalMusicAdapter(root).tracks()[0]
    assert track.title == "Tone Title"
    assert track.artists == ("Artist A", "Artist B")
    assert track.album == "Album X"


def test_without_tinytag_falls_back_to_filename(tmp_path, monkeypatch) -> None:
    root = tmp_path / "Music"
    root.mkdir()
    make_tagged_wav(root / "Artist - Song.wav")
    monkeypatch.setitem(sys.modules, "tinytag", None)  # имитируем отсутствие extras
    assert _read_tags(root / "Artist - Song.wav") == {}
    track = LocalMusicAdapter(root).tracks()[0]
    assert track.title == "Song"
    assert track.artists == ("Artist",)


def test_broken_file_is_still_playable(tmp_path) -> None:
    broken = tmp_path / "broken.wav"
    broken.write_bytes(b"not audio at all")
    assert _read_tags(broken) == {}
    track = LocalMusicAdapter(tmp_path).tracks()[0]
    assert track.available is True
    assert track.title == "broken"


def test_no_gpl_mutagen_left_in_project() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "mutagen" not in pyproject.lower()
    assert "tinytag" in pyproject
    assert 'license = "MIT"' in pyproject

    offenders = [
        str(path)
        for path in (ROOT / "src").rglob("*.py")
        if "mutagen" in path.read_text(encoding="utf-8").lower()
    ]
    assert offenders == []


def test_license_file_is_mit() -> None:
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert text.startswith("MIT License")
    assert "Copyright (c) 2026 NeuroWave contributors" in text
    assert "WITHOUT WARRANTY" in text
