import json
import urllib.parse

from fastapi.testclient import TestClient

from backend.app import app
from backend.api import ctrader_auth
from backend.core.auth import create_token


def _client() -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {create_token('tester')}"})


def test_auth_url_includes_one_time_state(monkeypatch):
    monkeypatch.setattr(ctrader_auth, "_read_env", lambda: {"CTRADER_CLIENT_ID": "cid"})
    ctrader_auth._OAUTH_STATES.clear()

    r = _client().get("/api/ctrader/auth-url")

    assert r.status_code == 200
    url = r.json()["url"]
    params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    state = params.get("state", [""])[0]
    assert state
    assert ctrader_auth._state_digest(state) in ctrader_auth._OAUTH_STATES


def test_oauth_callback_rejects_invalid_state(monkeypatch):
    called = False

    def _fake_urlopen(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("token exchange must not run")

    monkeypatch.setattr(ctrader_auth.urllib.request, "urlopen", _fake_urlopen)

    r = _client().get("/api/ctrader/callback?code=abc&state=bad")

    assert r.status_code == 400
    assert called is False


def test_oauth_callback_rejects_missing_state(monkeypatch):
    called = False

    def _fake_urlopen(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("token exchange must not run")

    monkeypatch.setattr(ctrader_auth.urllib.request, "urlopen", _fake_urlopen)

    r = _client().get("/api/ctrader/callback?code=abc")

    assert r.status_code == 422
    assert called is False


def test_oauth_callback_rejects_expired_state(monkeypatch):
    called = False

    def _fake_urlopen(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("token exchange must not run")

    monkeypatch.setattr(ctrader_auth.urllib.request, "urlopen", _fake_urlopen)
    state = ctrader_auth._new_oauth_state()
    ctrader_auth._OAUTH_STATES[ctrader_auth._state_digest(state)] = 0.0

    r = _client().get(f"/api/ctrader/callback?code=abc&state={urllib.parse.quote(state)}")

    assert r.status_code == 400
    assert r.json()["detail"] == "expired oauth state"
    assert called is False


def test_oauth_callback_consumes_state_and_hides_token_preview(monkeypatch):
    saved: dict[str, str] = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "access_token": "access-secret-value",
                    "refresh_token": "refresh-secret-value",
                    "expires_in": 3600,
                }
            ).encode()

    monkeypatch.setattr(
        ctrader_auth,
        "_read_env",
        lambda: {
            "CTRADER_CLIENT_ID": "cid",
            "CTRADER_CLIENT_SECRET": "secret",
            "CTRADER_ACCESS_TOKEN": saved.get("CTRADER_ACCESS_TOKEN", ""),
            "CTRADER_TOKEN_EXPIRES_AT": saved.get("CTRADER_TOKEN_EXPIRES_AT", ""),
        },
    )
    monkeypatch.setattr(ctrader_auth.urllib.request, "urlopen", lambda *args, **kwargs: _Response())
    monkeypatch.setattr(ctrader_auth, "_update_env", lambda key, value: saved.__setitem__(key, value))
    state = ctrader_auth._new_oauth_state()

    r = _client().get(f"/api/ctrader/callback?code=abc&state={urllib.parse.quote(state)}")

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "access_token" not in body
    assert saved["CTRADER_ACCESS_TOKEN"] == "access-secret-value"
    assert ctrader_auth._state_digest(state) not in ctrader_auth._OAUTH_STATES


def test_token_status_does_not_return_token_preview(monkeypatch):
    monkeypatch.setattr(
        ctrader_auth,
        "_read_env",
        lambda: {"CTRADER_ACCESS_TOKEN": "access-secret-value", "CTRADER_TOKEN_EXPIRES_AT": ""},
    )

    r = _client().get("/api/ctrader/token-status")

    assert r.status_code == 200
    body = r.json()
    assert body["has_token"] is True
    assert "token_preview" not in body
    assert body["_fact"]["contract"] == "ops.ctrader-token-status.v2"
    assert body["_fact"]["state"] == "unknown"
    assert body["_fact"]["reason_code"] == "token_expiry_not_observed"
