"""Вход Spotify по OAuth 2.0 Authorization Code + PKCE.

Публичному клиенту не нужен и не должен быть нужен client secret: владение
кодом доказывается ``code_verifier`` (RFC 7636). Callback слушает только
loopback-адрес ``127.0.0.1``, параметр ``state`` проверяется, а токены,
verifier и код никогда не печатаются.

Сеть инкапсулирована: обмен кода на токен идёт через тот же транспорт, что и
остальной Spotify-клиент, поэтому тесты не ходят в интернет.
"""

from __future__ import annotations

import base64
import hashlib
import html
import secrets
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable

from ..errors import ApiError, AuthError
from .auth import SpotifyCredentials
from .client import TOKEN_URL, UrllibTransport

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
DEFAULT_REDIRECT_PORT = 8888
REDIRECT_HOST = "127.0.0.1"
REDIRECT_PATH = "/callback"
CALLBACK_TIMEOUT = 300.0
#: Минимально нужные для «пульта» права: чтение состояния, управление и
#: профиль (для `me` — имя и продукт подписки).
SCOPES = (
    "user-read-playback-state",
    "user-modify-playback-state",
    "user-read-currently-playing",
    "user-read-private",
)
USER_AGENT = "NeuroWave/0.1 (+https://github.com/IIaJIaDuH/NeuroWave)"

_CALLBACK_PAGE = """<!doctype html>
<html lang="ru"><meta charset="utf-8"><title>NeuroWave</title>
<body style="font-family:sans-serif;margin:4rem">
<h1>NeuroWave</h1><p>{message}</p></body></html>"""


@dataclass(frozen=True)
class PKCEPair:
    """Пара ``code_verifier`` / ``code_challenge`` для одного входа."""

    verifier: str
    challenge: str


def make_verifier() -> str:
    """Случайный verifier: 86 символов из допустимых 43…128."""
    return secrets.token_urlsafe(64)


def challenge_for(verifier: str) -> str:
    """S256: base64url(SHA-256(verifier)) без выравнивающих ``=``."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def make_pair(verifier: str | None = None) -> PKCEPair:
    value = verifier or make_verifier()
    return PKCEPair(verifier=value, challenge=challenge_for(value))


def authorize_url(
    client_id: str,
    redirect_uri: str,
    pair: PKCEPair,
    state: str,
    scopes: tuple[str, ...] = SCOPES,
) -> str:
    """URL страницы согласия Spotify; никаких секретов здесь нет."""
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "code_challenge_method": "S256",
        "code_challenge": pair.challenge,
        "state": state,
        "scope": " ".join(scopes),
    }
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


class _CallbackHandler(BaseHTTPRequestHandler):
    """Принять единственный GET от Spotify и запомнить параметры."""

    def do_GET(self) -> None:  # noqa: N802 - имя из базового класса
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path != REDIRECT_PATH:
            self._respond(404, "Неизвестный адрес callback.")
            return
        params = urllib.parse.parse_qs(parsed.query)
        self.server.result = {  # type: ignore[attr-defined]
            "code": (params.get("code") or [""])[0],
            "state": (params.get("state") or [""])[0],
            "error": (params.get("error") or [""])[0],
            "description": (params.get("error_description") or [""])[0],
        }
        # Ответ не утверждает успех: state проверяется в `wait()`, а страница
        # служит только чтобы браузер не выглядел сломанным.
        self._respond(200, "Вход обработан. Вернитесь в терминал — вкладку можно закрыть.")

    def _respond(self, status: int, message: str) -> None:
        body = _CALLBACK_PAGE.format(message=html.escape(message)).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Не засорять stderr: адрес callback не является ошибкой."""


class CallbackServer:
    """Локальный loopback-сервер для одного redirect после входа.

    Порт по умолчанию фиксирован: Spotify требует, чтобы ``redirect_uri``
    в точности совпадал с зарегистрированным в приложении.
    """

    def __init__(
        self,
        port: int = DEFAULT_REDIRECT_PORT,
        host: str = REDIRECT_HOST,
        timeout: float = CALLBACK_TIMEOUT,
    ):
        try:
            self._server = HTTPServer((host, int(port)), _CallbackHandler)
        except OSError as exc:
            raise AuthError(
                f"Spotify: не удалось занять порт {port} для callback ({exc}). "
                "Закройте приложение на этом порту или укажите другой: "
                "neurowave login --client-id <id> --port N"
            ) from exc
        self._server.timeout = 0.5
        self._server.result = None  # type: ignore[attr-defined]
        self._host = host
        self._timeout = max(0.0, float(timeout))

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def redirect_uri(self) -> str:
        return f"http://{self._host}:{self.port}{REDIRECT_PATH}"

    def wait(self, expected_state: str) -> str:
        """Дождаться кода авторизации и проверить ``state``."""
        deadline = time.monotonic() + self._timeout
        try:
            while time.monotonic() < deadline:
                self._server.handle_request()
                result = getattr(self._server, "result", None)
                if result:
                    return self._validated(result, expected_state)
            raise AuthError(
                "Spotify: не дождались подтверждения входа. Попробуйте снова."
            )
        finally:
            self.close()

    @staticmethod
    def _validated(result: dict[str, str], expected_state: str) -> str:
        if result.get("error"):
            detail = (
                f": {result['description']}" if result.get("description") else ""
            )
            raise AuthError(f"Spotify отклонил вход ({result['error']}){detail}")
        state = result.get("state") or ""
        if not secrets.compare_digest(state, expected_state):
            raise AuthError("Spotify: параметр state не совпал — вход отменён.")
        code = result.get("code") or ""
        if not code:
            raise AuthError("Spotify не вернул код авторизации.")
        return code

    def close(self) -> None:
        self._server.server_close()


def exchange_code(
    transport: Any,
    client_id: str,
    code: str,
    verifier: str,
    redirect_uri: str,
    *,
    timeout: float = 10.0,
    clock: Callable[[], float] = time.time,
) -> SpotifyCredentials:
    """Обменять код на токены (``grant_type=authorization_code``)."""
    body = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
        }
    ).encode("utf-8")
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    response = transport.request("POST", TOKEN_URL, headers, body, timeout)
    payload = response.json() if response.status == 200 else None
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise AuthError(
            "Spotify не выдал токен: проверьте client_id, Redirect URI в "
            f"приложении и системное время. {_token_error(response)}".strip()
        )
    expires_in = payload.get("expires_in")
    refresh_token = payload.get("refresh_token")
    return SpotifyCredentials(
        access_token=str(payload["access_token"]),
        refresh_token=str(refresh_token) if refresh_token else None,
        client_id=client_id,
        expires_at=(
            clock() + float(expires_in)
            if isinstance(expires_in, (int, float))
            else None
        ),
    )


def _token_error(response: Any) -> str:
    """Причина отказа без токенов и verifier в тексте."""
    try:
        payload = response.json()
    except ApiError:
        return f"Ответ Spotify: HTTP {response.status}."
    if not isinstance(payload, dict):
        return f"Ответ Spotify: HTTP {response.status}."
    detail = payload.get("error_description") or payload.get("error")
    return f"Ответ Spotify: {detail}." if detail else f"Ответ Spotify: HTTP {response.status}."


def login_with_pkce(
    client_id: str,
    *,
    port: int = DEFAULT_REDIRECT_PORT,
    scopes: tuple[str, ...] = SCOPES,
    transport: Any | None = None,
    opener: Callable[[str], Any] | None = None,
    on_url: Callable[[str], None] | None = None,
    timeout: float = CALLBACK_TIMEOUT,
    clock: Callable[[], float] = time.time,
) -> SpotifyCredentials:
    """Полный вход: браузер → callback → обмен кода на токены."""
    client_id = (client_id or "").strip()
    if not client_id:
        raise AuthError("Spotify: нужен client_id приложения для входа по PKCE.")
    pair = make_pair()
    state = secrets.token_urlsafe(16)
    server = CallbackServer(port=port, timeout=timeout)
    try:
        url = authorize_url(client_id, server.redirect_uri, pair, state, scopes)
        if on_url is not None:
            on_url(url)
        # Браузера может не быть (headless): URL уже отдан вызывающему.
        try:
            (opener or webbrowser.open)(url)
        except Exception:  # noqa: BLE001 - открытие браузера не критично
            pass
        code = server.wait(state)
        return exchange_code(
            transport or UrllibTransport(),
            client_id,
            code,
            pair.verifier,
            server.redirect_uri,
            clock=clock,
        )
    finally:
        server.close()
