"""Доменные модели, не зависящие от API и UI."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence


def format_duration(ms: int | None) -> str:
    """Человекочитаемая длительность: ``3:07`` или ``1:02:03``."""
    if not ms or ms < 0:
        return "--:--"
    total = int(ms) // 1000
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


@dataclass(frozen=True)
class Track:
    """Одна композиция в терминах приложения, а не конкретного API.

    ``id`` уникален только внутри провайдера: «123» у Яндекса и «123» у
    SoundCloud — разные треки. Для истории, кеша и снимка очереди
    используется :attr:`key` — квалифицированный ``provider:track_id``.
    """

    id: str
    title: str
    artists: tuple[str, ...] = ()
    album: str | None = None
    duration_ms: int | None = None
    available: bool = True
    liked: bool = False
    provider: str | None = None

    @property
    def key(self) -> str:
        """Квалифицированный ID для истории, кеша и очереди."""
        if self.provider:
            return f"{self.provider}:{self.id}"
        return str(self.id)

    @classmethod
    def split_key(cls, key: str) -> tuple[str | None, str]:
        """Разобрать ``provider:track_id`` (без провайдера — ``(None, key)``)."""
        provider, separator, raw = str(key).partition(":")
        if not separator:
            return None, str(key)
        return (provider or None), raw

    @property
    def artists_line(self) -> str:
        return ", ".join(self.artists) if self.artists else "Неизвестный исполнитель"

    @property
    def duration_text(self) -> str:
        return format_duration(self.duration_ms)

    @property
    def label(self) -> str:
        return f"{self.artists_line} — {self.title}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "artists": list(self.artists),
            "album": self.album,
            "duration_ms": self.duration_ms,
            "available": self.available,
            "liked": self.liked,
            "provider": self.provider,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Track":
        return cls(
            id=str(data["id"]),
            title=str(data.get("title") or "Без названия"),
            artists=tuple(data.get("artists") or ()),
            album=data.get("album"),
            duration_ms=data.get("duration_ms"),
            available=bool(data.get("available", True)),
            liked=bool(data.get("liked", False)),
            provider=data.get("provider") or None,
        )


@dataclass(frozen=True)
class NamedRef:
    """Ссылка на исполнителя, альбом или плейлист (id + подпись)."""

    id: str
    name: str
    subtitle: str | None = None


@dataclass(frozen=True)
class Quality:
    """Фактическое качество источника; ``lossless`` не выводится из подписки."""

    codec: str | None = None
    bitrate_kbps: int | None = None
    lossless: bool = False

    def label(self) -> str:
        if not self.codec and not self.bitrate_kbps:
            return "качество источника неизвестно"
        codec = (self.codec or "?").upper()
        if self.bitrate_kbps:
            return f"{codec} {self.bitrate_kbps} kbps"
        return codec


@dataclass(frozen=True)
class StreamInfo:
    """Разрешённая ссылка на поток и её качество."""

    url: str
    quality: Quality = field(default_factory=Quality)


@dataclass(frozen=True)
class SearchResults:
    tracks: tuple[Track, ...] = ()
    artists: tuple[NamedRef, ...] = ()
    albums: tuple[NamedRef, ...] = ()
    playlists: tuple[NamedRef, ...] = ()

    def is_empty(self) -> bool:
        return not (self.tracks or self.artists or self.albums or self.playlists)


@dataclass(frozen=True)
class AccountInfo:
    uid: int | None = None
    login: str | None = None
    display_name: str | None = None
    service_available: bool = True
    plus: bool | None = None

    @property
    def title(self) -> str:
        return self.display_name or self.login or "Аккаунт Яндекс Музыки"


class RepeatMode(str, Enum):
    OFF = "off"
    ALL = "all"
    ONE = "one"

    @classmethod
    def parse(cls, value: str | "RepeatMode" | None) -> "RepeatMode":
        if isinstance(value, RepeatMode):
            return value
        try:
            return cls(str(value or "off").lower())
        except ValueError:
            return cls.OFF


def unique_tracks(tracks: Iterable[Track]) -> tuple[Track, ...]:
    """Убрать повторы по id, сохранив порядок (защита очереди от дублей)."""
    seen: set[str] = set()
    result: list[Track] = []
    for track in tracks:
        if track.id in seen:
            continue
        seen.add(track.id)
        result.append(track)
    return tuple(result)


def chunked(items: Sequence[Track], size: int) -> list[Sequence[Track]]:
    """Разбить последовательность (используется для пакетных запросов)."""
    if size <= 0:
        raise ValueError("size must be positive")
    return [items[i : i + size] for i in range(0, len(items), size)]
