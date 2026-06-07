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
def me(user: Annotated[str, Depends(get_current_user)]) -> dict:
    """Returns the current user. Uses the lenient get_current_user (returns "zhu" if no token)."""
    return {"user": user, "authenticated": user != "zhu"}


@router.get("/me-strict")
def me_strict(user: Annotated[str, Depends(require_user)]) -> dict:
    """Returns the current user, REQUIRES a valid Bearer token. Use this for new auth-required routes."""
    return {"user": user, "authenticated": True}
