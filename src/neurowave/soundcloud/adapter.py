"""Адаптер SoundCloud: метаданные трека, поиск и ссылка на поток.

Правила, зафиксированные в ADR-0004:

* в очередь попадают только ``playable`` треки — 30-секундное превью это не
  трек, и притворяться иначе нельзя;
* атрибуция обязательна: исполнитель берётся из ``user.username``, а ссылка на
  страницу трека сохраняется в :attr:`Track.url`;
* постоянный кеш запрещён условиями API, поэтому ``cache_allowed=False``.
"""

from __future__ import annotations

import urllib.parse
from typing import Any

from ..domain import AccountInfo, Quality, SearchResults, StreamInfo, Track
from ..errors import TrackUnavailable
from .client import PLAYABLE_ACCESS, SoundCloudClient

CODECS = {".mp3": "mp3", ".m4a": "aac", ".aac": "aac", ".ogg": "vorbis", ".opus": "opus"}


def _codec_from_url(url: str) -> str | None:
    """Кодек — только по фактическому расширению адреса CDN, без догадок."""
    path = urllib.parse.urlsplit(url).path
    suffix = path[path.rfind(".") :].lower() if "." in path else ""
    return CODECS.get(suffix)


class SoundCloudAdapter:
    """Каталог SoundCloud и поток в `mpv` для доступных треков."""

    provider = "soundcloud"

    def __init__(self, token: str | None = None, client: Any | None = None):
        self._client = client if client is not None else SoundCloudClient(token=token)

    @property
    def token(self) -> str | None:
        return getattr(self._client, "token", None)

    def verify(self) -> None:
        """Проверка ключа при входе: неверный ключ отваливается сразу."""
        self._client.verify()

    def search(self, text: str, limit: int = 20, kind: str = "all") -> SearchResults:
        query = (text or "").strip()
        if not query:
            return SearchResults()
        items = self._client.search_tracks(query, limit=limit)
        tracks: list[Track] = []
        for item in items:
            track = self._to_track(item)
            if track.id and track.available:
                tracks.append(track)
        return SearchResults(tracks=tuple(tracks[: max(0, int(limit))]))

    def stream_url(self, track_id: str) -> StreamInfo:
        """Проверить доступность и получить подписанный адрес потока."""
        track = self._to_track(self._client.track(track_id))
        if not track.available:
            raise TrackUnavailable(
                "SoundCloud отдаёт этот трек только как превью или заблокировал его"
            )
        location = self._client.stream_location(track_id)
        return StreamInfo(
            url=location,
            quality=Quality(codec=_codec_from_url(location), lossless=False),
        )

    def account(self) -> AccountInfo:
        return AccountInfo(
            display_name="SoundCloud",
            login=None,
            service_available=True,
            plus=None,
        )

    @classmethod
    def _to_track(cls, payload: Any) -> Track:
        data = payload if isinstance(payload, dict) else {}
        user = data.get("user") or {}
        username = str(user.get("username") or "").strip()
        access = str(data.get("access") or "").strip().lower()
        streamable = data.get("streamable")
        playable = access == PLAYABLE_ACCESS or (not access and streamable is not False)
        duration = data.get("duration")
        return Track(
            id=str(data.get("id") or ""),
            title=str(data.get("title") or "Без названия"),
            artists=(username,) if username else (),
            duration_ms=int(duration) if duration else None,
            available=playable,
            provider=cls.provider,
            url=data.get("permalink_url") or None,
        )
