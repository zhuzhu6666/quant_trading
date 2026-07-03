"""Auth endpoints: POST /api/auth/login (issue JWT), GET /api/auth/me (whoami)."""
import hashlib
import hmac
import logging
import os
import threading
import time
from collections import defaultdict, deque
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel

from backend.core.auth import AuthConfigError, create_token, get_current_user, require_user, validate_auth_config

router = APIRouter(prefix="/api/auth", tags=["auth"])
logger = logging.getLogger(__name__)

_LOGIN_ATTEMPTS: dict[str, deque[float]] = defaultdict(deque)
_LOGIN_ATTEMPTS_LOCK = threading.Lock()


def _get_valid_user() -> str:
    value = (os.environ.get("QUANT_AUTH_USER") or "").strip()
    if not value:
        raise AuthConfigError("QUANT_AUTH_USER is required")
    return value


def _get_password_hash() -> str:
    value = (os.environ.get("QUANT_PASSWORD_HASH") or "").strip()
    if not value:
        raise AuthConfigError("QUANT_PASSWORD_HASH is required")
    return value


def _verify_password(password: str) -> bool:
    supplied = hashlib.sha256(password.encode()).hexdigest()
    return hmac.compare_digest(supplied, _get_password_hash())


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or str(default))
    except (TypeError, ValueError):
        return default


def _login_rate_limit_window() -> int:
    return max(10, _env_int("QUANT_LOGIN_RATE_WINDOW_SECONDS", 60))


def _login_rate_limit_max_attempts() -> int:
    return max(3, _env_int("QUANT_LOGIN_RATE_MAX_ATTEMPTS", 10))


def _client_key(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for", "")
    ip = forwarded_for.split(",", 1)[0].strip()
    if not ip and request.client:
        ip = request.client.host
    return ip or "unknown"


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


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    user: str
    token: str
    token_type: str = "Bearer"
    expires_in: int = 86400


@router.post("/login", response_model=LoginResponse)
def login(req: LoginRequest, request: Request) -> LoginResponse:
    try:
        validate_auth_config()
        valid_user = _get_valid_user()
    except AuthConfigError as e:
        logger.error("auth configuration error: %s", e)
        raise HTTPException(
            status_code=500,
            detail={"error": "auth_not_configured", "msg": str(e)},
        )
    _enforce_login_rate_limit(request)
    if req.username != valid_user or not _verify_password(req.password):
        raise HTTPException(status_code=401, detail="invalid credentials")
    _clear_login_rate_limit(request)
    token = create_token(req.username)
    return LoginResponse(user=req.username, token=token, expires_in=86400)


@router.get("/me")
def me(user: Annotated[str, Depends(get_current_user)], authorization: Annotated[str | None, Header()] = None) -> dict:
    has_token = bool(authorization and authorization.lower().startswith("bearer "))
    return {"user": user, "authenticated": has_token}


@router.get("/me-strict")
def me_strict(user: Annotated[str, Depends(require_user)]) -> dict:
    return {"user": user, "authenticated": True}
