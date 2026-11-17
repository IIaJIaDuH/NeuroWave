"""Spotify как «пульт»: каталог и Spotify Connect без потока, без сети."""

from __future__ import annotations

import json

import pytest

from neurowave.config import Paths, Settings
from neurowave.domain import SearchResults, Track
from neurowave.errors import ApiError, AuthError, SubscriptionRequired
from neurowave.providers.base import AuthKind, Capability
from neurowave.providers.registry import ProviderNotImplemented, get_spec
from neurowave.providers.router import ProviderRouter
from neurowave.spotify import HttpResponse, SpotifyAdapter, SpotifyClient, SpotifyDevice
from neurowave.spotify.auth import SpotifyCredentials

TRACK = {
    "id": "tr-1",
    "name": "Nightcall",
    "duration_ms": 258000,
    "is_playable": True,
    "external_urls": {"spotify": "https://open.spotify.com/track/tr-1"},
    "artists": [{"name": "Kavinsky"}],
    "album": {"name": "OutRun"},
}


def make_response(payload=None, status: int = 200) -> HttpResponse:
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    return HttpResponse(status=status, headers={}, body=body)


class FakeTransport:
    """Транспорт без сети: запоминает запросы и отдаёт заготовленные ответы."""

    def __init__(self, responses=None):
        self._responses = list(responses or [])
        self.calls: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def request(self, method, url, headers, body, timeout):
        self.calls.append((method, url, dict(headers), body))
        response = (
            self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        )
        if isinstance(response, Exception):
            raise response
        return response


def make_client(transport: FakeTransport, token: str = "access-token") -> SpotifyClient:
    return SpotifyClient(token=token, transport=transport, sleep=lambda _: None)


def make_adapter(transport: FakeTransport, token: str = "access-token") -> SpotifyAdapter:
    return SpotifyAdapter(client=make_client(transport, token=token))


def body_json(body: bytes | None):
    return None if body is None else json.loads(body.decode("utf-8"))


# --- клиент -------------------------------------------------------------
def test_client_requires_token() -> None:
    client = SpotifyClient(token=None, transport=FakeTransport([make_response({})]))
    with pytest.raises(AuthError) as exc:
        client.search_tracks("x")
    assert "--token" in str(exc.value)


def test_client_search_uses_bearer_and_track_type() -> None:
    transport = FakeTransport([make_response({"tracks": {"items": [TRACK]}})])
    items = make_client(transport).search_tracks("nightcall", limit=5)

    assert items == [TRACK]
    method, url, headers, body = transport.calls[0]
    assert method == "GET"
    assert "/search?" in url
    assert "type=track" in url
    assert "limit=5" in url
    assert headers["Authorization"] == "Bearer access-token"
    assert body is None


def test_client_verify_hits_me() -> None:
    transport = FakeTransport([make_response({"id": "user", "product": "premium"})])
    make_client(transport).verify()
    method, url, _, _ = transport.calls[0]
    assert (method, url.endswith("/me")) == ("GET", True)


def test_client_maps_auth_subscription_and_device_errors() -> None:
    with pytest.raises(AuthError):
        make_client(FakeTransport([make_response({}, status=401)])).devices()
    with pytest.raises(SubscriptionRequired) as exc:
        make_client(FakeTransport([make_response({}, status=403)])).play(["spotify:track:1"])
    assert "Premium" in str(exc.value)
    with pytest.raises(ApiError) as exc:
        make_client(FakeTransport([make_response({}, status=404)])).pause()
    assert "устройства" in str(exc.value)
    with pytest.raises(ApiError) as exc:
        make_client(FakeTransport([make_response({}, status=429)])).pause()
    assert "частоту" in str(exc.value)


def test_client_retries_server_errors() -> None:
    transport = FakeTransport([make_response({}, status=503)])
    client = SpotifyClient(token="k", transport=transport, retries=3, sleep=lambda _: None)
    with pytest.raises(ApiError):
        client.devices()
    assert len(transport.calls) == 3

    recovering = FakeTransport([make_response({}, status=502), make_response({"devices": []})])
    client = SpotifyClient(token="k", transport=recovering, retries=3, sleep=lambda _: None)
    assert client.devices() == []
    assert len(recovering.calls) == 2


def test_client_control_endpoints() -> None:
    transport = FakeTransport([make_response(None, status=204) for _ in range(5)])
    client = make_client(transport)

    client.transfer("device-1")
    client.play(["spotify:track:tr-1"])
    client.pause()
    client.next_track()
    client.previous_track()

    calls = [(method, url.split("/v1")[1], body_json(body)) for method, url, _, body in transport.calls]
    assert calls[0] == ("PUT", "/me/player", {"device_ids": ["device-1"], "play": False})
    assert calls[1] == ("PUT", "/me/player/play", {"uris": ["spotify:track:tr-1"]})
    assert calls[2] == ("PUT", "/me/player/pause", None)
    assert calls[3] == ("POST", "/me/player/next", None)
    assert calls[4] == ("POST", "/me/player/previous", None)


def test_client_error_message_from_body_is_used() -> None:
    response = make_response({"error": {"message": "Player command failed"}}, status=400)
    with pytest.raises(ApiError) as exc:
        make_client(FakeTransport([response])).pause()
    assert "Player command failed" in str(exc.value)


# --- токены: хранение и обновление --------------------------------------
def test_credentials_plain_token_stays_plain() -> None:
    credentials = SpotifyCredentials.from_storage("plain-access")
    assert credentials.access_token == "plain-access"
    assert credentials.can_refresh is False
    assert credentials.to_storage() == "plain-access"


def test_credentials_json_roundtrip_and_broken_json() -> None:
    credentials = SpotifyCredentials(
        access_token="a", refresh_token="r", client_id="c", expires_at=123.0
    )
    restored = SpotifyCredentials.from_storage(credentials.to_storage())
    assert restored == credentials
    assert restored.can_refresh is True
    broken = '{"access_token": }'
    assert SpotifyCredentials.from_storage(broken).access_token == broken


def test_expiring_token_is_refreshed_before_request() -> None:
    now = 1000.0
    credentials = SpotifyCredentials(
        access_token="old", refresh_token="ref", client_id="cid", expires_at=now + 5
    )
    transport = FakeTransport(
        [
            make_response({"access_token": "new", "expires_in": 3600}),
            make_response({"id": "u1"}),
        ]
    )
    client = SpotifyClient(
        credentials=credentials, transport=transport, clock=lambda: now
    )
    client.verify()

    method, url, headers, body = transport.calls[0]
    assert method == "POST"
    assert url == "https://accounts.spotify.com/api/token"
    form = dict(item.split("=") for item in body.decode().split("&"))
    assert form == {
        "grant_type": "refresh_token",
        "refresh_token": "ref",
        "client_id": "cid",
    }
    assert transport.calls[1][2]["Authorization"] == "Bearer new"
    assert credentials.access_token == "new"
    assert credentials.expires_at == now + 3600
    assert json.loads(client.storage_token())["access_token"] == "new"


def test_401_refreshes_once_and_retries_request() -> None:
    credentials = SpotifyCredentials(access_token="stale", refresh_token="ref", client_id="cid")
    transport = FakeTransport(
        [
            make_response({"error": {"message": "The access token expired"}}, status=401),
            make_response({"access_token": "fresh", "expires_in": 3600}),
            make_response({"devices": []}),
        ]
    )
    client = SpotifyClient(credentials=credentials, transport=transport, clock=lambda: 0.0)
    assert client.devices() == []
    assert "accounts.spotify.com" in transport.calls[1][1]
    assert transport.calls[2][2]["Authorization"] == "Bearer fresh"


def test_rotated_refresh_token_is_kept() -> None:
    credentials = SpotifyCredentials(access_token="old", refresh_token="ref", client_id="cid")
    transport = FakeTransport(
        [make_response({"access_token": "new", "refresh_token": "ref-2", "expires_in": 3600})]
    )
    client = SpotifyClient(credentials=credentials, transport=transport, clock=lambda: 0.0)
    client._refresh()
    assert credentials.refresh_token == "ref-2"
    stored = json.loads(client.storage_token())
    assert stored["refresh_token"] == "ref-2"


def test_expired_token_without_refresh_explains_how_to_fix() -> None:
    transport = FakeTransport([make_response({}, status=401)])
    client = SpotifyClient(token="stale", transport=transport)
    with pytest.raises(AuthError) as exc:
        client.devices()
    message = str(exc.value)
    assert "истёк" in message
    assert "--refresh-token" in message
    assert "stale" not in message  # токен не утекает в текст


def test_locally_expired_token_is_refreshed_before_any_request() -> None:
    credentials = SpotifyCredentials(
        access_token="old", refresh_token="ref", client_id="cid", expires_at=500.0
    )
    transport = FakeTransport([make_response({"access_token": "new", "expires_in": 60})])
    client = SpotifyClient(
        credentials=credentials, transport=transport, clock=lambda: 1000.0
    )
    assert client.devices() == []  # ответ фейка без ключа "devices"
    assert len(transport.calls) == 2
    assert transport.calls[0][1].startswith("https://accounts.spotify.com")
    assert transport.calls[1][1].endswith("/me/player/devices")
    assert transport.calls[1][2]["Authorization"] == "Bearer new"


def test_expired_without_refresh_fails_without_network() -> None:
    credentials = SpotifyCredentials(access_token="old", expires_at=500.0)
    transport = FakeTransport([make_response({})])
    client = SpotifyClient(credentials=credentials, transport=transport, clock=lambda: 1000.0)
    with pytest.raises(AuthError) as exc:
        client.devices()
    assert "обновление не настроено" in str(exc.value)
    assert transport.calls == []


def test_failed_refresh_is_auth_error_not_silence() -> None:
    credentials = SpotifyCredentials(access_token="old", refresh_token="ref", client_id="cid")
    transport = FakeTransport([make_response({"error": "invalid_grant"}, status=400)])
    client = SpotifyClient(credentials=credentials, transport=transport, clock=lambda: 0.0)
    with pytest.raises(AuthError) as exc:
        client._refresh()
    assert "Не удалось обновить токен" in str(exc.value)


def test_adapter_exposes_storage_token_for_persistence() -> None:
    plain = SpotifyAdapter(client=SpotifyClient(token="plain", transport=FakeTransport([])))
    assert plain.storage_token() == "plain"

    credentials = SpotifyCredentials(access_token="a", refresh_token="r", client_id="c")
    adapter = SpotifyAdapter(client=SpotifyClient(credentials=credentials, transport=FakeTransport([])))
    assert json.loads(adapter.storage_token())["refresh_token"] == "r"


# --- адаптер ------------------------------------------------------------
def test_search_maps_tracks_for_catalog() -> None:
    transport = FakeTransport([make_response({"tracks": {"items": [TRACK]}})])
    results = make_adapter(transport).search("nightcall")

    assert len(results.tracks) == 1
    track = results.tracks[0]
    assert track.id == "tr-1"
    assert track.title == "Nightcall"
    assert track.artists == ("Kavinsky",)
    assert track.album == "OutRun"
    assert track.duration_ms == 258000
    assert track.provider == "spotify"
    assert track.key == "spotify:tr-1"
    assert track.url == "https://open.spotify.com/track/tr-1"


def test_empty_query_does_not_touch_network() -> None:
    transport = FakeTransport([make_response({"tracks": {"items": []}})])
    assert make_adapter(transport).search("  ").tracks == ()
    assert transport.calls == []


def test_account_maps_premium_product() -> None:
    premium = FakeTransport(
        [make_response({"id": "u1", "display_name": "Слушатель", "product": "premium"})]
    )
    account = make_adapter(premium).account()
    assert account.display_name == "Слушатель"
    assert account.login == "u1"
    assert account.plus is True

    free = FakeTransport([make_response({"id": "u2", "product": "free"})])
    assert make_adapter(free).account().plus is False


def test_devices_are_parsed_and_filtered() -> None:
    transport = FakeTransport(
        [
            make_response(
                {
                    "devices": [
                        {
                            "id": "d1",
                            "name": "Kitchen",
                            "type": "Speaker",
                            "is_active": True,
                            "volume_percent": 40,
                        },
                        {"name": "Без id"},
                    ]
                }
            )
        ]
    )
    devices = make_adapter(transport).devices()
    assert devices == [
        SpotifyDevice(id="d1", name="Kitchen", type="Speaker", is_active=True, volume_percent=40)
    ]


def test_remote_play_builds_spotify_uris_or_uses_explicit_one() -> None:
    by_id = FakeTransport([make_response(None, status=204)])
    make_adapter(by_id).remote_play(track_ids=["tr-1", "tr-2"])
    assert body_json(by_id.calls[0][3]) == {
        "uris": ["spotify:track:tr-1", "spotify:track:tr-2"]
    }

    by_uri = FakeTransport([make_response(None, status=204)])
    make_adapter(by_uri).remote_play(uri="spotify:album:zzz")
    assert body_json(by_uri.calls[0][3]) == {"uris": ["spotify:album:zzz"]}

    with pytest.raises(ValueError):
        make_adapter(FakeTransport([make_response(None, status=204)])).remote_play()


def test_adapter_has_no_stream_and_router_refuses() -> None:
    adapter = make_adapter(FakeTransport([make_response({"devices": []})]))
    assert not hasattr(adapter, "stream_url")

    router = ProviderRouter({"spotify": lambda: adapter}, default="spotify")
    with pytest.raises(ProviderNotImplemented) as exc:
        router.stream_url(Track(id="tr-1", title="Nightcall", provider="spotify"))
    assert "не отдаёт поток" in str(exc.value)
    assert router.cache_allowed(Track(id="tr-1", title="x", provider="spotify")) is False


# --- провайдер ----------------------------------------------------------
def test_spotify_provider_is_delegated_only() -> None:
    info = get_spec("spotify").info
    assert info.implemented is True
    assert info.auth is AuthKind.API_TOKEN
    assert info.supports(Capability.CATALOG)
    assert info.supports(Capability.DELEGATED_PLAYBACK)
    assert not info.supports(Capability.DIRECT_STREAM)
    assert info.plays_in_mpv is False
    assert info.cache_allowed is False
    assert info.subscription == "Premium"
    assert "mpv" in info.notes


# --- CLI ----------------------------------------------------------------
def patch_spotify(monkeypatch, devices=None, results=None):
    """Заменить сетевые методы адаптера, чтобы проверить только CLI-проводку."""
    calls: list[tuple] = []
    devices = (
        devices
        if devices is not None
        else [
            SpotifyDevice(
                id="dev-1", name="Kitchen", type="Speaker", is_active=False, volume_percent=35
            ),
            SpotifyDevice(id="dev-2", name="Living Room", type="TV", is_active=True),
        ]
    )
    results = results if results is not None else SearchResults(
        tracks=(Track(id="tr-1", title="Nightcall", artists=("Kavinsky",), provider="spotify"),)
    )
    monkeypatch.setattr(SpotifyAdapter, "verify", lambda self: None)
    monkeypatch.setattr(SpotifyAdapter, "devices", lambda self: list(devices))
    monkeypatch.setattr(
        SpotifyAdapter, "transfer", lambda self, device_id: calls.append(("transfer", device_id))
    )
    monkeypatch.setattr(SpotifyAdapter, "search", lambda self, text, limit=20, kind="all": results)
    monkeypatch.setattr(
        SpotifyAdapter,
        "remote_play",
        lambda self, track_ids=None, uri=None: calls.append(
            ("play", uri or tuple(track_ids or ()))
        ),
    )
    monkeypatch.setattr(SpotifyAdapter, "remote_pause", lambda self: calls.append(("pause",)))
    monkeypatch.setattr(SpotifyAdapter, "remote_next", lambda self: calls.append(("next",)))
    monkeypatch.setattr(
        SpotifyAdapter, "remote_previous", lambda self: calls.append(("previous",))
    )
    return calls


def test_cli_spotify_login_with_refresh_token_stores_credentials(
    tmp_path, capsys, monkeypatch
) -> None:
    from neurowave.cli import main
    from neurowave.storage import TokenStore

    monkeypatch.setattr(TokenStore, "_import_keyring", staticmethod(lambda: None))
    patch_spotify(monkeypatch)  # verify без сети
    home = tmp_path / "home"
    code = main(
        [
            "--home", str(home),
            "--provider", "spotify",
            "login", "--token", "access-1",
            "--refresh-token", "refresh-1",
            "--client-id", "client-1",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "Обновление токена настроено" in out
    assert "refresh-1" not in out  # значения токенов не печатаются

    stored = TokenStore(Paths.resolve({"NEUROWAVE_HOME": str(home)}), provider="spotify")
    data = json.loads(stored.load())
    assert data["access_token"] == "access-1"
    assert data["refresh_token"] == "refresh-1"
    assert data["client_id"] == "client-1"


def test_cli_refresh_flags_are_spotify_only(tmp_path, capsys) -> None:
    from neurowave.cli import main

    code = main(
        [
            "--home", str(tmp_path / "home"),
            "--provider", "soundcloud",
            "login", "--token", "k", "--refresh-token", "r", "--client-id", "c",
        ]
    )
    assert code == 2
    assert "только для Spotify" in capsys.readouterr().err


def test_persist_refreshed_token_saves_only_changes(tmp_path, monkeypatch) -> None:
    from types import SimpleNamespace

    from neurowave import cli
    from neurowave.config import Paths
    from neurowave.storage import TokenStore

    monkeypatch.setattr(TokenStore, "_import_keyring", staticmethod(lambda: None))
    store = TokenStore(Paths.resolve({"NEUROWAVE_HOME": str(tmp_path)}).ensure(), provider="spotify")
    store.save("old")

    adapter = SimpleNamespace(storage_token=lambda: "new")
    cli._persist_refreshed_token(SimpleNamespace(adapter=adapter, tokens=store))
    assert store.load() == "new"

    # Провайдер без ротации ничего не пишет и не падает.
    cli._persist_refreshed_token(SimpleNamespace(adapter=object(), tokens=store))
    assert store.load() == "new"


def login(home: str) -> int:
    from neurowave.cli import main

    return main(["--home", home, "--provider", "spotify", "login", "--token", "token-1"])


def test_cli_devices_lists_connect_targets(tmp_path, capsys, monkeypatch) -> None:
    from neurowave.cli import main

    patch_spotify(monkeypatch)
    home = str(tmp_path / "home")
    assert login(home) == 0
    capsys.readouterr()

    assert main(["--home", home, "--provider", "spotify", "devices"]) == 0
    out = capsys.readouterr().out
    assert "Kitchen" in out and "Living Room" in out
    assert "* Living Room" in out  # активное устройство помечено
    assert "35%" in out

    assert main(["--home", home, "--provider", "spotify", "--json", "devices"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert {row["id"] for row in data} == {"dev-1", "dev-2"}


def test_cli_devices_without_targets_is_honest(tmp_path, capsys, monkeypatch) -> None:
    from neurowave.cli import main

    patch_spotify(monkeypatch, devices=[])
    home = str(tmp_path / "home")
    login(home)
    capsys.readouterr()

    assert main(["--home", home, "--provider", "spotify", "devices"]) == 1
    assert "не найдены" in capsys.readouterr().out


def test_cli_connect_matches_id_name_and_substring(tmp_path, capsys, monkeypatch) -> None:
    from neurowave.cli import main

    calls = patch_spotify(monkeypatch)
    home = str(tmp_path / "home")
    login(home)
    capsys.readouterr()

    assert main(["--home", home, "--provider", "spotify", "connect", "dev-1"]) == 0
    assert main(["--home", home, "--provider", "spotify", "connect", "living room"]) == 0
    assert main(["--home", home, "--provider", "spotify", "connect", "Kitc"]) == 0
    out = capsys.readouterr().out
    assert "«Kitchen»" in out
    assert calls == [("transfer", "dev-1"), ("transfer", "dev-2"), ("transfer", "dev-1")]


def test_cli_connect_unknown_device_lists_available(tmp_path, capsys, monkeypatch) -> None:
    from neurowave.cli import main

    patch_spotify(monkeypatch)
    home = str(tmp_path / "home")
    login(home)
    capsys.readouterr()

    assert main(["--home", home, "--provider", "spotify", "connect", "Garage"]) == 2
    err = capsys.readouterr().err
    assert "Garage" in err
    assert "Kitchen" in err


def test_cli_remote_commands_control_device(tmp_path, capsys, monkeypatch) -> None:
    from neurowave.cli import main

    calls = patch_spotify(monkeypatch)
    home = str(tmp_path / "home")
    login(home)
    capsys.readouterr()

    assert main(["--home", home, "--provider", "spotify", "remote", "play", "nightcall"]) == 0
    assert main(["--home", home, "--provider", "spotify", "remote", "play", "--uri", "spotify:album:z"]) == 0
    assert main(["--home", home, "--provider", "spotify", "remote", "pause"]) == 0
    assert main(["--home", home, "--provider", "spotify", "remote", "next"]) == 0
    assert main(["--home", home, "--provider", "spotify", "remote", "previous"]) == 0

    out = capsys.readouterr().out
    assert "Играет на устройстве Spotify: Kavinsky — Nightcall" in out
    assert calls == [
        ("play", ("tr-1",)),
        ("play", "spotify:album:z"),
        ("pause",),
        ("next",),
        ("previous",),
    ]


def test_cli_play_is_refused_without_direct_stream(tmp_path, capsys, monkeypatch) -> None:
    from neurowave.cli import main

    patch_spotify(monkeypatch)
    home = str(tmp_path / "home")
    login(home)
    capsys.readouterr()

    assert main(["--home", home, "--provider", "spotify", "play", "nightcall"]) == 2
    err = capsys.readouterr().err
    assert "не поддерживает" in err
    assert "поток в mpv" in err


def test_cli_devices_requires_token(tmp_path, capsys) -> None:
    from neurowave.cli import main

    assert main(["--home", str(tmp_path / "home"), "--provider", "spotify", "devices"]) == 2
    err = capsys.readouterr().err
    assert "Требуется вход" in err
    assert "login --token" in err


# --- закрытые долги: refresh при входе, scopes, remote --device ---------
def test_verify_proves_refresh_token_and_sets_expiry() -> None:
    credentials = SpotifyCredentials(
        access_token="access", refresh_token="ref", client_id="cid"
    )
    transport = FakeTransport(
        [
            make_response({"id": "u1"}),
            make_response({"access_token": "fresh", "expires_in": 3600}),
        ]
    )
    client = SpotifyClient(
        credentials=credentials, transport=transport, clock=lambda: 100.0
    )

    client.verify()

    assert [call[1] for call in transport.calls] == [
        "https://api.spotify.com/v1/me",
        "https://accounts.spotify.com/api/token",
    ]
    assert credentials.access_token == "fresh"
    assert credentials.expires_at == 3700.0
    assert json.loads(client.storage_token())["refresh_token"] == "ref"


def test_verify_rejects_bad_refresh_token_at_login() -> None:
    credentials = SpotifyCredentials(
        access_token="access", refresh_token="bad-refresh", client_id="cid"
    )
    transport = FakeTransport(
        [
            make_response({"id": "u1"}),
            make_response({"error": "invalid_grant"}, status=400),
        ]
    )
    client = SpotifyClient(credentials=credentials, transport=transport)
    with pytest.raises(AuthError) as exc:
        client.verify()
    message = str(exc.value)
    assert "Не удалось обновить токен" in message
    assert "bad-refresh" not in message


def test_403_scope_error_names_missing_permissions() -> None:
    response = make_response(
        {"error": {"status": 403, "message": "Insufficient client scope"}}, status=403
    )
    with pytest.raises(AuthError) as exc:
        make_client(FakeTransport([response])).play(["spotify:track:tr-1"])
    message = str(exc.value)
    assert "Insufficient client scope" in message
    assert "user-modify-playback-state" in message


def test_cli_remote_device_moves_playback_first(tmp_path, capsys, monkeypatch) -> None:
    from neurowave.cli import main

    calls = patch_spotify(monkeypatch)
    home = str(tmp_path / "home")
    login(home)
    capsys.readouterr()

    code = main(
        [
            "--home", home,
            "--provider", "spotify",
            "remote", "play", "nightcall",
            "--device", "living room",
        ]
    )
    assert code == 0
    assert calls == [("transfer", "dev-2"), ("play", ("tr-1",))]
    assert "перенесено на «Living Room»" in capsys.readouterr().out


def test_cli_remote_unknown_device_is_named_error(tmp_path, capsys, monkeypatch) -> None:
    from neurowave.cli import main

    calls = patch_spotify(monkeypatch)
    home = str(tmp_path / "home")
    login(home)
    capsys.readouterr()

    code = main(
        [
            "--home", home,
            "--provider", "spotify",
            "remote", "pause",
            "--device", "nowhere",
        ]
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "не найдено" in err
    assert "Kitchen" in err
    assert calls == []
