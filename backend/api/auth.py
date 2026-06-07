"""Auth endpoints stub: POST /api/auth/login, GET /api/auth/me.

v1: accepts any password, returns a fake token. v2: real JWT + OAuth.
"""
import hashlib
import time
from typing import Annotated

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel

from backend.core.auth import get_current_user

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    user: str
    token: str
    expires_at: int


@router.post("/login", response_model=LoginResponse)
def login(req: LoginRequest) -> LoginResponse:
    # v1: accept anything; emit a fake token. v2 will verify against DB.
    token_src = f"{req.username}:{req.password}:{int(time.time())}"
    fake_token = hashlib.sha256(token_src.encode("utf-8")).hexdigest()[:32]
    return LoginResponse(user=req.username, token=fake_token, expires_at=int(time.time()) + 86400)


@router.get("/me")
def me(user: Annotated[str, Depends(get_current_user)]) -> dict:
    return {"user": user, "authenticated": True}
