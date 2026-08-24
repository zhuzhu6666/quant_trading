"""Authentication primitives for short-lived Auth v2 access credentials.

Normal access JWT validation is bound to the durable Auth v2 session. Stop and
emergency use a separate signed risk-reduction scope plus the fsync'd local
logout projection, so those actions remain available during a PostgreSQL
outage. Operations that create new risk use ``RequireRecentStepUp`` and always
fail closed without a recent password authentication and active session.
"""

from __future__ import annotations

import logging
import os as _os
import secrets
import threading
import time
import uuid
from typing import Annotated, Any, Final

import jwt
from fastapi import Depends, Header, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer


JWT_ALGORITHM: Final[str] = "HS256"
JWT_EXPIRY_SECONDS: Final[int] = 24 * 60 * 60
REFRESH_EXPIRY_SECONDS: Final[int] = 7 * 24 * 3600
WS_TICKET_EXPIRY_SECONDS: Final[int] = 30
STEP_UP_MAX_AGE_SECONDS: Final[int] = 5 * 60
RISK_REDUCTION_GRACE_SECONDS: Final[int] = REFRESH_EXPIRY_SECONDS

_JWT_SECRET: str | None = None
_logger = logging.getLogger(__name__)

# This dependency is deliberately non-raising.  The custom auth functions
# below retain the existing error contract (including 401 + WWW-Authenticate)
# while ``Security`` gives FastAPI/OpenAPI a truthful Bearer requirement for
# endpoints that depend on them.
_bearer_security = HTTPBearer(
    auto_error=False,
    scheme_name="BearerAuth",
    description="Auth v2 access JWT supplied as Authorization: Bearer <token>.",
)

_REVOCATION_LOCK = threading.RLock()
_REVOKED_SESSION_IDS: dict[str, float] = {}
_REVOKED_FAMILY_IDS: dict[str, float] = {}
_WS_TICKET_LOCK = threading.RLock()
_WS_TICKETS: dict[str, dict[str, Any]] = {}


class AuthConfigError(RuntimeError):
    """Raised when required authentication environment is missing or unsafe."""


def _env_flag(name: str, default: bool = False) -> bool:
    raw = (_os.environ.get(name) or ("1" if default else "0")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _required_env(name: str) -> str:
    value = (_os.environ.get(name) or "").strip()
    if not value:
        raise AuthConfigError(f"{name} is required")
    return value


def _get_jwt_secret() -> str:
    """Load JWT secret from QUANT_JWT_SECRET, failing closed if absent."""
    global _JWT_SECRET
    if _JWT_SECRET is None:
        _JWT_SECRET = _required_env("QUANT_JWT_SECRET")
    return _JWT_SECRET


def validate_auth_config() -> None:
    """Validate required Auth v2 environment and explicit legacy switches."""
    _get_jwt_secret()
    _required_env("QUANT_AUTH_USER")
    password_hash = _required_env("QUANT_PASSWORD_HASH")
    if password_hash.startswith("$argon2id$"):
        return
    is_legacy_sha256 = len(password_hash) == 64 and all(c in "0123456789abcdefABCDEF" for c in password_hash)
    if is_legacy_sha256 and _env_flag("QUANT_AUTH_ALLOW_LEGACY_SHA256"):
        return
    if is_legacy_sha256:
        raise AuthConfigError(
            "legacy SHA-256 password hash requires QUANT_AUTH_ALLOW_LEGACY_SHA256=1 during migration"
        )
    raise AuthConfigError("QUANT_PASSWORD_HASH must be an Argon2id encoded hash")


def create_access_token(
    user: str,
    *,
    session_id: str = "",
    family_id: str = "",
    auth_time: int | None = None,
    now: int | None = None,
) -> str:
    """Issue a 24-hour Auth v2 access JWT."""
    issued_at = int(time.time() if now is None else now)
    payload = {
        "sub": str(user),
        "iat": issued_at,
        "exp": issued_at + JWT_EXPIRY_SECONDS,
        "auth_time": int(issued_at if auth_time is None else auth_time),
        "jti": str(uuid.uuid4()),
        "sid": str(session_id or ""),
        "fid": str(family_id or ""),
        "typ": "access",
        "ver": 2,
        # The same signed credential may be accepted after normal access
        # expiry only by endpoints explicitly wired to RequireRiskReductionUser.
        # It grants no read/start/unlock authority and avoids making emergency
        # close availability depend on PostgreSQL refresh rotation.
        "scope": ["authenticated", "risk_reduce"],
        "risk_reduce_until": issued_at + RISK_REDUCTION_GRACE_SECONDS,
    }
    return jwt.encode(payload, _get_jwt_secret(), algorithm=JWT_ALGORITHM)


def create_token(user: str) -> str:
    """Compatibility helper used by internal callers and tests."""
    return create_access_token(user)


def _auth_http_error(code: str, message: str, *, status_code: int = 401) -> HTTPException:
    headers = {"WWW-Authenticate": "Bearer"} if status_code == 401 else None
    return HTTPException(
        status_code=status_code,
        detail={"error": code, "msg": message},
        headers=headers,
    )


def _extract_bearer(authorization: str | None) -> str:
    if not authorization:
        raise _auth_http_error("missing_authorization", "Authorization: Bearer *** required")
    if not authorization.lower().startswith("bearer "):
        raise _auth_http_error("invalid_authorization", "expected 'Bearer <token>'")
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise _auth_http_error("invalid_authorization", "Bearer token is empty")
    return token


def _cleanup_revocations(now: float) -> None:
    for session_id, expires_at in list(_REVOKED_SESSION_IDS.items()):
        if expires_at <= now:
            _REVOKED_SESSION_IDS.pop(session_id, None)
    for family_id, expires_at in list(_REVOKED_FAMILY_IDS.items()):
        if expires_at <= now:
            _REVOKED_FAMILY_IDS.pop(family_id, None)


def _raise_if_revoked(
    session_id: str,
    *,
    family_id: str = "",
    now: float | None = None,
    durable_store_fail_closed: bool = True,
) -> None:
    if not session_id and not family_id:
        return
    checked_at = float(time.time() if now is None else now)
    with _REVOCATION_LOCK:
        _cleanup_revocations(checked_at)
        if (
            session_id in _REVOKED_SESSION_IDS
            or family_id in _REVOKED_FAMILY_IDS
        ):
            raise _auth_http_error("session_revoked", "access session has been revoked")
    try:
        from backend.services.auth_revocations import auth_authority_is_revoked

        revoked = auth_authority_is_revoked(
            session_id=session_id,
            family_id=family_id,
            now=checked_at,
        )
    except Exception as exc:
        if durable_store_fail_closed:
            _logger.error("durable Auth revocation validation unavailable: %s", exc)
            raise _auth_http_error(
                "session_authority_unavailable",
                "session revocation authority is unavailable",
                status_code=503,
            ) from exc
        # Risk reduction must remain available during an auxiliary local-ledger
        # read failure. Logout fsyncs the ledger before reporting success, so
        # this is an explicit availability-over-observability safety fallback.
        _logger.critical(
            "durable Auth revocation validation unavailable for risk reduction: %s",
            exc,
        )
        return
    if revoked:
        raise _auth_http_error("session_revoked", "access session has been revoked")


def revoke_access_session_locally(
    session_id: str,
    *,
    family_id: str = "",
    additional_session_ids: tuple[str, ...] = (),
    expires_at: float | None = None,
) -> None:
    """Immediately deny a session in this process, independent of PostgreSQL."""
    if not session_id and not family_id and not additional_session_ids:
        return
    effective_expires_at = float(
        time.time() + REFRESH_EXPIRY_SECONDS if expires_at is None else expires_at
    )
    with _REVOCATION_LOCK:
        _cleanup_revocations(time.time())
        for value in (session_id, *additional_session_ids):
            if str(value or ""):
                _REVOKED_SESSION_IDS[str(value)] = effective_expires_at
        if family_id:
            _REVOKED_FAMILY_IDS[str(family_id)] = effective_expires_at


def revoke_access_session_durably(
    session_id: str,
    *,
    family_id: str = "",
    additional_session_ids: tuple[str, ...] = (),
    expires_at: float | None = None,
    reason: str = "logout",
) -> None:
    """Revoke immediately and fsync a restart-safe risk-reduction projection."""

    effective_expires_at = float(
        time.time() + REFRESH_EXPIRY_SECONDS if expires_at is None else expires_at
    )
    revoke_access_session_locally(
        session_id,
        family_id=family_id,
        additional_session_ids=additional_session_ids,
        expires_at=effective_expires_at,
    )
    from backend.services.auth_revocations import append_auth_revocations

    append_auth_revocations(
        session_ids=(session_id, *additional_session_ids),
        family_ids=(family_id,),
        expires_at=effective_expires_at,
        reason=reason,
    )


def _require_persistent_session(
    session_id: str,
    *,
    subject: str,
) -> None:
    if not session_id:
        return
    try:
        from backend.services.auth_sessions import session_is_active

        active = session_is_active(session_id, subject=subject)
    except Exception as exc:
        _logger.error("access session validation unavailable: %s", exc)
        raise _auth_http_error(
            "session_authority_unavailable",
            "session authority is unavailable",
            status_code=503,
        ) from exc
    if not active:
        raise _auth_http_error("session_revoked", "access session is not active")


def decode_access_token(
    token: str,
    *,
    allow_legacy: bool | None = None,
    validate_session: bool = True,
    durable_store_fail_closed: bool = True,
) -> dict[str, Any]:
    """Decode an access JWT and validate its durable session when present."""
    try:
        payload = jwt.decode(token, _get_jwt_secret(), algorithms=[JWT_ALGORITHM])
    except AuthConfigError as exc:
        _logger.error("auth configuration error: %s", exc)
        raise _auth_http_error("auth_not_configured", str(exc), status_code=500) from exc
    except jwt.ExpiredSignatureError as exc:
        raise _auth_http_error("token_expired", "JWT has expired; refresh or log in again") from exc
    except jwt.InvalidTokenError as exc:
        raise _auth_http_error("invalid_token", str(exc)[:200]) from exc

    subject = payload.get("sub")
    if not isinstance(subject, str) or not subject.strip():
        raise _auth_http_error("invalid_token", "JWT subject is missing")
    token_type = payload.get("typ")
    legacy_allowed = _env_flag("QUANT_AUTH_ALLOW_LEGACY_ACCESS_TOKEN") if allow_legacy is None else bool(allow_legacy)
    if token_type != "access":
        if token_type or not legacy_allowed:
            raise _auth_http_error("invalid_token_type", "an Auth v2 access token is required")
        payload = dict(payload)
        payload["legacy"] = True
        payload.setdefault("auth_time", payload.get("iat", 0))
        payload.setdefault("sid", "")

    session_id = str(payload.get("sid") or "")
    family_id = str(payload.get("fid") or "")
    _raise_if_revoked(
        session_id,
        family_id=family_id,
        durable_store_fail_closed=durable_store_fail_closed,
    )
    if validate_session:
        _require_persistent_session(session_id, subject=str(subject))
    return dict(payload)


def decode_risk_reduction_token(
    token: str,
    *,
    now: int | None = None,
) -> dict[str, Any]:
    """Validate a token for risk-reducing endpoints without PostgreSQL.

    A normal unexpired access token is accepted.  Once its 24-hour access
    lifetime ends, only an Auth v2 token carrying the signed ``risk_reduce``
    scope remains valid, and only until the fixed seven-day safety deadline.
    Legacy/stateless tokens never receive this grace.
    """

    checked_at = int(time.time() if now is None else now)
    try:
        return decode_access_token(
            token,
            validate_session=False,
            durable_store_fail_closed=False,
        )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        if str(detail.get("error") or "") != "token_expired":
            raise
    try:
        payload = jwt.decode(
            token,
            _get_jwt_secret(),
            algorithms=[JWT_ALGORITHM],
            options={"verify_exp": False},
        )
    except AuthConfigError as exc:
        raise _auth_http_error("auth_not_configured", str(exc), status_code=500) from exc
    except jwt.InvalidTokenError as exc:
        raise _auth_http_error("invalid_token", str(exc)[:200]) from exc

    subject = str(payload.get("sub") or "").strip()
    if not subject or subject != _required_env("QUANT_AUTH_USER"):
        raise _auth_http_error("invalid_token", "JWT subject is not the configured operator")
    if payload.get("typ") != "access" or int(payload.get("ver") or 0) != 2:
        raise _auth_http_error(
            "risk_reduction_scope_required",
            "an Auth v2 risk-reduction credential is required",
        )
    scope = payload.get("scope")
    scopes = {str(item) for item in scope} if isinstance(scope, (list, tuple, set)) else set()
    if "risk_reduce" not in scopes:
        raise _auth_http_error(
            "risk_reduction_scope_required",
            "token does not grant risk-reduction-only access",
        )
    try:
        issued_at = int(payload.get("iat") or 0)
        reduce_until = int(payload.get("risk_reduce_until") or 0)
    except (TypeError, ValueError) as exc:
        raise _auth_http_error("invalid_token", "risk-reduction lifetime is invalid") from exc
    if issued_at <= 0 or issued_at > checked_at + 30:
        raise _auth_http_error("invalid_token", "JWT issued-at time is invalid")
    if (
        reduce_until <= checked_at
        or reduce_until > issued_at + RISK_REDUCTION_GRACE_SECONDS
    ):
        raise _auth_http_error(
            "risk_reduction_grace_expired",
            "risk-reduction-only credential has expired",
        )
    _raise_if_revoked(
        str(payload.get("sid") or ""),
        family_id=str(payload.get("fid") or ""),
        now=checked_at,
        durable_store_fail_closed=False,
    )
    result = dict(payload)
    result["risk_reduction_only"] = True
    return result


def get_current_claims(
    authorization: Annotated[str | None, Header(include_in_schema=False)] = None,
    _bearer: HTTPAuthorizationCredentials | None = Security(_bearer_security),
) -> dict[str, Any]:
    return decode_access_token(_extract_bearer(authorization))


def get_current_user(
    authorization: Annotated[str | None, Header(include_in_schema=False)] = None,
    _bearer: HTTPAuthorizationCredentials | None = Security(_bearer_security),
) -> str:
    return require_user(authorization)


def require_user(
    authorization: Annotated[str | None, Header(include_in_schema=False)] = None,
    _bearer: HTTPAuthorizationCredentials | None = Security(_bearer_security),
) -> str:
    """Validate ordinary access against the durable Auth v2 session."""
    return str(get_current_claims(authorization)["sub"])


def get_risk_reduction_claims(
    authorization: Annotated[str | None, Header(include_in_schema=False)] = None,
    _bearer: HTTPAuthorizationCredentials | None = Security(_bearer_security),
) -> dict[str, Any]:
    return decode_risk_reduction_token(_extract_bearer(authorization))


def require_risk_reduction_user(
    authorization: Annotated[str | None, Header(include_in_schema=False)] = None,
    _bearer: HTTPAuthorizationCredentials | None = Security(_bearer_security),
) -> str:
    """Authorize only endpoints whose maximum effect is reducing risk."""

    return str(get_risk_reduction_claims(authorization)["sub"])


def require_recent_step_up(
    authorization: Annotated[str | None, Header(include_in_schema=False)] = None,
    _bearer: HTTPAuthorizationCredentials | None = Security(_bearer_security),
) -> str:
    """Require recent password auth plus an active server-side session.

    This dependency is only for operations that create or unlock new risk.
    PostgreSQL/session-store failure therefore fails closed with 503.
    """
    claims = get_current_claims(authorization)
    now = int(time.time())
    try:
        auth_time = int(claims.get("auth_time") or 0)
    except (TypeError, ValueError):
        auth_time = 0
    if auth_time <= 0 or now - auth_time > STEP_UP_MAX_AGE_SECONDS or auth_time > now + 30:
        raise _auth_http_error(
            "step_up_required",
            "password authentication within the last 5 minutes is required",
            status_code=403,
        )
    session_id = str(claims.get("sid") or "")
    if not session_id:
        if _env_flag("QUANT_AUTH_ALLOW_STATELESS_STEP_UP"):
            return str(claims["sub"])
        raise _auth_http_error("step_up_session_required", "an active Auth v2 session is required", status_code=403)
    try:
        from backend.services.auth_sessions import session_is_active

        active = session_is_active(session_id, subject=str(claims["sub"]))
    except Exception as exc:
        _logger.error("step-up session validation unavailable: %s", exc)
        raise _auth_http_error(
            "step_up_session_unavailable",
            "session authority is unavailable; new risk remains blocked",
            status_code=503,
        ) from exc
    if not active:
        raise _auth_http_error("step_up_session_inactive", "Auth v2 session is not active", status_code=403)
    return str(claims["sub"])


def create_ws_ticket(
    *,
    subject: str,
    session_id: str = "",
    family_id: str = "",
    now: float | None = None,
) -> tuple[str, float]:
    """Create a 30-second, process-local, one-time WebSocket ticket."""
    issued_at = float(time.time() if now is None else now)
    expires_at = issued_at + WS_TICKET_EXPIRY_SECONDS
    ticket = secrets.token_urlsafe(32)
    with _WS_TICKET_LOCK:
        for key, value in list(_WS_TICKETS.items()):
            if float(value.get("expires_at") or 0.0) <= issued_at:
                _WS_TICKETS.pop(key, None)
        _WS_TICKETS[ticket] = {
            "subject": str(subject),
            "session_id": str(session_id or ""),
            "family_id": str(family_id or ""),
            "expires_at": expires_at,
        }
    return ticket, expires_at


def consume_ws_ticket(ticket: str, *, now: float | None = None) -> dict[str, Any]:
    """Consume a WebSocket ticket exactly once."""
    checked_at = float(time.time() if now is None else now)
    with _WS_TICKET_LOCK:
        record = _WS_TICKETS.pop(str(ticket or ""), None)
    if not record:
        raise _auth_http_error("invalid_ws_ticket", "WebSocket ticket is invalid")
    if float(record.get("expires_at") or 0.0) <= checked_at:
        raise _auth_http_error("expired_ws_ticket", "WebSocket ticket has expired")
    session_id = str(record.get("session_id") or "")
    if session_id:
        _raise_if_revoked(
            session_id,
            family_id=str(record.get("family_id") or ""),
            now=checked_at,
        )
        _require_persistent_session(
            session_id,
            subject=str(record.get("subject") or ""),
        )
    return dict(record)


def reset_auth_state_for_tests() -> None:
    """Clear process-local revocations and tickets in isolated tests."""
    with _REVOCATION_LOCK:
        _REVOKED_SESSION_IDS.clear()
        _REVOKED_FAMILY_IDS.clear()
    with _WS_TICKET_LOCK:
        _WS_TICKETS.clear()


RequireUser = Annotated[str, Depends(require_user)]
RequireRecentStepUp = Annotated[str, Depends(require_recent_step_up)]
RequireRiskReductionUser = Annotated[str, Depends(require_risk_reduction_user)]


def __getattr__(name: str):
    """Backward compatibility for lazy ``JWT_SECRET`` imports."""
    if name == "JWT_SECRET":
        return _get_jwt_secret()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
