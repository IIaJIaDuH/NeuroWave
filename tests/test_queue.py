from __future__ import annotations

import pytest

from neurowave.domain import RepeatMode
from neurowave.errors import PlayerError
from neurowave.queue import Queue
from tests.conftest import make_track


class FirstChoice:
    def choice(self, sequence):
        return sequence[0]


def make_queue(*ids: int, repeat: str = "off", shuffle: bool = False) -> Queue:
    queue = Queue(repeat=repeat, shuffle=shuffle, rng=FirstChoice())
    queue.extend(make_track(i) for i in ids)
    queue.jump(0)
    return queue


def test_initial_state():
    queue = Queue()
    assert len(queue) == 0
    assert queue.current is None
    assert queue.next() is None


def test_next_walks_to_end_then_stops():
    queue = make_queue(1, 2, 3)
    assert queue.next().id == "2"
    assert queue.next().id == "3"
    assert queue.next() is None


def test_repeat_all_wraps_to_first():
    queue = make_queue(1, 2, repeat="all")
    assert queue.next().id == "2"
    assert queue.next().id == "1"


def test_repeat_one_only_for_automatic_advance():
    queue = make_queue(1, 2, repeat="one")
    assert queue.next(automatic=True).id == "1"
    assert queue.next(automatic=False).id == "2"


def test_previous_uses_history():
    queue = make_queue(1, 2, 3)
    queue.next()
    queue.next()
    assert queue.current.id == "3"
    assert queue.previous().id == "2"
    assert queue.previous().id == "1"


def test_jump_validates_position():
    queue = make_queue(1, 2)
    with pytest.raises(PlayerError):
        queue.jump(5)
    with pytest.raises(PlayerError):
        queue.jump(-1)


def test_remove_current_keeps_next_in_place():
    queue = make_queue(1, 2, 3)
    assert queue.remove("1") is True
    assert queue.current.id == "2"
    assert queue.position == 0
    assert queue.remove("missing") is False


def test_remove_before_current_adjusts_position():
    queue = make_queue(1, 2, 3)
    queue.next()
    queue.remove("1")
    assert queue.current.id == "2"
    assert queue.position == 0


def test_insert_next_and_move():
    queue = make_queue(1, 3)
    queue.insert_next(make_track(2))
    assert [t.id for t in queue.items] == ["1", "2", "3"]
    queue.next()
    assert queue.current.id == "2"
    queue.move(2, 0)
    assert [t.id for t in queue.items] == ["3", "1", "2"]
    assert queue.current.id == "2"


def test_shuffle_does_not_repeat_immediately():
    queue = Queue(repeat="all", shuffle=True)
    queue.extend(make_track(i) for i in range(1, 7))
    queue.jump(0)
    previous = queue.current.id
    for _ in range(30):
        track = queue.next()
        assert track is not None
        assert track.id != previous
        previous = track.id


def test_snapshot_roundtrip():
    queue = make_queue(1, 2, 3, repeat="all")
    queue.next()
    snapshot = queue.snapshot()
    restored = Queue.restore(snapshot)
    assert [t.id for t in restored.items] == ["1", "2", "3"]
    assert restored.position == 1
    assert restored.repeat is RepeatMode.ALL
    assert restored.current.id == "2"


def test_restore_handles_broken_position():
    restored = Queue.restore(
        {
            "items": [make_track(1).to_dict()],
            "position": 99,
            "repeat": "unknown",
        }
    )
    assert restored.position == 0
    assert restored.repeat is RepeatMode.OFF
