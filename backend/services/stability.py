from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from copy import deepcopy
from functools import wraps
import threading
import time
from typing import Any, Callable, Iterator, TypeVar


T = TypeVar("T")


class TimedCache:
    """Small process-local cache for heavy read endpoints.

    This intentionally has no persistence. It exists to prevent operator-page
    polling from repeatedly recomputing expensive read-only summaries.
    """

    def __init__(self) -> None:
        self._items: dict[str, tuple[float, Any]] = {}
        self._last_good: dict[str, tuple[float, Any]] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        now = time.time()
        with self._lock:
            item = self._items.get(key)
            if not item:
                return None
            expires_at, payload = item
            if expires_at <= now:
                self._items.pop(key, None)
                return None
            return deepcopy(payload)

    def set(self, key: str, payload: Any, *, ttl_sec: float) -> Any:
        ttl = max(1.0, float(ttl_sec or 0.0))
        cloned = deepcopy(payload)
        with self._lock:
            self._items[key] = (time.time() + ttl, cloned)
            self._last_good[key] = (time.time(), cloned)
        return deepcopy(cloned)

    def last_good(self, key: str) -> tuple[float, Any] | None:
        with self._lock:
            item = self._last_good.get(key)
            if not item:
                return None
            created_at, payload = item
            return created_at, deepcopy(payload)

    def invalidate(self, *prefixes: str) -> None:
        with self._lock:
            if not prefixes:
                self._items.clear()
                return
            for key in list(self._items):
                if any(key.startswith(prefix) for prefix in prefixes):
                    self._items.pop(key, None)

    def compute_lock(self, key: str) -> threading.Lock:
        with self._lock:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    def get_or_compute(
        self,
        key: str,
        compute: Callable[[], T],
        *,
        ttl_sec: float,
        stale_on_error: bool = True,
    ) -> T:
        cached = self.get(key)
        if cached is not None:
            return cached
        with self.compute_lock(key):
            cached = self.get(key)
            if cached is not None:
                return cached
            try:
                return self.set(key, compute(), ttl_sec=ttl_sec)
            except Exception:
                if stale_on_error:
                    fallback = self.last_good(key)
                    if fallback:
                        created_at, payload = fallback
                        if isinstance(payload, dict):
                            payload = {
                                **payload,
                                "stale": True,
                                "stale_reason": "compute_error",
                                "stale_at": time.time(),
                                "last_good_age_sec": round(max(0.0, time.time() - created_at), 3),
                            }
                        return payload
                raise


class TimingRegistry:
    def __init__(self, *, sample_limit: int = 200) -> None:
        self._sample_limit = max(20, int(sample_limit))
        self._items: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def record(
        self,
        name: str,
        elapsed_sec: float,
        *,
        ok: bool = True,
        error: str = "",
        extra: dict[str, Any] | None = None,
    ) -> None:
        elapsed_ms = max(0.0, float(elapsed_sec or 0.0) * 1000.0)
        now = time.time()
        with self._lock:
            item = self._items.get(name)
            if item is None:
                item = {
                    "count": 0,
                    "error_count": 0,
                    "total_ms": 0.0,
                    "max_ms": 0.0,
                    "samples_ms": deque(maxlen=self._sample_limit),
                    "last_ms": 0.0,
                    "last_ok": True,
                    "last_error": "",
                    "last_extra": {},
                    "updated_at": 0.0,
                }
                self._items[name] = item
            item["count"] += 1
            item["error_count"] += 0 if ok else 1
            item["total_ms"] += elapsed_ms
            item["max_ms"] = max(float(item["max_ms"]), elapsed_ms)
            item["samples_ms"].append(elapsed_ms)
            item["last_ms"] = elapsed_ms
            item["last_ok"] = bool(ok)
            item["last_error"] = str(error or "")[:300]
            item["last_extra"] = dict(extra or {})
            item["updated_at"] = now

    def snapshot(self, prefix: str | None = None) -> dict[str, Any]:
        with self._lock:
            result: dict[str, Any] = {}
            for name, item in self._items.items():
                if prefix and not name.startswith(prefix):
                    continue
                samples = list(item["samples_ms"])
                samples_sorted = sorted(samples)
                p95 = samples_sorted[int((len(samples_sorted) - 1) * 0.95)] if samples_sorted else 0.0
                count = int(item["count"] or 0)
                avg = float(item["total_ms"] or 0.0) / count if count else 0.0
                result[name] = {
                    "count": count,
                    "error_count": int(item["error_count"] or 0),
                    "avg_ms": round(avg, 3),
                    "p95_ms": round(float(p95), 3),
                    "max_ms": round(float(item["max_ms"] or 0.0), 3),
                    "last_ms": round(float(item["last_ms"] or 0.0), 3),
                    "last_ok": bool(item["last_ok"]),
                    "last_error": str(item["last_error"] or ""),
                    "last_extra": deepcopy(item["last_extra"]),
                    "updated_at": float(item["updated_at"] or 0.0),
                }
            return result


_TIMINGS = TimingRegistry()


def record_timing(
    name: str,
    elapsed_sec: float,
    *,
    ok: bool = True,
    error: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    _TIMINGS.record(name, elapsed_sec, ok=ok, error=error, extra=extra)


def timing_snapshot(prefix: str | None = None) -> dict[str, Any]:
    return _TIMINGS.snapshot(prefix)


@contextmanager
def measure(name: str, *, extra: dict[str, Any] | None = None) -> Iterator[None]:
    start = time.perf_counter()
    ok = True
    error = ""
    try:
        yield
    except Exception as exc:
        ok = False
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        record_timing(name, time.perf_counter() - start, ok=ok, error=error, extra=extra)


def record_timed(name: str) -> Callable[[Callable[..., T]], Callable[..., T]]:
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            with measure(name):
                return func(*args, **kwargs)

        return wrapper

    return decorator
