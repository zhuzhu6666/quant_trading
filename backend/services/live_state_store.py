"""Live state store: the ``_live_state`` container, its lock, and read projections.

Ownership: this module owns the live state dict and its read accessors.  The
single production writer remains the live process — write paths
(``_live_state_set`` / ``_live_state_update`` with their WS-notify and
pending-close hooks) stay in ``backend.services.live_service``.  API/WS layers
read through this module instead of reaching into live_service privates.
"""
from __future__ import annotations

import threading

from backend.services.live_runtime_state import (
    default_live_state,
    safe_container_snapshot,
    state_get,
)

_live_state: dict = default_live_state()

_LIVE_STATE_LOCK = threading.Lock()


def live_state_get(key: str, default=None, *, clone: bool = False):
    return state_get(_live_state, _LIVE_STATE_LOCK, key, default, clone=clone)


def live_state_snapshot() -> dict:
    """Return one immutable projection for API/WS serialization.

    Related fields are published in one locked state update. API readers must
    copy that projection once; reading ``_live_state`` field by field can
    otherwise combine the previous value with the next update and manufacture
    a mixed freshness envelope.
    """
    with _LIVE_STATE_LOCK:
        return safe_container_snapshot(_live_state)
