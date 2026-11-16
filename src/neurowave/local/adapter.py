"""Локальная библиотека: файлы на диске как музыкальный провайдер.

Никакой сети, аккаунта, DRM и кеша: файл уже лежит на диске, и `mpv` играет
его напрямую. Теги читаются через `tinytag` (MIT), если установлен extras
``[local]``; иначе используются имена файлов и папок — без выдумывания данных.

Все пути проверяются на выход за пределы музыкальной папки: ``id`` трека —
относительный путь, и он не должен уметь читать ``/etc/passwd``.
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

from ..domain import AccountInfo, Quality, SearchResults, StreamInfo, Track
from ..errors import TrackUnavailable

AUDIO_EXTENSIONS = {
    ".mp3",
    ".flac",
    ".ogg",
    ".oga",
    ".opus",
    ".m4a",
    ".mp4",
    ".aac",
    ".wav",
    ".aiff",
    ".aif",
    ".wma",
}
LOSSLESS_EXTENSIONS = {".flac", ".wav", ".aiff", ".aif"}
CODECS = {
    ".mp3": "mp3",
    ".flac": "flac",
    ".ogg": "vorbis",
    ".oga": "vorbis",
    ".opus": "opus",
    ".m4a": "aac",
    ".mp4": "aac",
    ".aac": "aac",
    ".wav": "pcm",
    ".aiff": "pcm",
    ".aif": "pcm",
    ".wma": "wma",
}
SCAN_LIMIT_DEFAULT = 5000
WAVE_LIMIT = 50


def default_root(settings: Any = None) -> Path:
    """Папка локальной музыки: из настроек, иначе ``~/Music``."""
    raw = getattr(settings, "local_music_dir", "") if settings is not None else ""
    raw = (raw or "").strip()
    return Path(raw).expanduser() if raw else Path.home() / "Music"


def _read_tags(path: Path) -> dict[str, Any]:
    """Теги через tinytag (MIT). Без него — пустой словарь и имена файлов.

    tinytag отдаёт bitrate в кбит/с и одну строку исполнителя, поэтому
    несколько исполнителей через «;» разделяются здесь же. Ошибка чтения
    тегов не должна делать файл недоступным: он всё равно играет.
    """
    try:
        from tinytag import TinyTag  # type: ignore
    except Exception:
        return {}
    try:
        tag = TinyTag.get(str(path))
    except Exception:
        return {}
    if tag is None:
        return {}

    def text(value: Any) -> str:
        return str(value).strip() if value is not None else ""

    result: dict[str, Any] = {}
    title = text(tag.title)
    if title:
        result["title"] = title
    artist = text(tag.artist) or text(tag.albumartist)
    if artist:
        result["artists"] = tuple(part.strip() for part in artist.split(";") if part.strip())
    album = text(tag.album)
    if album:
        result["album"] = album
    if tag.duration:
        result["duration_ms"] = int(float(tag.duration) * 1000)
    if tag.bitrate:
        result["bitrate_kbps"] = max(1, int(round(float(tag.bitrate))))
    return result


def _from_filename(path: Path, root: Path) -> tuple[str, tuple[str, ...], str | None]:
    """«Исполнитель - Название.mp3» и папка альбома как резервный источник."""
    stem = path.stem.strip()
    artists: tuple[str, ...] = ()
    title = stem
    if " - " in stem:
        possible_artist, _, possible_title = stem.partition(" - ")
        if possible_artist.strip() and possible_title.strip():
            artists = (possible_artist.strip(),)
            title = possible_title.strip()
    album = None
    parent = path.parent
    if parent != root and parent.name:
        album = parent.name
    return title or stem, artists, album


class LocalMusicAdapter:
    """Каталог файлов на диске, отдающий `mpv` прямые пути.

    Теги читает tinytag (MIT), если установлен extras `[local]`.
    """

    provider = "local"
    token: str | None = None  # для единообразия с сетевыми провайдерами

    def __init__(
        self,
        root: Path | str | None = None,
        limit: int = SCAN_LIMIT_DEFAULT,
        rng: random.Random | None = None,
    ):
        self._root = Path(root).expanduser().resolve() if root else default_root().resolve()
        self._limit = max(0, int(limit))
        self._rng = rng or random.Random()
        self._tracks: list[Track] | None = None
        self._bitrates: dict[str, int] = {}

    # --- каталог ------------------------------------------------------
    @property
    def root(self) -> Path:
        return self._root

    @property
    def exists(self) -> bool:
        return self._root.is_dir()

    def _files(self) -> list[Path]:
        if not self._root.is_dir():
            return []
        files: list[Path] = []
        for path in sorted(self._root.rglob("*")):
            if len(files) >= self._limit:
                break
            if not path.is_file():
                continue
            try:
                relative = path.relative_to(self._root)
            except ValueError:
                continue
            if any(part.startswith(".") for part in relative.parts):
                continue
            if path.suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            files.append(path)
        return files

    def tracks(self) -> list[Track]:
        if self._tracks is None:
            self._tracks = [self._make_track(path) for path in self._files()]
        return list(self._tracks)

    def refresh(self) -> None:
        """Забыть просканированное (файлы могли измениться)."""
        self._tracks = None
        self._bitrates.clear()

    def _make_track(self, path: Path) -> Track:
        relative = path.relative_to(self._root).as_posix()
        tags = _read_tags(path)
        fallback_title, fallback_artists, fallback_album = _from_filename(path, self._root)
        bitrate = tags.get("bitrate_kbps")
        if bitrate:
            self._bitrates[relative] = int(bitrate)
        return Track(
            id=relative,
            title=str(tags.get("title") or fallback_title),
            artists=tuple(tags.get("artists") or fallback_artists),
            album=tags.get("album") or fallback_album,
            duration_ms=tags.get("duration_ms"),
            available=True,
            provider=self.provider,
        )

    # --- контракт провайдера -----------------------------------------
    def search(self, text: str, limit: int = 20, kind: str = "all") -> SearchResults:
        needle = (text or "").strip().casefold()
        items = self.tracks()
        if needle:
            items = [
                track
                for track in items
                if needle in track.title.casefold()
                or needle in (track.album or "").casefold()
                or any(needle in artist.casefold() for artist in track.artists)
            ]
        return SearchResults(tracks=tuple(items[: max(0, int(limit))]))

    def wave_tracks(self) -> list[Track]:
        """Не персонализация, а случайная выборка: честно объявлено в возможностях."""
        items = [track for track in self.tracks() if track.available]
        if not items:
            return []
        count = len(items) if len(items) <= WAVE_LIMIT else WAVE_LIMIT
        return self._rng.sample(items, count)

    def stream_url(self, track_id: str) -> StreamInfo:
        relative = Path(str(track_id))
        if relative.is_absolute() or ".." in relative.parts:
            raise TrackUnavailable("Некорректный путь к локальному файлу")
        candidate = (self._root / relative).resolve()
        if not self._inside_root(candidate):
            raise TrackUnavailable("Файл вне музыкальной папки")
        if not candidate.is_file():
            raise TrackUnavailable(f"Файл не найден: {track_id}")
        suffix = candidate.suffix.lower()
        quality = Quality(
            codec=CODECS.get(suffix),
            bitrate_kbps=self._bitrates.get(relative.as_posix()),
            lossless=suffix in LOSSLESS_EXTENSIONS,
        )
        return StreamInfo(url=str(candidate), quality=quality)

    def account(self) -> AccountInfo:
        name = self._root.name or str(self._root)
        return AccountInfo(
            display_name=f"Локальная музыка · {name}",
            login=None,
            service_available=self.exists,
            plus=None,
        )

    def playlists(self) -> list[Any]:
        return []

    def liked_tracks(self, limit: int = 100) -> list[Track]:
        return []

    def like(self, track_id: str) -> bool:
        return False

    def unlike(self, track_id: str) -> bool:
        return False

    def _inside_root(self, candidate: Path) -> bool:
        return candidate.is_relative_to(self._root)
