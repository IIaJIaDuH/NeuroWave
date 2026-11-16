"""Регрессии, найденные живыми прогонами: потоки, очередь, снимок качества."""

from __future__ import annotations

import threading

from neurowave.audio.backend import NullBackend
from neurowave.domain import Quality, RepeatMode, StreamInfo, Track
from neurowave.player import PlayerService
from neurowave.queue import Queue
from neurowave.storage.db import Database


def test_database_accepts_writes_from_another_thread(tmp_path) -> None:
    """Автопереход пишет историю из потока событий, а не из главного."""
    db = Database(tmp_path / "state.db")
    track = Track(id="1", title="Трек", provider="local")
    outcome: dict[str, object] = {}

    def worker() -> None:
        try:
            outcome["id"] = db.record_play(track)
        except Exception as exc:  # noqa: BLE001 — проверяем именно отсутствие сбоя
            outcome["error"] = exc

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    try:
        assert "error" not in outcome, outcome.get("error")
        assert db.recent_tracks(limit=5)[0].title == "Трек"
    finally:
        db.close()


def test_passed_empty_queue_is_not_replaced() -> None:
    """``queue or Queue(...)`` терял очередь из-за __len__ вместе с repeat."""
    queue = Queue(repeat=RepeatMode.ALL, shuffle=True)
    player = PlayerService(adapter=object(), backend=NullBackend(), queue=queue)

    assert player.queue is queue
    assert player.queue.repeat is RepeatMode.ALL
    assert player.queue.shuffle is True


def test_track_event_reports_its_own_quality() -> None:
    """Снимок качества: диспетчер может уйти на следующий трек до отправки."""

    class Adapter:
        def stream_url(self, track_id: str) -> StreamInfo:
            return StreamInfo(url="fake://a", quality=Quality(codec="flac", lossless=True))

    events: list[tuple[str, dict]] = []
    backend = NullBackend()
    player = PlayerService(
        adapter=Adapter(),
        backend=backend,
        listener=lambda event, payload: events.append((event, payload)),
    )

    original_load = backend.load

    def load(url: str, start_ms: int = 0) -> None:
        # Эмулируем автопереход, случившийся во время загрузки текущего трека.
        player._current_quality = Quality(codec="mp3", bitrate_kbps=191)
        original_load(url, start_ms)

    backend.load = load  # type: ignore[method-assign]
    player.play_track(Track(id="a", title="A", provider="local"))

    track_event = next(payload for event, payload in events if event == "track")
    assert track_event["quality"].codec == "flac"
