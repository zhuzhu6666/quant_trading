"""Live state store: the ``_live_state`` container, its lock, and accessors.

Ownership: this module owns the live state dict and its accessors.  The single
production writer is the live process — writes go through
:func:`live_state_set` / :func:`live_state_update` here; API/WS layers read
through :func:`live_state_get` / :func:`live_state_snapshot` instead of
reaching into live_service privates.

Update hooks: the WS change broadcast and the pending-close bookkeeping live
outside this module (live_service / live_close_settlement).  They register
themselves via :func:`set_update_hooks` at process startup; the store applies
``session_pending_close_add/remove`` kwargs through the pre-update hook so the
kwargs contract stays in one place.
"""
from __future__ import annotations

import threading
from typing import Any, Callable

from backend.services.live_runtime_state import (
    default_live_state,
    safe_container_snapshot,
    state_get,
    state_set,
    state_update,
)

_live_state: dict = default_live_state()

_LIVE_STATE_LOCK = threading.Lock()

_pre_update_hook: Callable[[dict], dict] | None = None
_post_update_hook: Callable[[], None] | None = None


def set_update_hooks(
    *,
    pre_update: Callable[[dict], dict] | None,
    post_update: Callable[[], None] | None,
) -> None:
    """Register the writer-side hooks (live process only, once at startup)."""
    global _pre_update_hook, _post_update_hook
    _pre_update_hook = pre_update
    _post_update_hook = post_update


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


def live_state_set(key: str, value) -> None:
    state_set(_live_state, _LIVE_STATE_LOCK, key, value)
    if _post_update_hook is not None:
        _post_update_hook()


def live_state_update(**kwargs) -> None:
    kwargs = dict(kwargs)
    if _pre_update_hook is not None:
        kwargs = _pre_update_hook(kwargs)
    state_update(_live_state, _LIVE_STATE_LOCK, **kwargs)
    if _post_update_hook is not None:
        _post_update_hook()
