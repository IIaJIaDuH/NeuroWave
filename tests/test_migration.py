"""Миграция SQLite v1 → v2: старые строки получают провайдера ``yandex``."""

from __future__ import annotations

import sqlite3

from neurowave.domain import Track
from neurowave.storage.db import _SCHEMA_V1, Database


def make_v1_database(path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA_V1)
    conn.execute("PRAGMA user_version = 1")
    conn.execute(
        "INSERT INTO history(track_id, title, artists, played_at, finished) VALUES(?, ?, ?, ?, 0)",
        ("777", "Старый трек", "Старый артист", 100.0),
    )
    conn.execute(
        "INSERT INTO likes(track_id, title, artists, updated_at) VALUES(?, ?, ?, ?)",
        ("777", "Старый трек", "Старый артист", 100.0),
    )
    conn.commit()
    conn.close()


def test_v1_database_is_migrated_to_qualified_keys(tmp_path) -> None:
    path = tmp_path / "state.db"
    make_v1_database(path)

    db = Database(path)
    try:
        assert db.schema_version == 2

        recent = db.recent_tracks(limit=5)
        assert len(recent) == 1
        assert recent[0].id == "777"
        assert recent[0].provider == "yandex"
        assert recent[0].key == "yandex:777"

        assert db.liked_ids() == {"yandex:777"}
        assert db.is_liked("yandex:777") is True
        assert db.is_liked("777") is False
    finally:
        db.close()


def test_migration_keeps_settings_and_is_idempotent(tmp_path) -> None:
    path = tmp_path / "state.db"
    make_v1_database(path)
    conn = sqlite3.connect(str(path))
    conn.execute("INSERT INTO settings(key, value) VALUES('volume', '55')")
    conn.commit()
    conn.close()

    for _ in range(2):
        db = Database(path)
        assert db.schema_version == 2
        assert db.get_setting("volume") == 55
        assert db.liked_ids() == {"yandex:777"}
        db.close()


def test_new_rows_keep_their_own_provider(tmp_path) -> None:
    db = Database(tmp_path / "state.db")
    try:
        db.record_play(Track(id="1", title="локальный", provider="local"), played_at=10.0)
        db.record_play(Track(id="1", title="яндекс", provider="yandex"), played_at=20.0)
        db.like(Track(id="1", title="локальный", provider="local"))

        recent = db.recent_tracks(limit=5)
        assert [track.key for track in recent] == ["yandex:1", "local:1"]
        assert db.liked_ids() == {"local:1"}
    finally:
        db.close()


def test_newer_schema_is_refused(tmp_path) -> None:
    path = tmp_path / "state.db"
    make_v1_database(path)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA user_version = 99")
    conn.commit()
    conn.close()

    import pytest

    from neurowave.errors import NeuroWaveError

    with pytest.raises(NeuroWaveError):
        Database(path)
