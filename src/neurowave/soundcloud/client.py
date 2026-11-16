"""HTTP-клиент SoundCloud API на стандартной библиотеке.

Никакая другая часть приложения не знает про URL, заголовки и редиректы.
Ключ приходит из хранилища токенов и никогда не попадает в сообщения об
ошибках: наружу отдаются только статусы и понятные причины.

SoundCloud принимает два вида ключа:

* access token пользователя (начинается с ``2-``) — уходит в заголовке
  ``Authorization: OAuth …``;
* client_id приложения (Artist Pro) — уходит параметром ``client_id``.
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

from ..errors import ApiError, AuthError, NetworkError, TrackUnavailable

API_ROOT = "https://api.soundcloud.com"
USER_AGENT = "NeuroWave/0.1 (+https://github.com/IIaJIaDuH/NeuroWave)"
PLAYABLE_ACCESS = "playable"
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
USER_TOKEN_PREFIX = "2-"


@dataclass(frozen=True)
class HttpResponse:
    """Ответ транспорта: статус, заголовки и тело (может быть пустым)."""

    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""

    def json(self) -> Any:
        if not self.body:
            return None
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError("SoundCloud вернул некорректный JSON") from exc


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """Заголовок без учёта регистра (urllib и тесты пишут по-разному)."""
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Не идти по редиректу: от CDN нужен только ``Location``."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


class UrllibTransport:
    """Настоящая сеть. Тесты подменяют её фейком с тем же контрактом."""

    def json(self, url: str, headers: Mapping[str, str], timeout: float) -> HttpResponse:
        request = urllib.request.Request(url, headers=dict(headers))
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

    def resolve(self, url: str, headers: Mapping[str, str], timeout: float) -> HttpResponse:
        opener = urllib.request.build_opener(_NoRedirect())
        request = urllib.request.Request(url, headers=dict(headers))
        try:
            with opener.open(request, timeout=timeout) as response:
                return HttpResponse(
                    status=int(response.status),
                    headers=dict(response.headers.items()),
                )
        except urllib.error.HTTPError as exc:
            known = dict((exc.headers or {}).items())
            if exc.code in REDIRECT_STATUSES:
                return HttpResponse(status=int(exc.code), headers=known)
            return HttpResponse(status=int(exc.code), headers=known, body=exc.read() or b"")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise NetworkError(f"Сеть недоступна: {exc}") from exc


class SoundCloudClient:
    """Минимальный клиент: поиск треков, метаданные и адрес потока."""

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

    # --- API ----------------------------------------------------------
    def search_tracks(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Только полные треки: ``access=playable`` вместо 30-секундных превью."""
        payload = self._get_json(
            "/tracks",
            {
                "q": query,
                "limit": max(1, int(limit)),
                "access": PLAYABLE_ACCESS,
                "linked_partitioning": 1,
            },
        )
        if isinstance(payload, dict):
            items = payload.get("collection") or []
        else:
            items = payload or []
        return [item for item in items if isinstance(item, dict)]

    def verify(self) -> None:
        """Проверить ключ дешёвым запросом — сразу, а не на первом поиске."""
        self._get_json("/tracks", {"limit": 1, "access": PLAYABLE_ACCESS})

    def track(self, track_id: str) -> dict[str, Any]:
        payload = self._get_json(f"/tracks/{self._quote(track_id)}")
        if not isinstance(payload, dict) or not payload.get("id"):
            raise TrackUnavailable("SoundCloud не вернул данные трека")
        return payload

    def stream_location(self, track_id: str) -> str:
        """Разрешить редирект ``/stream`` в подписанный адрес CDN для mpv."""
        headers, params = self._auth()
        url = self._url(f"/tracks/{self._quote(track_id)}/stream", params)
        response = self._transport.resolve(url, headers, self._timeout)
        location = _header(response.headers, "Location")
        if response.status in REDIRECT_STATUSES:
            if not location:
                raise ApiError("SoundCloud не вернул адрес потока")
            return location
        if response.status == 401:
            raise AuthError("SoundCloud отклонил ключ: проверьте client_id/access token")
        if response.status in (403, 404):
            raise TrackUnavailable(
                "SoundCloud не отдаёт поток: трек заблокирован или доступен только превью"
            )
        if response.status == 429:
            raise ApiError("SoundCloud ограничил частоту запросов: подождите и повторите")
        if response.status >= 500:
            raise NetworkError(f"CDN SoundCloud ответил {response.status}")
        raise ApiError(f"SoundCloud вернул {response.status} вместо адреса потока")

    # --- внутреннее ---------------------------------------------------
    @staticmethod
    def _quote(track_id: str) -> str:
        return urllib.parse.quote(str(track_id), safe="")

    def _auth(self) -> tuple[dict[str, str], dict[str, str]]:
        if not self._token:
            raise AuthError(
                "Нет ключа SoundCloud. Выполните: neurowave login --token <client_id>"
            )
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        params: dict[str, str] = {}
        if self._token.startswith(USER_TOKEN_PREFIX):
            headers["Authorization"] = f"OAuth {self._token}"
        else:
            params["client_id"] = self._token
        return headers, params

    def _url(self, path: str, params: Mapping[str, str]) -> str:
        query = urllib.parse.urlencode(
            {key: value for key, value in params.items() if value not in (None, "")}
        )
        return f"{self._api_root}{path}" + (f"?{query}" if query else "")

    def _get_json(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        headers, auth_params = self._auth()
        merged: dict[str, Any] = {**auth_params, **(params or {})}
        url = self._url(path, merged)
        for attempt in range(1, self._retries + 1):
            response = self._transport.json(url, headers, self._timeout)
            if 200 <= response.status < 300:
                return response.json()
            if response.status == 401:
                raise AuthError("SoundCloud отклонил ключ: проверьте client_id/access token")
            if response.status in (403, 404):
                raise TrackUnavailable(f"SoundCloud: нет доступа ({response.status})")
            if response.status == 429:
                raise ApiError("SoundCloud ограничил частоту запросов: подождите и повторите")
            if response.status >= 500 and attempt < self._retries:
                delay = self._backoff * (2 ** (attempt - 1))
                self._sleep(delay + self._rng.uniform(0, delay * 0.2))
                continue
            raise ApiError(f"SoundCloud вернул {response.status}")
        raise ApiError("SoundCloud: запрос не выполнен")
