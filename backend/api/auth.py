"""Auth endpoints: POST /api/auth/login (issue JWT), GET /api/auth/me (whoami).

v2: password validated via SHA256. Username must be 'quant'.
"""
import hashlib
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from backend.core.auth import create_token, get_current_user, require_user

router = APIRouter(prefix="/api/auth", tags=["auth"])

_VALID_USER = "zhu"
_PASSWORD_HASH = "1bc3201a9f24a2fe48f634f90d406aaf6cbf5e36e292870ecba98d74b065ee1b"


def _verify_password(password: str) -> bool:
    return hashlib.sha256(password.encode()).hexdigest() == _PASSWORD_HASH


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    user: str
    token: str
    token_type: str = "Bearer"
    expires_in: int = 86400


@router.post("/login", response_model=LoginResponse)
def login(req: LoginRequest) -> LoginResponse:
    if req.username != _VALID_USER or not _verify_password(req.password):
        raise HTTPException(status_code=401, detail="invalid credentials")
    token = create_token(req.username)
    return LoginResponse(user=req.username, token=token, expires_in=86400)


@router.get("/me")
def me(user: Annotated[str, Depends(get_current_user)], authorization: Annotated[str | None, Header()] = None) -> dict:
    has_token = bool(authorization and authorization.lower().startswith("bearer "))
    return {"user": user, "authenticated": has_token}


@router.get("/me-strict")
def me_strict(user: Annotated[str, Depends(require_user)]) -> dict:
    return {"user": user, "authenticated": True}
