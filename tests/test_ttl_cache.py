from __future__ import annotations

import time

from backend.core.ttl_cache import TTLCache


def test_ttl_cache_hit_within_ttl_and_deepcopy_isolation():
    cache = TTLCache(maxsize=4, ttl_seconds=60.0)
    payload = {"items": [{"a": 1}]}
    cache.put("k", payload)
    # Mutating the source after put must not leak into the cache.
    payload["items"][0]["a"] = 99
    got = cache.get("k")
    assert got == {"items": [{"a": 1}]}
    # Mutating the returned copy must not leak into later reads.
    got["items"][0]["a"] = 42
    assert cache.get("k") == {"items": [{"a": 1}]}


def test_ttl_cache_expires_after_ttl(monkeypatch):
    cache = TTLCache(maxsize=4, ttl_seconds=60.0)
    cache.put("k", {"v": 1})
    assert cache.get("k") == {"v": 1}
    real_monotonic = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: real_monotonic() + 61.0)
    assert cache.get("k") is None


def test_ttl_cache_lru_eviction():
    cache = TTLCache(maxsize=2, ttl_seconds=60.0)
    cache.put("a", {"v": "a"})
    cache.put("b", {"v": "b"})
    cache.get("a")  # refresh a
    cache.put("c", {"v": "c"})  # evicts b
    assert cache.get("b") is None
    assert cache.get("a") is not None
    assert cache.get("c") is not None
