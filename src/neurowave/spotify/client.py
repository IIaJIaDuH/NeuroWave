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

API_ROOT = "https://api.spotify.com/v1"
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
        transport: Any | None = None,
        timeout: float = 10.0,
        retries: int = 3,
        backoff: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
        api_root: str = API_ROOT,
    ):
        self._token = (token or "").strip() or None
        self._transport = transport or UrllibTransport()
        self._timeout = max(1.0, float(timeout))
        self._retries = max(1, int(retries))
        self._backoff = max(0.0, float(backoff))
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._api_root = api_root.rstrip("/")

    @property
    def token(self) -> str | None:
        return self._token

    def set_token(self, token: str | None) -> None:
        self._token = (token or "").strip() or None

    # --- чтение -------------------------------------------------------
    def me(self) -> dict[str, Any]:
        payload = self._request("GET", "/me")
        return payload if isinstance(payload, dict) else {}

    def verify(self) -> None:
        """Проверить токен дешёвым запросом при входе."""
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
        if not self._token:
            raise AuthError(
                "Нет токена Spotify. Выполните: neurowave login --token <access token>"
            )
        return {
            "Authorization": f"Bearer {self._token}",
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
        headers = self._headers()
        url = self._url(path, params)
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        for attempt in range(1, self._retries + 1):
            response = self._transport.request(method, url, headers, body, self._timeout)
            if 200 <= response.status < 300:
                return response.json()
            if response.status == 401:
                raise AuthError("Spotify отклонил токен: выполните вход заново")
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
