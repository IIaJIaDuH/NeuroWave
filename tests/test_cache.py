from __future__ import annotations

import pytest

from neurowave.errors import CacheError
from neurowave.storage.cache import MediaCache


class Clock:
    def __init__(self, now: float = 1_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_put_and_get(tmp_path):
    cache = MediaCache(tmp_path / "c", quota_bytes=100, ttl_seconds=3600)
    path = cache.put_bytes("key-1", b"0123456789")
    assert path.exists()
    assert cache.get("key-1") == path
    assert cache.has("key-1")
    assert cache.stats().count == 1
    assert cache.stats().bytes_used == 10


def test_ttl_expiry_removes_file(tmp_path):
    clock = Clock()
    cache = MediaCache(tmp_path / "c", quota_bytes=100, ttl_seconds=10, clock=clock)
    path = cache.put_bytes("key", b"data")
    clock.now += 11
    assert cache.get("key") is None
    assert not path.exists()
    assert cache.stats().count == 0


def test_quota_evicts_least_recently_used(tmp_path):
    cache = MediaCache(tmp_path / "c", quota_bytes=20, ttl_seconds=0)
    cache.put_bytes("a", b"a" * 10)
    cache.put_bytes("b", b"b" * 10)
    cache.get("a")  # a свежее b
    cache.put_bytes("c", b"c" * 10)
    assert cache.has("a")
    assert not cache.has("b")
    assert cache.has("c")
    assert cache.stats().bytes_used == 20


def test_oversized_object_rejected(tmp_path):
    cache = MediaCache(tmp_path / "c", quota_bytes=5)
    with pytest.raises(CacheError):
        cache.put_bytes("big", b"x" * 6)


def test_empty_data_rejected(tmp_path):
    cache = MediaCache(tmp_path / "c")
    with pytest.raises(CacheError):
        cache.put_bytes("empty", b"")


def test_index_survives_restart(tmp_path):
    directory = tmp_path / "c"
    cache = MediaCache(directory, quota_bytes=100)
    cache.put_bytes("key", b"payload")
    reopened = MediaCache(directory, quota_bytes=100)
    assert reopened.has("key")
    assert reopened.get("key") is not None


def test_clear_removes_everything(tmp_path):
    cache = MediaCache(tmp_path / "c", quota_bytes=100)
    cache.put_bytes("key", b"payload")
    cache.clear()
    assert cache.stats().count == 0
    assert cache.get("key") is None


def test_store_file(tmp_path):
    source = tmp_path / "input.bin"
    source.write_bytes(b"abc")
    cache = MediaCache(tmp_path / "c", quota_bytes=100)
    path = cache.store_file("from-file", source)
    assert path.read_bytes() == b"abc"
