from __future__ import annotations

from neurowave.storage.db import Database
from tests.conftest import make_track


def test_settings_roundtrip(tmp_path):
    db = Database(tmp_path / "state.db")
    assert db.get_setting("missing", "fallback") == "fallback"
    db.set_setting("volume", 42)
    assert db.get_setting("volume") == 42
    db.close()


def test_history_deduplicates_and_orders(tmp_path):
    db = Database(tmp_path / "state.db")
    first = db.record_play(make_track(1), played_at=100.0)
    db.record_play(make_track(2), played_at=200.0)
    db.record_play(make_track(1), played_at=300.0)
    db.mark_finished(first)
    recent = db.recent_tracks(limit=5)
    assert [track.id for track in recent] == ["1", "2"]
    assert recent[0].title == "Song 1"
    db.close()


def test_likes(tmp_path):
    db = Database(tmp_path / "state.db")
    track = make_track(5)
    db.like(track)
    assert db.is_liked("5")
    assert db.liked_ids() == {"5"}
    db.unlike("5")
    assert not db.is_liked("5")
    db.close()


def test_queue_snapshot(tmp_path):
    db = Database(tmp_path / "state.db")
    assert db.load_queue() is None
    db.save_queue({"items": [make_track(1).to_dict()], "position": 0})
    data = db.load_queue()
    assert data is not None
    assert data["items"][0]["id"] == "1"
    db.close()


def test_schema_migration_is_idempotent(tmp_path):
    path = tmp_path / "state.db"
    db = Database(path)
    db.set_setting("k", 1)
    db.close()
    reopened = Database(path)
    assert reopened.get_setting("k") == 1
    reopened.close()
