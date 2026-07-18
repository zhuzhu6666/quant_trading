"""Rotating refresh-session store for Auth v2.

Normal access, refresh, logout, and high-risk step-up checks use the
``auth_session`` governance table. Stop/emergency validate the signed
risk-reduction scope plus the durable local logout projection, so those
actions do not depend on PostgreSQL. Tests may explicitly select the isolated
in-memory store with ``QUANT_AUTH_SESSION_STORE=memory``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Final


REFRESH_TOKEN_TTL_SECONDS: Final[int] = 7 * 24 * 3600


class RefreshSessionError(RuntimeError):
    """A refresh session is missing, invalid, expired, or was replayed."""

    def __init__(self, code: str, message: str, *, status_code: int = 401):
        self.code = str(code)
        self.status_code = int(status_code)
        super().__init__(message)


@dataclass(frozen=True)
class RefreshGrant:
    session_id: str
    subject: str
    refresh_token: str
    issued_at: float
    expires_at: float
    auth_time: int
    family_id: str


@dataclass(frozen=True)
class StepUpGrant:
    """Persistent authority returned after a password step-up commits."""

    session_id: str
    subject: str
    auth_time: int
    family_id: str


_MEMORY_LOCK = threading.RLock()
_MEMORY_SESSIONS: dict[str, dict[str, Any]] = {}


def _memory_store_enabled() -> bool:
    return (os.environ.get("QUANT_AUTH_SESSION_STORE") or "").strip().lower() == "memory"


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _private_hash(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _new_token(session_id: str) -> str:
    return f"{session_id}.{secrets.token_urlsafe(32)}"


def _parse_session_id(token: str) -> str:
    value = str(token or "").strip()
    session_id, separator, secret = value.partition(".")
    if not separator or not secret:
        raise RefreshSessionError("invalid_refresh_token", "refresh token format is invalid")
    try:
        parsed = uuid.UUID(session_id)
    except (ValueError, TypeError) as exc:
        raise RefreshSessionError("invalid_refresh_token", "refresh token format is invalid") from exc
    return str(parsed)


def _metadata(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("metadata_json") or {}
    if isinstance(raw, dict):
        return dict(raw)
    try:
        parsed = json.loads(str(raw))
        return dict(parsed) if isinstance(parsed, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _make_row(
    *,
    session_id: str,
    subject: str,
    token: str,
    issued_at: float,
    expires_at: float,
    auth_time: int,
    family_id: str,
    parent_session_id: str,
    client_fingerprint: str,
    ip_address: str,
    user_agent: str,
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "subject": subject,
        "token_jti": str(uuid.uuid4()),
        "token_hash": _token_hash(token),
        "status": "active",
        "client_fingerprint": _private_hash(client_fingerprint),
        "ip_hash": _private_hash(ip_address),
        "user_agent": str(user_agent or "")[:500],
        "issued_at": issued_at,
        "expires_at": expires_at,
        "last_seen_at": issued_at,
        "revoked_at": 0.0,
        "revoked_by": "",
        "revoke_reason": "",
        "metadata_json": json.dumps(
            {
                "family_id": family_id,
                "parent_session_id": parent_session_id,
                "auth_time": int(auth_time),
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        "created_at": issued_at,
        "updated_at": issued_at,
    }


def _insert_postgres(conn: Any, row: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO auth_session (
            session_id, subject, token_jti, token_hash, status,
            client_fingerprint, ip_hash, user_agent, issued_at, expires_at,
            last_seen_at, revoked_at, revoked_by, revoke_reason, metadata_json,
            created_at, updated_at
        ) VALUES (
            %(session_id)s, %(subject)s, %(token_jti)s, %(token_hash)s, %(status)s,
            %(client_fingerprint)s, %(ip_hash)s, %(user_agent)s, %(issued_at)s,
            %(expires_at)s, %(last_seen_at)s, %(revoked_at)s, %(revoked_by)s,
            %(revoke_reason)s, %(metadata_json)s, %(created_at)s, %(updated_at)s
        )
        """,
        row,
    )


def create_refresh_session(
    subject: str,
    *,
    auth_time: int | None = None,
    client_fingerprint: str = "",
    ip_address: str = "",
    user_agent: str = "",
    now: float | None = None,
) -> RefreshGrant:
    issued_at = float(time.time() if now is None else now)
    session_id = str(uuid.uuid4())
    family_id = str(uuid.uuid4())
    token = _new_token(session_id)
    effective_auth_time = int(issued_at if auth_time is None else auth_time)
    expires_at = issued_at + REFRESH_TOKEN_TTL_SECONDS
    row = _make_row(
        session_id=session_id,
        subject=str(subject),
        token=token,
        issued_at=issued_at,
        expires_at=expires_at,
        auth_time=effective_auth_time,
        family_id=family_id,
        parent_session_id="",
        client_fingerprint=client_fingerprint,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    if _memory_store_enabled():
        with _MEMORY_LOCK:
            _MEMORY_SESSIONS[session_id] = row
    else:
        from backend.core.db import get_state_pg_conn

        conn = get_state_pg_conn()
        try:
            _insert_postgres(conn, row)
            conn.commit()
        finally:
            conn.close()
    return RefreshGrant(
        session_id=session_id,
        subject=str(subject),
        refresh_token=token,
        issued_at=issued_at,
        expires_at=expires_at,
        auth_time=effective_auth_time,
        family_id=family_id,
    )


def _reject_inactive(row: dict[str, Any], token: str, *, now: float) -> None:
    if not hmac.compare_digest(str(row.get("token_hash") or ""), _token_hash(token)):
        raise RefreshSessionError("invalid_refresh_token", "refresh token is invalid")
    status = str(row.get("status") or "")
    if status == "rotated":
        raise RefreshSessionError(
            "refresh_token_reuse",
            "a rotated refresh token was reused; the session family is revoked",
        )
    if status != "active":
        raise RefreshSessionError("refresh_session_inactive", "refresh session is not active")
    if float(row.get("expires_at") or 0.0) <= now:
        raise RefreshSessionError("refresh_session_expired", "refresh session has expired")


def _grant_from_rotation(row: dict[str, Any], new_row: dict[str, Any], token: str) -> RefreshGrant:
    meta = _metadata(new_row)
    return RefreshGrant(
        session_id=str(new_row["session_id"]),
        subject=str(new_row["subject"]),
        refresh_token=token,
        issued_at=float(new_row["issued_at"]),
        expires_at=float(new_row["expires_at"]),
        auth_time=int(meta.get("auth_time") or new_row["issued_at"]),
        family_id=str(meta.get("family_id") or ""),
    )


def _revoke_memory_family(family_id: str, *, now: float, reason: str) -> None:
    for candidate in _MEMORY_SESSIONS.values():
        if _metadata(candidate).get("family_id") == family_id and candidate.get("status") == "active":
            candidate.update(
                status="revoked",
                revoked_at=now,
                revoked_by="auth_v2",
                revoke_reason=reason,
                updated_at=now,
            )


def rotate_refresh_session(
    token: str,
    *,
    client_fingerprint: str = "",
    ip_address: str = "",
    user_agent: str = "",
    now: float | None = None,
) -> RefreshGrant:
    session_id = _parse_session_id(token)
    rotated_at = float(time.time() if now is None else now)

    if _memory_store_enabled():
        with _MEMORY_LOCK:
            row = _MEMORY_SESSIONS.get(session_id)
            if row is None:
                raise RefreshSessionError("invalid_refresh_token", "refresh token is invalid")
            try:
                _reject_inactive(row, token, now=rotated_at)
            except RefreshSessionError as exc:
                if exc.code == "refresh_token_reuse":
                    _revoke_memory_family(
                        str(_metadata(row).get("family_id") or ""),
                        now=rotated_at,
                        reason="refresh_token_reuse",
                    )
                raise
            meta = _metadata(row)
            new_session_id = str(uuid.uuid4())
            new_token = _new_token(new_session_id)
            new_row = _make_row(
                session_id=new_session_id,
                subject=str(row["subject"]),
                token=new_token,
                issued_at=rotated_at,
                expires_at=rotated_at + REFRESH_TOKEN_TTL_SECONDS,
                auth_time=int(meta.get("auth_time") or row["issued_at"]),
                family_id=str(meta.get("family_id") or session_id),
                parent_session_id=session_id,
                client_fingerprint=client_fingerprint,
                ip_address=ip_address,
                user_agent=user_agent,
            )
            row.update(status="rotated", last_seen_at=rotated_at, updated_at=rotated_at)
            _MEMORY_SESSIONS[new_session_id] = new_row
            return _grant_from_rotation(row, new_row, new_token)

    from backend.core.db import get_state_pg_conn

    conn = get_state_pg_conn()
    try:
        row = conn.execute(
            "SELECT * FROM auth_session WHERE session_id = %s FOR UPDATE",
            (session_id,),
        ).fetchone()
        if row is None:
            raise RefreshSessionError("invalid_refresh_token", "refresh token is invalid")
        row = dict(row)
        try:
            _reject_inactive(row, token, now=rotated_at)
        except RefreshSessionError as exc:
            if exc.code == "refresh_token_reuse":
                family_id = str(_metadata(row).get("family_id") or "")
                conn.execute(
                    """
                    UPDATE auth_session
                    SET status = 'revoked', revoked_at = %s, revoked_by = 'auth_v2',
                        revoke_reason = 'refresh_token_reuse', updated_at = %s
                    WHERE status = 'active'
                      AND metadata_json::jsonb ->> 'family_id' = %s
                    """,
                    (rotated_at, rotated_at, family_id),
                )
                conn.commit()
            raise
        meta = _metadata(row)
        new_session_id = str(uuid.uuid4())
        new_token = _new_token(new_session_id)
        new_row = _make_row(
            session_id=new_session_id,
            subject=str(row["subject"]),
            token=new_token,
            issued_at=rotated_at,
            expires_at=rotated_at + REFRESH_TOKEN_TTL_SECONDS,
            auth_time=int(meta.get("auth_time") or row["issued_at"]),
            family_id=str(meta.get("family_id") or session_id),
            parent_session_id=session_id,
            client_fingerprint=client_fingerprint,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        updated = conn.execute(
            """
            UPDATE auth_session
            SET status = 'rotated', last_seen_at = %s, updated_at = %s
            WHERE session_id = %s AND status = 'active'
            """,
            (rotated_at, rotated_at, session_id),
        )
        if updated.rowcount != 1:
            raise RefreshSessionError("refresh_session_race", "refresh session was already consumed")
        _insert_postgres(conn, new_row)
        conn.commit()
        return _grant_from_rotation(row, new_row, new_token)
    except Exception:
        if not conn.closed:
            conn.rollback()
        raise
    finally:
        conn.close()


def session_is_active(session_id: str, *, subject: str = "", now: float | None = None) -> bool:
    checked_at = float(time.time() if now is None else now)
    if not session_id:
        return False
    if _memory_store_enabled():
        with _MEMORY_LOCK:
            row = _MEMORY_SESSIONS.get(str(session_id))
            return bool(
                row
                and row.get("status") == "active"
                and float(row.get("expires_at") or 0.0) > checked_at
                and (not subject or str(row.get("subject")) == str(subject))
            )
    from backend.core.db import get_state_pg_conn

    conn = get_state_pg_conn(read_only=True)
    try:
        row = conn.execute(
            """
            SELECT 1 FROM auth_session
            WHERE session_id = %s AND status = 'active' AND expires_at > %s
              AND (%s = '' OR subject = %s)
            LIMIT 1
            """,
            (str(session_id), checked_at, str(subject), str(subject)),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _validate_step_up_row(
    row: dict[str, Any] | None,
    *,
    session_id: str,
    subject: str,
    family_id: str,
    now: float,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Validate that step-up still targets the signed active session."""

    if row is None:
        raise RefreshSessionError(
            "step_up_session_inactive",
            "Auth v2 session is not active",
            status_code=403,
        )
    effective_row = dict(row)
    if (
        str(effective_row.get("session_id") or "") != str(session_id)
        or str(effective_row.get("subject") or "") != str(subject)
        or str(effective_row.get("status") or "") != "active"
        or float(effective_row.get("expires_at") or 0.0) <= now
    ):
        raise RefreshSessionError(
            "step_up_session_inactive",
            "Auth v2 session is not active",
            status_code=403,
        )
    metadata = _metadata(effective_row)
    persisted_family_id = str(metadata.get("family_id") or session_id)
    if family_id and not hmac.compare_digest(str(family_id), persisted_family_id):
        raise RefreshSessionError(
            "step_up_session_mismatch",
            "access token does not match the persistent session family",
            status_code=403,
        )
    return effective_row, metadata, persisted_family_id


def step_up_refresh_session(
    session_id: str,
    *,
    subject: str,
    family_id: str = "",
    now: float | None = None,
) -> StepUpGrant:
    """Persist a fresh password-auth time on the current active session.

    The existing refresh token is deliberately not rotated here.  A later
    refresh copies this committed ``auth_time`` into its child session, while
    a refresh/logout racing this call wins the row lock and makes step-up fail
    closed instead of minting an access token for stale authority.
    """

    effective_session_id = str(session_id or "").strip()
    effective_subject = str(subject or "").strip()
    if not effective_session_id or not effective_subject:
        raise RefreshSessionError(
            "step_up_session_required",
            "an active Auth v2 session is required",
            status_code=403,
        )
    stepped_up_at = float(time.time() if now is None else now)
    auth_time = int(stepped_up_at)

    if _memory_store_enabled():
        with _MEMORY_LOCK:
            row, metadata, persisted_family_id = _validate_step_up_row(
                _MEMORY_SESSIONS.get(effective_session_id),
                session_id=effective_session_id,
                subject=effective_subject,
                family_id=str(family_id or ""),
                now=stepped_up_at,
            )
            metadata["auth_time"] = auth_time
            row.update(
                metadata_json=json.dumps(
                    metadata,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                last_seen_at=stepped_up_at,
                updated_at=stepped_up_at,
            )
            _MEMORY_SESSIONS[effective_session_id] = row
            return StepUpGrant(
                session_id=effective_session_id,
                subject=effective_subject,
                auth_time=auth_time,
                family_id=persisted_family_id,
            )

    from backend.core.db import get_state_pg_conn

    conn = get_state_pg_conn()
    try:
        selected = conn.execute(
            "SELECT * FROM auth_session WHERE session_id = %s FOR UPDATE",
            (effective_session_id,),
        ).fetchone()
        _row, metadata, persisted_family_id = _validate_step_up_row(
            dict(selected) if selected is not None else None,
            session_id=effective_session_id,
            subject=effective_subject,
            family_id=str(family_id or ""),
            now=stepped_up_at,
        )
        metadata["auth_time"] = auth_time
        updated = conn.execute(
            """
            UPDATE auth_session
            SET metadata_json = %s, last_seen_at = %s, updated_at = %s
            WHERE session_id = %s AND subject = %s AND status = 'active'
              AND expires_at > %s
            """,
            (
                json.dumps(metadata, sort_keys=True, separators=(",", ":")),
                stepped_up_at,
                stepped_up_at,
                effective_session_id,
                effective_subject,
                stepped_up_at,
            ),
        )
        if int(updated.rowcount or 0) != 1:
            raise RefreshSessionError(
                "step_up_session_race",
                "Auth v2 session changed while step-up was committing",
                status_code=403,
            )
        conn.commit()
        return StepUpGrant(
            session_id=effective_session_id,
            subject=effective_subject,
            auth_time=auth_time,
            family_id=persisted_family_id,
        )
    except Exception:
        if not getattr(conn, "closed", False):
            conn.rollback()
        raise
    finally:
        conn.close()


def session_family_ids(session_id: str) -> tuple[str, tuple[str, ...]]:
    """Return the durable refresh family and every known member session id."""

    effective_session_id = str(session_id or "")
    if not effective_session_id:
        return "", ()
    if _memory_store_enabled():
        with _MEMORY_LOCK:
            row = _MEMORY_SESSIONS.get(effective_session_id)
            if row is None:
                return "", ()
            family_id = str(_metadata(row).get("family_id") or effective_session_id)
            members = tuple(
                sorted(
                    str(candidate_id)
                    for candidate_id, candidate in _MEMORY_SESSIONS.items()
                    if str(_metadata(candidate).get("family_id") or candidate_id)
                    == family_id
                )
            )
            return family_id, members

    from backend.core.db import get_state_pg_conn

    conn = get_state_pg_conn(read_only=True)
    try:
        row = conn.execute(
            "SELECT metadata_json FROM auth_session WHERE session_id = %s",
            (effective_session_id,),
        ).fetchone()
        if row is None:
            return "", ()
        family_id = str(_metadata(dict(row)).get("family_id") or effective_session_id)
        rows = conn.execute(
            """
            SELECT session_id
            FROM auth_session
            WHERE session_id = %s
               OR metadata_json::jsonb ->> 'family_id' = %s
            ORDER BY created_at, session_id
            """,
            (effective_session_id, family_id),
        ).fetchall()
        return family_id, tuple(str(item["session_id"]) for item in rows)
    finally:
        conn.close()


def revoke_refresh_session(
    *,
    session_id: str = "",
    token: str = "",
    actor: str = "auth_logout",
    reason: str = "logout",
    now: float | None = None,
) -> bool:
    effective_session_id = str(session_id or "")
    if token:
        parsed = _parse_session_id(token)
        if effective_session_id and effective_session_id != parsed:
            raise RefreshSessionError("session_mismatch", "refresh token does not match access session")
        effective_session_id = parsed
    if not effective_session_id:
        return False
    revoked_at = float(time.time() if now is None else now)
    if _memory_store_enabled():
        with _MEMORY_LOCK:
            row = _MEMORY_SESSIONS.get(effective_session_id)
            if row is None:
                return False
            if token and not hmac.compare_digest(str(row.get("token_hash") or ""), _token_hash(token)):
                raise RefreshSessionError("invalid_refresh_token", "refresh token is invalid")
            family_id = str(_metadata(row).get("family_id") or effective_session_id)
            for candidate_id, candidate in _MEMORY_SESSIONS.items():
                if str(_metadata(candidate).get("family_id") or candidate_id) != family_id:
                    continue
                candidate.update(
                    status="revoked",
                    revoked_at=revoked_at,
                    revoked_by=str(actor),
                    revoke_reason=str(reason),
                    updated_at=revoked_at,
                )
            return True
    from backend.core.db import get_state_pg_conn

    conn = get_state_pg_conn()
    try:
        row = conn.execute(
            """SELECT token_hash, metadata_json
               FROM auth_session WHERE session_id = %s FOR UPDATE""",
            (effective_session_id,),
        ).fetchone()
        if row is None:
            return False
        if token and not hmac.compare_digest(str(row.get("token_hash") or ""), _token_hash(token)):
            raise RefreshSessionError("invalid_refresh_token", "refresh token is invalid")
        family_id = str(_metadata(dict(row)).get("family_id") or effective_session_id)
        result = conn.execute(
            """
            UPDATE auth_session
            SET status = 'revoked', revoked_at = %s, revoked_by = %s,
                revoke_reason = %s, updated_at = %s
            WHERE session_id = %s
               OR metadata_json::jsonb ->> 'family_id' = %s
            """,
            (
                revoked_at,
                str(actor),
                str(reason),
                revoked_at,
                effective_session_id,
                family_id,
            ),
        )
        conn.commit()
        return int(result.rowcount or 0) > 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def reset_memory_sessions_for_tests() -> None:
    with _MEMORY_LOCK:
        _MEMORY_SESSIONS.clear()


__all__ = [
    "REFRESH_TOKEN_TTL_SECONDS",
    "RefreshGrant",
    "RefreshSessionError",
    "StepUpGrant",
    "create_refresh_session",
    "reset_memory_sessions_for_tests",
    "revoke_refresh_session",
    "rotate_refresh_session",
    "session_family_ids",
    "session_is_active",
    "step_up_refresh_session",
]
