import threading

import pytest

from backend.services.live_runtime_state import (
    cache_get_or_refresh,
    default_live_state,
    state_get,
    state_set,
    state_update,
)


def test_live_runtime_state_defaults_and_clone_reads():
    state = default_live_state()
    lock = threading.Lock()

    state_update(state, lock, positions=[{"position_id": 1}])
    cloned = state_get(state, lock, "positions", clone=True)
    cloned.append({"position_id": 2})

    assert state_get(state, lock, "broker") == "ctrader"
    assert state_get(state, lock, "positions", clone=True) == [{"position_id": 1}]

    state_set(state, lock, "loop_running", True)
    assert state_get(state, lock, "loop_running") is True


def test_live_runtime_cache_single_flight_and_stale_fallback():
    cache = {}
    lock = threading.Lock()
    calls = {"n": 0}

    def _fetch():
        calls["n"] += 1
        return {"value": calls["n"]}

    assert cache_get_or_refresh(cache, 30.0, _fetch, lock) == {"value": 1}
    assert cache_get_or_refresh(cache, 30.0, _fetch, lock) == {"value": 1}
    assert calls["n"] == 1

    def _boom():
        raise RuntimeError("temporary")

    assert cache_get_or_refresh(cache, -1.0, _boom, lock) == {"value": 1}

    with pytest.raises(RuntimeError):
        cache_get_or_refresh({}, -1.0, _boom, lock)
