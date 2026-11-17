"""M3U8 для Car USB: UTF-8, относительные пути, только проверенные файлы."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Sequence

PLAYLIST_NAME = "playlist.m3u8"


@dataclass(frozen=True)
class PlaylistEntry:
    destination: str
    duration_ms: int
    artist: str
    title: str

    def label(self) -> str:
        artist = _one_line(self.artist) or "Неизвестный исполнитель"
        title = _one_line(self.title) or "Без названия"
        return f"{artist} - {title}"


def render_m3u8(entries: Sequence[PlaylistEntry], subdir: str) -> str:
    """Пути относительно самой папки плейлиста, без абсолютных путей."""

    lines = ["#EXTM3U"]
    folder = PurePosixPath(subdir)
    for entry in entries:
        try:
            relative = PurePosixPath(entry.destination).relative_to(folder)
        except ValueError:
            continue
        seconds = max(0, int(entry.duration_ms) // 1000)
        lines.append(f"#EXTINF:{seconds},{entry.label()}")
        lines.append(str(relative))
    return "\n".join(lines) + "\n"


def _one_line(value: str) -> str:
    return " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())
