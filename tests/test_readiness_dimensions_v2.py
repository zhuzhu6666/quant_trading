from __future__ import annotations

from backend.services.backend_readiness import BackendReadinessService
from backend.services.live_readiness import build_live_readiness
from config import runtime_config as rc


def _dimensions(**overrides):
    payload = {
        "is_runtime_state_db": True,
        "global_blockers": [],
        "live_status": {
            "ctrader": {"status": "connected"},
            "loop": {"running": True, "accepting_new_risk": True},
            "readiness": {"ok": True},
        },
        "execution_semantics": {"blocking_components": []},
        "startup_status": {"blocking_components": []},
        "incident_control": {"mode": "normal"},
        "runtime_weight_integrity": {"ok": True},
        "factor_blend_health": {"ok": True, "status": "healthy"},
        "governance": {
            "factor_governance_runtime": {"enabled": True, "ok": True, "status": "fresh"}
        },
        "config_runtime_drift": {"drift": False, "semantic_drift": False},
        "audit_health": {"ok": True},
        "replay": {"ok": True},
        "stability": {
            "runtime_config_snapshot": {"ok": True},
            "runtime_config_overlay": {"ok": True, "suspicious": False},
        },
        "learning_worker": {
            "ok": True,
            "mutation_capability": {"available": True, "status": "available"},
        },
        "risk_metrics": {
            "ok": True,
            "status": "known",
            "var_status": "known",
        },
    }
    payload.update(overrides)
    return BackendReadinessService._build_readiness_dimensions(**payload)


def test_readiness_dimensions_are_independent_authorities() -> None:
    rc.reset_for_tests()
    try:
        result = _dimensions(global_blockers=[{"component": "frontend_projection"}])
    finally:
        rc.reset_for_tests()

    assert result["ready_for_frontend"] is False
    assert result["ready_for_live_execution"] is True
    assert result["ready_for_live_alpha"] is True
    assert result["ready_for_autonomous_mutation"] is True
    assert result["ready_for_release"] is True
    assert result["authorization_boundary"]["frontend_readiness_authorizes_controls"] is False
    assert result["authorization_boundary"]["frontend_readiness_authorizes_release"] is False


def test_incident_mode_blocks_new_risk_but_not_frontend_or_release() -> None:
    rc.reset_for_tests()
    try:
        result = _dimensions(incident_control={"mode": "no_new_risk"})
    finally:
        rc.reset_for_tests()

    assert result["ready_for_frontend"] is True
    assert result["ready_for_live_execution"] is False
    assert result["ready_for_live_alpha"] is False
    assert result["ready_for_release"] is True


def test_worker_mutation_circuit_only_blocks_autonomous_mutation() -> None:
    rc.reset_for_tests()
    try:
        result = _dimensions(
            learning_worker={
                "ok": True,
                "mutation_capability": {
                    "available": False,
                    "status": "circuit_open",
                },
            }
        )
    finally:
        rc.reset_for_tests()

    assert result["ready_for_frontend"] is True
    assert result["ready_for_live_execution"] is True
    assert result["ready_for_live_alpha"] is True
    assert result["ready_for_autonomous_mutation"] is False
    assert result["ready_for_release"] is True


def test_directional_portfolio_degradation_blocks_only_live_alpha() -> None:
    rc.reset_for_tests()
    try:
        result = _dimensions(
            factor_blend_health={
                "ok": False,
                "status": "critical",
                "directional_portfolio_guard": {
                    "schema_version": "directional_portfolio_guard.v1",
                    "status": "degraded",
                    "voter_count": 2,
                    "independent_group_count": 2,
                    "reason_codes": ["insufficient_directional_alpha_voters"],
                },
            }
        )
    finally:
        rc.reset_for_tests()

    assert result["ready_for_frontend"] is True
    assert result["ready_for_live_execution"] is True
    assert result["ready_for_live_alpha"] is False
    assert result["ready_for_release"] is True
    blockers = result["blockers"]["live_alpha"]
    assert any(item["reason"] == "directional_portfolio_degraded" for item in blockers)


def test_stale_factor_projection_uses_one_stable_live_alpha_blocker() -> None:
    rc.reset_for_tests()
    try:
        result = _dimensions(
            factor_blend_health={
                "ok": False,
                "status": "critical",
                "projection_status": "stale",
                "directional_portfolio_guard": {},
            }
        )
    finally:
        rc.reset_for_tests()

    reasons = [
        item["reason"] for item in result["blockers"]["live_alpha"]
        if item["component"] == "factor_blend_health"
    ]
    assert reasons == ["runtime_factor_selection_projection_unavailable"]


def test_blocked_factor_governance_runtime_blocks_autonomous_mutation() -> None:
    rc.reset_for_tests()
    try:
        result = _dimensions(
            governance={
                "factor_governance_runtime": {
                    "enabled": True,
                    "ok": False,
                    "status": "blocked_by_v16_command",
                }
            }
        )
    finally:
        rc.reset_for_tests()

    assert result["ready_for_autonomous_mutation"] is False
    reasons = {
        item["reason"] for item in result["blockers"]["autonomous_mutation"]
    }
    assert "governance_runtime_not_ready" in reasons


def test_unknown_canonical_var_is_projected_as_live_readiness_blocker() -> None:
    rc.reset_for_tests()
    try:
        result = _dimensions(
            risk_metrics={
                "ok": False,
                "status": "warming_up",
                "var_status": "warming_up",
            }
        )
    finally:
        rc.reset_for_tests()

    assert result["ready_for_frontend"] is True
    assert result["ready_for_live_execution"] is False
    assert result["ready_for_live_alpha"] is False
    assert result["ready_for_autonomous_mutation"] is True
    reasons = {
        item["reason"] for item in result["blockers"]["live_execution"]
    }
    assert "canonical_forward_var_not_ready" in reasons


def test_live_loop_readiness_does_not_duplicate_one_failed_admission() -> None:
    rc.reset_for_tests()
    try:
        result = _dimensions(
            live_status={
                "ctrader": {"status": "connected"},
                "loop": {
                    "running": True,
                    "accepting_new_risk": False,
                    "blockers": ["no_new_risk_latched"],
                },
                "readiness": {
                    "ok": False,
                    "reasons": ["loop_not_accepting_new_risk"],
                },
            }
        )
    finally:
        rc.reset_for_tests()

    loop_blockers = [
        item for item in result["blockers"]["live_execution"]
        if item["component"] == "live_loop"
    ]
    assert len(loop_blockers) == 1
    assert loop_blockers[0]["reason"] == "not_ready"


def test_live_readiness_keeps_specific_safety_reasons_without_generic_duplicates() -> None:
    result = build_live_readiness(
        loop={
            "running": True,
            "phase": "running",
            "ready": True,
            "accepting_new_risk": False,
            "blockers": ["no_new_risk_latched"],
            "safety_heartbeat_age_sec": 1.0,
            "safety": {
                "accepting_new_risk": False,
                "reconciliation_state": "stale",
                "unknown_execution_count": 1,
                "blockers": ["positions_reconciliation_failed", "unknown_execution"],
            },
        },
        state={
            "diag": {"bridge_ready": True},
            "account_reconciled": {"ok": True},
            "account_updated_at": 100.0,
            "account_reconcile_id": "account-1",
            "positions_updated_at": 100.0,
            "positions_reconcile_id": "positions-1",
        },
        positions=[],
        checked_at=100.0,
        v2_active=True,
        broker_status="connected",
        broker_error=None,
    )

    assert "no_new_risk_latched" in result["reasons"]
    assert "positions_reconciliation_failed" in result["reasons"]
    assert "unknown_execution" in result["reasons"]
    assert "loop_not_accepting_new_risk" not in result["reasons"]
    assert "safety_position_reconcile_not_fresh" not in result["reasons"]
    assert "unresolved_execution_intent" not in result["reasons"]


def test_global_operator_pause_blocks_all_mode_autonomous_expansion() -> None:
    rc.reset_for_tests()
    rc.patch({"governance_expansion_paused": True, "autonomy_mode": "demo_autonomous"})
    try:
        result = _dimensions()
        assert rc.autonomy_expansion_freeze_applies(rc.shared()) is True
    finally:
        rc.reset_for_tests()

    assert result["ready_for_autonomous_mutation"] is False
    reasons = {
        item["reason"] for item in result["blockers"]["autonomous_mutation"]
    }
    assert "operator_pause_active" in reasons
