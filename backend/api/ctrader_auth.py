"""cTrader OAuth 回调 — 重新获取 access token.

流程:
  1. GET /api/ctrader/auth-url → 返回授权 URL
  2. 浏览器访问该 URL → cTrader 登录 → 重定向到 /api/ctrader/callback?code=XXX
  3. 后端自动用 code 换 token → 存到 .env → 返回结果
"""
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.request
import urllib.parse
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from backend.core.auth import RequireUser

router = APIRouter(prefix="/api/ctrader", tags=["ctrader-auth"])

_ENV_PATH = Path(__file__).resolve().parent.parent.parent / ".env"
_CTRADER_AUTH_URL = "https://openapi.ctrader.com/apps/auth"
_CTRADER_TOKEN_URL = "https://openapi.ctrader.com/apps/token"
_OAUTH_STATE_TTL_SECONDS = 10 * 60
_OAUTH_STATES: dict[str, float] = {}
_OAUTH_STATES_LOCK = threading.Lock()


def _state_digest(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def _new_oauth_state() -> str:
    state = secrets.token_urlsafe(32)
    expires_at = time.time() + _OAUTH_STATE_TTL_SECONDS
    digest = _state_digest(state)
    with _OAUTH_STATES_LOCK:
        now = time.time()
        for key, expiry in list(_OAUTH_STATES.items()):
            if expiry <= now:
                _OAUTH_STATES.pop(key, None)
        _OAUTH_STATES[digest] = expires_at
    return state


def _consume_oauth_state(state: str) -> None:
    if not state:
        raise HTTPException(400, "missing oauth state")
    digest = _state_digest(state)
    with _OAUTH_STATES_LOCK:
        expires_at = _OAUTH_STATES.pop(digest, None)
    if expires_at is None:
        raise HTTPException(400, "invalid oauth state")
    if expires_at <= time.time():
        raise HTTPException(400, "expired oauth state")


def _read_env() -> dict[str, str]:
    env = {}
    if _ENV_PATH.exists():
        with open(_ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip("\"'")
    return env


def _update_env(key: str, value: str) -> None:
    """更新 .env 中的单个 key"""
    lines = []
    found = False
    if _ENV_PATH.exists():
        with open(_ENV_PATH) as f:
            lines = f.readlines()
    with open(_ENV_PATH, "w") as f:
        for line in lines:
            if line.startswith(f"{key}="):
                f.write(f"{key}={value}\n")
                found = True
            else:
                f.write(line)
        if not found:
            f.write(f"{key}={value}\n")
    # Also update os.environ
    os.environ[key] = value


class AuthUrlResponse(BaseModel):
    url: str


@router.get("/auth-url", response_model=AuthUrlResponse)
def get_auth_url(_user: RequireUser) -> AuthUrlResponse:
    """返回 cTrader OAuth 授权 URL."""
    env = _read_env()
    client_id = env.get("CTRADER_CLIENT_ID", "")
    if not client_id:
        raise HTTPException(400, "CTRADER_CLIENT_ID not set")

    redirect_uri = "https://www.zhuzhu666.icu/api/ctrader/callback"
    state = _new_oauth_state()
    params = urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "trading",  # accounts=只读, trading=可交易
        "state": state,
    })
    return AuthUrlResponse(url=f"{_CTRADER_AUTH_URL}?{params}")


@router.get("/callback")
def oauth_callback(code: str = Query(...), state: str = Query(...)) -> dict:
    """cTrader OAuth 回调 — 用 code 换 token 并存到 .env.

    免鉴权: cTrader 重定向是浏览器直接跳转, 不带 JWT header。
    """
    _consume_oauth_state(state)
    env = _read_env()

    client_id = env.get("CTRADER_CLIENT_ID", "")
    client_secret = env.get("CTRADER_CLIENT_SECRET", "")
    redirect_uri = "https://www.zhuzhu666.icu/api/ctrader/callback"

    if not client_id or not client_secret:
        raise HTTPException(400, "Credentials not configured")

    # 用 code 换 token
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "client_secret": client_secret,
    }).encode()

    req = urllib.request.Request(_CTRADER_TOKEN_URL, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            token_data = json.loads(resp.read())
    except urllib.request.HTTPError as e:
        body = e.read().decode()[:500]
        raise HTTPException(502, f"Token exchange failed: {e.code} {body}")
    except Exception as e:
        raise HTTPException(502, f"Token exchange error: {e}")

    access_token = token_data.get("access_token") or token_data.get("accessToken")
    refresh_token = token_data.get("refresh_token") or token_data.get("refreshToken")
    expires_in = token_data.get("expires_in") or token_data.get("expiresIn", 0)

    if not access_token:
        return {"ok": False, "error": "no access_token in response", "raw": token_data}

    # 存到 .env
    _update_env("CTRADER_ACCESS_TOKEN", access_token)
    if refresh_token:
        _update_env("CTRADER_REFRESH_TOKEN", refresh_token)
    if expires_in:
        import time
        _update_env("CTRADER_TOKEN_EXPIRES_AT", str(int(time.time() + int(expires_in))))

    return {
        "ok": True,
        "expires_in": expires_in,
        "msg": "Token saved to .env. Restart quant-backend to apply.",
    }


@router.get("/token-status")
def token_status(_user: RequireUser) -> dict:
    """检查当前 token 状态."""
    env = _read_env()
    token = env.get("CTRADER_ACCESS_TOKEN", "")
    expires = env.get("CTRADER_TOKEN_EXPIRES_AT", "")
    import time
    now = time.time()
    if expires:
        try:
            remaining = float(expires) - now
            return {
                "has_token": bool(token),
                "expires_at": expires,
                "remaining_hours": round(remaining / 3600, 1),
                "expired": remaining < 0,
            }
        except ValueError:
            pass
    return {"has_token": bool(token), "expires_at": None}
