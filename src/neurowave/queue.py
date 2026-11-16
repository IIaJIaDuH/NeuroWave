"""Очередь воспроизведения.

Очередь не знает ни про API, ни про аудиодвижок: только треки, позиция и
правила выбора следующего. Это позволяет покрыть её быстрыми unit-тестами.
"""

from __future__ import annotations

import random
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .domain import RepeatMode, Track
from .errors import PlayerError

HISTORY_LIMIT = 50
SHUFFLE_AVOID_WINDOW = 5


class Queue:
    def __init__(
        self,
        repeat: RepeatMode | str = RepeatMode.OFF,
        shuffle: bool = False,
        rng: random.Random | None = None,
    ):
        self._items: list[Track] = []
        self._position = -1
        self._history: list[str] = []
        self._repeat = RepeatMode.parse(repeat)
        self._shuffle = bool(shuffle)
        self._rng = rng or random.Random()

    # --- доступ -------------------------------------------------------
    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[Track]:
        return iter(self._items)

    @property
    def items(self) -> tuple[Track, ...]:
        return tuple(self._items)

    @property
    def position(self) -> int:
        return self._position

    @property
    def repeat(self) -> RepeatMode:
        return self._repeat

    @property
    def shuffle(self) -> bool:
        return self._shuffle

    @property
    def current(self) -> Track | None:
        if 0 <= self._position < len(self._items):
            return self._items[self._position]
        return None

    @property
    def upcoming(self) -> tuple[Track, ...]:
        if self._position < 0:
            return tuple(self._items)
        return tuple(self._items[self._position + 1 :])

    # --- изменение ----------------------------------------------------
    def clear(self) -> None:
        self._items = []
        self._position = -1
        self._history = []

    def extend(self, tracks: Iterable[Track]) -> None:
        for track in tracks:
            self._items.append(track)

    def add(self, track: Track) -> int:
        self._items.append(track)
        if self._position < 0:
            self._position = 0
        return len(self._items) - 1

    def insert_next(self, track: Track) -> int:
        index = self._position + 1
        self._items.insert(index, track)
        return index

    def remove(self, track_id: str) -> bool:
        for index, track in enumerate(self._items):
            if track.id == track_id:
                del self._items[index]
                if not self._items:
                    self._position = -1
                elif index < self._position:
                    self._position -= 1
                elif index == self._position:
                    self._position = min(index, len(self._items) - 1)
                return True
        return False

    def move(self, from_position: int, to_position: int) -> None:
        self._check_position(from_position)
        self._check_position(to_position)
        if from_position == to_position:
            return
        current_id = self.current.id if self.current else None
        track = self._items.pop(from_position)
        self._items.insert(to_position, track)
        if current_id is not None:
            self._position = next(
                (i for i, item in enumerate(self._items) if item.id == current_id),
                self._position,
            )

    def jump(self, position: int) -> Track:
        self._check_position(position)
        if self._position != position:
            self._remember(self.current)
        self._position = position
        return self._items[position]

    # --- навигация ----------------------------------------------------
    def next(self, automatic: bool = False) -> Track | None:
        """Следующий трек или ``None``, если очередь закончилась."""
        if not self._items:
            return None

        if automatic and self._repeat is RepeatMode.ONE:
            return self.current

        if self._shuffle and len(self._items) > 1:
            candidate: int | None = self._shuffle_candidate()
        else:
            candidate = self._position + 1
            if candidate >= len(self._items):
                if self._repeat is RepeatMode.ALL:
                    candidate = 0
                else:
                    candidate = None

        if candidate is None:
            return None

        self._remember(self.current)
        self._position = candidate
        return self.current

    def preview_next(self, automatic: bool = True) -> Track | None:
        """Следующий трек без изменения состояния (для предзагрузки)."""
        if not self._items:
            return None
        if automatic and self._repeat is RepeatMode.ONE:
            return self.current
        if self._shuffle:
            return None  # предугадывать случайный выбор не пытаемся
        candidate = self._position + 1
        if candidate >= len(self._items):
            return self._items[0] if self._repeat is RepeatMode.ALL else None
        return self._items[candidate]

    def previous(self) -> Track | None:
        if not self._items:
            return None
        if self._history:
            track_id = self._history.pop()
            for index, track in enumerate(self._items):
                if track.id == track_id:
                    self._position = index
                    return self.current
        if self._position > 0:
            self._position -= 1
        return self.current

    # --- режимы -------------------------------------------------------
    def set_repeat(self, mode: RepeatMode | str) -> None:
        self._repeat = RepeatMode.parse(mode)

    def set_shuffle(self, enabled: bool, rng: random.Random | None = None) -> None:
        self._shuffle = bool(enabled)
        if rng is not None:
            self._rng = rng

    # --- снимок состояния --------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        return {
            "items": [track.to_dict() for track in self._items],
            "position": self._position,
            "repeat": self._repeat.value,
            "shuffle": self._shuffle,
            "history": list(self._history),
        }

    @classmethod
    def restore(cls, data: Mapping[str, Any]) -> "Queue":
        queue = cls(
            repeat=RepeatMode.parse(str(data.get("repeat", "off"))),
            shuffle=bool(data.get("shuffle", False)),
        )
        for item in data.get("items") or []:
            queue._items.append(Track.from_dict(item))
        position = int(data.get("position", -1))
        queue._position = position if 0 <= position < len(queue._items) else (
            0 if queue._items else -1
        )
        history = [str(item) for item in data.get("history") or []]
        queue._history = history[-HISTORY_LIMIT:]
        return queue

    # --- внутреннее ---------------------------------------------------
    def _check_position(self, position: int) -> None:
        if not 0 <= position < len(self._items):
            raise PlayerError(f"Некорректная позиция в очереди: {position}")

    def _remember(self, track: Track | None) -> None:
        if track is None:
            return
        self._history.append(track.id)
        if len(self._history) > HISTORY_LIMIT:
            del self._history[: len(self._history) - HISTORY_LIMIT]

    def _shuffle_candidate(self) -> int | None:
        recent = self._history[-SHUFFLE_AVOID_WINDOW:]
        pool = [
            index
            for index, track in enumerate(self._items)
            if index != self._position and track.id not in recent
        ]
        if not pool:
            pool = [i for i in range(len(self._items)) if i != self._position]
        if not pool:
            return None
        return self._rng.choice(pool)
