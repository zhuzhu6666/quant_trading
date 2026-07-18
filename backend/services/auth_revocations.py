"""Durable local projection of Auth v2 logout revocations.

PostgreSQL ``auth_session`` remains the cross-process authority for normal
access and refresh.  Risk-reducing endpoints must remain callable during a
PostgreSQL outage, so logout also appends the revoked session/family keys to a
small fsync'd local ledger.  This projection survives a backend restart and is
consulted without making stop/emergency depend on PostgreSQL.
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterable


class AuthRevocationStoreError(RuntimeError):
    """The durable logout projection could not be read or written."""


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_LOCK = threading.RLock()


def _ledger_path() -> Path:
    configured = (os.environ.get("QUANT_AUTH_REVOCATION_STATE_PATH") or "").strip()
    if configured:
        path = Path(configured).expanduser()
        return path if path.is_absolute() else _PROJECT_ROOT / path
    return _PROJECT_ROOT / "data" / "safety" / "auth_session_revocations.jsonl"


def _keys(*, session_ids: Iterable[str], family_ids: Iterable[str]) -> set[str]:
    values = {
        f"sid:{str(value).strip()}"
        for value in session_ids
        if str(value or "").strip()
    }
    values.update(
        f"fid:{str(value).strip()}"
        for value in family_ids
        if str(value or "").strip()
    )
    return values


def append_auth_revocations(
    *,
    session_ids: Iterable[str] = (),
    family_ids: Iterable[str] = (),
    expires_at: float,
    revoked_at: float | None = None,
    reason: str = "logout",
) -> dict[str, Any]:
    """Append one durable revocation fact and fsync it before returning."""

    keys = sorted(_keys(session_ids=session_ids, family_ids=family_ids))
    if not keys:
        return {"ok": True, "written": False, "keys": []}
    effective_revoked_at = float(time.time() if revoked_at is None else revoked_at)
    effective_expires_at = max(effective_revoked_at, float(expires_at or 0.0))
    payload = {
        "schema_version": "auth_revocation.v1",
        "keys": keys,
        "revoked_at": effective_revoked_at,
        "expires_at": effective_expires_at,
        "reason": str(reason or "logout")[:200],
    }
    line = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    path = _ledger_path()
    try:
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                path,
                os.O_APPEND | os.O_CREAT | os.O_WRONLY,
                0o600,
            )
            with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        raise AuthRevocationStoreError(
            f"cannot persist Auth logout revocation to {path}: {exc}"
        ) from exc
    return {"ok": True, "written": True, "keys": keys, "path": str(path)}


def revoked_auth_keys(*, now: float | None = None) -> set[str]:
    """Read all unexpired revocation keys from the append-only ledger."""

    checked_at = float(time.time() if now is None else now)
    path = _ledger_path()
    if not path.exists():
        return set()
    active: set[str] = set()
    try:
        with _LOCK:
            with path.open("r", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise AuthRevocationStoreError(
                            f"invalid Auth revocation ledger JSON at line {line_number}"
                        ) from exc
                    if not isinstance(payload, dict):
                        raise AuthRevocationStoreError(
                            f"invalid Auth revocation ledger record at line {line_number}"
                        )
                    if str(payload.get("schema_version") or "") != "auth_revocation.v1":
                        raise AuthRevocationStoreError(
                            f"unsupported Auth revocation ledger record at line {line_number}"
                        )
                    if float(payload.get("expires_at") or 0.0) <= checked_at:
                        continue
                    raw_keys = payload.get("keys")
                    if not isinstance(raw_keys, list):
                        raise AuthRevocationStoreError(
                            f"invalid Auth revocation keys at line {line_number}"
                        )
                    active.update(str(value) for value in raw_keys if str(value or ""))
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except AuthRevocationStoreError:
        raise
    except OSError as exc:
        raise AuthRevocationStoreError(
            f"cannot read Auth logout revocations from {path}: {exc}"
        ) from exc
    return active


def auth_authority_is_revoked(
    *,
    session_id: str = "",
    family_id: str = "",
    now: float | None = None,
) -> bool:
    candidates = _keys(session_ids=(session_id,), family_ids=(family_id,))
    if not candidates:
        return False
    return bool(candidates & revoked_auth_keys(now=now))


def clear_auth_revocations_for_tests() -> None:
    """Remove only an explicitly test-scoped ledger."""

    configured = (os.environ.get("QUANT_AUTH_REVOCATION_STATE_PATH") or "").strip()
    if not configured:
        raise AuthRevocationStoreError(
            "QUANT_AUTH_REVOCATION_STATE_PATH is required before clearing test revocations"
        )
    path = _ledger_path()
    try:
        with _LOCK:
            if path.exists():
                path.unlink()
    except OSError as exc:
        raise AuthRevocationStoreError(
            f"cannot clear Auth revocation test ledger {path}: {exc}"
        ) from exc


__all__ = [
    "AuthRevocationStoreError",
    "append_auth_revocations",
    "auth_authority_is_revoked",
    "clear_auth_revocations_for_tests",
    "revoked_auth_keys",
]
