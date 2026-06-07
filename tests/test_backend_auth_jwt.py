"""Verify JWT auth roundtrip."""
import time

import jwt
import pytest
from fastapi.testclient import TestClient

from backend.app import app
from backend.core.auth import JWT_ALGORITHM, JWT_EXPIRY_SECONDS, JWT_SECRET, create_token, get_current_user, require_user

client = TestClient(app)


def test_login_returns_jwt():
    r = client.post("/api/auth/login", json={"username": "zhu", "password": "anything"})
    assert r.status_code == 200
    body = r.json()
    assert body["user"] == "zhu"
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == JWT_EXPIRY_SECONDS
    # Verify the token is a real JWT (decodable, has sub/iat/exp)
    payload = jwt.decode(body["token"], JWT_SECRET, algorithms=[JWT_ALGORITHM])
    assert payload["sub"] == "zhu"
    assert "iat" in payload
    assert "exp" in payload


def test_me_no_auth_returns_zhu_backcompat():
    """No header → lenient dep returns "zhu" (Phase 3.14 stub compat)."""
    r = client.get("/api/auth/me")
    assert r.status_code == 200
    body = r.json()
    assert body["user"] == "zhu"
    assert body["authenticated"] is False


def test_me_with_valid_jwt():
    token = create_token("alice")
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert body["user"] == "alice"
    assert body["authenticated"] is True


def test_me_strict_no_auth_401():
    """Strict dep requires valid Bearer token."""
    r = client.get("/api/auth/me-strict")
    assert r.status_code == 401
    assert "WWW-Authenticate" in r.headers


def test_me_strict_invalid_token_401():
    r = client.get("/api/auth/me-strict", headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401


def test_me_strict_expired_token_401():
    """Expired token should raise 401 (not silently fall back)."""
    now = int(time.time())
    payload = {"sub": "alice", "iat": now - 100000, "exp": now - 1}
    expired_token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    r = client.get("/api/auth/me-strict", headers={"Authorization": f"Bearer {expired_token}"})
    assert r.status_code == 401
    body = r.json()
    assert "token_expired" in str(body)


def test_get_current_user_lenient_no_header():
    from backend.core.auth import get_current_user
    assert get_current_user(authorization=None) == "zhu"


def test_get_current_user_lenient_with_valid():
    from backend.core.auth import get_current_user
    token = create_token("bob")
    assert get_current_user(authorization=f"Bearer {token}") == "bob"
