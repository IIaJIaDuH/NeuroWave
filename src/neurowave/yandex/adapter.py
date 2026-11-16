"""Интеграция с Яндекс Музыкой.

Тонкий адаптер над неофициальной библиотекой ``yandex-music``. Здесь живут
все обращения к сервису, повторы с backoff и преобразование моделей API в
доменные модели. Токен не логируется и не покидает этот слой.
"""

from __future__ import annotations

import random
import time
from typing import Any, Callable

from ..domain import (
    AccountInfo,
    NamedRef,
    Quality,
    SearchResults,
    StreamInfo,
    Track,
)
from ..errors import (
    ApiError,
    AuthError,
    ConfigError,
    NetworkError,
    SubscriptionRequired,
    TrackUnavailable,
)

WAVE_STATION = "user:onyourwave"
LOSSLESS_CODECS = {"flac", "alac", "wav", "aiff"}
FEEDBACK_TYPES = {
    "started": "trackStarted",
    "finished": "trackFinished",
    "skip": "skip",
    "like": "like",
    "dislike": "dislike",
}


def _require_yandex_music():
    try:
        import yandex_music  # type: ignore
        from yandex_music import exceptions as ym_exceptions  # type: ignore
    except ImportError as exc:  # pragma: no cover - защита от неполной установки
        raise ConfigError(
            "Не установлена библиотека yandex-music. Выполните: pip install yandex-music"
        ) from exc
    return yandex_music, ym_exceptions


class YandexMusicAdapter:
    """Музыкальный сервис как источник каталога и ссылок на потоки."""

    def __init__(
        self,
        token: str | None = None,
        client: Any | None = None,
        retries: int = 3,
        backoff: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ):
        self._token = token
        self._client = client
        self._retries = max(1, int(retries))
        self._backoff = max(0.0, float(backoff))
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._wave_batch_id: str | None = None
        self._wave_queue: str | None = None

    # --- подключение --------------------------------------------------
    @property
    def client(self) -> Any:
        if self._client is None:
            if not self._token:
                raise AuthError("Нет сохранённого токена. Выполните вход: neurowave login")
            ym, _ = _require_yandex_music()
            self._client = ym.Client(token=self._token).init()
        return self._client

    @property
    def token(self) -> str | None:
        return self._token

    def set_token(self, token: str) -> None:
        self._token = token
        self._client = None

    def device_login(
        self,
        on_code: Callable[[str, str], None],
        device_name: str = "NeuroWave",
        timeout: float | None = None,
    ) -> str:
        """OAuth Device Flow. ``on_code(verification_url, user_code)``."""
        if self._client is None:
            ym, _ = _require_yandex_music()
            self._client = ym.Client()
        client = self._client

        def callback(code: Any) -> None:
            on_code(
                getattr(code, "verification_url", "https://oauth.yandex.ru/device"),
                getattr(code, "user_code", ""),
            )

        try:
            token = client.device_auth(callback, device_name=device_name, timeout=timeout)
        except Exception as exc:
            raise AuthError(f"Авторизация не завершена: {exc}") from exc
        access = getattr(token, "access_token", None)
        if not access:
            raise AuthError("Сервис не вернул токен доступа")
        self.set_token(access)
        return access

    # --- аккаунт ------------------------------------------------------
    def account(self) -> AccountInfo:
        client = self.client
        account = self._call(lambda: client.me)
        plus: bool | None = None
        try:
            status = client.account_status()
            plus = getattr(status, "plus", None)
        except Exception:
            plus = None
        return AccountInfo(
            uid=getattr(account, "uid", None),
            login=getattr(account, "login", None),
            display_name=getattr(account, "display_name", None),
            service_available=bool(getattr(account, "service_available", True)),
            plus=plus,
        )

    # --- поиск и каталог ---------------------------------------------
    def search(self, text: str, limit: int = 20, kind: str = "all") -> SearchResults:
        text = text.strip()
        if not text:
            return SearchResults()
        client = self.client
        result = self._call(client.search, text, type_=kind)

        tracks: list[Track] = []
        artists: list[NamedRef] = []
        albums: list[NamedRef] = []
        playlists: list[NamedRef] = []

        found_tracks = getattr(getattr(result, "tracks", None), "results", None) or []
        for item in found_tracks[:limit]:
            tracks.append(self._to_track(item))

        for item in (getattr(getattr(result, "artists", None), "results", None) or [])[:limit]:
            artists.append(
                NamedRef(id=str(getattr(item, "id", "")), name=getattr(item, "name", "") or "?")
            )
        for item in (getattr(getattr(result, "albums", None), "results", None) or [])[:limit]:
            title = getattr(item, "title", "") or "?"
            year = getattr(item, "year", None)
            albums.append(
                NamedRef(
                    id=str(getattr(item, "id", "")),
                    name=title,
                    subtitle=str(year) if year else None,
                )
            )
        for item in (getattr(getattr(result, "playlists", None), "results", None) or [])[:limit]:
            owner = getattr(item, "owner", None)
            uid = getattr(owner, "uid", None)
            kind_ = getattr(item, "kind", None)
            identifier = f"{uid}:{kind_}" if uid is not None and kind_ is not None else str(
                getattr(item, "uuid", "")
            )
            playlists.append(
                NamedRef(
                    id=identifier,
                    name=getattr(item, "title", "") or "?",
                    subtitle="плейлист",
                )
            )
        return SearchResults(
            tracks=tuple(tracks),
            artists=tuple(artists),
            albums=tuple(albums),
            playlists=tuple(playlists),
        )

    def liked_tracks(self, limit: int = 100) -> list[Track]:
        client = self.client
        tracks_list = self._call(client.users_likes_tracks)
        if tracks_list is None:
            return []
        tracks = self._call(tracks_list.fetch_tracks) or []
        return [self._to_track(item, liked=True) for item in tracks[:limit]]

    def recent_tracks(self, limit: int = 20) -> list[Track]:
        client = self.client
        history = self._call(client.music_history)
        if history is None:
            return []
        result: list[Track] = []
        for item in getattr(history, "history_tabs", None) or []:
            for group in getattr(item, "items", None) or []:
                track = getattr(group, "track", None)
                if track is not None and len(result) < limit:
                    result.append(self._to_track(track))
        return result

    def like(self, track_id: str) -> bool:
        client = self.client
        return bool(self._call(client.users_likes_tracks_add, [track_id]))

    def unlike(self, track_id: str) -> bool:
        client = self.client
        return bool(self._call(client.users_likes_tracks_remove, [track_id]))

    def playlists(self) -> list[NamedRef]:
        client = self.client
        playlists = self._call(client.users_playlists_list) or []
        result: list[NamedRef] = []
        for item in playlists:
            count = getattr(item, "track_count", None)
            result.append(
                NamedRef(
                    id=str(getattr(item, "kind", "")),
                    name=getattr(item, "title", "") or "?",
                    subtitle=f"{count} треков" if count is not None else None,
                )
            )
        return result

    def playlist_tracks(self, identifier: str | int, limit: int = 200) -> list[Track]:
        client = self.client
        playlist = self._call(client.users_playlists, identifier)
        if playlist is None:
            raise TrackUnavailable(f"Плейлист {identifier} недоступен")
        shorts = getattr(playlist, "tracks", None) or []
        ids = [getattr(short, "track_id", None) for short in shorts]
        ids = [str(i) for i in ids if i is not None][:limit]
        if not ids:
            return []
        tracks = self._call(client.tracks, ids) or []
        return [self._to_track(item) for item in tracks]

    # --- «Моя волна» ---------------------------------------------------
    def wave_tracks(self) -> list[Track]:
        client = self.client
        result = self._call(
            client.rotor_station_tracks, WAVE_STATION, queue=self._wave_queue
        )
        if result is None:
            raise ApiError("«Моя волна» не вернула треки")
        self._wave_batch_id = getattr(result, "batch_id", None)
        self._wave_queue = self._wave_batch_id
        sequence = getattr(result, "sequence", None) or []
        tracks: list[Track] = []
        for item in sequence:
            track = getattr(item, "track", None)
            if track is None:
                continue
            tracks.append(self._to_track(track, liked=bool(getattr(item, "liked", False))))
        if not tracks:
            raise ApiError("«Моя волна» вернула пустой список треков")
        return tracks

    def wave_feedback(
        self,
        kind: str,
        track_id: str | None = None,
        total_played_seconds: float | None = None,
        ignore_errors: bool = True,
    ) -> bool:
        """Отправить feedback; ошибка feedback не должна ломать воспроизведение."""
        type_ = FEEDBACK_TYPES.get(kind, kind)
        client = self.client
        try:
            return bool(
                self._call(
                    client.rotor_station_feedback,
                    WAVE_STATION,
                    type_,
                    batch_id=self._wave_batch_id,
                    track_id=track_id,
                    total_played_seconds=total_played_seconds,
                )
            )
        except (NetworkError, ApiError):
            if ignore_errors:
                return False
            raise

    # --- поток --------------------------------------------------------
    def stream_url(self, track_id: str) -> StreamInfo:
        client = self.client
        infos = self._call(client.tracks_download_info, track_id, get_direct_links=True) or []
        usable: list[Any] = [info for info in infos if getattr(info, "direct_link", None)]
        if not usable:
            for info in sorted(
                infos, key=lambda i: getattr(i, "bitrate_in_kbps", 0) or 0, reverse=True
            ):
                resolver = getattr(info, "get_direct_link", None)
                if resolver is None:
                    continue
                try:
                    link = resolver()
                except Exception:
                    continue
                if link:
                    setattr(info, "direct_link", link)
                    usable.append(info)
                    break
        if not usable:
            raise TrackUnavailable(
                "Сервис не выдал ссылку на поток: трек недоступен или требует подписки"
            )
        best = max(usable, key=lambda i: getattr(i, "bitrate_in_kbps", 0) or 0)
        codec = str(getattr(best, "codec", "") or "").lower() or None
        bitrate = getattr(best, "bitrate_in_kbps", None)
        quality = Quality(
            codec=codec,
            bitrate_kbps=int(bitrate) if bitrate else None,
            lossless=bool(codec and codec in LOSSLESS_CODECS),
        )
        return StreamInfo(url=str(best.direct_link), quality=quality)

    # --- преобразование ------------------------------------------------
    @staticmethod
    def _to_track(item: Any, liked: bool = False) -> Track:
        artists = tuple(
            str(getattr(artist, "name", "")).strip()
            for artist in (getattr(item, "artists", None) or [])
            if getattr(artist, "name", None)
        )
        album = None
        albums = getattr(item, "albums", None) or []
        if albums:
            album = getattr(albums[0], "title", None)
        available = getattr(item, "available", None)
        return Track(
            id=str(getattr(item, "id", "")),
            title=getattr(item, "title", None) or "Без названия",
            artists=artists,
            album=album,
            duration_ms=getattr(item, "duration_ms", None),
            available=True if available is None else bool(available),
            liked=liked,
            provider="yandex",
        )

    # --- повторы -------------------------------------------------------
    def _call(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        _, ym_exceptions = _require_yandex_music()
        unauthorized = getattr(ym_exceptions, "UnauthorizedError", ())
        network = tuple(
            exc
            for exc in (
                getattr(ym_exceptions, "NetworkError", None),
                getattr(ym_exceptions, "TimedOutError", None),
            )
            if exc is not None
        )
        not_found = getattr(ym_exceptions, "NotFoundError", ())
        base_error = getattr(ym_exceptions, "YandexMusicError", Exception)

        for attempt in range(1, self._retries + 1):
            try:
                return func(*args, **kwargs)
            except unauthorized as exc:  # type: ignore[misc]
                raise AuthError("Токен недействителен. Выполните вход заново") from exc
            except network as exc:  # type: ignore[misc]
                if attempt >= self._retries:
                    raise NetworkError(f"Сеть недоступна: {exc}") from exc
                delay = self._backoff * (2 ** (attempt - 1))
                self._sleep(delay + self._rng.uniform(0, delay * 0.2))
            except not_found as exc:  # type: ignore[misc]
                raise TrackUnavailable(str(exc)) from exc
            except base_error as exc:
                message = str(exc)
                if "premium" in message.lower() or "plus" in message.lower():
                    raise SubscriptionRequired(message) from exc
                raise ApiError(message) from exc
        raise ApiError("Запрос не выполнен")

