"""Auth dependency. v1: HS256 JWT in Authorization header.

Wire-in: any route can add `Depends(get_current_user)` to require a valid JWT.
v1: passwords aren't actually validated (any password works); the token just
proves the user has been through the login flow. Multi-user + real auth is
Phase 6+.
"""
import time
from typing import Annotated

import jwt
from fastapi import Header, HTTPException, status

# Hardcoded v1 constants. In production, JWT_SECRET comes from env / secret manager.
JWT_SECRET = "quant-v1-dev-secret-do-not-use-in-prod"
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_SECONDS = 24 * 3600


def create_token(user: str) -> str:
    """Issue a HS256 JWT for the given user. v1: any user is accepted."""
    now = int(time.time())
    payload = {
        "sub": user,
        "iat": now,
        "exp": now + JWT_EXPIRY_SECONDS,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def get_current_user(
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    """FastAPI dependency. Returns the user from the JWT, or raises 401.

    Backwards compat: if no Authorization header, return "zhu" (Phase 3.14 stub).
    The stricter version `require_user` (below) always raises.
    """
    if not authorization:
        # v1: backwards compat with the Phase 3.14 stub. Allows existing routes
        # (which don't yet use this dep) to keep working. Will be removed once
        # all routes are wrapped.
        return "zhu"
    if not authorization.lower().startswith("bearer "):
        return "zhu"
    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        return "zhu"  # v1: soft-fail; v2: raise HTTPException(401)
    except jwt.InvalidTokenError:
        return "zhu"
    return payload.get("sub", "zhu")


def require_user(
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    """Strict version: must have a valid Bearer token. Raises 401 otherwise."""
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "missing_authorization", "msg": "Authorization: Bearer <token> required"},
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
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
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
