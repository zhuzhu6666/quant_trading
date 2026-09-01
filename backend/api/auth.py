"""Auth v2 endpoints: login, step-up, rotating refresh, logout, and WS ticket."""

from __future__ import annotations

import hashlib
import hmac
import logging
from backend.core.env import get_env, truthy_env
import threading
import time
from collections import defaultdict, deque
from typing import Annotated, Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel

from backend.core.auth import (
    AuthConfigError,
    JWT_EXPIRY_SECONDS,
    REFRESH_EXPIRY_SECONDS,
    WS_TICKET_EXPIRY_SECONDS,
    create_access_token,
    create_ws_ticket,
    get_current_claims,
    get_current_user,
    require_user,
    revoke_access_session_durably,
    validate_auth_config,
)
from backend.services.auth_sessions import (
    RefreshGrant,
    RefreshSessionError,
    create_refresh_session,
    revoke_refresh_session,
    rotate_refresh_session,
    session_family_ids,
    step_up_refresh_session,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])
logger = logging.getLogger(__name__)

_LOGIN_ATTEMPTS: dict[str, deque[float]] = defaultdict(deque)
_LOGIN_ATTEMPTS_LOCK = threading.Lock()
_PASSWORD_HASHER = PasswordHasher()
_REFRESH_COOKIE = "quant_refresh"

def _get_valid_user() -> str:
    value = (get_env("QUANT_AUTH_USER") or "").strip()
    if not value:
        raise AuthConfigError("QUANT_AUTH_USER is required")
    return value

def _get_password_hash() -> str:
    value = (get_env("QUANT_PASSWORD_HASH") or "").strip()
    if not value:
        raise AuthConfigError("QUANT_PASSWORD_HASH is required")
    return value

def _verify_password_with_metadata(password: str) -> tuple[bool, bool]:
    """Return ``(valid, legacy_sha256)`` without silent SHA downgrade."""
    encoded = _get_password_hash()
    if encoded.startswith("$argon2id$"):
        try:
            return bool(_PASSWORD_HASHER.verify(encoded, password)), False
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False, False
    if not truthy_env("QUANT_AUTH_ALLOW_LEGACY_SHA256"):
        return False, False
    supplied = hashlib.sha256(password.encode("utf-8")).hexdigest()
    return hmac.compare_digest(supplied, encoded), True

def _verify_password(password: str) -> bool:
    """Backward-compatible boolean verifier used by older unit callers."""
    return _verify_password_with_metadata(password)[0]

def _env_int(name: str, default: int) -> int:
    try:
        return int(get_env(name) or str(default))
    except (TypeError, ValueError):
        return default

def _login_rate_limit_window() -> int:
    return max(10, _env_int("QUANT_LOGIN_RATE_WINDOW_SECONDS", 60))

def _login_rate_limit_max_attempts() -> int:
    return max(3, _env_int("QUANT_LOGIN_RATE_MAX_ATTEMPTS", 10))

def _client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for", "")
    ip = forwarded_for.split(",", 1)[0].strip()
    if not ip and request.client:
        ip = request.client.host
    return ip or "unknown"

def _client_key(request: Request) -> str:
    return _client_ip(request)

def _client_metadata(request: Request) -> dict[str, str]:
    return {
        "client_fingerprint": request.headers.get("x-client-fingerprint", "")[:500],
        "ip_address": _client_ip(request),
        "user_agent": request.headers.get("user-agent", "")[:500],
    }

def _enforce_login_rate_limit(request: Request) -> None:
    now = time.time()
    window = _login_rate_limit_window()
    key = _client_key(request)
    with _LOGIN_ATTEMPTS_LOCK:
        attempts = _LOGIN_ATTEMPTS[key]
        while attempts and now - attempts[0] > window:
            attempts.popleft()
        if len(attempts) >= _login_rate_limit_max_attempts():
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="too many login attempts; try again later",
            )
        attempts.append(now)

def _clear_login_rate_limit(request: Request) -> None:
    key = _client_key(request)
    with _LOGIN_ATTEMPTS_LOCK:
        _LOGIN_ATTEMPTS.pop(key, None)

def _set_refresh_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=_REFRESH_COOKIE,
        value=token,
        max_age=REFRESH_EXPIRY_SECONDS,
        httponly=True,
        secure=not truthy_env("QUANT_AUTH_INSECURE_COOKIE"),
        samesite="strict",
        path="/api/auth",
    )

def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(key=_REFRESH_COOKIE, path="/api/auth")

def _session_http_error(exc: RefreshSessionError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"error": exc.code, "msg": str(exc)},
    )

class LoginRequest(BaseModel):
    username: str
    password: str

class RefreshRequest(BaseModel):
    refresh_token: str = ""

class LogoutRequest(BaseModel):
    refresh_token: str = ""

class StepUpRequest(BaseModel):
    password: str

class LoginResponse(BaseModel):
    user: str
    token: str
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int = JWT_EXPIRY_SECONDS
    refresh_expires_in: int = REFRESH_EXPIRY_SECONDS
    password_rehash_required: bool = False

class StepUpResponse(BaseModel):
    user: str
    token: str
    access_token: str
    token_type: str = "Bearer"
    expires_in: int = JWT_EXPIRY_SECONDS
    session_id: str
    auth_time: int
    password_rehash_required: bool = False

def _response_for_grant(grant: RefreshGrant, *, password_rehash_required: bool = False) -> LoginResponse:
    access_token = create_access_token(
        grant.subject,
        session_id=grant.session_id,
        family_id=grant.family_id,
        auth_time=grant.auth_time,
    )
    return LoginResponse(
        user=grant.subject,
        token=access_token,
        access_token=access_token,
        refresh_token=grant.refresh_token,
        password_rehash_required=bool(password_rehash_required),
    )

@router.post("/login", response_model=LoginResponse)
def login(req: LoginRequest, request: Request, response: Response) -> LoginResponse:
    try:
        validate_auth_config()
        valid_user = _get_valid_user()
    except AuthConfigError as exc:
        logger.error("auth configuration error: %s", exc)
        raise HTTPException(
            status_code=500,
            detail={"error": "auth_not_configured", "msg": str(exc)},
        ) from exc
    _enforce_login_rate_limit(request)
    valid_password, legacy_password = _verify_password_with_metadata(req.password)
    if req.username != valid_user or not valid_password:
        raise HTTPException(status_code=401, detail="invalid credentials")
    _clear_login_rate_limit(request)
    try:
        grant = create_refresh_session(req.username, auth_time=int(time.time()), **_client_metadata(request))
    except Exception as exc:
        logger.error("auth session creation failed: %s", exc)
        raise HTTPException(
            status_code=503,
            detail={"error": "auth_session_unavailable", "msg": "could not create refresh session"},
        ) from exc
    _set_refresh_cookie(response, grant.refresh_token)
    return _response_for_grant(grant, password_rehash_required=legacy_password)

@router.post("/refresh", response_model=LoginResponse)
def refresh(
    req: RefreshRequest,
    request: Request,
    response: Response,
    refresh_cookie: Annotated[str | None, Cookie(alias=_REFRESH_COOKIE)] = None,
) -> LoginResponse:
    token = str(req.refresh_token or refresh_cookie or "").strip()
    if not token:
        raise HTTPException(
            status_code=401,
            detail={"error": "missing_refresh_token", "msg": "refresh token is required"},
        )
    try:
        grant = rotate_refresh_session(token, **_client_metadata(request))
    except RefreshSessionError as exc:
        _clear_refresh_cookie(response)
        raise _session_http_error(exc) from exc
    except Exception as exc:
        logger.error("refresh session rotation unavailable: %s", exc)
        raise HTTPException(
            status_code=503,
            detail={"error": "auth_session_unavailable", "msg": "refresh authority is unavailable"},
        ) from exc
    _set_refresh_cookie(response, grant.refresh_token)
    return _response_for_grant(grant)

@router.post("/step-up", response_model=StepUpResponse)
def step_up(
    req: StepUpRequest,
    request: Request,
    claims: Annotated[dict[str, Any], Depends(get_current_claims)],
) -> StepUpResponse:
    """Re-authenticate the current persistent session without logging in again."""

    try:
        validate_auth_config()
        valid_user = _get_valid_user()
    except AuthConfigError as exc:
        logger.error("auth configuration error during step-up: %s", exc)
        raise HTTPException(
            status_code=500,
            detail={"error": "auth_not_configured", "msg": str(exc)},
        ) from exc

    subject = str(claims.get("sub") or "")
    session_id = str(claims.get("sid") or "")
    family_id = str(claims.get("fid") or "")
    if not session_id:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "step_up_session_required",
                "msg": "an active Auth v2 session is required",
            },
        )

    _enforce_login_rate_limit(request)
    valid_password, legacy_password = _verify_password_with_metadata(req.password)
    if subject != valid_user or not valid_password:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "invalid_step_up_credentials",
                "msg": "password verification failed",
            },
        )

    try:
        grant = step_up_refresh_session(
            session_id,
            subject=subject,
            family_id=family_id,
        )
    except RefreshSessionError as exc:
        raise _session_http_error(exc) from exc
    except Exception as exc:
        logger.error("step-up session persistence unavailable: %s", exc)
        raise HTTPException(
            status_code=503,
            detail={
                "error": "step_up_session_unavailable",
                "msg": "session authority is unavailable; new risk remains blocked",
            },
        ) from exc

    _clear_login_rate_limit(request)
    access_token = create_access_token(
        grant.subject,
        session_id=grant.session_id,
        family_id=grant.family_id,
        auth_time=grant.auth_time,
    )
    return StepUpResponse(
        user=grant.subject,
        token=access_token,
        access_token=access_token,
        session_id=grant.session_id,
        auth_time=grant.auth_time,
        password_rehash_required=legacy_password,
    )

@router.post("/logout")
def logout(
    req: LogoutRequest,
    request: Request,
    response: Response,
    claims: Annotated[dict[str, Any], Depends(get_current_claims)],
    refresh_cookie: Annotated[str | None, Cookie(alias=_REFRESH_COOKIE)] = None,
) -> dict[str, Any]:
    del request
    session_id = str(claims.get("sid") or "")
    family_id = str(claims.get("fid") or "")
    refresh_token = str(req.refresh_token or refresh_cookie or "").strip()
    # Logout revokes every authority carried by this signed access token.  The
    # risk-reduction-only grace intentionally outlives normal access expiry, so
    # retaining the revocation only until ``exp`` would make a logged-out token
    # valid again for stop/emergency after fifteen minutes.
    expires_at = max(
        float(claims.get("exp") or 0.0),
        float(claims.get("risk_reduce_until") or 0.0),
        time.time(),
    )
    revoked = False
    try:
        # The signed access token already carries enough authority identity to
        # revoke this session/family without PostgreSQL.  Persist that fact
        # first so a session-store outage cannot leave a token usable for the
        # local risk-reduction grace after logout was attempted.
        revoke_access_session_durably(
            session_id,
            family_id=family_id,
            expires_at=expires_at,
            reason="logout",
        )
        persisted_family_id, family_session_ids = session_family_ids(session_id)
        family_id = family_id or persisted_family_id
        # Expand the durable projection to every session row known by the
        # server.  This append is idempotent; the first append above remains
        # authoritative if PostgreSQL is unavailable here.
        revoke_access_session_durably(
            session_id,
            family_id=family_id,
            additional_session_ids=tuple(family_session_ids),
            expires_at=expires_at,
            reason="logout",
        )
        revoked = revoke_refresh_session(
            session_id=session_id,
            token=refresh_token,
            actor=str(claims.get("sub") or "auth_logout"),
            reason="logout",
        )
    except RefreshSessionError as exc:
        raise _session_http_error(exc) from exc
    except Exception as exc:
        logger.error("logout persistence unavailable after local revocation: %s", exc)
        raise HTTPException(
            status_code=503,
            detail={
                "error": "logout_persistence_unavailable",
                "msg": "logout did not commit to every session authority",
            },
        ) from exc
    finally:
        _clear_refresh_cookie(response)
    return {"ok": True, "revoked": bool(revoked), "session_id": session_id}

@router.post("/ws-ticket")
def ws_ticket(
    claims: Annotated[dict[str, Any], Depends(get_current_claims)],
) -> dict[str, Any]:
    ticket, expires_at = create_ws_ticket(
        subject=str(claims["sub"]),
        session_id=str(claims.get("sid") or ""),
        family_id=str(claims.get("fid") or ""),
    )
    return {
        "ticket": ticket,
        "expires_in": WS_TICKET_EXPIRY_SECONDS,
        "expires_at": expires_at,
    }

@router.get("/me")
def me(
    user: Annotated[str, Depends(get_current_user)],
    claims: Annotated[dict[str, Any], Depends(get_current_claims)],
    authorization: Annotated[str | None, Header(include_in_schema=False)] = None,
) -> dict[str, Any]:
    has_token = bool(authorization and authorization.lower().startswith("bearer "))
    return {
        "user": user,
        "authenticated": has_token,
        "session_id": str(claims.get("sid") or ""),
        "auth_time": int(claims.get("auth_time") or 0),
    }

@router.get("/me-strict")
def me_strict(user: Annotated[str, Depends(require_user)]) -> dict[str, Any]:
    return {"user": user, "authenticated": True}
