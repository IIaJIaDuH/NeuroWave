"""SoundCloud: клиент, адаптер и маршрутизация без сети (фейковый транспорт)."""

from __future__ import annotations

import json

import pytest

from neurowave.audio import NullBackend
from neurowave.config import Settings
from neurowave.domain import Track, unique_tracks
from neurowave.errors import ApiError, AuthError, NetworkError, TrackUnavailable
from neurowave.player import PlayerService
from neurowave.providers.base import AuthKind, Capability
from neurowave.providers.registry import get_spec
from neurowave.providers.router import ProviderRouter
from neurowave.soundcloud import HttpResponse, SoundCloudAdapter, SoundCloudClient
from neurowave.storage.cache import MediaCache

PLAYABLE = {
    "id": 101,
    "title": "Midnight Drive",
    "duration": 215000,
    "access": "playable",
    "streamable": True,
    "permalink_url": "https://soundcloud.com/artist/midnight-drive",
    "user": {"username": "Some Artist", "permalink_url": "https://soundcloud.com/artist"},
}
PREVIEW = {
    "id": 202,
    "title": "Snippet",
    "duration": 30000,
    "access": "preview",
    "permalink_url": "https://soundcloud.com/artist/snippet",
    "user": {"username": "Some Artist"},
}
BLOCKED = {
    "id": 303,
    "title": "Blocked",
    "access": "blocked",
    "streamable": False,
    "user": {"username": "Some Artist"},
}
CDN_URL = "https://cf-media.sndcdn.com/abc123.128.mp3?Policy=xyz"


def make_response(payload, status: int = 200, headers=None) -> HttpResponse:
    return HttpResponse(
        status=status,
        headers=headers or {},
        body=json.dumps(payload).encode("utf-8"),
    )


class FakeTransport:
    """Транспорт без сети: отдаёт заранее заданные ответы по порядку."""

    def __init__(self, json_responses=None, redirect=None):
        self._json = list(json_responses or [])
        self._redirect = redirect
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def json(self, url, headers, timeout):
        self.calls.append(("json", url, dict(headers)))
        response = self._json.pop(0) if len(self._json) > 1 else self._json[0]
        if isinstance(response, Exception):
            raise response
        return response

    def resolve(self, url, headers, timeout):
        self.calls.append(("resolve", url, dict(headers)))
        assert self._redirect is not None, "resolve не ожидался"
        return self._redirect


def make_client(transport: FakeTransport, token: str = "app-client-id") -> SoundCloudClient:
    sleeps: list[float] = []
    return SoundCloudClient(token=token, transport=transport, sleep=sleeps.append)


# --- клиент -------------------------------------------------------------
def test_client_sends_client_id_for_app_key() -> None:
    transport = FakeTransport([make_response({"collection": [PLAYABLE]})])
    client = make_client(transport)
    client.search_tracks("midnight", limit=5)

    kind, url, headers = transport.calls[0]
    assert kind == "json"
    assert "client_id=app-client-id" in url
    assert "access=playable" in url
    assert "q=midnight" in url
    assert "Authorization" not in headers


def test_client_uses_oauth_header_for_user_token() -> None:
    transport = FakeTransport([make_response({"collection": []})])
    make_client(transport, token="2-123-user-token").search_tracks("x")
    _, url, headers = transport.calls[0]
    assert headers["Authorization"] == "OAuth 2-123-user-token"
    assert "client_id=" not in url


def test_client_requires_key() -> None:
    client = SoundCloudClient(token=None, transport=FakeTransport([make_response({})]))
    with pytest.raises(AuthError) as exc:
        client.search_tracks("x")
    assert "--token" in str(exc.value)


def test_client_verify_accepts_valid_key_with_cheap_request() -> None:
    transport = FakeTransport([make_response({"collection": []})])
    make_client(transport).verify()
    _, url, headers = transport.calls[0]
    assert "/tracks?" in url
    assert "limit=1" in url
    assert headers.get("Authorization") is None  # client_id ушёл параметром


def test_client_verify_rejects_bad_key() -> None:
    transport = FakeTransport([make_response({}, status=401)])
    with pytest.raises(AuthError) as exc:
        make_client(transport).verify()
    assert "отклонил ключ" in str(exc.value)
    assert len(transport.calls) == 1  # 401 не повторяем


def test_adapter_verify_delegates_to_client() -> None:
    transport = FakeTransport([make_response({"collection": []})])
    make_adapter(transport).verify()
    assert len(transport.calls) == 1

    bad = FakeTransport([make_response({}, status=401)])
    with pytest.raises(AuthError):
        make_adapter(bad).verify()


def test_client_maps_http_errors() -> None:
    with pytest.raises(AuthError):
        make_client(FakeTransport([make_response({}, status=401)])).search_tracks("x")
    with pytest.raises(TrackUnavailable):
        make_client(FakeTransport([make_response({}, status=404)])).search_tracks("x")
    with pytest.raises(ApiError) as exc:
        make_client(FakeTransport([make_response({}, status=429)])).search_tracks("x")
    assert "частоту" in str(exc.value)


def test_client_retries_server_errors_then_fails() -> None:
    transport = FakeTransport([make_response({}, status=503)])
    client = SoundCloudClient(token="k", transport=transport, retries=3, sleep=lambda _: None)
    with pytest.raises(ApiError):
        client.search_tracks("x")
    assert len(transport.calls) == 3


def test_client_recovers_after_transient_error() -> None:
    transport = FakeTransport(
        [make_response({}, status=502), make_response({"collection": [PLAYABLE]})]
    )
    client = SoundCloudClient(token="k", transport=transport, retries=3, sleep=lambda _: None)
    assert client.search_tracks("x") == [PLAYABLE]
    assert len(transport.calls) == 2


def test_stream_location_returns_signed_cdn_url() -> None:
    transport = FakeTransport(
        redirect=HttpResponse(status=302, headers={"Location": CDN_URL})
    )
    client = make_client(transport)
    assert client.stream_location("101") == CDN_URL
    kind, url, _ = transport.calls[0]
    assert kind == "resolve"
    assert url.endswith("/tracks/101/stream?client_id=app-client-id")


def test_stream_location_rejects_blocked_and_missing() -> None:
    with pytest.raises(TrackUnavailable):
        make_client(FakeTransport(redirect=HttpResponse(status=403))).stream_location("1")
    with pytest.raises(NetworkError):
        make_client(FakeTransport(redirect=HttpResponse(status=503))).stream_location("1")
    with pytest.raises(ApiError):
        make_client(FakeTransport(redirect=HttpResponse(status=302, headers={}))).stream_location("1")


# --- адаптер ------------------------------------------------------------
def make_adapter(transport: FakeTransport, token: str = "app-client-id") -> SoundCloudAdapter:
    return SoundCloudAdapter(client=make_client(transport, token=token))


def test_search_maps_tracks_with_attribution() -> None:
    transport = FakeTransport([make_response({"collection": [PLAYABLE]})])
    results = make_adapter(transport).search("midnight", limit=10)

    assert len(results.tracks) == 1
    track = results.tracks[0]
    assert track.id == "101"
    assert track.title == "Midnight Drive"
    assert track.artists == ("Some Artist",)
    assert track.duration_ms == 215000
    assert track.provider == "soundcloud"
    assert track.key == "soundcloud:101"
    assert track.url == "https://soundcloud.com/artist/midnight-drive"
    assert track.available is True


def test_search_drops_preview_and_blocked_tracks() -> None:
    transport = FakeTransport([make_response({"collection": [PLAYABLE, PREVIEW, BLOCKED]})])
    results = make_adapter(transport).search("anything")
    assert [track.id for track in results.tracks] == ["101"]


def test_empty_query_does_not_touch_the_network() -> None:
    transport = FakeTransport([make_response({"collection": []})])
    assert make_adapter(transport).search("   ").tracks == ()
    assert transport.calls == []


def test_stream_url_checks_access_then_returns_mp3() -> None:
    transport = FakeTransport(
        json_responses=[make_response(PLAYABLE)],
        redirect=HttpResponse(status=302, headers={"Location": CDN_URL}),
    )
    stream = make_adapter(transport).stream_url("101")
    assert stream.url == CDN_URL
    assert stream.quality.codec == "mp3"
    assert stream.quality.lossless is False


def test_stream_url_refuses_preview_tracks() -> None:
    transport = FakeTransport(
        json_responses=[make_response(PREVIEW)],
        redirect=HttpResponse(status=302, headers={"Location": CDN_URL}),
    )
    with pytest.raises(TrackUnavailable) as exc:
        make_adapter(transport).stream_url("202")
    assert "превью" in str(exc.value)
    assert [call[0] for call in transport.calls] == ["json"]  # до CDN не дошли


def test_stream_url_requires_key() -> None:
    adapter = SoundCloudAdapter(client=SoundCloudClient(token=None, transport=FakeTransport([])))
    with pytest.raises(AuthError):
        adapter.stream_url("101")


# --- провайдер и маршрутизация -----------------------------------------
def test_provider_spec_is_honest_about_capabilities() -> None:
    info = get_spec("soundcloud").info
    assert info.implemented is True
    assert info.auth is AuthKind.API_TOKEN
    assert info.cache_allowed is False
    assert info.plays_in_mpv is True
    assert info.supports(Capability.CATALOG)
    assert not info.supports(Capability.LIBRARY)
    assert not info.supports(Capability.PERSONAL_RADIO)
    assert not info.supports(Capability.PLAYLISTS)
    assert "атрибуция" in info.notes


def test_router_disables_cache_and_plays_soundcloud_track(tmp_path) -> None:
    transport = FakeTransport(
        json_responses=[make_response(PLAYABLE)],
        redirect=HttpResponse(status=302, headers={"Location": CDN_URL}),
    )
    adapter = make_adapter(transport)
    router = ProviderRouter({"soundcloud": lambda: adapter}, default="soundcloud")
    cache = MediaCache(tmp_path / "cache")
    cache.put_bytes("soundcloud:101", b"stale-should-not-be-used")

    player = PlayerService(
        adapter=adapter,
        backend=NullBackend(),
        settings=Settings(volume=50),
        cache=cache,
        router=router,
        prefetch=False,
    )
    try:
        track = Track(id="101", title="Midnight Drive", provider="soundcloud")
        assert router.cache_allowed(track) is False
        player.play_track(track)
        assert player.backend.loaded_url == CDN_URL
        assert player.current_quality is not None
        assert player.current_quality.codec == "mp3"
    finally:
        player.close()


def test_unique_tracks_keeps_same_id_from_different_providers() -> None:
    tracks = unique_tracks(
        [
            Track(id="1", title="a", provider="soundcloud"),
            Track(id="1", title="b", provider="local"),
            Track(id="1", title="a again", provider="soundcloud"),
        ]
    )
    assert [track.key for track in tracks] == ["soundcloud:1", "local:1"]


# --- CLI ----------------------------------------------------------------
def test_cli_soundcloud_without_key_explains_how_to_login(tmp_path, capsys) -> None:
    from neurowave.cli import main

    code = main(["--home", str(tmp_path / "home"), "--provider", "soundcloud", "search", "x"])
    captured = capsys.readouterr()
    assert code == 2
    assert "login --token" in captured.err


def patch_verify(monkeypatch, behaviour) -> None:
    """Подменить сетевую проверку ключа, чтобы CLI-тесты шли без сети."""
    from neurowave.soundcloud import SoundCloudAdapter

    def fake_verify(self) -> None:
        if isinstance(behaviour, Exception):
            raise behaviour

    monkeypatch.setattr(SoundCloudAdapter, "verify", fake_verify)


def test_cli_soundcloud_login_verifies_and_saves_key(tmp_path, capsys, monkeypatch) -> None:
    from neurowave.cli import main

    patch_verify(monkeypatch, None)
    home = tmp_path / "home"
    code = main(
        ["--home", str(home), "--provider", "soundcloud", "login", "--token", "app-key-123"]
    )
    captured = capsys.readouterr()
    assert code == 0
    assert "Проверяю ключ…" in captured.out
    assert "Ключ сохранён" in captured.out
    assert "app-key-123" not in captured.out  # ключ не печатается

    code = main(["--home", str(home), "--provider", "soundcloud", "--json", "status"])
    data = json.loads(capsys.readouterr().out)
    assert data["provider"]["id"] == "soundcloud"
    assert data["token"]["present"] is True
    assert data["token"]["required"] is True


def test_cli_soundcloud_login_rejects_bad_key_before_saving(tmp_path, capsys, monkeypatch) -> None:
    from neurowave.cli import main

    patch_verify(monkeypatch, AuthError("SoundCloud отклонил ключ: проверьте client_id/access token"))
    home = tmp_path / "home"
    code = main(
        ["--home", str(home), "--provider", "soundcloud", "login", "--token", "bad-key"]
    )
    captured = capsys.readouterr()
    assert code == 2
    assert "отклонил ключ" in captured.err
    assert "Ключ сохранён" not in captured.out

    main(["--home", str(home), "--provider", "soundcloud", "--json", "status"])
    data = json.loads(capsys.readouterr().out)
    assert data["token"]["present"] is False


def test_cli_soundcloud_login_keeps_key_when_network_unavailable(
    tmp_path, capsys, monkeypatch
) -> None:
    from neurowave.cli import main

    patch_verify(monkeypatch, NetworkError("Сеть недоступна: timeout"))
    home = tmp_path / "home"
    code = main(
        ["--home", str(home), "--provider", "soundcloud", "login", "--token", "key-123"]
    )
    captured = capsys.readouterr()
    assert code == 0
    assert "Ключ не проверен" in captured.err
    assert "Ключ сохранён" in captured.out


def test_cli_yandex_rejects_manual_key(tmp_path, capsys) -> None:
    from neurowave.cli import main

    code = main(["--home", str(tmp_path / "home"), "login", "--token", "nope"])
    assert code == 2
    assert "интерактивный вход" in capsys.readouterr().err


def test_cli_soundcloud_library_is_refused(tmp_path, capsys, monkeypatch) -> None:
    from neurowave.cli import main

    patch_verify(monkeypatch, None)
    home = tmp_path / "home"
    main(["--home", str(home), "--provider", "soundcloud", "login", "--token", "k"])
    capsys.readouterr()
    code = main(["--home", str(home), "--provider", "soundcloud", "library"])
    assert code == 2
    assert "не поддерживает" in capsys.readouterr().err


# --- закрытый долг: метаданные трека не читаются дважды подряд ----------
def test_track_metadata_is_cached_in_memory() -> None:
    transport = FakeTransport([make_response(PLAYABLE)])
    client = make_client(transport)

    first = client.track("101")
    second = client.track("101")

    assert first == second == PLAYABLE
    assert len(transport.calls) == 1
    assert first is not second  # наружу — копия, кеш не меняется снаружи


def test_stream_url_reuses_cached_track_metadata() -> None:
    transport = FakeTransport(
        [make_response(PLAYABLE)],
        redirect=HttpResponse(status=302, headers={"Location": CDN_URL}),
    )
    adapter = SoundCloudAdapter(client=make_client(transport))

    adapter.stream_url("101")
    adapter.stream_url("101")

    json_calls = [call for call in transport.calls if call[0] == "json"]
    assert len(json_calls) == 1


def test_track_cache_expires_and_is_bounded() -> None:
    now = [0.0]
    transport = FakeTransport([make_response(PLAYABLE)])
    client = SoundCloudClient(
        token="k", transport=transport, cache_ttl=10, cache_size=1, clock=lambda: now[0]
    )
    client.track("101")
    now[0] = 11.0
    client.track("101")
    assert len(transport.calls) == 2

    other = {**PLAYABLE, "id": 202}
    two = FakeTransport([make_response(PLAYABLE), make_response(other)])
    bounded = SoundCloudClient(token="k", transport=two, cache_size=1, clock=lambda: 0.0)
    bounded.track("101")
    bounded.track("202")
    bounded.track("101")  # 101 уже вытеснен
    assert len(two.calls) == 3
