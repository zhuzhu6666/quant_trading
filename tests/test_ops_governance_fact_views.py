import ast
from pathlib import Path

from backend.services.ops_governance_fact_views import (
    autonomy_scope_enforcement_fact_payload,
    factor_catalog_fact_payload,
    governance_mutation_fact_payload,
    incident_control_status_fact_payload,
    ledger_read_fact_payload,
    persisted_record_fact_payload,
    proposal_refresh_fact_payload,
    release_approval_trail_fact_payload,
    runtime_config_projection_observation,
    unverified_compat_fact_payload,
    v15_phase0_fact_payload,
)


def test_phase0_fact_uses_readiness_observation_and_preserves_legacy_shape():
    payload = v15_phase0_fact_payload(
        {
            "ok": True,
            "schema_version": "ops_v15_phase0_completion.v1",
            "phase0": {"implementation_complete": True},
            "readiness_generated_at": 100.0,
        },
        now=110.0,
    )

    assert payload["ok"] is True
    assert payload["phase0"]["implementation_complete"] is True
    assert payload["_fact"]["contract"] == "ops.v15-phase0-completion.v2"
    assert payload["_fact"]["state"] == "known"
    assert payload["_fact"]["observed_at"] == 100.0


def test_incident_control_never_uses_request_generated_updated_at_as_fact():
    legacy = {
        "ok": True,
        "incident_control": {
            "mode": "normal",
            # RuntimeIncidentControlService keeps this legacy request-time
            # field.  It is not an authoritative projection timestamp.
            "updated_at": 100.0,
            "local_safety_latch": {"state": "not_set", "active": False},
        },
    }
    unknown = incident_control_status_fact_payload(legacy, now=100.0)
    known = incident_control_status_fact_payload(
        legacy,
        projection_observed_at=95.0,
        now=100.0,
    )

    assert unknown["_fact"]["state"] == "unknown"
    assert unknown["_fact"]["observed_at"] is None
    assert known["_fact"]["state"] == "known"
    assert known["_fact"]["observed_at"] == 95.0
    assert known["_fact"]["components"]["projection"]["state"] == "known"


def test_runtime_overlay_observation_is_read_only_and_never_runs_ddl(monkeypatch):
    calls = []

    class FakeConn:
        def execute(self, sql, params):
            calls.append((sql, params))
            return self

        def fetchone(self):
            return {"updated_at": 99.0}

        def close(self):
            calls.append(("closed", ()))

    monkeypatch.setattr("backend.core.db.get_state_pg_conn", lambda **_kwargs: FakeConn())

    result = runtime_config_projection_observation()

    assert result["ok"] is True
    assert result["observed_at"] == 99.0
    assert "SELECT updated_at" in calls[0][0]
    assert "CREATE" not in calls[0][0].upper()


def test_governance_mutation_requires_committed_intent_not_legacy_applied():
    legacy_applied = governance_mutation_fact_payload(
        {
            "ok": True,
            "result": {
                "ok": True,
                "status": "applied",
                "mutation": {
                    "ok": True,
                    "status": "applied",
                    "updated_at": 99.0,
                },
                "local_safety_latch": {"active": True, "created_at": 99.5},
            },
        },
        contract="ops.incident-control-mutation.v2",
        result_path=("result",),
        now=100.0,
    )
    committed = governance_mutation_fact_payload(
        {
            "ok": True,
            "result": {
                "ok": True,
                "mutation": {
                    "ok": True,
                    "status": "committed",
                    "mutation_id": "gmut-1",
                    "snapshot": {"created_at": 99.0},
                },
            },
        },
        contract="ops.incident-control-mutation.v2",
        result_path=("result",),
        now=100.0,
    )

    assert legacy_applied["_fact"]["state"] == "unknown"
    assert legacy_applied["_fact"]["components"]["local_safety_latch"]["state"] == "known"
    assert committed["_fact"]["state"] == "known"
    assert committed["_fact"]["observed_at"] == 99.0


def test_persisted_record_requires_identity_and_real_commit_timestamp():
    missing_timestamp = persisted_record_fact_payload(
        {"ok": True, "event": {"event_id": "evt-1"}},
        contract="ops.audit-event.v2",
        source="state_v1.audit_event",
        record_path=("event",),
        id_fields=("event_id",),
        observed_paths=(("created_at",),),
        now=100.0,
    )
    committed = persisted_record_fact_payload(
        {"ok": True, "event": {"event_id": "evt-1", "created_at": 99.0}},
        contract="ops.audit-event.v2",
        source="state_v1.audit_event",
        record_path=("event",),
        id_fields=("event_id",),
        observed_paths=(("created_at",),),
        now=100.0,
    )

    assert missing_timestamp["_fact"]["state"] == "unknown"
    assert missing_timestamp["_fact"]["reason_code"] == "committed_observation_timestamp_missing"
    assert committed["_fact"]["state"] == "known"


def test_unverified_compat_fact_never_promotes_request_time_to_known():
    payload = unverified_compat_fact_payload(
        {
            "ok": True,
            "generated_at": 99.0,
            "status": "healthy",
        },
        contract="ops.compat-example.v2",
        reason_code="authoritative_observation_not_exposed",
        now=100.0,
    )

    assert payload["ok"] is True
    assert payload["_fact"]["state"] == "unknown"
    assert payload["_fact"]["source"] == "none"
    assert payload["_fact"]["observed_at"] is None
    assert payload["_fact"]["reason_code"] == "authoritative_observation_not_exposed"


def test_persisted_record_can_require_artifact_commit_evidence():
    missing_artifact = persisted_record_fact_payload(
        {
            "report": {
                "replay_run_id": "parity-1",
                "created_at": 99.0,
                "artifact_path": "",
                "report_artifact_hash": "",
            }
        },
        contract="ops.parity-replay-run.v2",
        source="filesystem:parity-replay-artifact",
        record_path=("report",),
        id_fields=("replay_run_id",),
        observed_paths=(("created_at",),),
        required_fields=("artifact_path", "report_artifact_hash"),
        now=100.0,
    )
    persisted = persisted_record_fact_payload(
        {
            "report": {
                "replay_run_id": "parity-1",
                "created_at": 99.0,
                "artifact_path": "/tmp/parity-1.json",
                "report_artifact_hash": "abc123",
            }
        },
        contract="ops.parity-replay-run.v2",
        source="filesystem:parity-replay-artifact",
        record_path=("report",),
        id_fields=("replay_run_id",),
        observed_paths=(("created_at",),),
        required_fields=("artifact_path", "report_artifact_hash"),
        now=100.0,
    )

    assert missing_artifact["_fact"]["state"] == "unknown"
    assert missing_artifact["_fact"]["reason_code"] == "durable_commit_not_confirmed"
    assert persisted["_fact"]["state"] == "known"


def test_ledger_reads_distinguish_missing_stale_and_source_error():
    missing = ledger_read_fact_payload(
        {"ok": False, "ledger": {"status": "missing_rows", "items": []}},
        contract="ops.example-ledger.v2",
        source="state_v1.example",
        entity_path=("ledger",),
        observed_paths=(),
        item_paths=(("items",),),
        now=100.0,
    )
    stale = ledger_read_fact_payload(
        {"ok": True, "ledger": {"items": [{"created_at": 1.0}]}},
        contract="ops.example-ledger.v2",
        source="state_v1.example",
        entity_path=("ledger",),
        observed_paths=(),
        item_paths=(("items",),),
        now=200.0,
    )
    failed = ledger_read_fact_payload(
        {"ok": False, "ledger": {"status": "database_error", "error": "pg down"}},
        contract="ops.example-ledger.v2",
        source="state_v1.example",
        entity_path=("ledger",),
        now=100.0,
    )

    assert missing["_fact"]["state"] == "unknown"
    assert stale["_fact"]["state"] == "stale"
    assert failed["_fact"]["state"] == "error"


def test_scope_enforcement_only_reports_known_for_committed_or_noop_result():
    pending = autonomy_scope_enforcement_fact_payload(
        {
            "ok": True,
            "enforcement_event": {
                "event_id": "evt-1",
                "status": "applied",
                "created_at": 99.0,
                "mutation": {"status": "applied"},
            },
        },
        now=100.0,
    )
    committed = autonomy_scope_enforcement_fact_payload(
        {
            "ok": True,
            "enforcement_event": {
                "event_id": "evt-2",
                "status": "applied",
                "created_at": 99.0,
                "mutation": {"status": "committed", "mutation_id": "gmut-2"},
            },
        },
        now=100.0,
    )
    noop = autonomy_scope_enforcement_fact_payload(
        {
            "ok": True,
            "enforcement_event": {
                "event_id": "evt-3",
                "status": "already_at_or_stricter",
                "created_at": 99.0,
            },
        },
        now=100.0,
    )

    assert pending["_fact"]["state"] == "unknown"
    assert committed["_fact"]["state"] == "known"
    assert noop["_fact"]["state"] == "known"


def test_factor_catalog_uses_snapshot_or_domain_item_timestamp_only():
    live_unknown = factor_catalog_fact_payload(
        {"snapshot_mode": "live", "items": [{"factor_id": "f1"}]},
        now=100.0,
    )
    live_known = factor_catalog_fact_payload(
        {
            "snapshot_mode": "live",
            "items": [{"factor_id": "f1", "health_updated_at": 99.0}],
        },
        now=100.0,
    )
    snapshot = factor_catalog_fact_payload(
        {
            "snapshot_mode": "latest",
            "source": "factor_catalog_snapshot",
            "created_at": 98.0,
            "items": [],
        },
        now=100.0,
    )

    assert live_unknown["_fact"]["state"] == "unknown"
    assert live_known["_fact"]["state"] == "known"
    assert snapshot["_fact"]["state"] == "known"


def test_proposal_refresh_requires_post_write_projection_reconcile():
    payload = {"ok": True, "refresh": {"ok": True, "refreshed_count": 1}}
    missing = proposal_refresh_fact_payload(payload, reconciled_projection={}, now=100.0)
    reconciled = proposal_refresh_fact_payload(
        payload,
        reconciled_projection={"ok": True, "items": [{"proposal_id": "p1", "updated_at": 99.0}]},
        now=100.0,
    )

    assert missing["_fact"]["state"] == "unknown"
    assert reconciled["_fact"]["state"] == "known"


def test_release_approval_empty_trail_uses_authoritative_release_row():
    payload = {
        "ok": True,
        "approval_trail": {"ok": True, "run_id": "r1", "events": []},
    }
    result = release_approval_trail_fact_payload(
        payload,
        release={"ok": True, "run_id": "r1", "updated_at": 99.0},
        now=100.0,
    )

    assert result["_fact"]["state"] == "known"
    assert result["_fact"]["observed_at"] == 99.0


def test_v16_ops_routes_attach_endpoint_specific_facts(monkeypatch):
    from backend.api import ops as ops_api

    observed = 1_900_000_000.0

    class FakeReadiness:
        def build(self):
            return {"generated_at": observed, "brain_state": {}}

    class FakeBrainState:
        def latest_snapshot(self):
            return {"ok": True, "snapshot_id": "brain-1", "created_at": observed}

    class FakeMemory:
        def latest_indexed(self, *, limit):
            return {
                "ok": True,
                "items": [{"memory_id": "memory-1", "last_used_at": observed}],
            }

    class FakeMemoryIntegrity:
        def build(self):
            return {
                "ok": True,
                "status": "healthy",
                "observed_at": observed,
                "boundary": {"read_only": True, "affects_trading": False},
            }

    class FakeActionPlans:
        def latest_plans(self, *, limit):
            return {
                "ok": True,
                "plans": [{"plan_id": "plan-1", "created_at": observed}],
            }

    class FakeActionPlanEvals:
        def latest_evals(self, *, limit):
            return {
                "ok": True,
                "evals": [{"eval_id": "eval-1", "created_at": observed}],
            }

    class FakeLowImpact:
        def latest_executions(self, *, limit):
            return {
                "ok": True,
                "executions": [{"execution_id": "exec-1", "created_at": observed}],
            }

        def execute_latest(self, **_kwargs):
            return {
                "ok": True,
                "created_at": observed,
                "executions": [{"execution_id": "exec-2", "created_at": observed}],
            }

    class FakeMediumImpact:
        def latest_governance(self, *, limit):
            return {
                "ok": True,
                "items": [{"governance_id": "gov-1", "created_at": observed}],
            }

        def materialize_latest(self, **_kwargs):
            return {
                "ok": True,
                "created_at": observed,
                "items": [{"governance_id": "gov-2", "created_at": observed}],
            }

    class FakeReviews:
        def latest_reviews(self, *, limit):
            return {
                "ok": True,
                "items": [{"review_id": "review-1", "created_at": observed}],
            }

        def review_latest(self, **_kwargs):
            return {
                "ok": True,
                "created_at": observed,
                "items": [{"review_id": "review-2", "created_at": observed}],
            }

    class FakeGuardrails:
        def latest_guardrails(self, *, limit):
            return {
                "ok": True,
                "items": [{"guardrail_id": "guard-1", "created_at": observed}],
            }

        def evaluate(self, **_kwargs):
            return {
                "guardrail_id": "guard-2",
                "created_at": observed,
                "updated_at": observed,
            }

        def tighten(self, **_kwargs):
            return {
                "ok": True,
                "incident_control_result": {
                    "mutation": {
                        "status": "committed",
                        "mutation_id": "gmut-guard",
                        "snapshot": {"created_at": observed},
                    }
                },
            }

    monkeypatch.setattr(ops_api, "BackendReadinessService", FakeReadiness)
    monkeypatch.setattr(ops_api, "BrainStateService", FakeBrainState)
    monkeypatch.setattr(ops_api, "BrainMemoryService", FakeMemory)
    monkeypatch.setattr(ops_api, "MemoryIntegrityReportService", FakeMemoryIntegrity)
    monkeypatch.setattr(ops_api, "BrainActionPlannerService", FakeActionPlans)
    monkeypatch.setattr(ops_api, "BrainActionPlanEvaluatorService", FakeActionPlanEvals)
    monkeypatch.setattr(ops_api, "BrainLowImpactExecutorService", FakeLowImpact)
    monkeypatch.setattr(ops_api, "BrainMediumImpactGovernanceService", FakeMediumImpact)
    monkeypatch.setattr(ops_api, "BrainGovernanceCandidateReviewService", FakeReviews)
    monkeypatch.setattr(ops_api, "BrainLiveReadyGuardrailService", FakeGuardrails)
    monkeypatch.setattr(
        "backend.services.ops_governance_fact_views.time.time", lambda: observed + 1.0
    )

    responses = [
        ops_api.get_brain_state(None),
        ops_api.get_brain_memory(None),
        ops_api.get_brain_action_plans(None),
        ops_api.get_brain_action_plan_evals(None),
        ops_api.get_brain_low_impact_executions(None),
        ops_api.run_brain_low_impact_execution(ops_api.BrainLowImpactExecutionRequest(), None),
        ops_api.get_brain_medium_impact_governance(None),
        ops_api.materialize_brain_medium_impact_governance(
            ops_api.BrainMediumImpactGovernanceRequest(), None
        ),
        ops_api.get_brain_governance_candidate_reviews(None),
        ops_api.review_brain_governance_candidates(
            ops_api.BrainGovernanceCandidateReviewRequest(), None
        ),
        ops_api.get_brain_live_ready_guardrails(None),
        ops_api.evaluate_brain_live_ready_guardrail(
            ops_api.BrainLiveReadyGuardrailEvaluateRequest(), None
        ),
        ops_api.tighten_brain_live_ready_guardrail(
            ops_api.BrainLiveReadyGuardrailTightenRequest(), None
        ),
    ]

    contracts = [response["_fact"]["contract"] for response in responses]
    assert len(set(contracts)) == len(contracts)
    assert all(response["_fact"]["state"] == "known" for response in responses)
    assert contracts[:4] == [
        "ops.v16-brain-state.v2",
        "ops.v16-brain-memory.v2",
        "ops.v16-action-plans.v2",
        "ops.v16-action-plan-evals.v2",
    ]
    assert responses[1]["memory"]["integrity"]["status"] == "healthy"


def test_autonomy_proposal_routes_do_not_claim_unreconciled_refresh(monkeypatch):
    from backend.api import ops as ops_api

    observed = 1_900_000_000.0

    class FakeProposals:
        def latest(self, *, limit, status, refresh):
            return {
                "ok": True,
                "items": [{"proposal_id": "p1", "updated_at": observed}],
            }

        def get(self, proposal_id):
            return {
                "ok": True,
                "proposal": {
                    "proposal_id": proposal_id,
                    "created_at": observed,
                    "updated_at": observed,
                },
            }

        def refresh(self, *, limit):
            return {"ok": True, "refreshed_count": 1}

        def review(self, proposal_id, **_kwargs):
            return {
                "ok": True,
                "status": "reviewed",
                "proposal_id": proposal_id,
                "review": {"reviewed_at": observed},
            }

    monkeypatch.setattr(ops_api, "ProposalRegistryService", FakeProposals)
    monkeypatch.setattr(
        "backend.services.ops_governance_fact_views.time.time", lambda: observed + 1.0
    )

    listing = ops_api.get_autonomy_proposals(None)
    item = ops_api.get_autonomy_proposal("p1", None)
    refresh = ops_api.refresh_autonomy_proposals(None)
    review = ops_api.review_autonomy_proposal(
        "p1", ops_api.ProposalReviewRequest(), None
    )

    assert listing["_fact"]["state"] == "known"
    assert item["_fact"]["state"] == "known"
    assert refresh["_fact"]["state"] == "unknown"
    assert refresh["_fact"]["reason_code"] == "proposal_projection_reconcile_missing"
    assert review["_fact"]["state"] == "known"


def test_v15_ops_routes_attach_facts_without_changing_legacy_fields(monkeypatch):
    from backend.api import ops as ops_api

    observed = 1_900_000_000.0

    class FakeReadiness:
        def build(self):
            return {"generated_at": observed, "autonomy_health": {"posture": "full"}}

    class FakePhase0:
        def build(self, *, readiness):
            return {"implementation_complete": True, "readiness": readiness}

    class FakeIncident:
        def status(self):
            return {
                "mode": "normal",
                "local_safety_latch": {"active": False, "state": "not_set"},
                "updated_at": observed + 1.0,
            }

        def set_mode(self, *_args, **_kwargs):
            return {
                "ok": True,
                "mutation": {
                    "status": "committed",
                    "mutation_id": "gmut-incident",
                    "snapshot": {"created_at": observed},
                },
            }

        def latest_playbook(self):
            return {"ok": True, "playbook_id": "pb-1", "created_at": observed}

    class FakeAutonomyHealth:
        def latest_scope_approval(self):
            return {"ok": True, "event_id": "approval-1", "created_at": observed}

        def record_scope_approval(self, **_kwargs):
            return {"ok": True, "event_id": "approval-2", "created_at": observed}

        def latest_scope_enforcement(self):
            return {"ok": True, "event_id": "enforce-1", "created_at": observed}

        def enforce_scope_recommendation(self, **_kwargs):
            return {
                "ok": True,
                "event_id": "enforce-2",
                "status": "already_at_or_stricter",
                "created_at": observed,
            }

    class FakeRelease:
        def latest_release(self):
            return {"ok": True, "run_id": "rel-1", "updated_at": observed}

        def start_release(self, **_kwargs):
            return {
                "ok": True,
                "run_id": "rel-2",
                "created_at": observed,
                "updated_at": observed,
            }

        def approval_trail(self, run_id):
            return {"ok": True, "run_id": run_id, "events": []}

        def get_release(self, run_id):
            return {"ok": True, "run_id": run_id, "updated_at": observed}

        def record_approval_event(self, run_id, **_kwargs):
            return {
                "ok": True,
                "run_id": run_id,
                "event_id": "release-approval-1",
                "created_at": observed,
            }

    monkeypatch.setattr(ops_api, "BackendReadinessService", FakeReadiness)
    monkeypatch.setattr(ops_api, "V15Phase0CompletionService", FakePhase0)
    monkeypatch.setattr(ops_api, "RuntimeIncidentControlService", FakeIncident)
    monkeypatch.setattr(
        ops_api,
        "runtime_config_projection_observation",
        lambda: {"ok": True, "observed_at": observed, "error": ""},
    )
    monkeypatch.setattr(ops_api, "AutonomyHealthService", FakeAutonomyHealth)
    monkeypatch.setattr(ops_api, "ReleaseControlService", FakeRelease)
    monkeypatch.setattr(
        "backend.services.ops_governance_fact_views.time.time", lambda: observed + 1.0
    )

    responses = [
        ops_api.get_incident_control(None),
        ops_api.set_incident_control(
            ops_api.IncidentControlRequest(mode="no_new_risk"), None
        ),
        ops_api.get_latest_incident_playbook(None),
        ops_api.get_latest_autonomy_scope_approval(None),
        ops_api.record_autonomy_scope_approval(
            ops_api.AutonomyScopeApprovalRequest(), None
        ),
        ops_api.get_latest_autonomy_scope_enforcement(None),
        ops_api.enforce_autonomy_scope(
            ops_api.AutonomyScopeEnforcementRequest(), None
        ),
        ops_api.get_v15_phase0_completion(None),
        ops_api.get_latest_release_run(None),
        ops_api.start_release_run(ops_api.ReleaseRunStartRequest(), None),
        ops_api.get_release_approval_trail("rel-1", None),
        ops_api.record_release_approval_event(
            "rel-1", ops_api.ReleaseApprovalEventRequest(), None
        ),
    ]

    assert all("_fact" in response for response in responses)
    assert all(response["_fact"]["state"] == "known" for response in responses)
    assert responses[0]["incident_control"]["mode"] == "normal"
    assert responses[-1]["approval_event"]["event_id"] == "release-approval-1"


def test_factor_catalog_route_adds_fact_for_live_and_snapshot(monkeypatch):
    from backend.api import factor_v4

    observed = 1_900_000_000.0
    monkeypatch.setattr(
        "backend.services.factor_catalog.build_factor_catalog",
        lambda: [{"factor_id": "f1", "health_updated_at": observed}],
    )
    monkeypatch.setattr(
        "backend.services.factor_catalog.latest_factor_catalog_snapshot",
        lambda: {
            "ok": True,
            "snapshot_id": "snap-1",
            "source": "state_v1.factor_catalog_snapshot",
            "created_at": observed,
            "items": [],
        },
    )
    monkeypatch.setattr(
        "backend.services.ops_governance_fact_views.time.time", lambda: observed + 1.0
    )

    live = factor_v4.get_factor_catalog(None)
    snapshot = factor_v4.get_factor_catalog(None, snapshot="latest")

    assert live["snapshot_mode"] == "live"
    assert live["_fact"]["state"] == "known"
    assert snapshot["snapshot_mode"] == "latest"
    assert snapshot["_fact"]["state"] == "known"


def test_all_ops_route_return_paths_are_endpoint_fact_wrapped():
    """Prevent a new compatibility route from bypassing the fact boundary."""

    from backend.api import ops as ops_api

    tree = ast.parse(Path(ops_api.__file__).read_text(encoding="utf-8"))
    endpoints = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        route_decorators = [
            decorator
            for decorator in node.decorator_list
            if isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and isinstance(decorator.func.value, ast.Name)
            and decorator.func.value.id == "router"
        ]
        if route_decorators:
            endpoints.append(node)

    # The duplicate /api/ops/replay/parity-run route was removed when
    # backtesting converged on POST /api/backtest/run.
    assert len(endpoints) >= 67
    for endpoint in endpoints:
        returns = [item for item in ast.walk(endpoint) if isinstance(item, ast.Return)]
        assert returns, endpoint.name
        for statement in returns:
            value = statement.value
            assert isinstance(value, ast.Call), (
                f"{endpoint.name}:{statement.lineno} bypasses endpoint _fact"
            )
            if isinstance(value.func, ast.Name):
                outer_call = value.func.id
            elif isinstance(value.func, ast.Attribute):
                outer_call = value.func.attr
            else:
                outer_call = ""
            direct_fact_helper = outer_call.endswith("_fact_payload")
            attach_fact_adapter = outer_call == "dict" and any(
                isinstance(item, ast.Call)
                and isinstance(item.func, ast.Name)
                and item.func.id == "attach_fact"
                for item in ast.walk(value)
            )
            assert direct_fact_helper or attach_fact_adapter, (
                f"{endpoint.name}:{statement.lineno} bypasses endpoint _fact"
            )

        if endpoint.name != "get_backend_readiness":
            names = {
                item.func.id
                for item in ast.walk(endpoint)
                if isinstance(item, ast.Call) and isinstance(item.func, ast.Name)
            }
            assert "readiness_fact_payload" not in names, endpoint.name


def test_ops_compat_and_ledger_routes_keep_endpoint_level_truth(monkeypatch):
    from backend.api import ops as ops_api

    observed = 1_900_000_000.0

    class FakeScorecard:
        def scorecard(self, *, limit):
            return {
                "ok": True,
                "generated_at": observed + 100.0,
                "items": [
                    {"source_agent": "v16_brain", "latest_activity_at": observed}
                ],
            }

        def chain_health(self, *, limit):
            return {
                "ok": True,
                "status": "ok",
                "generated_at": observed,
            }

    class FakeCommands:
        def latest_commands(self, *, limit):
            return {
                "ok": True,
                "commands": [{"command_id": "cmd-1", "created_at": observed}],
            }

    class FakePruning:
        def materialize_latest(self, **_kwargs):
            return {"ok": True, "created_at": observed, "items": []}

        def promote_ready(self, **_kwargs):
            return {"ok": True, "created_at": observed, "items": []}

        def bridge_ready_candidates(self, **_kwargs):
            return {"ok": True, "created_at": observed, "items": []}

    monkeypatch.setattr(ops_api, "AgentScorecardService", FakeScorecard)
    monkeypatch.setattr(ops_api, "V16BrainOrchestratorService", FakeCommands)
    monkeypatch.setattr(ops_api, "FactorPruningGovernanceService", FakePruning)
    monkeypatch.setattr(
        "backend.services.ops_governance_fact_views.time.time", lambda: observed + 1.0
    )

    scorecard = ops_api.get_agent_scorecard(None)
    chain = ops_api.get_agent_chain_health(None)
    commands = ops_api.get_brain_commands(None)
    materialize = ops_api.materialize_factor_pruning_governance(
        ops_api.FactorPruningGovernanceRequest(), None
    )
    promote = ops_api.promote_factor_pruning_governance(
        ops_api.FactorPruningGovernancePromoteRequest(), None
    )
    bridge = ops_api.bridge_factor_pruning_governance(
        ops_api.FactorPruningGovernanceBridgeRequest(), None
    )

    assert scorecard["_fact"]["contract"] == "ops.agent-scorecard.v2"
    assert scorecard["_fact"]["state"] == "known"
    assert scorecard["_fact"]["observed_at"] == observed
    assert chain["_fact"]["contract"] == "ops.agent-chain-health.v2"
    assert chain["_fact"]["state"] == "unknown"
    assert chain["_fact"]["source"] == "none"
    assert commands["_fact"]["contract"] == "ops.v16-brain-commands.v2"
    assert commands["_fact"]["state"] == "known"
    assert all(
        response["_fact"]["state"] == "unknown"
        for response in (materialize, promote, bridge)
    )
    assert all(
        response["_fact"]["source"] == "none"
        for response in (materialize, promote, bridge)
    )


def test_replay_ops_facts_distinguish_durable_reports_from_previews(monkeypatch):
    from backend.api import ops as ops_api

    observed = 1_900_000_000.0

    class FakeReplay:
        @staticmethod
        def _report(run_id):
            return {
                "replay_run_id": run_id,
                "created_at": observed,
                "replay_error": "",
            }

        def latest_report(self):
            return self._report("replay-latest")

        def run_factor_gate_risk_replay(self, **_kwargs):
            return self._report("replay-run")

        def run_bar_replay_evidence(self, **_kwargs):
            return self._report("replay-bar-run")

        def run_bar_window_preview(self, **_kwargs):
            return self._report("replay-preview")

        def list_bar_preview_decisions(self, **_kwargs):
            return {
                "items": [{"decision_id": "d-1", "decision_ts": observed}]
            }

    monkeypatch.setattr(ops_api, "ReplayHarnessService", FakeReplay)
    monkeypatch.setattr(
        "backend.services.ops_governance_fact_views.time.time", lambda: observed + 1.0
    )

    latest = ops_api.get_latest_replay_report(None)
    run = ops_api.run_replay_harness(None)
    bar_run = ops_api.run_bar_replay_harness(None)
    preview = ops_api.run_bar_replay_preview(None)
    decisions = ops_api.list_bar_replay_decisions(None)

    assert all(
        response["_fact"]["state"] == "known"
        for response in (latest, run, bar_run, decisions)
    )
    assert preview["_fact"]["state"] == "unknown"
    assert preview["_fact"]["source"] == "none"
    assert preview["_fact"]["reason_code"] == "replay_preview_is_not_persisted"
