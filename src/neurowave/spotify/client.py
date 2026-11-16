"""HTTP-клиент Spotify Web API на стандартной библиотеке.

Клиент умеет только то, что разрешено ADR-0004: поиск метаданных, список
устройств Spotify Connect и управление ими. Ссылок на аудиопоток здесь нет и
быть не может — Web API их не выдаёт.

Токен — OAuth access token с нужными scopes; отправляется заголовком
``Authorization: Bearer`` и никогда не попадает в сообщения об ошибках.
"""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from ..errors import ApiError, AuthError, NetworkError, SubscriptionRequired
from .auth import SpotifyCredentials

API_ROOT = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"
REFRESH_HINT = (
    "Выполните вход заново: neurowave login --token <access token> "
    "--refresh-token <refresh token> --client-id <client_id>"
)
USER_AGENT = "NeuroWave/0.1 (+https://github.com/IIaJIaDuH/NeuroWave)"
PREMIUM_HINT = (
    "Spotify: управление воспроизведением требует Premium и активного устройства. "
    "Проверьте подписку и откройте приложение Spotify."
)
NO_DEVICE_HINT = (
    "Spotify: нет активного устройства. Посмотрите `neurowave devices` и "
    "перенесите воспроизведение через `neurowave connect <имя>`."
)


@dataclass(frozen=True)
class HttpResponse:
    """Ответ транспорта: статус, заголовки и тело (204 — пустое)."""

    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""

    def json(self) -> Any:
        if not self.body:
            return None
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError("Spotify вернул некорректный JSON") from exc


def _error_message(response: HttpResponse) -> str:
    """Достать ``error.message`` из тела, если сервис его прислал."""
    try:
        payload = response.json()
    except ApiError:
        return ""
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if message:
                return str(message)
        if isinstance(error, str):
            return error
    return ""


class UrllibTransport:
    """Настоящая сеть; тесты подменяют её фейком с тем же контрактом."""

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse:
        request = urllib.request.Request(
            url, data=body, headers=dict(headers), method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return HttpResponse(
                    status=int(response.status),
                    headers=dict(response.headers.items()),
                    body=response.read(),
                )
        except urllib.error.HTTPError as exc:
            return HttpResponse(
                status=int(exc.code),
                headers=dict((exc.headers or {}).items()),
                body=exc.read() or b"",
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise NetworkError(f"Сеть недоступна: {exc}") from exc


class SpotifyClient:
    """Минимальный клиент: поиск, устройства и команды Spotify Connect."""

    def __init__(
        self,
        token: str | None = None,
        credentials: SpotifyCredentials | None = None,
        refresh_token: str | None = None,
        client_id: str | None = None,
        transport: Any | None = None,
        timeout: float = 10.0,
        retries: int = 3,
        backoff: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        rng: random.Random | None = None,
        api_root: str = API_ROOT,
        token_url: str = TOKEN_URL,
    ):
        self._credentials = credentials or SpotifyCredentials.from_storage(token)
        if refresh_token:
            self._credentials.refresh_token = refresh_token.strip() or None
        if client_id:
            self._credentials.client_id = client_id.strip() or None
        self._transport = transport or UrllibTransport()
        self._timeout = max(1.0, float(timeout))
        self._retries = max(1, int(retries))
        self._backoff = max(0.0, float(backoff))
        self._sleep = sleep
        self._clock = clock
        self._rng = rng or random.Random()
        self._api_root = api_root.rstrip("/")
        self._token_url = token_url

    @property
    def token(self) -> str | None:
        return self._credentials.access_token or None

    @property
    def credentials(self) -> SpotifyCredentials:
        return self._credentials

    def storage_token(self) -> str:
        """Сериализованное состояние для `TokenStore` (токены не печатать)."""
        return self._credentials.to_storage()

    def set_token(self, token: str | None) -> None:
        self._credentials = SpotifyCredentials.from_storage(token)

    # --- чтение -------------------------------------------------------
    def me(self) -> dict[str, Any]:
        payload = self._request("GET", "/me")
        return payload if isinstance(payload, dict) else {}

    def verify(self) -> None:
        """Проверить токен дешёвым запросом при входе (с обновлением, если нужно)."""
        self.me()

    def search_tracks(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            "/search",
            params={
                "q": query,
                "type": "track",
                "limit": max(1, min(50, int(limit))),
            },
        )
        if not isinstance(payload, dict):
            return []
        tracks = payload.get("tracks") or {}
        items = tracks.get("items") or []
        return [item for item in items if isinstance(item, dict)]

    def devices(self) -> list[dict[str, Any]]:
        payload = self._request("GET", "/me/player/devices")
        if not isinstance(payload, dict):
            return []
        return [item for item in payload.get("devices") or [] if isinstance(item, dict)]

    # --- пульт --------------------------------------------------------
    def transfer(self, device_id: str, play: bool = False) -> None:
        self._request(
            "PUT",
            "/me/player",
            payload={"device_ids": [str(device_id)], "play": bool(play)},
        )

    def play(self, uris: list[str]) -> None:
        self._request("PUT", "/me/player/play", payload={"uris": list(uris)})

    def pause(self) -> None:
        self._request("PUT", "/me/player/pause")

    def next_track(self) -> None:
        self._request("POST", "/me/player/next")

    def previous_track(self) -> None:
        self._request("POST", "/me/player/previous")

    # --- внутреннее ---------------------------------------------------
    def _url(self, path: str, params: Mapping[str, Any] | None = None) -> str:
        query = urllib.parse.urlencode(
            {
                key: value
                for key, value in (params or {}).items()
                if value not in (None, "")
            }
        )
        return f"{self._api_root}{path}" + (f"?{query}" if query else "")

    def _headers(self) -> dict[str, str]:
        access = self._credentials.access_token
        if not access:
            raise AuthError(
                "Нет токена Spotify. Выполните: neurowave login --token <access token>"
            )
        return {
            "Authorization": f"Bearer {access}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }

    def _request(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any] | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> Any:
        self._ensure_token()
        url = self._url(path, params)
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        refreshed = False
        for attempt in range(1, self._retries + 1):
            headers = self._headers()
            response = self._transport.request(method, url, headers, body, self._timeout)
            if 200 <= response.status < 300:
                return response.json()
            if response.status == 401:
                # Один раз пробуем обновить токен и повторить тот же запрос.
                if not refreshed and self._credentials.can_refresh and attempt < self._retries:
                    self._refresh()
                    refreshed = True
                    continue
                raise self._expired_error()
            if response.status == 403:
                raise SubscriptionRequired(
                    _error_message(response) or PREMIUM_HINT
                )
            if response.status == 404:
                raise ApiError(_error_message(response) or NO_DEVICE_HINT)
            if response.status == 429:
                raise ApiError("Spotify ограничил частоту запросов: подождите и повторите")
            if response.status >= 500 and attempt < self._retries:
                delay = self._backoff * (2 ** (attempt - 1))
                self._sleep(delay + self._rng.uniform(0, delay * 0.2))
                continue
            message = _error_message(response)
            raise ApiError(
                f"Spotify вернул {response.status}" + (f": {message}" if message else "")
            )
        raise ApiError("Spotify: запрос не выполнен")

    # --- токен --------------------------------------------------------
    def _ensure_token(self) -> None:
        """Заранее обновить истекающий access token, если есть refresh token."""
        credentials = self._credentials
        if credentials.access_token and not credentials.expired(self._clock()):
            return
        if credentials.can_refresh:
            self._refresh()
            return
        if credentials.access_token:
            raise AuthError(
                "Access token Spotify истёк или скоро истечёт, а обновление не "
                f"настроено. {REFRESH_HINT}"
            )
        raise AuthError(
            "Нет токена Spotify. Выполните: neurowave login --token <access token>"
        )

    def _expired_error(self) -> AuthError:
        if self._credentials.can_refresh:
            return AuthError(
                "Spotify отклонил токен даже после обновления: refresh token "
                f"отозван или неверен. {REFRESH_HINT}"
            )
        return AuthError(
            "Токен Spotify истёк или отозван, обновление не настроено. "
            f"{REFRESH_HINT}"
        )

    def _refresh(self) -> None:
        """Обменять refresh token на новый access token (`accounts.spotify.com`)."""
        credentials = self._credentials
        if not credentials.can_refresh:
            raise AuthError(
                "Для обновления токена Spotify нужны refresh token и client_id. "
                f"{REFRESH_HINT}"
            )
        body = urllib.parse.urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": credentials.refresh_token or "",
                "client_id": credentials.client_id or "",
            }
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        response = self._transport.request(
            "POST", self._token_url, headers, body, self._timeout
        )
        payload = response.json() if response.status == 200 else None
        if not isinstance(payload, dict) or not payload.get("access_token"):
            raise AuthError(
                "Не удалось обновить токен Spotify: проверьте refresh token и "
                f"client_id. {REFRESH_HINT}"
            )
        credentials.access_token = str(payload["access_token"])
        rotated = payload.get("refresh_token")
        if rotated:
            credentials.refresh_token = str(rotated)
        expires_in = payload.get("expires_in")
        if isinstance(expires_in, (int, float)):
            credentials.expires_at = self._clock() + float(expires_in)
