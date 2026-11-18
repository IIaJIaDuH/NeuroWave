"""Контрактные тесты интеграции с Яндекс Музыкой (WO-01).

Проверяем границу с неофициальной библиотекой ``yandex-music`` через
обезличенные снимки ответов (``tests/fixtures/yandex``) и подменённый клиент.
Никакой сети и настоящих токенов здесь нет.

Матрица состояний: успешный ответ → доменная модель; пустой ответ → пустая
выдача; изменённая схема → контролируемая ошибка или деградация без порчи
моделей; 400/401/403/404/429/5xx/timeout → доменные ошибки с конечным числом
попыток и без тела ответа сервера в тексте.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest
from yandex_music import exceptions as ym_exceptions

from neurowave.audio import NullBackend
from neurowave.cli import main
from neurowave.config import Settings
from neurowave.domain import Track
from neurowave.errors import (
    ApiError,
    AuthError,
    ConfigError,
    NetworkError,
    NeuroWaveError,
    SubscriptionRequired,
    TrackUnavailable,
)
from neurowave.local import LocalMusicAdapter
from neurowave.player import PlayerService
from neurowave.providers.router import ProviderRouter
from neurowave.state import PlayerStatus
from neurowave.yandex import YandexMusicAdapter
from neurowave.yandex import adapter as adapter_module

FIXTURES = Path(__file__).parent / "fixtures" / "yandex"
SECRET_TOKEN = "secret-token-never-in-messages"
SECRET_BODY_MARKER = "INTERNAL-BODY-MARKER"


# --- обезличенные фикстуры ------------------------------------------------
def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def ns(value: Any) -> Any:
    """Словарь снимка → объект, как его отдаёт yandex-music после разбора."""
    if isinstance(value, dict):
        return SimpleNamespace(**{key: ns(item) for key, item in value.items()})
    if isinstance(value, list):
        return [ns(item) for item in value]
    return value


class FakeDownload:
    def __init__(self, codec: Any, bitrate: Any, link: Any, resolver: Callable[[], Any] | None = None):
        self.codec = codec
        self.bitrate_in_kbps = bitrate
        self.direct_link = link
        self._resolver = resolver

    def get_direct_link(self) -> Any:
        if self._resolver is None:
            raise ym_exceptions.NetworkError("timed out")
        return self._resolver()


class FakeClient:
    """Подменённый клиент: считает вызовы и отдаёт заданные ответы."""

    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()
        self.search_response: Any = ns(fixture("search_success.json"))
        self.download_response: Any = [
            FakeDownload(item["codec"], item["bitrate_in_kbps"], item["direct_link"])
            for item in fixture("download_info_success.json")["items"]
        ]
        self.rotor_response: Any = ns(fixture("rotor_success.json"))
        library = fixture("library_success.json")
        self.liked_response: Any = ns(library["liked_tracks"])
        self.playlist_response: Any = ns(library["playlist"])
        self.playlist_tracks_response: Any = ns(library["playlist_tracks"])
        self.playlists_response: Any = [SimpleNamespace(kind=3, title="Anonymized", track_count=1)]
        self.me = SimpleNamespace(
            account=SimpleNamespace(
                uid=42, login="listener", display_name="Listener", service_available=True
            )
        )
        self.like_result: Any = True
        self.feedback_result: Any = True

    # каталог
    def search(self, text, type_="all"):
        self.calls["search"] += 1
        return self.search_response

    def users_likes_tracks(self):
        self.calls["users_likes_tracks"] += 1
        return SimpleNamespace(fetch_tracks=self._fetch_tracks)

    def _fetch_tracks(self):
        self.calls["fetch_tracks"] += 1
        return self.liked_response

    def music_history(self):  # pragma: no cover - не используется в тестах
        self.calls["music_history"] += 1
        return None

    # библиотека
    def users_likes_tracks_add(self, track_ids):
        self.calls["like"] += 1
        return self.like_result

    def users_likes_tracks_remove(self, track_ids):
        self.calls["unlike"] += 1
        return self.like_result

    def users_playlists_list(self):
        self.calls["playlists"] += 1
        return self.playlists_response

    def users_playlists(self, identifier):
        self.calls["playlist"] += 1
        return self.playlist_response

    def tracks(self, ids):
        self.calls["tracks"] += 1
        return self.playlist_tracks_response

    # поток и волна
    def tracks_download_info(self, track_id, get_direct_links=False):
        self.calls["download_info"] += 1
        return self.download_response

    def rotor_station_tracks(self, station, queue=None):
        self.calls["rotor"] += 1
        return self.rotor_response

    def rotor_station_feedback(self, *args, **kwargs):
        self.calls["feedback"] += 1
        return self.feedback_result


def make_adapter(
    client: FakeClient | None = None,
    *,
    retries: int = 3,
    backoff: float = 0.01,
    sleeps: list[float] | None = None,
) -> tuple[YandexMusicAdapter, list[float]]:
    recorded: list[float] = [] if sleeps is None else sleeps
    adapter = YandexMusicAdapter(
        client=client or FakeClient(),
        retries=retries,
        backoff=backoff,
        sleep=recorded.append,
    )
    return adapter, recorded


def failing(
    exc: BaseException,
    client: FakeClient | None = None,
    name: str | None = None,
) -> Callable[..., Any]:
    """Функция, которая всегда падает; при желании считает попытки."""

    def boom(*args, **kwargs):
        if client is not None and name is not None:
            client.calls[name] += 1
        raise exc

    return boom


# --- успешные контракты ---------------------------------------------------
def test_contract_search_success_from_fixture():
    adapter, _ = make_adapter()
    results = adapter.search("anonymized")

    assert [track.id for track in results.tracks] == ["1001"]
    track = results.tracks[0]
    assert track.title == "Anonymized Song"
    assert track.artists == ("Anonymized Artist",)
    assert track.album == "Anonymized Album"
    assert track.duration_ms == 187000
    assert track.key == "yandex:1001"
    assert results.artists[0].name == "Anonymized Artist"
    assert results.albums[0].subtitle == "2021"
    assert results.playlists[0].id == "42:3"


def test_contract_stream_prefers_lossless_from_fixture():
    adapter, _ = make_adapter()
    stream = adapter.stream_url("1001")

    assert stream.url == "https://example.invalid/stream/flac"
    assert stream.quality.codec == "flac"
    assert stream.quality.bitrate_kbps == 1411
    assert stream.quality.lossless is True


def test_contract_rotor_sequence_skips_items_without_track():
    adapter, _ = make_adapter()
    tracks = adapter.wave_tracks()

    assert [track.id for track in tracks] == ["1002"]
    assert tracks[0].liked is True


def test_contract_library_likes_and_playlist():
    adapter, _ = make_adapter()
    assert [track.id for track in adapter.liked_tracks()] == ["1003"]
    assert adapter.liked_tracks()[0].liked is True
    assert adapter.playlists()[0].id == "3"
    assert [track.id for track in adapter.playlist_tracks(3)] == ["1003"]


# --- пустые и изменившиеся ответы ---------------------------------------
@pytest.mark.parametrize(
    "response",
    [
        None,
        SimpleNamespace(tracks=None),
        SimpleNamespace(tracks=SimpleNamespace(results=None)),
        SimpleNamespace(tracks=SimpleNamespace(results=[])),
        SimpleNamespace(tracks=SimpleNamespace(items=[{"id": 1001}])),  # поле переименовано
        SimpleNamespace(tracks=SimpleNamespace(results={"1001": {"id": 1001}})),
        SimpleNamespace(tracks="unexpected"),
    ],
)
def test_search_empty_or_changed_shape_is_empty_not_crash(response):
    client = FakeClient()
    client.search_response = response
    adapter, _ = make_adapter(client)

    results = adapter.search("anonymized")

    assert results.is_empty()


def test_search_all_tracks_malformed_is_controlled_error():
    client = FakeClient()
    client.search_response = SimpleNamespace(
        tracks=SimpleNamespace(results=[{"no_id": True}, SimpleNamespace(title="x")])
    )
    adapter, _ = make_adapter(client)

    with pytest.raises(ApiError) as exc:
        adapter.search("anonymized")
    assert "формат" in str(exc.value)


def test_track_without_id_is_skipped_but_valid_ones_survive():
    client = FakeClient()
    client.search_response = SimpleNamespace(
        tracks=SimpleNamespace(
            results=[
                SimpleNamespace(id=None, title="broken"),
                SimpleNamespace(id=1005, title="ok"),
            ]
        )
    )
    adapter, _ = make_adapter(client)

    assert [track.id for track in adapter.search("x").tracks] == ["1005"]


def test_malformed_fields_degrade_without_corrupt_models():
    item = SimpleNamespace(
        id=1004,
        title="",
        artists="Solo Artist",
        albums=[],
        duration_ms="not-a-number",
        available="false",
    )
    track = YandexMusicAdapter._to_track(item)

    assert track is not None
    assert track.id == "1004"
    assert track.title == "Без названия"
    assert track.artists == ("Solo Artist",)
    assert track.album is None
    assert track.duration_ms is None
    assert track.available is False
    assert track.duration_text == "--:--"  # CLI не получает строку вместо числа


def test_numeric_strings_are_normalized():
    item = SimpleNamespace(id=1006, title="t", duration_ms="123000", available=True)
    track = YandexMusicAdapter._to_track(item)

    assert track is not None
    assert track.duration_ms == 123000
    assert track.duration_text == "2:03"


# --- HTTP и сетевые ошибки -----------------------------------------------
def test_401_maps_to_auth_error_without_retry():
    client = FakeClient()
    client.search = failing(
        ym_exceptions.UnauthorizedError('{"error":"unauthorized"}'), client, "search"
    )
    adapter, sleeps = make_adapter(client)

    with pytest.raises(AuthError) as exc:
        adapter.search("x")

    assert "neurowave login" in str(exc.value)
    assert client.calls["search"] == 1
    assert sleeps == []


def test_403_with_subscription_signs_maps_to_subscription_required():
    """401 и 403 библиотека отдаёт одним классом; подписка различается по тексту."""
    client = FakeClient()
    client.search = failing(
        ym_exceptions.UnauthorizedError('{"error":"forbidden","message":"Plus subscription required"}')
    )
    adapter, _ = make_adapter(client)

    with pytest.raises(SubscriptionRequired):
        adapter.search("x")


def test_404_maps_to_track_unavailable_and_is_not_retried():
    """NotFoundError наследуется от NetworkError: раньше 404 уходил в retry."""
    client = FakeClient()
    client.tracks_download_info = failing(
        ym_exceptions.NotFoundError('{"error":"not found"}'), client, "download_info"
    )
    adapter, sleeps = make_adapter(client)

    with pytest.raises(TrackUnavailable):
        adapter.stream_url("1001")

    assert client.calls["download_info"] == 1
    assert sleeps == []


def test_400_maps_to_api_error_without_retry():
    """BadRequestError тоже наследник NetworkError, но повторять его бессмысленно."""
    client = FakeClient()
    client.search = failing(ym_exceptions.BadRequestError("bad request"), client, "search")
    adapter, sleeps = make_adapter(client)

    with pytest.raises(ApiError) as exc:
        adapter.search("x")

    assert "400" in str(exc.value)
    assert client.calls["search"] == 1
    assert sleeps == []


def test_429_retries_are_bounded_and_explained():
    client = FakeClient()
    client.search = failing(
        ym_exceptions.NetworkError('Unknown HTTPError (429): {"error":"too many requests"}')
    )
    adapter, sleeps = make_adapter(client, retries=5, backoff=10.0)

    with pytest.raises(NetworkError) as exc:
        adapter.search("x")

    assert "429" in str(exc.value)
    assert sum(sleeps) <= adapter_module.MAX_RETRY_DELAY * 1.2
    assert client.calls["search"] < 5


def test_5xx_retries_then_fails_without_leaking_server_body():
    client = FakeClient()
    client.search = failing(
        ym_exceptions.NetworkError(f'Unknown HTTPError (503): {{"error":"{SECRET_BODY_MARKER}"}}'),
        client,
        "search",
    )
    adapter, _ = make_adapter(client, retries=2)

    with pytest.raises(NetworkError) as exc:
        adapter.search("x")

    message = str(exc.value)
    assert "503" in message
    assert SECRET_BODY_MARKER not in message
    assert client.calls["search"] == 2


def test_transient_network_error_recovers():
    client = FakeClient()
    original = client.search
    state = {"failures": 1}

    def flaky(*args, **kwargs):
        if state["failures"]:
            state["failures"] -= 1
            raise ym_exceptions.NetworkError("connection reset")
        return original(*args, **kwargs)

    client.search = flaky  # type: ignore[method-assign]
    adapter, _ = make_adapter(client)

    assert adapter.search("x").tracks


def test_timeout_is_reported_as_timeout():
    client = FakeClient()
    client.search = failing(ym_exceptions.TimedOutError())
    adapter, _ = make_adapter(client, retries=1)

    with pytest.raises(NetworkError) as exc:
        adapter.search("x")

    assert "вовремя" in str(exc.value)


def test_schema_mismatch_maps_to_actionable_api_error():
    client = FakeClient()
    client.search = failing(ym_exceptions.SchemaMismatchError("unexpected field"))
    adapter, _ = make_adapter(client)

    with pytest.raises(ApiError) as exc:
        adapter.search("x")

    assert "yandex-music" in str(exc.value)


def test_write_operations_are_not_retried():
    client = FakeClient()
    client.users_likes_tracks_add = failing(
        ym_exceptions.NetworkError("connection reset"), client, "like"
    )
    client.users_likes_tracks_remove = failing(
        ym_exceptions.NetworkError("connection reset"), client, "unlike"
    )
    client.rotor_station_feedback = failing(
        ym_exceptions.NetworkError("connection reset"), client, "feedback"
    )
    adapter, sleeps = make_adapter(client, retries=5)

    with pytest.raises(NetworkError):
        adapter.like("1001")
    with pytest.raises(NetworkError):
        adapter.unlike("1001")

    assert client.calls["like"] == 1
    assert client.calls["unlike"] == 1
    assert sleeps == []
    # feedback по умолчанию глотает свои ошибки, но тоже не повторяется
    assert adapter.wave_feedback("started", "1001") is False
    assert client.calls["feedback"] == 1


# --- поток ---------------------------------------------------------------
def test_stream_without_usable_link_is_track_unavailable():
    client = FakeClient()
    client.download_response = [
        FakeDownload("mp3", 320, None, resolver=None),  # resolver падает сетью
        FakeDownload(None, 128, None, resolver=None),
    ]
    adapter, _ = make_adapter(client, retries=1)

    with pytest.raises(TrackUnavailable):
        adapter.stream_url("1001")


def test_stream_resolves_link_when_library_did_not_include_it():
    client = FakeClient()
    client.download_response = [
        FakeDownload("mp3", 192, None, resolver=lambda: "https://example.invalid/late"),
    ]
    adapter, _ = make_adapter(client)

    stream = adapter.stream_url("1001")

    assert stream.url == "https://example.invalid/late"
    assert stream.quality.bitrate_kbps == 192
    assert stream.quality.lossless is False


@pytest.mark.parametrize("response", [{"codec": "mp3"}, "unexpected", 42])
def test_stream_non_list_response_is_api_error(response):
    client = FakeClient()
    client.download_response = response
    adapter, _ = make_adapter(client)

    with pytest.raises(ApiError):
        adapter.stream_url("1001")


def test_stream_missing_download_info_is_api_error():
    client = FakeClient()
    client.tracks_download_info = failing(ym_exceptions.NotFoundError("404"))
    adapter, _ = make_adapter(client)

    with pytest.raises(TrackUnavailable):
        adapter.stream_url("1001")


# --- подключение ---------------------------------------------------------
class _ClientThatFailsInit:
    def __init__(self, token=None, **kwargs):
        self.token = token

    def init(self):
        raise ym_exceptions.NetworkError(
            f'Unknown HTTPError (503): {{"error":"{SECRET_BODY_MARKER}"}}'
        )


class _ClientWithoutResult:
    def __init__(self, token=None, **kwargs):
        self.token = token

    def init(self):
        return None


class _ClientThatWorks:
    def __init__(self, token=None, **kwargs):
        self.token = token
        self.ready = False

    def init(self):
        self.ready = True
        return self


def test_connect_failure_is_domain_error_without_token_or_body(monkeypatch):
    monkeypatch.setattr(
        adapter_module,
        "_require_yandex_music",
        lambda: (SimpleNamespace(Client=_ClientThatFailsInit), ym_exceptions),
    )
    adapter = YandexMusicAdapter(token=SECRET_TOKEN, retries=1, sleep=lambda _s: None)

    with pytest.raises(NetworkError) as exc:
        _ = adapter.client

    message = str(exc.value)
    assert SECRET_TOKEN not in message
    assert SECRET_BODY_MARKER not in message


def test_connect_empty_result_is_api_error(monkeypatch):
    monkeypatch.setattr(
        adapter_module,
        "_require_yandex_music",
        lambda: (SimpleNamespace(Client=_ClientWithoutResult), ym_exceptions),
    )
    adapter = YandexMusicAdapter(token=SECRET_TOKEN, retries=1, sleep=lambda _s: None)

    with pytest.raises(ApiError):
        _ = adapter.client


def test_connect_success_is_cached(monkeypatch):
    monkeypatch.setattr(
        adapter_module,
        "_require_yandex_music",
        lambda: (SimpleNamespace(Client=_ClientThatWorks), ym_exceptions),
    )
    adapter = YandexMusicAdapter(token=SECRET_TOKEN, retries=1, sleep=lambda _s: None)

    first = adapter.client
    second = adapter.client

    assert first is second
    assert first.ready is True


# --- деградация: локальная музыка при мёртвом Яндексе --------------------
def make_local(tmp_path) -> tuple[LocalMusicAdapter, Track, Path]:
    music = tmp_path / "Music"
    music.mkdir()
    audio = music / "Anonymized Artist - Anonymized Song.mp3"
    audio.write_bytes(b"audio")
    adapter = LocalMusicAdapter(music)
    return adapter, adapter.tracks()[0], audio


def broken_yandex() -> Any:
    raise ConfigError("yandex-music недоступна: имитация полного отказа")


def test_local_playback_works_with_dead_yandex_adapter(tmp_path):
    local, track, audio = make_local(tmp_path)
    router = ProviderRouter(
        {"yandex": broken_yandex, "local": lambda: local}, default="yandex"
    )
    player = PlayerService(
        adapter=local,
        backend=NullBackend(),
        settings=Settings(volume=50),
        router=router,
        prefetch=False,
    )
    try:
        played = player.play_track(track)
        assert played is not None
        assert player.state.status is PlayerStatus.PLAYING
        assert player.backend.loaded_url == str(audio.resolve())

        # Яндекс полностью недоступен: «волна» падает доменной ошибкой,
        # а не трассировкой, и сервис остаётся работоспособным.
        with pytest.raises(NeuroWaveError):
            player.start_wave()
        assert player.play_track(track) is not None
        assert player.state.status is PlayerStatus.PLAYING
    finally:
        player.close()


def test_router_reports_broken_provider_as_domain_error():
    router = ProviderRouter({"yandex": broken_yandex, "local": lambda: None}, default="local")

    with pytest.raises(ConfigError):
        router.stream_url(Track(id="1", title="t", provider="yandex"))


def test_doctor_flags_outdated_yandex_library(tmp_path, capsys, monkeypatch):
    import yandex_music

    monkeypatch.setattr(yandex_music, "__version__", "2.4.0")
    code = main(["--home", str(tmp_path), "--provider", "yandex", "--json", "doctor"])
    checks = {item["check"]: item for item in json.loads(capsys.readouterr().out)}

    assert checks["yandex-music"]["ok"] is False
    assert "pip install -U" in checks["yandex-music"]["detail"]
    assert code == 1


def test_doctor_accepts_supported_yandex_library(tmp_path, capsys):
    main(["--home", str(tmp_path), "--provider", "yandex", "--json", "doctor"])
    checks = {item["check"]: item for item in json.loads(capsys.readouterr().out)}

    assert checks["yandex-music"]["ok"] is True


def test_local_cli_status_works_when_yandex_library_is_unavailable(tmp_path, capsys, monkeypatch):
    """``--provider local`` не требует ни Яндекса, ни его библиотеки, ни сети."""

    def unavailable():
        raise ConfigError("yandex-music не установлена")

    monkeypatch.setattr(adapter_module, "_require_yandex_music", unavailable)

    code = main(["--home", str(tmp_path), "--provider", "local", "--json", "status"])
    data = json.loads(capsys.readouterr().out)

    assert code == 0
    assert data["provider"]["id"] == "local"
    assert data["token"]["required"] is False
