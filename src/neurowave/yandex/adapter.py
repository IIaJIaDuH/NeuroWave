"""Интеграция с Яндекс Музыкой.

Тонкий адаптер над неофициальной библиотекой ``yandex-music``. Здесь живут
все обращения к сервису, повторы с backoff и преобразование моделей API в
доменные модели. Токен не логируется и не покидает этот слой.

Правила устойчивости:

* любой ответ сервиса может измениться — обязательные поля проверяются,
  лишние игнорируются, испорченные модели не доходят до UI;
* ошибки классифицируются по факту (401/403 → :class:`AuthError` или
  :class:`SubscriptionRequired`, 404 → :class:`TrackUnavailable`,
  400/несовместимая схема → :class:`ApiError`), а не по подстроке в тексте;
* повторяются только временные сбои (сеть, timeout, 429, 5xx), суммарная
  пауза ограничена, операции записи не повторяются;
* сообщения для пользователя не содержат тело ответа сервера, токен и
  ссылки на поток.
"""

from __future__ import annotations

import random
import re
import time
from collections.abc import Mapping
from typing import Any, Callable
from urllib.parse import urlsplit

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
    NeuroWaveError,
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

#: Верхняя граница суммарного ожидания между повторами. Даже при большом
#: ``retries`` команда завершается за предсказуемое время, а не «висит».
MAX_RETRY_DELAY = 4.0

#: Признаки ограничения подписки в тексте ошибки 403 (401 и 403 библиотека
#: отдаёт одним классом ``UnauthorizedError`` и не даёт статус отдельно).
SUBSCRIPTION_MARKERS = ("subscription", "premium", "plus", "подписк")

_SCHEMA_HINT = (
    "Сервис вернул ответ в неизвестном формате. Обновите библиотеку: "
    "pip install -U yandex-music"
)

#: Подсказка на случай, если библиотека переименовала или убрала метод.
_METHOD_HINT = (
    "Библиотека yandex-music не поддерживает операцию «{name}»: обновите её: "
    "pip install -U yandex-music"
)

_ALLOWED_STREAM_SCHEMES = {"http", "https"}
_MISSING = object()


def _classes(exceptions_module: Any, *names: str) -> tuple[type[BaseException], ...]:
    """Собрать существующие классы исключений библиотеки (без падений)."""
    found: list[type[BaseException]] = []
    for name in names:
        candidate = getattr(exceptions_module, name, None)
        if isinstance(candidate, type) and issubclass(candidate, BaseException):
            found.append(candidate)
    return tuple(found)


def _as_int(value: Any) -> int | None:
    """Привести число из «почти правильного» ответа; иначе ``None``."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        # OverflowError — ``inf``/``-inf`` из JSON-ответа: не число для плеера.
        return None


def _vendor_attr(obj: Any, name: str) -> Any:
    """Атрибут клиента библиотеки или контролируемая ошибка.

    Метод разрешается *до* входа в :meth:`_call`, поэтому переименование в
    новой версии библиотеки иначе выходило бы наружу как ``AttributeError``
    мимо обработчика CLI. Ловим только отсутствие атрибута на границе с
    вендором и не маскируем прочие программные ошибки.
    """
    try:
        return getattr(obj, name)
    except AttributeError:
        raise ApiError(_METHOD_HINT.format(name=name)) from None


def _vendor_method(obj: Any, name: str) -> Callable[..., Any]:
    """Метод клиента библиотеки; отсутствующий или невызываемый — ``ApiError``."""
    method = _vendor_attr(obj, name)
    if not callable(method):
        raise ApiError(_METHOD_HINT.format(name=name))
    return method


def _valid_stream_url(value: Any) -> str | None:
    """Ссылка на поток, которую можно отдать плееру; иначе ``None``.

    Принимаем только абсолютные ``http(s)`` URL: ``file:``, ``javascript:`` и
    прочие схемы из ответа сервиса не должны попадать в mpv. Пробелы по краям
    срезаем, сам URL наружу в сообщениях не выводим.
    """
    if not isinstance(value, str):
        return None
    url = value.strip()
    if not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() not in _ALLOWED_STREAM_SCHEMES:
        return None
    if not parts.netloc:
        return None
    return url


def _as_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"false", "0", "no", "off"}:
            return False
        if lowered in {"true", "1", "yes", "on"}:
            return True
    return bool(value)


def _sequence(value: Any) -> list[Any]:
    """Ответ-список или пустой список; словарь и строка списком не считаются."""
    if isinstance(value, (list, tuple)):
        return [item for item in value if item is not None]
    return []


def _http_status(exc: BaseException) -> int | None:
    """HTTP-код из исключения библиотеки.

    ``yandex_music`` 3.x не хранит статус в атрибуте: 400/404/409 — отдельные
    классы, а 429/5xx приходят обычным ``NetworkError`` с кодом в тексте.
    Читаем первую трёхзначную цифру в скобках; текст наружу не выводим.
    """
    match = re.search(r"\((\d{3})\)", str(exc))
    if match is None:
        return None
    status = int(match.group(1))
    return status if 100 <= status <= 599 else None


def _timed_out(exc: BaseException, timed_out_classes: tuple[type[BaseException], ...]) -> bool:
    if timed_out_classes and isinstance(exc, timed_out_classes):
        return True
    return isinstance(exc, TimeoutError) or "timed out" in str(exc).lower()


def _network_message(
    exc: BaseException, timed_out_classes: tuple[type[BaseException], ...]
) -> str:
    """Понятная причина и действие, без тела ответа и URL потока."""
    status = _http_status(exc)
    if status == 429:
        return (
            "Сервис ограничил частоту запросов (429). Повторите позже; "
            "локальные файлы играют без сети: neurowave --provider local play"
        )
    if status is not None and 500 <= status < 600:
        return (
            f"Сервис временно недоступен ({status}). Повторите позже; "
            "локальные файлы играют без сети: neurowave --provider local play"
        )
    if status is not None and status in {409, 413}:
        return (
            f"Сервис отклонил запрос ({status}). Повторите позже или обновите "
            "yandex-music: pip install -U yandex-music"
        )
    if _timed_out(exc, timed_out_classes):
        return (
            "Сервис не ответил вовремя. Проверьте сеть; локальные файлы играют "
            "без сети: neurowave --provider local play"
        )
    return (
        "Нет связи с сервисом. Проверьте сеть или VPN; локальные файлы играют "
        "без сети: neurowave --provider local play"
    )


def _auth_error(exc: BaseException) -> NeuroWaveError:
    """401 и 403 приходят одним классом — различаем по признакам подписки."""
    text = str(exc).lower()
    if any(marker in text for marker in SUBSCRIPTION_MARKERS):
        return SubscriptionRequired(
            "Сервис требует активную подписку Плюс для этого действия."
        )
    return AuthError(
        "Сервис отклонил доступ: токен недействителен или доступ ограничен. "
        "Выполните вход заново: neurowave login"
    )


def _api_error(exc: BaseException, schema_classes: tuple[type[BaseException], ...]) -> ApiError:
    if schema_classes and isinstance(exc, schema_classes):
        return ApiError(_SCHEMA_HINT)
    return ApiError(
        "Сервис вернул ошибку, которую нельзя исправить локально. "
        "Повторите позже или обновите yandex-music."
    )


def _retryable(
    exc: BaseException, timed_out_classes: tuple[type[BaseException], ...]
) -> bool:
    """Повторяем только то, что может пройти само: сеть, timeout, 429, 5xx."""
    if _timed_out(exc, timed_out_classes):
        return True
    status = _http_status(exc)
    if status is None:
        return True
    return status == 429 or 500 <= status < 600


def _plus_flag(value: Any) -> bool | None:
    """Свести статус Плюса к bool|None.

    API отдаёт объект ``Plus`` (или словарь) с полем ``has_plus``, а не сам
    флаг; сырое значение ломало вывод статуса подписки.
    """
    if value is None or isinstance(value, bool):
        return value
    has_plus = getattr(value, "has_plus", None)
    if has_plus is None and isinstance(value, dict):
        has_plus = value.get("has_plus")
    if has_plus is None:
        return None
    return bool(has_plus)


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

            def connect() -> Any:
                return ym.Client(token=self._token).init()

            # Подключение — чтение: временный сбой сети можно повторить, но
            # только в пределах общего лимита ожидания.
            connected = self._call(connect)
            if connected is None:
                raise ApiError("Сервис не подтвердил подключение: пустой ответ")
            self._client = connected
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
        except Exception as exc:  # noqa: BLE001 - библиотека, OAuth и сеть
            # Текст чужой ошибки может содержать тело ответа: наружу — только
            # понятное действие, без цепочки с сырым ответом в traceback.
            raise AuthError(
                "Авторизация не завершена (код истёк или вход отменён). "
                "Повторите: neurowave login"
            ) from None
        access = getattr(token, "access_token", None)
        if not access:
            raise AuthError("Сервис не вернул токен доступа. Повторите: neurowave login")
        self.set_token(access)
        return access

    # --- аккаунт ------------------------------------------------------
    def account(self) -> AccountInfo:
        client = self.client
        # ``client.me`` — это Status, а сам пользователь лежит в ``me.account``.
        me = self._call(lambda: _vendor_attr(client, "me"))
        account = getattr(me, "account", None) or me
        plus: bool | None = None
        try:
            status = client.account_status()
            plus = _plus_flag(getattr(status, "plus", None))
        except Exception:  # noqa: BLE001 - без статуса Плюса аккаунт всё ещё полезен
            plus = None
        return AccountInfo(
            uid=_as_int(getattr(account, "uid", None)),
            login=getattr(account, "login", None),
            display_name=getattr(account, "display_name", None)
            or getattr(account, "full_name", None),
            service_available=_as_bool(getattr(account, "service_available", None)),
            plus=plus,
        )

    # --- поиск и каталог ---------------------------------------------
    def search(self, text: str, limit: int = 20, kind: str = "all") -> SearchResults:
        text = text.strip()
        if not text:
            return SearchResults()
        client = self.client
        result = self._call(_vendor_method(client, "search"), text, type_=kind)

        tracks = self._tracks(_results(result, "tracks"), limit)
        artists = [
            NamedRef(id=str(getattr(item, "id")), name=getattr(item, "name") or "?")
            for item in _results(result, "artists")[:limit]
            if _named_id(item)
        ]
        albums: list[NamedRef] = []
        for item in _results(result, "albums")[:limit]:
            if not _named_id(item):
                continue
            year = _as_int(getattr(item, "year", None))
            albums.append(
                NamedRef(
                    id=str(getattr(item, "id")),
                    name=getattr(item, "title", None) or "?",
                    subtitle=str(year) if year else None,
                )
            )
        playlists: list[NamedRef] = []
        for item in _results(result, "playlists")[:limit]:
            owner = getattr(item, "owner", None)
            uid = getattr(owner, "uid", None)
            kind_ = getattr(item, "kind", None)
            identifier = (
                f"{uid}:{kind_}"
                if uid is not None and kind_ is not None
                else str(getattr(item, "uuid", "") or "")
            )
            if not identifier:
                continue
            playlists.append(
                NamedRef(
                    id=identifier,
                    name=getattr(item, "title", None) or "?",
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
        tracks_list = self._call(_vendor_method(client, "users_likes_tracks"))
        if tracks_list is None:
            return []
        fetch = getattr(tracks_list, "fetch_tracks", None)
        if not callable(fetch):
            raise ApiError(_SCHEMA_HINT)
        raw = self._call(fetch)
        return self._tracks(raw, limit, liked=True)

    def recent_tracks(self, limit: int = 20) -> list[Track]:
        client = self.client
        history = self._call(_vendor_method(client, "music_history"))
        if history is None:
            return []
        result: list[Track] = []
        for tab in _sequence(getattr(history, "history_tabs", None)):
            for group in _sequence(getattr(tab, "items", None)):
                track = self._to_track(getattr(group, "track", None))
                if track is not None and len(result) < limit:
                    result.append(track)
        return result

    def like(self, track_id: str) -> bool:
        client = self.client
        # Запись: повтор после таймаута может дать второй запрос, поэтому
        # ровно одна попытка.
        return bool(
            self._call(
                _vendor_method(client, "users_likes_tracks_add"),
                [track_id],
                retry=False,
            )
        )

    def unlike(self, track_id: str) -> bool:
        client = self.client
        return bool(
            self._call(
                _vendor_method(client, "users_likes_tracks_remove"),
                [track_id],
                retry=False,
            )
        )

    def playlists(self) -> list[NamedRef]:
        client = self.client
        playlists = self._call(_vendor_method(client, "users_playlists_list"))
        result: list[NamedRef] = []
        for item in _sequence(playlists):
            kind = getattr(item, "kind", None)
            if kind is None or str(kind) == "":
                continue
            count = _as_int(getattr(item, "track_count", None))
            result.append(
                NamedRef(
                    id=str(kind),
                    name=getattr(item, "title", None) or "?",
                    subtitle=f"{count} треков" if count is not None else None,
                )
            )
        return result

    def playlist_tracks(self, identifier: str | int, limit: int = 200) -> list[Track]:
        client = self.client
        playlist = self._call(_vendor_method(client, "users_playlists"), identifier)
        if playlist is None:
            raise TrackUnavailable(f"Плейлист {identifier} недоступен")
        shorts = _sequence(getattr(playlist, "tracks", None))
        ids = [str(track_id) for track_id in (getattr(s, "track_id", None) for s in shorts) if track_id]
        if not ids:
            return []
        raw = self._call(_vendor_method(client, "tracks"), ids[:limit])
        return self._tracks(raw, limit)

    # --- «Моя волна» ---------------------------------------------------
    def wave_tracks(self) -> list[Track]:
        client = self.client
        result = self._call(
            _vendor_method(client, "rotor_station_tracks"),
            WAVE_STATION,
            queue=self._wave_queue,
        )
        if result is None:
            raise ApiError("«Моя волна» не вернула треки")
        self._wave_batch_id = getattr(result, "batch_id", None)
        self._wave_queue = self._wave_batch_id
        sequence = _sequence(getattr(result, "sequence", None))
        tracks: list[Track] = []
        for item in sequence:
            track = self._to_track(
                getattr(item, "track", None),
                liked=_as_bool(getattr(item, "liked", None), default=False),
            )
            if track is not None:
                tracks.append(track)
        if not tracks:
            if sequence:
                raise ApiError(
                    "«Моя волна» вернула треки в неизвестном формате. " + _SCHEMA_HINT
                )
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
                    _vendor_method(client, "rotor_station_feedback"),
                    WAVE_STATION,
                    type_,
                    batch_id=self._wave_batch_id,
                    track_id=track_id,
                    total_played_seconds=total_played_seconds,
                    retry=False,
                )
            )
        except NeuroWaveError:
            if ignore_errors:
                return False
            raise

    # --- поток --------------------------------------------------------
    def stream_url(self, track_id: str) -> StreamInfo:
        client = self.client
        infos = self._call(
            _vendor_method(client, "tracks_download_info"),
            track_id,
            get_direct_links=True,
        )
        if infos is None:
            infos = []
        if isinstance(infos, Mapping) or not isinstance(infos, (list, tuple)):
            raise ApiError(
                "Сервис вернул ссылки на поток в неизвестном формате. " + _SCHEMA_HINT
            )
        candidates = [info for info in infos if info is not None]

        usable: list[tuple[Any, str]] = []
        malformed = False
        for info in candidates:
            raw_link = getattr(info, "direct_link", None)
            if not raw_link:
                continue
            link = _valid_stream_url(raw_link)
            if link is None:
                # Ссылка есть, но отдать её плееру нельзя — это порча данных.
                malformed = True
                continue
            usable.append((info, link))

        best: Any = None
        url: str | None = None
        if usable:
            best, url = max(usable, key=lambda pair: _bitrate(pair[0]))
        else:
            # Ссылки нет в ответе — библиотека умеет запрашивать её отдельно.
            for info in sorted(candidates, key=_bitrate, reverse=True):
                resolver = getattr(info, "get_direct_link", None)
                if not callable(resolver):
                    continue
                try:
                    raw = self._call(resolver)
                except NeuroWaveError:
                    # Один недоступный вариант не отменяет остальные.
                    continue
                if not raw:
                    continue
                link = _valid_stream_url(raw)
                if link is None:
                    malformed = True
                    continue
                best, url = info, link
                break

        if best is None or url is None:
            if malformed:
                raise ApiError(
                    "Сервис вернул непригодную ссылку на поток. " + _SCHEMA_HINT
                )
            raise TrackUnavailable(
                "Сервис не выдал ссылку на поток: трек недоступен или требует подписки"
            )

        raw_codec = getattr(best, "codec", None)
        codec = raw_codec.strip().lower() if isinstance(raw_codec, str) and raw_codec.strip() else None
        bitrate = _as_int(getattr(best, "bitrate_in_kbps", None))
        quality = Quality(
            codec=codec,
            bitrate_kbps=bitrate if bitrate and bitrate > 0 else None,
            lossless=bool(codec and codec in LOSSLESS_CODECS),
        )
        return StreamInfo(url=url, quality=quality)

    # --- преобразование ------------------------------------------------
    def _tracks(self, raw: Any, limit: int, liked: bool = False) -> list[Track]:
        """Преобразовать список ответа, отбросив непригодные элементы.

        Если сервис прислал непустой список, но ни один элемент не удалось
        прочитать, это изменение схемы, а не пустая выдача — сообщаем явно,
        чтобы UI не показывал «ничего не найдено» вместо поломки.
        """
        selected = _sequence(raw)[:limit]
        if not selected:
            return []
        tracks = [
            track
            for track in (self._to_track(item, liked=liked) for item in selected)
            if track is not None
        ]
        if not tracks:
            raise ApiError(_SCHEMA_HINT)
        return tracks

    @classmethod
    def _to_track(cls, item: Any, liked: bool = False) -> Track | None:
        """Доменная модель из ответа или ``None``, если запись непригодна.

        Обязателен только идентификатор: без него трек нельзя ни играть, ни
        сохранить в историю. Остальные поля деградируют, а не роняют список.
        """
        if item is None:
            return None
        raw_id = getattr(item, "id", None)
        if isinstance(item, Mapping):
            raw_id = item.get("id", raw_id)
        if raw_id is None or str(raw_id).strip() == "":
            return None

        artists: list[str] = []
        raw_artists = getattr(item, "artists", None)
        if isinstance(raw_artists, str):
            if raw_artists.strip():
                artists.append(raw_artists.strip())
        else:
            for artist in _sequence(raw_artists):
                name = artist if isinstance(artist, str) else getattr(artist, "name", None)
                if isinstance(name, str) and name.strip():
                    artists.append(name.strip())

        album: str | None = None
        raw_albums = getattr(item, "albums", None)
        if isinstance(raw_albums, str):
            album = raw_albums.strip() or None
        else:
            for entry in _sequence(raw_albums):
                title = entry if isinstance(entry, str) else getattr(entry, "title", None)
                if isinstance(title, str) and title.strip():
                    album = title.strip()
                    break

        title = getattr(item, "title", None)
        if not isinstance(title, str) or not title.strip():
            title = "Без названия"

        duration = _as_int(getattr(item, "duration_ms", None))
        if duration is not None and duration < 0:
            duration = None

        return Track(
            id=str(raw_id),
            title=title,
            artists=tuple(artists),
            album=album,
            duration_ms=duration,
            available=_as_bool(getattr(item, "available", None)),
            liked=liked,
            provider="yandex",
        )

    # --- повторы -------------------------------------------------------
    def _call(
        self,
        func: Callable[..., Any],
        *args: Any,
        retry: bool = True,
        **kwargs: Any,
    ) -> Any:
        """Вызвать библиотеку, приведя любую ошибку к доменной.

        Порядок важен: ``NotFoundError`` и ``BadRequestError`` наследуются от
        ``NetworkError``, поэтому проверяются раньше него — иначе 404 уходил
        в повторные попытки и превращался в «сеть недоступна».
        """
        _, ym_exceptions = _require_yandex_music()
        unauthorized = _classes(ym_exceptions, "UnauthorizedError")
        not_found = _classes(ym_exceptions, "NotFoundError")
        bad_request = _classes(ym_exceptions, "BadRequestError")
        schema_mismatch = _classes(ym_exceptions, "SchemaMismatchError")
        timed_out = _classes(ym_exceptions, "TimedOutError")
        network = _classes(ym_exceptions, "NetworkError", "TimedOutError")
        base_error = getattr(ym_exceptions, "YandexMusicError", Exception)

        attempts = self._retries if retry else 1
        waited = 0.0
        for attempt in range(1, attempts + 1):
            try:
                return func(*args, **kwargs)
            except unauthorized as exc:  # type: ignore[misc]
                raise _auth_error(exc) from None
            except not_found:  # type: ignore[misc]
                # Текст исключения библиотеки — тело ответа сервера, наружу его
                # не выводим.
                raise TrackUnavailable(
                    "Трек, плейлист или ссылка недоступны в сервисе (404)"
                ) from None
            except bad_request:  # type: ignore[misc]
                raise ApiError(
                    "Сервис отклонил запрос (400): вероятно, изменился формат "
                    "запроса. Обновите yandex-music: pip install -U yandex-music"
                ) from None
            except schema_mismatch:  # type: ignore[misc]
                raise ApiError(_SCHEMA_HINT) from None
            except network as exc:  # type: ignore[misc]
                if attempt >= attempts or not _retryable(exc, timed_out):
                    raise NetworkError(_network_message(exc, timed_out)) from None
                delay = min(self._backoff * (2 ** (attempt - 1)), MAX_RETRY_DELAY)
                delay += self._rng.uniform(0.0, delay * 0.2)
                if waited + delay > MAX_RETRY_DELAY:
                    raise NetworkError(_network_message(exc, timed_out)) from None
                waited += delay
                self._sleep(delay)
            except base_error as exc:
                raise _api_error(exc, schema_mismatch) from None
        raise ApiError("Запрос не выполнен: сервис не ответил")


def _results(result: Any, name: str) -> list[Any]:
    """``result.<name>.results`` как список.

    Честно пустая выдача — это отсутствующая секция, ``results: null`` или
    пустой список. Если секция есть, но структура не та (поле переименовали,
    вместо списка словарь/строка), это изменение схемы: возвращать «ничего не
    найдено» нельзя, иначе UI скрывает поломку сервиса.
    """
    if isinstance(result, Mapping):
        section = result.get(name)
    else:
        section = getattr(result, name, None)
    if section is None:
        return []
    if isinstance(section, (list, tuple)):
        return [item for item in section if item is not None]

    if isinstance(section, Mapping):
        raw = section.get("results", _MISSING)
    else:
        raw = getattr(section, "results", _MISSING)
    if raw is _MISSING:
        raise ApiError(_SCHEMA_HINT)
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [item for item in raw if item is not None]
    raise ApiError(_SCHEMA_HINT)


def _named_id(item: Any) -> bool:
    raw = getattr(item, "id", None)
    return raw is not None and str(raw) != ""


def _bitrate(info: Any) -> int:
    return _as_int(getattr(info, "bitrate_in_kbps", None)) or 0
