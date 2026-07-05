import pytest

from backend.services.stability import TimedCache, measure, timing_snapshot


def test_timed_cache_returns_last_good_on_compute_error():
    cache = TimedCache()
    assert cache.get_or_compute("k", lambda: {"value": 1}, ttl_sec=30) == {"value": 1}

    cache.invalidate("k")

    def _boom():
        raise RuntimeError("temporary failure")

    payload = cache.get_or_compute("k", _boom, ttl_sec=30, stale_on_error=True)

    assert payload["value"] == 1
    assert payload["stale"] is True
    assert payload["stale_reason"] == "compute_error"
    assert payload["last_good_age_sec"] >= 0


def test_measure_records_timing_success_and_error():
    with measure("tests.stability.success"):
        pass

    with pytest.raises(ValueError):
        with measure("tests.stability.error"):
            raise ValueError("bad")

    timings = timing_snapshot("tests.stability.")

    assert timings["tests.stability.success"]["count"] >= 1
    assert timings["tests.stability.success"]["last_ok"] is True
    assert timings["tests.stability.error"]["error_count"] >= 1
    assert timings["tests.stability.error"]["last_ok"] is False
