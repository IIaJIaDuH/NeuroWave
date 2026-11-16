from __future__ import annotations

from types import SimpleNamespace

import pytest
from yandex_music.exceptions import NetworkError as YMNetworkError
from yandex_music.exceptions import UnauthorizedError as YMUnauthorizedError

from neurowave.errors import AuthError, NetworkError, TrackUnavailable
from neurowave.yandex import YandexMusicAdapter


def fake_track(track_id: int = 5) -> SimpleNamespace:
    return SimpleNamespace(
        id=track_id,
        title="Song",
        artists=[SimpleNamespace(name="Artist")],
        albums=[SimpleNamespace(title="Album")],
        duration_ms=123_000,
        available=True,
    )


class FakeDownload:
    def __init__(self, codec: str, bitrate: int, link: str | None):
        self.codec = codec
        self.bitrate_in_kbps = bitrate
        self.direct_link = link
        self.preview = False


class FakeClient:
    def __init__(self):
        self.me = SimpleNamespace(
            uid=7, login="user", display_name="User", service_available=True
        )
        self.feedback_calls: list[tuple[tuple, dict]] = []
        self.network_failures = 0
        self.track = fake_track()

    def account_status(self):
        return SimpleNamespace(plus=True)

    def tracks_download_info(self, track_id, get_direct_links=False):
        if self.network_failures:
            self.network_failures -= 1
            raise YMNetworkError("boom")
        return [
            FakeDownload("mp3", 320, "https://cdn/mp3"),
            FakeDownload("flac", 1411, "https://cdn/flac"),
        ]

    def search(self, text, type_="all"):
        return SimpleNamespace(
            tracks=SimpleNamespace(results=[self.track]),
            artists=SimpleNamespace(results=[SimpleNamespace(id=1, name="Artist")]),
            albums=SimpleNamespace(results=[SimpleNamespace(id=2, title="Album", year=2020)]),
            playlists=SimpleNamespace(
                results=[
                    SimpleNamespace(owner=SimpleNamespace(uid=3), kind=4, title="Playlist")
                ]
            ),
        )

    def rotor_station_tracks(self, station, queue=None):
        return SimpleNamespace(
            batch_id="batch-1",
            sequence=[SimpleNamespace(track=self.track, liked=True)],
        )

    def rotor_station_feedback(self, *args, **kwargs):
        self.feedback_calls.append((args, kwargs))
        return True

    def users_likes_tracks(self):
        return SimpleNamespace(fetch_tracks=lambda: [fake_track(9)])

    def users_likes_tracks_add(self, track_ids):
        return True

    def users_likes_tracks_remove(self, track_ids):
        return True

    def users_playlists_list(self):
        return [SimpleNamespace(kind=1, title="My list", track_count=3)]

    def users_playlists(self, kind):
        return SimpleNamespace(tracks=[SimpleNamespace(track_id=5)])

    def tracks(self, ids):
        return [fake_track(5)]


@pytest.fixture
def client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def adapter(client: FakeClient) -> YandexMusicAdapter:
    return YandexMusicAdapter(client=client, retries=3, backoff=0.01, sleep=lambda _s: None)


def test_account_mapping(adapter):
    account = adapter.account()
    assert account.uid == 7
    assert account.login == "user"
    assert account.plus is True
    assert account.title == "User"


def test_search_mapping(adapter):
    results = adapter.search("song")
    assert len(results.tracks) == 1
    track = results.tracks[0]
    assert track.id == "5"
    assert track.artists == ("Artist",)
    assert track.album == "Album"
    assert results.artists[0].name == "Artist"
    assert results.playlists[0].id == "3:4"


def test_stream_prefers_best_quality(adapter):
    stream = adapter.stream_url("5")
    assert stream.url == "https://cdn/flac"
    assert stream.quality.lossless is True
    assert stream.quality.bitrate_kbps == 1411


def test_stream_without_direct_links_raises(client):
    client.tracks_download_info = lambda track_id, get_direct_links=False: []  # type: ignore
    adapter = YandexMusicAdapter(client=client)
    with pytest.raises(TrackUnavailable):
        adapter.stream_url("5")


def test_wave_mapping_and_feedback(adapter, client):
    tracks = adapter.wave_tracks()
    assert len(tracks) == 1
    assert tracks[0].liked is True
    assert adapter.wave_feedback("started", "5") is True
    args, kwargs = client.feedback_calls[-1]
    assert args[1] == "trackStarted"
    assert kwargs["batch_id"] == "batch-1"
    assert kwargs["track_id"] == "5"


def test_network_retry_then_success(client):
    client.network_failures = 2
    adapter = YandexMusicAdapter(client=client, retries=3, backoff=0.01, sleep=lambda _s: None)
    stream = adapter.stream_url("5")
    assert stream.url == "https://cdn/flac"


def test_network_failure_after_retries(client):
    client.network_failures = 10
    adapter = YandexMusicAdapter(client=client, retries=2, backoff=0.01, sleep=lambda _s: None)
    with pytest.raises(NetworkError):
        adapter.stream_url("5")


def test_unauthorized_maps_to_auth_error(client):
    def boom(*args, **kwargs):
        raise YMUnauthorizedError("401")

    client.search = boom  # type: ignore
    adapter = YandexMusicAdapter(client=client, retries=1, sleep=lambda _s: None)
    with pytest.raises(AuthError):
        adapter.search("song")


def test_liked_tracks(adapter):
    tracks = adapter.liked_tracks()
    assert tracks[0].id == "9"
    assert tracks[0].liked is True


def test_playlists_and_tracks(adapter):
    playlists = adapter.playlists()
    assert playlists[0].id == "1"
    assert playlists[0].subtitle == "3 треков"
    tracks = adapter.playlist_tracks(1)
    assert tracks[0].id == "5"


def test_missing_token_requires_login():
    adapter = YandexMusicAdapter()
    with pytest.raises(AuthError):
        _ = adapter.client
