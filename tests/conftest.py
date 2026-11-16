from __future__ import annotations

import pytest

from neurowave.audio import NullBackend
from neurowave.config import Settings
from neurowave.domain import Quality, SearchResults, StreamInfo, Track
from neurowave.errors import TrackUnavailable
from neurowave.player import PlayerService


def make_track(number: int, **overrides) -> Track:
    defaults = dict(
        id=str(number),
        title=f"Song {number}",
        artists=(f"Artist {number}",),
        album="Test Album",
        duration_ms=180_000,
        available=True,
    )
    defaults.update(overrides)
    return Track(**defaults)


class FakeAdapter:
    """Музыкальный сервис без сети: детерминированные треки и feedback."""

    def __init__(self, tracks: list[Track] | None = None):
        self.tracks = list(tracks or [make_track(i) for i in (1, 2, 3)])
        self.feedback: list[tuple[str, str | None]] = []
        self.stream_calls: list[str] = []
        self.fail_ids: set[str] = set()

    def wave_tracks(self) -> list[Track]:
        return list(self.tracks)

    def wave_feedback(
        self,
        kind: str,
        track_id: str | None = None,
        total_played_seconds: float | None = None,
    ) -> bool:
        self.feedback.append((kind, track_id))
        return True

    def stream_url(self, track_id: str) -> StreamInfo:
        self.stream_calls.append(track_id)
        if track_id in self.fail_ids:
            raise TrackUnavailable(f"track {track_id} unavailable")
        return StreamInfo(f"https://audio.test/{track_id}.mp3", Quality("mp3", 320))

    def search(self, text: str, limit: int = 20, kind: str = "all") -> SearchResults:
        return SearchResults(tracks=tuple(self.tracks[:limit]))

    def liked_tracks(self, limit: int = 100) -> list[Track]:
        return list(self.tracks[:limit])

    def like(self, track_id: str) -> bool:
        return True

    def unlike(self, track_id: str) -> bool:
        return True

    def account(self):
        from neurowave.domain import AccountInfo

        return AccountInfo(uid=1, login="tester", display_name="Tester", plus=True)


@pytest.fixture(autouse=True)
def _isolate_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Тесты не должны писать в настоящий keyring машины.

    Иначе токены прогонов оседают в Secret Service пользователя, а результат
    зависит от того, есть ли на машине keyring и что в нём осталось.
    """
    monkeypatch.setenv("NEUROWAVE_KEYRING", "off")


@pytest.fixture
def tracks() -> list[Track]:
    return [make_track(i) for i in (1, 2, 3)]


@pytest.fixture
def adapter(tracks: list[Track]) -> FakeAdapter:
    return FakeAdapter(tracks)


@pytest.fixture
def backend() -> NullBackend:
    return NullBackend()


@pytest.fixture
def player(adapter: FakeAdapter, backend: NullBackend) -> PlayerService:
    service = PlayerService(
        adapter=adapter,
        backend=backend,
        settings=Settings(volume=55),
        prefetch=False,
    )
    yield service
    service.close()
