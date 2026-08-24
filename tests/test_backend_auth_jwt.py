"""Verify JWT auth roundtrip."""
import hashlib
import time

import jwt
import pytest
from fastapi.testclient import TestClient

from backend.app import app
from backend.core.auth import JWT_ALGORITHM, JWT_EXPIRY_SECONDS, create_token, get_current_user, require_user

# Set a known password hash for testing
_PW = "test_pass_123"
_HASH = hashlib.sha256(_PW.encode()).hexdigest()
_TEST_JWT_SECRET = "test-jwt-secret-2026-do-not-use-in-prod"


@pytest.fixture(autouse=True)
def _set_test_password(monkeypatch):
    """Override the password hash so the login test can use a known password."""
    monkeypatch.setenv("QUANT_JWT_SECRET", _TEST_JWT_SECRET)
    monkeypatch.setenv("QUANT_PASSWORD_HASH", _HASH)
    monkeypatch.setenv("QUANT_AUTH_USER", "zhu")
    import backend.core.auth as auth_core

    previous_secret = auth_core._JWT_SECRET
    auth_core._JWT_SECRET = None
    from backend.api.auth import _LOGIN_ATTEMPTS

    _LOGIN_ATTEMPTS.clear()
    yield
    _LOGIN_ATTEMPTS.clear()
    # Other API modules create compatibility tokens during collection.  Do not
    # leave this module's temporary signing key cached after the fixture exits.
    auth_core._JWT_SECRET = previous_secret


client = TestClient(app)


def test_login_returns_jwt():
    r = client.post("/api/auth/login", json={"username": "zhu", "password": _PW})
    assert r.status_code == 200
    body = r.json()
    assert body["user"] == "zhu"
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == JWT_EXPIRY_SECONDS
    # Verify the token is a real JWT (decodable, has sub/iat/exp)
    payload = jwt.decode(body["token"], _TEST_JWT_SECRET, algorithms=[JWT_ALGORITHM])
    assert payload["sub"] == "zhu"
    assert "iat" in payload
    assert "exp" in payload


def test_login_requires_auth_environment(monkeypatch):
    import backend.core.auth as auth_core

    monkeypatch.delenv("QUANT_JWT_SECRET", raising=False)
    auth_core._JWT_SECRET = None
    r = client.post("/api/auth/login", json={"username": "zhu", "password": _PW})

    assert r.status_code == 500
    assert r.json()["detail"]["error"] == "auth_not_configured"


def test_login_requires_password_hash(monkeypatch):
    monkeypatch.delenv("QUANT_PASSWORD_HASH", raising=False)
    r = client.post("/api/auth/login", json={"username": "zhu", "password": _PW})

    assert r.status_code == 500
    assert r.json()["detail"]["error"] == "auth_not_configured"


def test_login_uses_env_overrides(monkeypatch):
    pw = "override_pw"
    monkeypatch.setenv("QUANT_AUTH_USER", "alice")
    monkeypatch.setenv("QUANT_PASSWORD_HASH", hashlib.sha256(pw.encode()).hexdigest())
    r = client.post("/api/auth/login", json={"username": "alice", "password": pw})
    assert r.status_code == 200
    assert r.json()["user"] == "alice"


def test_login_rate_limit_blocks_repeated_attempts(monkeypatch):
    monkeypatch.setenv("QUANT_LOGIN_RATE_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("QUANT_LOGIN_RATE_WINDOW_SECONDS", "60")
    headers = {"X-Forwarded-For": "203.0.113.10"}
    for _ in range(3):
        r = client.post(
            "/api/auth/login",
            json={"username": "zhu", "password": "wrong"},
            headers=headers,
        )
        assert r.status_code == 401

    r = client.post(
        "/api/auth/login",
        json={"username": "zhu", "password": "wrong"},
        headers=headers,
    )
    assert r.status_code == 429


def test_me_no_auth_returns_401():
    """No Authorization header -> 401 (strict auth)."""
    r = client.get("/api/auth/me")
    assert r.status_code == 401


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
    expired_token = jwt.encode(payload, _TEST_JWT_SECRET, algorithm=JWT_ALGORITHM)
    r = client.get("/api/auth/me-strict", headers={"Authorization": f"Bearer {expired_token}"})
    assert r.status_code == 401
    body = r.json()
    assert "token_expired" in str(body)


def test_me_strict_missing_subject_401():
    now = int(time.time())
    payload = {"iat": now, "exp": now + 60}
    token = jwt.encode(payload, _TEST_JWT_SECRET, algorithm=JWT_ALGORITHM)

    r = client.get("/api/auth/me-strict", headers={"Authorization": f"Bearer {token}"})

    assert r.status_code == 401
    assert r.json()["detail"]["error"] == "invalid_token"


def test_get_current_user_no_header_raises_401():
    from backend.core.auth import get_current_user
    import pytest
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        get_current_user(authorization=None)
    assert exc_info.value.status_code == 401


def test_get_current_user_with_valid():
    from backend.core.auth import get_current_user
    token = create_token("bob")
    assert get_current_user(authorization=f"Bearer {token}") == "bob"


def test_openapi_declares_bearer_for_protected_routes_only():
    """OpenAPI must describe auth without changing the runtime auth contract."""
    schema = app.openapi()

    assert schema["components"]["securitySchemes"]["BearerAuth"] == {
        "type": "http",
        "scheme": "bearer",
        "description": "Auth v2 access JWT supplied as Authorization: Bearer <token>.",
    }
    for path in ("/api/auth/me", "/api/auth/me-strict", "/api/auth/step-up", "/api/live/account", "/api/live/start"):
        operations = schema["paths"][path].values()
        assert any(operation.get("security") == [{"BearerAuth": []}] for operation in operations)

    for path in ("/api/health", "/api/auth/login", "/api/auth/refresh", "/api/ctrader/callback"):
        for operation in schema["paths"][path].values():
            assert "security" not in operation
