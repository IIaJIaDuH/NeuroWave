"""Spotify PKCE: verifier/challenge, callback и обмен кода — без интернета."""

from __future__ import annotations

import json
import threading
import urllib.parse
import urllib.request

import pytest

from neurowave.errors import AuthError
from neurowave.spotify import HttpResponse
from neurowave.spotify.auth import SpotifyCredentials
from neurowave.spotify.oauth import (
    DEFAULT_REDIRECT_PORT,
    SCOPES,
    CallbackServer,
    authorize_url,
    challenge_for,
    exchange_code,
    login_with_pkce,
    make_pair,
)

TOKEN_URL = "https://accounts.spotify.com/api/token"


class FakeTransport:
    """Транспорт без сети: запоминает запросы и отдаёт ответы по порядку."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[tuple] = []

    def request(self, method, url, headers, body, timeout):
        self.calls.append((method, url, dict(headers), body))
        response = (
            self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        )
        if isinstance(response, Exception):
            raise response
        return response


def make_response(payload=None, status: int = 200) -> HttpResponse:
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    return HttpResponse(status=status, headers={}, body=body)


def token_payload(**overrides):
    payload = {
        "access_token": "access-1",
        "refresh_token": "refresh-1",
        "expires_in": 3600,
        "token_type": "Bearer",
    }
    payload.update(overrides)
    return payload


def hit(redirect_uri: str, query: str) -> None:
    with urllib.request.urlopen(f"{redirect_uri}?{query}", timeout=5) as reply:
        reply.read()


def emulate_browser(redirect_uri: str, query: str) -> None:
    threading.Thread(target=hit, args=(redirect_uri, query), daemon=True).start()


# --- verifier / challenge ------------------------------------------------
def test_challenge_matches_rfc7636_vector() -> None:
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert challenge_for(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_generated_pair_is_within_spotify_limits() -> None:
    pair = make_pair()
    assert 43 <= len(pair.verifier) <= 128
    assert pair.challenge == challenge_for(pair.verifier)
    assert "=" not in pair.challenge  # base64url без выравнивания
    assert set(pair.verifier) <= set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_~."
    )


def test_authorize_url_has_pkce_state_and_scopes() -> None:
    pair = make_pair("v" * 50)
    url = authorize_url(
        "client-1", "http://127.0.0.1:8888/callback", pair, "state-1"
    )
    parsed = urllib.parse.urlsplit(url)
    params = urllib.parse.parse_qs(parsed.query)

    assert (parsed.scheme, parsed.netloc, parsed.path) == (
        "https",
        "accounts.spotify.com",
        "/authorize",
    )
    assert params["client_id"] == ["client-1"]
    assert params["response_type"] == ["code"]
    assert params["redirect_uri"] == ["http://127.0.0.1:8888/callback"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["code_challenge"] == [pair.challenge]
    assert params["state"] == ["state-1"]
    assert params["scope"] == [" ".join(SCOPES)]
    assert "user-modify-playback-state" in params["scope"][0]


# --- callback ------------------------------------------------------------
def test_callback_uri_is_loopback_with_fixed_default_port() -> None:
    server = CallbackServer(port=0)
    try:
        assert server.redirect_uri == f"http://127.0.0.1:{server.port}/callback"
    finally:
        server.close()
    assert DEFAULT_REDIRECT_PORT == 8888


def test_callback_returns_code_for_expected_state() -> None:
    server = CallbackServer(port=0, timeout=10)
    emulate_browser(server.redirect_uri, "code=code-1&state=state-1")
    assert server.wait("state-1") == "code-1"


def test_callback_rejects_foreign_state() -> None:
    server = CallbackServer(port=0, timeout=10)
    emulate_browser(server.redirect_uri, "code=code-1&state=other")
    with pytest.raises(AuthError) as exc:
        server.wait("expected")
    assert "state" in str(exc.value)


def test_callback_surfaces_denied_access() -> None:
    server = CallbackServer(port=0, timeout=10)
    emulate_browser(
        server.redirect_uri, "error=access_denied&error_description=User%20denied"
    )
    with pytest.raises(AuthError) as exc:
        server.wait("state-1")
    assert "access_denied" in str(exc.value)
    assert "User denied" in str(exc.value)


def test_callback_times_out_without_request() -> None:
    server = CallbackServer(port=0, timeout=0.3)
    with pytest.raises(AuthError) as exc:
        server.wait("state-1")
    assert "не дождались" in str(exc.value)


# --- обмен кода ----------------------------------------------------------
def test_exchange_failure_names_reason_without_leaking_secrets() -> None:
    transport = FakeTransport(
        [
            make_response(
                {"error": "invalid_grant", "error_description": "Invalid redirect URI"},
                status=400,
            )
        ]
    )
    with pytest.raises(AuthError) as exc:
        exchange_code(
            transport,
            "client-1",
            "code-1",
            "verifier-1",
            "http://127.0.0.1:8888/callback",
        )
    message = str(exc.value)
    assert "Invalid redirect URI" in message
    assert "verifier-1" not in message
    assert "code-1" not in message


def test_full_login_uses_callback_state_and_verifier() -> None:
    transport = FakeTransport([make_response(token_payload())])
    seen: dict[str, object] = {}

    def opener(url: str) -> None:
        seen["url"] = url
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        redirect = params["redirect_uri"][0]
        state = params["state"][0]
        seen["params"] = params
        emulate_browser(redirect, f"code=code-1&state={state}")
        seen["redirect"] = redirect

    credentials = login_with_pkce(
        "client-1",
        port=0,
        transport=transport,
        opener=opener,
        on_url=lambda url: seen.setdefault("printed", url),
        timeout=10,
    )

    assert credentials == SpotifyCredentials(
        access_token="access-1",
        refresh_token="refresh-1",
        client_id="client-1",
        expires_at=credentials.expires_at,
    )
    assert credentials.expires_at is not None
    assert seen["printed"] == seen["url"]

    method, url, headers, body = transport.calls[0]
    assert (method, url) == ("POST", TOKEN_URL)
    assert headers["Content-Type"] == "application/x-www-form-urlencoded"
    form = urllib.parse.parse_qs(body.decode("utf-8"))
    params = seen["params"]
    assert form["grant_type"] == ["authorization_code"]
    assert form["code"] == ["code-1"]
    assert form["client_id"] == ["client-1"]
    assert form["redirect_uri"] == [seen["redirect"]]
    # Verifier в запросе действительно соответствует challenge из ссылки.
    assert challenge_for(form["code_verifier"][0]) == params["code_challenge"][0]
    assert "refresh-1" not in json.dumps(seen)


# --- CLI -----------------------------------------------------------------
def test_cli_pkce_login_saves_credentials_and_client_id(
    tmp_path, capsys, monkeypatch
) -> None:
    from neurowave.cli import main
    from neurowave.config import ConfigStore, Paths
    from neurowave.domain import AccountInfo
    from neurowave.providers import spotify as provider_module
    from neurowave.storage import TokenStore

    monkeypatch.setattr(TokenStore, "_import_keyring", staticmethod(lambda: None))
    captured: dict[str, object] = {}

    def fake_login(client_id, **kwargs):
        captured["client_id"] = client_id
        captured["port"] = kwargs.get("port")
        if kwargs.get("on_url"):
            kwargs["on_url"]("https://accounts.spotify.com/authorize?state=x")
        return SpotifyCredentials(
            access_token="access-cli",
            refresh_token="refresh-cli",
            client_id=client_id,
            expires_at=9_999_999_999.0,
        )

    monkeypatch.setattr(provider_module, "login_with_pkce", fake_login)
    monkeypatch.setattr(
        "neurowave.spotify.adapter.SpotifyAdapter.account",
        lambda self: AccountInfo(display_name="Слушатель", plus=True),
    )

    home = tmp_path / "home"
    code = main(
        [
            "--home", str(home),
            "--provider", "spotify",
            "login", "--client-id", "cid-1", "--port", "8899",
        ]
    )
    captured_out = capsys.readouterr()

    assert code == 0
    assert captured == {"client_id": "cid-1", "port": 8899}
    assert "Вход выполнен" in captured_out.out
    assert "accounts.spotify.com/authorize" in captured_out.out
    assert "access-cli" not in captured_out.out + captured_out.err
    assert "refresh-cli" not in captured_out.out + captured_out.err

    paths = Paths.resolve({"NEUROWAVE_HOME": str(home)})
    stored = json.loads(TokenStore(paths, provider="spotify").load())
    assert stored["access_token"] == "access-cli"
    assert stored["refresh_token"] == "refresh-cli"
    assert stored["client_id"] == "cid-1"
    assert ConfigStore(paths).load().spotify_client_id == "cid-1"


def test_cli_pkce_login_without_client_id_explains_registration(
    tmp_path, capsys
) -> None:
    from neurowave.cli import main

    code = main(["--home", str(tmp_path), "--provider", "spotify", "login"])
    assert code == 2
    err = capsys.readouterr().err
    assert "developer.spotify.com/dashboard" in err
    assert "http://127.0.0.1:8888/callback" in err
    assert "login --client-id" in err
