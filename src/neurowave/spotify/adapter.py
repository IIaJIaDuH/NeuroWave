"""Адаптер Spotify: каталог и пульт Spotify Connect.

Здесь намеренно **нет** ``stream_url``: Web API не выдаёт ссылку на поток, и
`ProviderRouter` откажет в воспроизведении через наш `mpv` с понятным текстом.
Всё управление идёт командами устройства Spotify, где Premium обязателен.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..domain import AccountInfo, SearchResults, Track
from .client import SpotifyClient

TRACK_URI_PREFIX = "spotify:track:"


@dataclass(frozen=True)
class SpotifyDevice:
    """Устройство Spotify Connect, на которое можно перенести звук."""

    id: str
    name: str
    type: str = "unknown"
    is_active: bool = False
    volume_percent: int | None = None


class SpotifyAdapter:
    """Каталог и управление чужим плеером; собственного звука нет."""

    provider = "spotify"

    def __init__(self, token: str | None = None, client: Any | None = None):
        self._client = client if client is not None else SpotifyClient(token=token)

    @property
    def token(self) -> str | None:
        return getattr(self._client, "token", None)

    def storage_token(self) -> str:
        """Текущие credentials для сохранения после обновления токена."""
        storage = getattr(self._client, "storage_token", None)
        return storage() if callable(storage) else (self.token or "")

    def verify(self) -> None:
        """Токен проверяется при входе, а не на первом поиске."""
        self._client.verify()

    # --- каталог ------------------------------------------------------
    def search(self, text: str, limit: int = 20, kind: str = "all") -> SearchResults:
        query = (text or "").strip()
        if not query:
            return SearchResults()
        items = self._client.search_tracks(query, limit=limit)
        tracks = [self._to_track(item) for item in items]
        return SearchResults(tracks=tuple(track for track in tracks if track.id))

    def account(self) -> AccountInfo:
        payload = self._client.me()
        product = str(payload.get("product") or "").lower()
        return AccountInfo(
            uid=None,
            login=payload.get("id"),
            display_name=payload.get("display_name") or payload.get("id") or "Spotify",
            service_available=True,
            plus=None if not product else product == "premium",
        )

    # --- пульт --------------------------------------------------------
    def devices(self) -> list[SpotifyDevice]:
        result: list[SpotifyDevice] = []
        for item in self._client.devices():
            device_id = str(item.get("id") or "")
            if not device_id:
                continue
            volume = item.get("volume_percent")
            result.append(
                SpotifyDevice(
                    id=device_id,
                    name=str(item.get("name") or device_id),
                    type=str(item.get("type") or "unknown"),
                    is_active=bool(item.get("is_active")),
                    volume_percent=int(volume) if volume is not None else None,
                )
            )
        return result

    def transfer(self, device_id: str) -> None:
        self._client.transfer(device_id)

    def remote_play(
        self, track_ids: list[str] | None = None, uri: str | None = None
    ) -> None:
        target = (uri or "").strip()
        if target:
            self._client.play([target])
            return
        uris = [f"{TRACK_URI_PREFIX}{track_id}" for track_id in (track_ids or [])]
        if not uris:
            raise ValueError("Нужен track_id или uri")
        self._client.play(uris)

    def remote_pause(self) -> None:
        self._client.pause()

    def remote_next(self) -> None:
        self._client.next_track()

    def remote_previous(self) -> None:
        self._client.previous_track()

    # --- преобразование ------------------------------------------------
    @classmethod
    def _to_track(cls, payload: Any) -> Track:
        data = payload if isinstance(payload, dict) else {}
        artists = tuple(
            str(artist.get("name")).strip()
            for artist in data.get("artists") or []
            if isinstance(artist, dict) and artist.get("name")
        )
        album = data.get("album") or {}
        playable = data.get("is_playable")
        return Track(
            id=str(data.get("id") or ""),
            title=str(data.get("name") or "Без названия"),
            artists=artists,
            album=str(album.get("name")) if isinstance(album, dict) and album.get("name") else None,
            duration_ms=data.get("duration_ms"),
            available=True if playable is None else bool(playable),
            provider=cls.provider,
            url=(data.get("external_urls") or {}).get("spotify"),
        )
