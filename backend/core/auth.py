"""Auth dependency. v2: HS256 JWT in Authorization header, SHA256 password validation.

Wire-in: any route can add `Depends(get_current_user)` to require a valid JWT.
v2: password validated via SHA256 in backend/api/auth.py._verify_password().
The token just proves the user has been through the login flow.

SEC-1 fix (audit 2026-06-21): document current state accurately.
JWT secret must be set via QUANT_JWT_SECRET env var (otherwise auto-generated per-startup).
Password hash should be set via QUANT_PASSWORD_HASH env var (otherwise uses hardcoded default)."""

import logging
import os as _os
import secrets
import time
from typing import Annotated

import jwt
from fastapi import Header, HTTPException, status

JWT_ALGORITHM = "HS256"
JWT_EXPIRY_SECONDS = 24 * 3600

_JWT_SECRET: str | None = None
_JWT_SECRET_WARNED = False
_logger = logging.getLogger(__name__)


def _get_jwt_secret() -> str:
    """惰性加载 JWT_SECRET, 避免模块导入时 KeyError 炸整个 app。

    若环境变量未设置, 生成一个 dev 级临时密钥
    (每次启动变化, 前端需要重新登录)。
    """
    global _JWT_SECRET, _JWT_SECRET_WARNED
    if _JWT_SECRET is None:
        _JWT_SECRET = _os.environ.get("QUANT_JWT_SECRET")
        if not _JWT_SECRET:
            _JWT_SECRET = secrets.token_hex(32)
            if not _JWT_SECRET_WARNED:
                _logger.warning(
                    "QUANT_JWT_SECRET not set; using ephemeral dev JWT secret for this process"
                )
                _JWT_SECRET_WARNED = True
    return _JWT_SECRET


def create_token(user: str) -> str:
    """Issue a HS256 JWT for the given user. v1: any user is accepted."""
    now = int(time.time())
    payload = {
        "sub": user,
        "iat": now,
        "exp": now + JWT_EXPIRY_SECONDS,
    }
    return jwt.encode(payload, _get_jwt_secret(), algorithm=JWT_ALGORITHM)


def get_current_user(
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    """FastAPI dependency. Returns the user from the JWT, or raises 401."""
    return require_user(authorization)


def require_user(
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    """Strict version: must have a valid Bearer token. Raises 401 otherwise."""
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "missing_authorization", "msg": "Authorization: Bearer *** required"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "invalid_authorization", "msg": "expected 'Bearer <token>'"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = jwt.decode(token, _get_jwt_secret(), algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "token_expired", "msg": "JWT has expired; log in again"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "invalid_token", "msg": str(e)[:200]},
            headers={"WWW-Authenticate": "Bearer"},
        )
    return payload.get("sub", "zhu")


# (audit 2026-06-08: pre-built Annotated alias for ergonomic use in route
# signatures. Saves writing `Annotated[str, Depends(require_user)]` in every
# route. Use as `user: RequireUser` in the function signature.)
from typing import Annotated as _Annotated  # noqa: E402
from fastapi import Depends as _Depends  # noqa: E402
RequireUser = _Annotated[str, _Depends(require_user)]


def __getattr__(name):
    """向后兼容: from backend.core.auth import JWT_SECRET 可惰性加载。"""
    if name == "JWT_SECRET":
        return _get_jwt_secret()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
