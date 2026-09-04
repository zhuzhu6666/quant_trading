"""Tiny thread-safe TTL cache for read-heavy context builders.

Stored values are deep-copied on both put and get so cached payloads are
never shared mutably between callers; a cache hit only saves the
recomputation (SQL scans, gzip/JSON decode), never correctness.
"""
from __future__ import annotations

import copy
import threading
import time
from collections import OrderedDict
from typing import Any


class TTLCache:
    def __init__(self, *, maxsize: int = 32, ttl_seconds: float = 60.0):
        self._maxsize = max(1, int(maxsize))
        self._ttl = max(0.0, float(ttl_seconds))
        self._lock = threading.Lock()
        self._data: "OrderedDict[tuple, tuple[float, Any]]" = OrderedDict()

    def get(self, key: tuple) -> Any:
        now = time.monotonic()
        with self._lock:
            item = self._data.pop(key, None)
            if item is None:
                return None
            expires_at, value = item
            if now >= expires_at:
                return None
            self._data[key] = item
            return copy.deepcopy(value)

    def put(self, key: tuple, value: Any) -> None:
        with self._lock:
            self._data[key] = (time.monotonic() + self._ttl, copy.deepcopy(value))
            self._data.move_to_end(key)
            while len(self._data) > self._maxsize:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
