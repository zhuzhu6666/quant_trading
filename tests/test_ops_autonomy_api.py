from __future__ import annotations


def test_ops_agent_authority_route_returns_registry(monkeypatch, auth_client):
    class FakeAgentAuthorityRegistryService:
        def list_agents(self):
            return {
                "ok": True,
                "schema_version": "agent_authority_registry.v1",
                "registry_version": "agent_authority_registry.v1",
                "registered_agents": 6,
                "sources": [],
            }

        def status(self):
            return {
                "ok": True,
                "schema_version": "agent_authority_status.v1",
                "status": "ok",
                "registered_agents": 6,
                "unknown_sources": [],
                "contract_violations": [],
            }

    monkeypatch.setattr("backend.api.ops.AgentAuthorityRegistryService", FakeAgentAuthorityRegistryService)

    response = auth_client.get("/api/ops/agent-authority")

    assert response.status_code == 200
    assert response.json()["schema_version"] == "ops_agent_authority.v1"
    assert response.json()["agent_authority"]["registered_agents"] == 6
    assert response.json()["status"]["status"] == "ok"


def test_ops_agent_scorecard_routes_return_read_models(monkeypatch, auth_client):
    class FakeAgentBriefingContextService:
        def build(self, *, limit: int):
            return {"ok": True, "schema_version": "agent_briefing_context.v1", "limit": limit}

    class FakeAgentScorecardService:
        def scorecard(self, *, limit: int):
            return {"ok": True, "schema_version": "agent_scorecard.v1", "items": [], "summary": {"limit": limit}}

        def latest_trade_attributions(self, *, limit: int, include_external_links: bool = False):
            return {
                "ok": True,
                "schema_version": "agent_trade_attribution.v1",
                "items": [],
                "summary": {"limit": limit, "include_external_links": include_external_links},
            }

        def chain_health(self, *, limit: int):
            return {"ok": True, "schema_version": "agent_chain_health.v1", "status": "ok", "limit": limit}

    monkeypatch.setattr("backend.api.ops.AgentBriefingContextService", FakeAgentBriefingContextService)
    monkeypatch.setattr("backend.api.ops.AgentScorecardService", FakeAgentScorecardService)

    scorecard = auth_client.get("/api/ops/agent-scorecard?limit=3")
    briefing = auth_client.get("/api/ops/agent-briefing?limit=6")
    attribution = auth_client.get("/api/ops/agent-trade-attribution?limit=4")
    health = auth_client.get("/api/ops/agent-chain-health?limit=5")

    assert scorecard.status_code == 200
    assert scorecard.json()["schema_version"] == "ops_agent_scorecard.v1"
    assert scorecard.json()["scorecard"]["summary"]["limit"] == 3
    assert briefing.json()["schema_version"] == "ops_agent_briefing.v1"
    assert briefing.json()["briefing"]["limit"] == 6
    assert attribution.json()["schema_version"] == "ops_agent_trade_attribution.v1"
    assert attribution.json()["trade_attribution"]["summary"]["limit"] == 4
    assert health.json()["schema_version"] == "ops_agent_chain_health.v1"
    assert health.json()["agent_chain_health"]["limit"] == 5


def test_ops_autonomous_evolution_cycle_uses_light_readiness_by_default(monkeypatch, auth_client):
    class FakeRunner:
        def build_light_readiness(self):
            return {
                "schema_version": "light_test.v1",
                "governance": {"autonomy_mode": "demo_nursery"},
                "autonomy_health": {"posture": "full"},
            }

    class FakeReadiness:
        def build(self):
            raise AssertionError("full readiness should be opt-in for this endpoint")

    class FakeCycle:
        def status(self, *, readiness, refresh_proposals: bool, include_chain_health: bool):
            return {
                "ok": True,
                "schema_version": "autonomous_evolution_cycle.v1",
                "status": "ready_for_guarded_demo_apply",
                "stable_demo_nursery_ready": True,
                "readiness_schema": readiness.get("schema_version"),
                "include_chain_health": include_chain_health,
            }

    monkeypatch.setattr("backend.api.ops.AutonomousEvolutionNurseryRunner", FakeRunner)
    monkeypatch.setattr("backend.api.ops.BackendReadinessService", FakeReadiness)
    monkeypatch.setattr("backend.api.ops.AutonomousEvolutionCycleService", FakeCycle)

    response = auth_client.get("/api/ops/autonomy/evolution-cycle")

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "ops_autonomous_evolution_cycle.v1"
    assert payload["readiness_mode"] == "light"
    assert payload["cycle"]["readiness_schema"] == "light_test.v1"
    assert payload["cycle"]["include_chain_health"] is False


def test_ops_autonomous_evolution_run_requires_confirmation_for_blocking_apply(auth_client):
    response = auth_client.post(
        "/api/ops/autonomy/evolution-cycle/run",
        json={"apply_when_ready": True},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "blocking_apply_requires_confirmation"


def test_ops_autonomous_demo_apply_plan_and_step_routes(monkeypatch, auth_client):
    calls: dict[str, object] = {}

    class FakeStepper:
        def plan(self):
            return {
                "ok": True,
                "schema_version": "autonomous_demo_apply_plan.v1",
                "steps": [{"step": "governor_review"}],
            }

        def run_step(self, step: str, *, limit, confirm_step: bool, actor: str):
            calls["step"] = {
                "step": step,
                "limit": limit,
                "confirm_step": confirm_step,
                "actor": actor,
            }
            return {
                "ok": True,
                "schema_version": "autonomous_demo_apply_step.v1",
                "status": "completed",
                "step": step,
            }

    monkeypatch.setattr("backend.api.ops.AutonomousDemoApplyStepper", FakeStepper)

    plan_response = auth_client.get("/api/ops/autonomy/demo-apply-plan")
    step_response = auth_client.post(
        "/api/ops/autonomy/demo-apply-step",
        json={"step": "governor_review", "limit": 4, "confirm_step": True, "actor": "test"},
    )

    assert plan_response.status_code == 200
    assert plan_response.json()["schema_version"] == "ops_autonomous_demo_apply_plan.v1"
    assert step_response.status_code == 200
    assert step_response.json()["schema_version"] == "ops_autonomous_demo_apply_step.v1"
    assert calls["step"] == {
        "step": "governor_review",
        "limit": 4,
        "confirm_step": True,
        "actor": "test",
    }


def test_ops_autonomous_demo_apply_step_can_run_async(monkeypatch, auth_client):
    calls: dict[str, object] = {}

    class FakeStepper:
        def start_background_step(self, step: str, *, limit, confirm_step: bool, actor: str):
            calls["start"] = {
                "step": step,
                "limit": limit,
                "confirm_step": confirm_step,
                "actor": actor,
            }
            return {
                "ok": True,
                "schema_version": "autonomous_demo_apply_step.v1",
                "status": "accepted",
                "step": step,
                "limit": limit,
                "experiment_id": "exp_async",
                "run_id": "run_async",
            }

        def run_accepted_step(self, *, step: str, experiment_id: str, run_id: str, limit: int):
            calls["background"] = {
                "step": step,
                "experiment_id": experiment_id,
                "run_id": run_id,
                "limit": limit,
            }

    monkeypatch.setattr("backend.api.ops.AutonomousDemoApplyStepper", FakeStepper)

    response = auth_client.post(
        "/api/ops/autonomy/demo-apply-step",
        json={
            "step": "factor_pruning_bridge",
            "limit": 1,
            "confirm_step": True,
            "actor": "test",
            "run_async": True,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["step_result"]["status"] == "accepted"
    assert body["step_result"]["run_id"] == "run_async"
    assert calls["start"] == {
        "step": "factor_pruning_bridge",
        "limit": 1,
        "confirm_step": True,
        "actor": "test",
    }
    assert calls["background"] == {
        "step": "factor_pruning_bridge",
        "experiment_id": "exp_async",
        "run_id": "run_async",
        "limit": 1,
    }


def test_ops_autonomous_demo_apply_step_requires_step_confirmation(monkeypatch, auth_client):
    class FakeStepper:
        def run_step(self, step: str, *, limit, confirm_step: bool, actor: str):
            return {
                "ok": False,
                "schema_version": "autonomous_demo_apply_step.v1",
                "status": "confirmation_required",
                "step": step,
            }

    monkeypatch.setattr("backend.api.ops.AutonomousDemoApplyStepper", FakeStepper)

    response = auth_client.post(
        "/api/ops/autonomy/demo-apply-step",
        json={"step": "governor_review"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["status"] == "confirmation_required"


def test_ops_brain_candidate_submit_requires_review_by_default(monkeypatch, auth_client):
    calls: dict[str, object] = {}

    class FakeReviewService:
        @staticmethod
        def boundary():
            return {"review_only": True}

        def review_candidate(self, candidate_id: str, *, run_llm: bool, llm_dry_run: bool, persist: bool):
            calls["review"] = {
                "candidate_id": candidate_id,
                "run_llm": run_llm,
                "llm_dry_run": llm_dry_run,
                "persist": persist,
            }
            return {
                "ok": True,
                "status": "reviewed",
                "review": {
                    "candidate_id": candidate_id,
                    "review_status": "needs_evidence",
                    "bridge_ready": False,
                    "evidence_gaps": ["agent_negative_effect_history_requires_counter_evidence"],
                },
            }

    class FakeCandidateService:
        def submit_candidate_to_policy_suggestion(self, candidate_id: str, *, actor: str):
            calls["submit"] = {"candidate_id": candidate_id, "actor": actor}
            return {"ok": True, "status": "submitted_to_policy_suggestion"}

    monkeypatch.setattr("backend.api.ops.BrainGovernanceCandidateReviewService", FakeReviewService)
    monkeypatch.setattr("backend.api.ops.BrainGovernanceCandidateService", FakeCandidateService)

    response = auth_client.post(
        "/api/ops/brain/governance-candidates/candidate_bad/submit",
        json={"actor": "test"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["submit_result"]["status"] == "blocked_candidate_review"
    assert "agent_negative_effect_history_requires_counter_evidence" in payload["submit_result"]["evidence_gaps"]
    assert calls["review"]["candidate_id"] == "candidate_bad"
    assert "submit" not in calls


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
