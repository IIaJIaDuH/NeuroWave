"""SQLite для истории, лайков, снимка очереди и настроек.

Схема версионируется через ``PRAGMA user_version``; миграции выполняются в
транзакции, чтобы не оставить наполовину обновлённую базу.

С версии 2 история и лайки хранят **квалифицированный** ключ
``provider:track_id``: без него «123» из разных сервисов — один и тот же трек.
Старые строки версии 1 помечаются провайдером ``yandex`` (других тогда не было).
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Mapping

from ..domain import Track
from ..errors import NeuroWaveError

SCHEMA_VERSION = 2

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS history (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id  TEXT NOT NULL,
    title     TEXT NOT NULL,
    artists   TEXT NOT NULL,
    played_at REAL NOT NULL,
    finished  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_history_played_at ON history(played_at);
CREATE TABLE IF NOT EXISTS likes (
    track_id   TEXT PRIMARY KEY,
    title      TEXT,
    artists    TEXT,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS queue_state (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    payload    TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""

# v2: квалифицированные ключи. Таблица likes перестраивается, потому что её
# первичный ключ был сырым track_id.
_SCHEMA_V2 = """
ALTER TABLE history ADD COLUMN provider TEXT;
ALTER TABLE history ADD COLUMN track_key TEXT;
UPDATE history
   SET provider = COALESCE(provider, 'yandex'),
       track_key = COALESCE(track_key, 'yandex:' || track_id);
CREATE INDEX IF NOT EXISTS idx_history_track_key ON history(track_key);

CREATE TABLE likes_v2 (
    track_key  TEXT PRIMARY KEY,
    provider   TEXT,
    track_id   TEXT NOT NULL,
    title      TEXT,
    artists    TEXT,
    updated_at REAL NOT NULL
);
INSERT OR IGNORE INTO likes_v2(track_key, provider, track_id, title, artists, updated_at)
    SELECT 'yandex:' || track_id, 'yandex', track_id, title, artists, updated_at FROM likes;
DROP TABLE likes;
ALTER TABLE likes_v2 RENAME TO likes;
"""


class Database:
    def __init__(self, path: Path | str):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._migrate()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def schema_version(self) -> int:
        return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    def _migrate(self) -> None:
        version = self.schema_version
        if version > SCHEMA_VERSION:
            raise NeuroWaveError(
                f"База создана более новой версией приложения (schema {version})"
            )
        if version < 1:
            with self._conn:
                self._conn.executescript(_SCHEMA_V1)
                self._conn.execute("PRAGMA user_version = 1")
            version = 1
        if version < 2:
            with self._conn:
                self._conn.executescript(_SCHEMA_V2)
                self._conn.execute("PRAGMA user_version = 2")

    # --- настройки ----------------------------------------------------
    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self._conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return default

    def set_setting(self, key: str, value: Any) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    # --- история ------------------------------------------------------
    def record_play(self, track: Track, played_at: float | None = None) -> int:
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO history(track_id, provider, track_key, title, artists, played_at) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (
                    track.id,
                    track.provider,
                    track.key,
                    track.title,
                    ", ".join(track.artists),
                    float(played_at if played_at is not None else time.time()),
                ),
            )
        return int(cursor.lastrowid)

    def mark_finished(self, history_id: int, finished: bool = True) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE history SET finished = ? WHERE id = ?",
                (1 if finished else 0, int(history_id)),
            )

    def recent_tracks(self, limit: int = 20) -> list[Track]:
        rows = self._conn.execute(
            "SELECT track_id, provider, title, artists FROM history "
            "GROUP BY track_key ORDER BY MAX(played_at) DESC LIMIT ?",
            (max(0, int(limit)),),
        ).fetchall()
        return [
            Track(
                id=str(row["track_id"]),
                title=row["title"] or "Без названия",
                artists=tuple(filter(None, (row["artists"] or "").split(", "))),
                provider=row["provider"],
            )
            for row in rows
        ]

    # --- лайки --------------------------------------------------------
    def like(self, track: Track, updated_at: float | None = None) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO likes(track_key, provider, track_id, title, artists, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(track_key) DO UPDATE SET title=excluded.title, "
                "artists=excluded.artists, updated_at=excluded.updated_at",
                (
                    track.key,
                    track.provider,
                    track.id,
                    track.title,
                    ", ".join(track.artists),
                    float(updated_at if updated_at is not None else time.time()),
                ),
            )

    def unlike(self, track_key: str) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM likes WHERE track_key = ?", (track_key,))

    def liked_ids(self) -> set[str]:
        rows = self._conn.execute("SELECT track_key FROM likes").fetchall()
        return {str(row["track_key"]) for row in rows}

    def is_liked(self, track_key: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM likes WHERE track_key = ?", (track_key,)
        ).fetchone()
        return row is not None

    # --- очередь ------------------------------------------------------
    def save_queue(self, snapshot: Mapping[str, Any]) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO queue_state(id, payload, updated_at) VALUES(1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, "
                "updated_at=excluded.updated_at",
                (json.dumps(snapshot, ensure_ascii=False), time.time()),
            )

    def load_queue(self) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT payload FROM queue_state WHERE id = 1"
        ).fetchone()
        if row is None:
            return None
        try:
            data = json.loads(row["payload"])
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def close(self) -> None:
        self._conn.close()
