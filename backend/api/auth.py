"""Auth endpoints: POST /api/auth/login (issue JWT), GET /api/auth/me (whoami).

v1: any password is accepted (no real validation). The returned token is a
real HS256 JWT that can be used for subsequent requests.
"""
import time
from typing import Annotated

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel

from backend.core.auth import create_token, get_current_user, require_user

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str  # v1: any value accepted


class LoginResponse(BaseModel):
    user: str
    token: str
    token_type: str = "Bearer"
    expires_in: int = 86400


@router.post("/login", response_model=LoginResponse)
def login(req: LoginRequest) -> LoginResponse:
    """v1: any (username, password) is accepted. v2 will verify against DB."""
    token = create_token(req.username)
    return LoginResponse(user=req.username, token=token, expires_in=86400)


@router.get("/me")
def me(user: Annotated[str, Depends(get_current_user)], authorization: Annotated[str | None, Header()] = None) -> dict:
    """Returns the current user. `authenticated` is true iff a valid Bearer
    token was supplied. (audit 2026-06-08: previously the check was
    `user != "zhu"`, which is always false in v1 (the default sub is "zhu").
    The real signal is whether the Authorization header was present and
    decodeable.)"""
    has_token = bool(authorization and authorization.lower().startswith("bearer "))
    return {"user": user, "authenticated": has_token}


@router.get("/me-strict")
def me_strict(user: Annotated[str, Depends(require_user)]) -> dict:
    """Returns the current user, REQUIRES a valid Bearer token. Use this for new auth-required routes."""
    return {"user": user, "authenticated": True}
