from __future__ import annotations


def test_ops_autonomy_proposals_routes_use_registry_service(monkeypatch, auth_client):
    calls: dict[str, object] = {}

    class FakeProposalRegistryService:
        def latest(self, *, limit: int, status: str, refresh: bool):
            calls["latest"] = {"limit": limit, "status": status, "refresh": refresh}
            return {
                "ok": True,
                "schema_version": "proposal_registry_list.v1",
                "items": [{"proposal_id": "policy_suggestion:ps1"}],
                "summary": {"proposal_count": 1},
            }

        def get(self, proposal_id: str):
            calls["get"] = proposal_id
            return {"ok": True, "proposal": {"proposal_id": proposal_id}}

        def refresh(self, *, limit: int):
            calls["refresh"] = limit
            return {"ok": True, "refreshed_count": limit}

        def review(self, proposal_id: str, *, actor: str, decision: str, route: str, notes: str):
            calls["review"] = {
                "proposal_id": proposal_id,
                "actor": actor,
                "decision": decision,
                "route": route,
                "notes": notes,
            }
            return {"ok": True, "status": "reviewed", "proposal_id": proposal_id}

    monkeypatch.setattr("backend.api.ops.ProposalRegistryService", FakeProposalRegistryService)

    listing = auth_client.get("/api/ops/autonomy/proposals?limit=3&status=active&refresh=true")
    item = auth_client.get("/api/ops/autonomy/proposals/policy_suggestion%3Aps1")
    refresh = auth_client.post("/api/ops/autonomy/proposals/refresh?limit=7")
    review = auth_client.post(
        "/api/ops/autonomy/proposals/policy_suggestion%3Aps1/review",
        json={"actor": "test", "decision": "reviewed", "route": "observe", "notes": "ok"},
    )

    assert listing.status_code == 200
    assert listing.json()["schema_version"] == "ops_autonomy_proposals.v1"
    assert calls["latest"] == {"limit": 3, "status": "active", "refresh": True}
    assert item.json()["proposal"]["proposal"]["proposal_id"] == "policy_suggestion:ps1"
    assert refresh.json()["refresh"]["refreshed_count"] == 7
    assert review.json()["review"]["status"] == "reviewed"
    assert calls["review"]["decision"] == "reviewed"


def test_ops_live_autonomy_routes_use_governed_service(monkeypatch, auth_client):
    calls: dict[str, object] = {}

    class FakeBackendReadinessService:
        def build(self):
            return {"generated_at": 10.0, "ready_for_frontend": True}

    class FakeLiveAutonomyService:
        def status(self, *, readiness, refresh_proposals: bool):
            calls["status"] = {"readiness": readiness, "refresh_proposals": refresh_proposals}
            return {"ok": True, "autonomy_mode": "live_candidate"}

        def evaluate(self, *, readiness, refresh_proposals: bool, persist: bool, actor: str, reason: str):
            calls["evaluate"] = {
                "readiness": readiness,
                "refresh_proposals": refresh_proposals,
                "persist": persist,
                "actor": actor,
                "reason": reason,
            }
            return {"ok": False, "status": "blocked", "blockers": [{"component": "replay"}]}

        def unlock(self, *, actor: str, reason: str, confirm: bool, readiness):
            calls["unlock"] = {"actor": actor, "reason": reason, "confirm": confirm, "readiness": readiness}
            return {"ok": True, "status": "unlocked"}

        def revoke(self, *, actor: str, reason: str):
            calls["revoke"] = {"actor": actor, "reason": reason}
            return {"ok": True, "status": "revoked"}

    monkeypatch.setattr("backend.api.ops.BackendReadinessService", FakeBackendReadinessService)
    monkeypatch.setattr("backend.api.ops.LiveAutonomyService", FakeLiveAutonomyService)

    status = auth_client.get("/api/ops/autonomy/live-status?refresh_proposals=true")
    evaluate = auth_client.post(
        "/api/ops/autonomy/live-unlock/evaluate",
        json={"actor": "test", "reason": "dry run"},
    )
    unlock = auth_client.post(
        "/api/ops/autonomy/live-unlock",
        json={"actor": "test", "reason": "confirmed", "confirm": True},
    )
    revoke = auth_client.post(
        "/api/ops/autonomy/live-unlock/revoke",
        json={"actor": "test", "reason": "stop"},
    )

    assert status.status_code == 200
    assert status.json()["live_autonomy"]["autonomy_mode"] == "live_candidate"
    assert calls["status"]["refresh_proposals"] is True
    assert evaluate.json()["evaluation"]["status"] == "blocked"
    assert calls["evaluate"]["persist"] is True
    assert unlock.json()["unlock"]["status"] == "unlocked"
    assert calls["unlock"]["confirm"] is True
    assert revoke.json()["revoke"]["status"] == "revoked"
