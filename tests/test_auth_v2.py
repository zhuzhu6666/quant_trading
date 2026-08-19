from __future__ import annotations

import hashlib
import time

import pytest
from argon2 import PasswordHasher
from fastapi import HTTPException, Request, Response
from fastapi.testclient import TestClient

from backend.app import app
from backend.core import auth as auth_core
from backend.services.auth_sessions import (
    RefreshSessionError,
    create_refresh_session,
    reset_memory_sessions_for_tests,
    rotate_refresh_session,
    session_is_active,
    step_up_refresh_session,
)


@pytest.fixture(autouse=True)
def _auth_v2_isolation(monkeypatch, tmp_path):
    monkeypatch.setenv("QUANT_AUTH_SESSION_STORE", "memory")
    monkeypatch.setenv("QUANT_AUTH_INSECURE_COOKIE", "1")
    monkeypatch.setenv("QUANT_AUTH_USER", "operator")
    monkeypatch.setenv(
        "QUANT_AUTH_REVOCATION_STATE_PATH",
        str(tmp_path / "auth-revocations.jsonl"),
    )
    monkeypatch.setenv("QUANT_PASSWORD_HASH", PasswordHasher().hash("correct horse"))
    monkeypatch.delenv("QUANT_AUTH_ALLOW_LEGACY_SHA256", raising=False)
    auth_core._JWT_SECRET = None
    auth_core.reset_auth_state_for_tests()
    reset_memory_sessions_for_tests()
    yield
    auth_core.reset_auth_state_for_tests()
    reset_memory_sessions_for_tests()


def test_argon2_login_issues_24_hour_access_and_rotating_refresh():
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": "operator", "password": "correct horse"},
    )
    assert login.status_code == 200
    body = login.json()
    assert body["expires_in"] == 24 * 60 * 60
    assert body["refresh_expires_in"] == 7 * 24 * 3600
    assert body["token"] == body["access_token"]
    assert body["password_rehash_required"] is False

    first_refresh = body["refresh_token"]
    refreshed = client.post("/api/auth/refresh", json={"refresh_token": first_refresh})
    assert refreshed.status_code == 200
    second_refresh = refreshed.json()["refresh_token"]
    assert second_refresh != first_refresh

    replay = client.post("/api/auth/refresh", json={"refresh_token": first_refresh})
    assert replay.status_code == 401
    assert replay.json()["detail"]["error"] == "refresh_token_reuse"

    family_revoked = client.post("/api/auth/refresh", json={"refresh_token": second_refresh})
    assert family_revoked.status_code == 401
    assert family_revoked.json()["detail"]["error"] == "refresh_session_inactive"


def test_tauri_origin_preflight_is_allowed_without_wildcard_cors():
    client = TestClient(app)
    response = client.options(
        "/api/auth/login",
        headers={
            "Origin": "http://tauri.localhost",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type,x-confirm",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://tauri.localhost"
    assert response.headers["access-control-allow-credentials"] == "true"
    assert "POST" in response.headers["access-control-allow-methods"]
    assert "Authorization" in response.headers["access-control-allow-headers"]
    assert "X-Confirm" in response.headers["access-control-allow-headers"]


def test_logout_revokes_access_durably_and_refresh_session():
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": "operator", "password": "correct horse"},
    ).json()
    headers = {"Authorization": f"Bearer {login['access_token']}"}
    claims = auth_core.decode_access_token(login["access_token"])
    logout = client.post(
        "/api/auth/logout",
        headers=headers,
        json={"refresh_token": login["refresh_token"]},
    )
    assert logout.status_code == 200
    assert logout.json()["revoked"] is True
    assert client.get("/api/auth/me", headers=headers).status_code == 401
    with pytest.raises(HTTPException) as expired_access_but_safety_grace:
        auth_core.decode_risk_reduction_token(
            login["access_token"],
            now=int(claims["exp"]) + 1,
        )
    assert expired_access_but_safety_grace.value.detail["error"] == "session_revoked"


def test_logout_revocation_survives_process_cache_reset_and_covers_rotated_family():
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": "operator", "password": "correct horse"},
    ).json()
    login_claims = auth_core.decode_access_token(login["access_token"])
    refreshed = client.post(
        "/api/auth/refresh",
        json={"refresh_token": login["refresh_token"]},
    ).json()
    headers = {"Authorization": f"Bearer {refreshed['access_token']}"}
    claims = auth_core.decode_access_token(refreshed["access_token"])

    logout = client.post(
        "/api/auth/logout",
        headers=headers,
        json={"refresh_token": refreshed["refresh_token"]},
    )
    assert logout.status_code == 200

    # Simulate a fresh backend process: clear only in-memory JWT/ticket state.
    # The append-only logout projection and server-side session rows remain.
    auth_core.reset_auth_state_for_tests()
    with pytest.raises(HTTPException) as normal_access:
        auth_core.decode_access_token(refreshed["access_token"])
    assert normal_access.value.detail["error"] == "session_revoked"
    with pytest.raises(HTTPException) as safety_grace:
        auth_core.decode_risk_reduction_token(
            refreshed["access_token"],
            now=int(claims["exp"]) + 1,
        )
    assert safety_grace.value.detail["error"] == "session_revoked"

    assert login_claims["fid"] == claims["fid"]
    with pytest.raises(HTTPException) as rotated_family_token:
        auth_core.decode_risk_reduction_token(
            login["access_token"],
            now=int(claims["exp"]) + 1,
        )
    assert rotated_family_token.value.detail["error"] == "session_revoked"


def test_logout_durably_revokes_before_session_store_lookup(monkeypatch):
    from backend.api import auth as auth_api

    issued_at = int(time.time())
    grant = create_refresh_session("operator", now=float(issued_at), auth_time=issued_at)
    token = auth_core.create_access_token(
        "operator",
        session_id=grant.session_id,
        family_id=grant.family_id,
        auth_time=grant.auth_time,
        now=issued_at,
    )
    claims = auth_core.decode_access_token(token)

    def _session_store_unavailable(_session_id: str):
        raise RuntimeError("postgres unavailable")

    monkeypatch.setattr(auth_api, "session_family_ids", _session_store_unavailable)
    with pytest.raises(HTTPException) as unavailable:
        auth_api.logout(
            req=auth_api.LogoutRequest(),
            request=None,
            response=Response(),
            claims=claims,
            refresh_cookie=None,
        )
    assert unavailable.value.status_code == 503

    # Simulate a new process. The fsync'd projection, not the in-memory cache,
    # must still deny the token's risk-reduction grace.
    auth_core.reset_auth_state_for_tests()
    with pytest.raises(HTTPException) as revoked:
        auth_core.decode_risk_reduction_token(token)
    assert revoked.value.detail["error"] == "session_revoked"


def test_ws_ticket_is_one_time_and_expires():
    ticket, _ = auth_core.create_ws_ticket(subject="operator", now=100.0)
    assert auth_core.consume_ws_ticket(ticket, now=101.0)["subject"] == "operator"
    with pytest.raises(HTTPException) as replay:
        auth_core.consume_ws_ticket(ticket, now=102.0)
    assert replay.value.detail["error"] == "invalid_ws_ticket"

    expired, _ = auth_core.create_ws_ticket(subject="operator", now=100.0)
    with pytest.raises(HTTPException) as exc:
        auth_core.consume_ws_ticket(expired, now=131.0)
    assert exc.value.detail["error"] == "expired_ws_ticket"


def test_recent_step_up_requires_active_session(monkeypatch):
    monkeypatch.delenv("QUANT_AUTH_ALLOW_STATELESS_STEP_UP", raising=False)
    base = int(time.time())
    grant = create_refresh_session("operator", now=float(base), auth_time=base)
    token = auth_core.create_access_token(
        "operator",
        session_id=grant.session_id,
        auth_time=grant.auth_time,
        now=base,
    )
    monkeypatch.setattr(auth_core.time, "time", lambda: float(base + 100))
    assert auth_core.require_recent_step_up(f"Bearer {token}") == "operator"

    monkeypatch.setattr(auth_core.time, "time", lambda: float(base + 301))
    with pytest.raises(HTTPException) as stale:
        auth_core.require_recent_step_up(f"Bearer {token}")
    assert stale.value.detail["error"] == "step_up_required"


def _step_up_request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/auth/step-up",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )


def test_step_up_persists_auth_time_and_refresh_preserves_it():
    first = create_refresh_session("operator", now=1_000.0, auth_time=1_000)
    stepped_up = step_up_refresh_session(
        first.session_id,
        subject="operator",
        family_id=first.family_id,
        now=1_301.0,
    )
    assert stepped_up.auth_time == 1_301
    assert stepped_up.family_id == first.family_id

    rotated = rotate_refresh_session(first.refresh_token, now=1_400.0)
    assert rotated.auth_time == 1_301
    assert rotated.family_id == first.family_id


def test_step_up_route_reauthenticates_current_session_without_new_refresh_token():
    from backend.api import auth as auth_api

    issued_at = int(time.time()) - auth_core.STEP_UP_MAX_AGE_SECONDS - 1
    grant = create_refresh_session("operator", now=float(issued_at), auth_time=issued_at)
    stale_access = auth_core.create_access_token(
        "operator",
        session_id=grant.session_id,
        family_id=grant.family_id,
        auth_time=issued_at,
        now=issued_at,
    )
    claims = auth_core.decode_access_token(stale_access)
    response = auth_api.step_up(
        req=auth_api.StepUpRequest(password="correct horse"),
        request=_step_up_request(),
        claims=claims,
    )

    fresh_claims = auth_core.decode_access_token(response.access_token)
    assert response.token == response.access_token
    assert response.session_id == grant.session_id
    assert fresh_claims["sid"] == grant.session_id
    assert fresh_claims["fid"] == grant.family_id
    assert int(fresh_claims["auth_time"]) == response.auth_time
    assert auth_core.require_recent_step_up(f"Bearer {response.access_token}") == "operator"

    # The existing refresh credential remains valid and carries the committed
    # step-up time into its rotated child session.
    rotated = rotate_refresh_session(grant.refresh_token)
    assert rotated.auth_time == response.auth_time


def test_step_up_wrong_password_does_not_advance_session_auth_time():
    from backend.api import auth as auth_api

    grant = create_refresh_session("operator", now=1_000.0, auth_time=1_000)
    claims = {
        "sub": "operator",
        "sid": grant.session_id,
        "fid": grant.family_id,
    }
    with pytest.raises(HTTPException) as denied:
        auth_api.step_up(
            req=auth_api.StepUpRequest(password="wrong password"),
            request=_step_up_request(),
            claims=claims,
        )
    assert denied.value.status_code == 403
    assert denied.value.detail["error"] == "invalid_step_up_credentials"

    rotated = rotate_refresh_session(grant.refresh_token, now=1_100.0)
    assert rotated.auth_time == 1_000


def test_step_up_persistence_failure_blocks_new_access_token(monkeypatch):
    from backend.api import auth as auth_api

    grant = create_refresh_session("operator")

    def _unavailable(*_args, **_kwargs):
        raise RuntimeError("postgres unavailable")

    minted: list[str] = []
    monkeypatch.setattr(auth_api, "step_up_refresh_session", _unavailable)
    monkeypatch.setattr(
        auth_api,
        "create_access_token",
        lambda *_args, **_kwargs: minted.append("unexpected") or "unexpected",
    )
    with pytest.raises(HTTPException) as unavailable:
        auth_api.step_up(
            req=auth_api.StepUpRequest(password="correct horse"),
            request=_step_up_request(),
            claims={
                "sub": "operator",
                "sid": grant.session_id,
                "fid": grant.family_id,
            },
        )
    assert unavailable.value.status_code == 503
    assert unavailable.value.detail["error"] == "step_up_session_unavailable"
    assert minted == []


def test_step_up_rejects_subject_or_family_mismatch():
    grant = create_refresh_session("operator", now=1_000.0, auth_time=1_000)
    with pytest.raises(RefreshSessionError) as subject_mismatch:
        step_up_refresh_session(
            grant.session_id,
            subject="other",
            family_id=grant.family_id,
            now=1_100.0,
        )
    assert subject_mismatch.value.code == "step_up_session_inactive"

    with pytest.raises(RefreshSessionError) as family_mismatch:
        step_up_refresh_session(
            grant.session_id,
            subject="operator",
            family_id="wrong-family",
            now=1_100.0,
        )
    assert family_mismatch.value.code == "step_up_session_mismatch"


def test_step_up_legacy_sha256_requires_explicit_compatibility(monkeypatch):
    from backend.api import auth as auth_api

    grant = create_refresh_session("operator")
    claims = {
        "sub": "operator",
        "sid": grant.session_id,
        "fid": grant.family_id,
    }
    monkeypatch.setenv(
        "QUANT_PASSWORD_HASH",
        hashlib.sha256(b"correct horse").hexdigest(),
    )
    monkeypatch.delenv("QUANT_AUTH_ALLOW_LEGACY_SHA256", raising=False)
    with pytest.raises(HTTPException) as disabled:
        auth_api.step_up(
            req=auth_api.StepUpRequest(password="correct horse"),
            request=_step_up_request(),
            claims=claims,
        )
    assert disabled.value.detail["error"] == "auth_not_configured"

    monkeypatch.setenv("QUANT_AUTH_ALLOW_LEGACY_SHA256", "1")
    response = auth_api.step_up(
        req=auth_api.StepUpRequest(password="correct horse"),
        request=_step_up_request(),
        claims=claims,
    )
    assert response.password_rehash_required is True


def test_expired_access_retains_only_local_risk_reduction_authority():
    issued_at = int(time.time()) - auth_core.JWT_EXPIRY_SECONDS - 5
    token = auth_core.create_access_token(
        "operator",
        session_id="pg-session-can-be-unavailable",
        auth_time=issued_at,
        now=issued_at,
    )
    authorization = f"Bearer {token}"

    with pytest.raises(HTTPException) as normal_access:
        auth_core.require_user(authorization)
    assert normal_access.value.detail["error"] == "token_expired"

    # This path verifies signature/scope/lifetime and local revocation only;
    # it performs no refresh-session/PostgreSQL lookup.
    assert auth_core.require_risk_reduction_user(authorization) == "operator"

    with pytest.raises(HTTPException) as new_risk:
        auth_core.require_recent_step_up(authorization)
    assert new_risk.value.detail["error"] == "token_expired"


def test_session_store_outage_blocks_normal_access_but_not_risk_reduction(monkeypatch):
    from backend.services import auth_sessions

    issued_at = int(time.time())
    token = auth_core.create_access_token(
        "operator",
        session_id="pg-session-down",
        family_id="family-pg-session-down",
        now=issued_at,
    )

    def _unavailable(*_args, **_kwargs):
        raise RuntimeError("postgres unavailable")

    monkeypatch.setattr(auth_sessions, "session_is_active", _unavailable)
    with pytest.raises(HTTPException) as normal_access:
        auth_core.require_user(f"Bearer {token}")
    assert normal_access.value.status_code == 503
    assert normal_access.value.detail["error"] == "session_authority_unavailable"
    assert auth_core.require_risk_reduction_user(f"Bearer {token}") == "operator"


def test_revocation_projection_read_failure_does_not_block_risk_reduction(monkeypatch):
    from backend.services import auth_revocations

    issued_at = int(time.time())
    token = auth_core.create_access_token(
        "operator",
        session_id="revocation-ledger-down",
        family_id="family-revocation-ledger-down",
        now=issued_at,
    )

    def _unavailable(**_kwargs):
        raise auth_revocations.AuthRevocationStoreError("ledger unavailable")

    monkeypatch.setattr(auth_revocations, "auth_authority_is_revoked", _unavailable)
    with pytest.raises(HTTPException) as normal_access:
        auth_core.require_user(f"Bearer {token}")
    assert normal_access.value.status_code == 503
    assert normal_access.value.detail["error"] == "session_authority_unavailable"
    assert auth_core.require_risk_reduction_user(f"Bearer {token}") == "operator"


def test_risk_reduction_grace_honors_local_logout_revocation():
    issued_at = int(time.time()) - auth_core.JWT_EXPIRY_SECONDS - 5
    token = auth_core.create_access_token(
        "operator",
        session_id="revoked-session",
        now=issued_at,
    )
    auth_core.revoke_access_session_locally("revoked-session")

    with pytest.raises(HTTPException) as revoked:
        auth_core.require_risk_reduction_user(f"Bearer {token}")
    assert revoked.value.detail["error"] == "session_revoked"


def test_expired_access_can_emergency_when_pg_audit_is_unavailable(monkeypatch, tmp_path):
    from backend.api import live as live_api

    issued_at = int(time.time()) - auth_core.JWT_EXPIRY_SECONDS - 5
    token = auth_core.create_access_token("operator", session_id="pg-down", now=issued_at)
    user = auth_core.require_risk_reduction_user(f"Bearer {token}")
    expected = {
        "schema_version": "live_emergency_close.v2",
        "status": "completed",
        "ok": True,
        "emergency_id": "expired-access-pg-down",
        "remaining_position_ids": [],
        "resume_required": True,
    }
    monkeypatch.setenv("QUANT_SAFETY_STATE_DIR", str(tmp_path / "safety"))
    monkeypatch.setattr(live_api, "emergency_close", lambda *_args, **_kwargs: expected)
    monkeypatch.setattr(
        live_api,
        "record_api_mutation",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("postgres unavailable")),
    )

    result = live_api.emergency(
        _user=user,
        req=live_api.EmergencyCloseRequest(broker="ctrader"),
        x_confirm="emergency",
    )

    assert result == expected


def test_legacy_sha256_needs_explicit_switch(monkeypatch):
    encoded = hashlib.sha256(b"correct horse").hexdigest()
    monkeypatch.setenv("QUANT_PASSWORD_HASH", encoded)
    monkeypatch.delenv("QUANT_AUTH_ALLOW_LEGACY_SHA256", raising=False)
    with pytest.raises(auth_core.AuthConfigError):
        auth_core.validate_auth_config()

    monkeypatch.setenv("QUANT_AUTH_ALLOW_LEGACY_SHA256", "1")
    auth_core.validate_auth_config()


def test_refresh_service_detects_direct_replay_and_revokes_family():
    first = create_refresh_session("operator")
    second = rotate_refresh_session(first.refresh_token)
    assert session_is_active(second.session_id, subject="operator") is True
    with pytest.raises(RefreshSessionError) as replay:
        rotate_refresh_session(first.refresh_token)
    assert replay.value.code == "refresh_token_reuse"
    assert session_is_active(second.session_id, subject="operator") is False
