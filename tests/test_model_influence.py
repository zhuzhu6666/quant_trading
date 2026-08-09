import json
import sqlite3
from types import SimpleNamespace

from backend.services.model_influence import (
    MODEL_STAGES,
    ModelInfluenceService,
    default_model_influence_config,
    normalized_model_influence_config,
)
from backend.services.model_influence_governance import ModelInfluenceGovernanceService
from backend.services.v16_brain_orchestrator import V16BrainOrchestratorService
from risk.policy_service import RiskPolicyService


def _cfg(model_type: str, policy: dict):
    config = default_model_influence_config()
    config["models"][model_type] = {
        **config["models"][model_type],
        "stage": "demo_canary",
        "feature_schema_version": "pit.v2",
        "artifact_sha256": "sha",
        **policy,
    }
    return SimpleNamespace(
        autonomy_mode="demo_nursery",
        runtime_incident_mode="normal",
        demo_model_influence_enabled=True,
        model_influence_config=config,
    )


def test_position_model_can_only_tighten_or_bounded_reduce(tmp_path):
    service = ModelInfluenceService(tmp_path / "state.db")
    cfg = _cfg("position_quality_lightgbm", {
        "allowed_effects": ["tighten", "reduce"],
        "tighten_threshold": 0.7,
        "reduce_threshold": 0.88,
        "max_reduce_fraction": 0.25,
    })
    fused = service.fuse_position(
        verdict={"action": "hold", "confidence": 0.8, "recommended_controls": {}},
        advisory={"ok": True, "model_version": "2.0", "exit_risk_score": 0.95},
        position_id="p1",
        cfg=cfg,
        tighten_controls={"stop_loss": 10.0},
    )
    assert fused["action"] == "reduce"
    assert fused["recommended_controls"]["reduce_fraction"] == 0.25
    assert fused["recommended_controls"]["allow_full_close_fallback"] is False

    # The same model artifact cannot repeatedly reduce the same position.
    second = service.fuse_position(
        verdict={"action": "hold", "confidence": 0.8, "recommended_controls": {}},
        advisory={"ok": True, "model_version": "2.0", "exit_risk_score": 0.95},
        position_id="p1",
        cfg=cfg,
        tighten_controls={"stop_loss": 10.0},
    )
    assert second["action"] == "hold"
    assert second["model_influence"]["applied"] is False


def test_position_model_cannot_bypass_supervisor_evidence_boundary(tmp_path):
    service = ModelInfluenceService(tmp_path / "state.db")
    cfg = _cfg("position_quality_lightgbm", {
        "allowed_effects": ["tighten", "reduce"],
        "tighten_threshold": 0.7,
        "reduce_threshold": 0.88,
        "max_reduce_fraction": 0.25,
    })
    fused = service.fuse_position(
        verdict={
            "action": "hold",
            "confidence": 0.8,
            "evidence": {"model_action_boundary_ready": False},
            "recommended_controls": {},
        },
        advisory={"ok": True, "model_version": "2.0", "exit_risk_score": 0.95},
        position_id="p-boundary",
        cfg=cfg,
        tighten_controls={"stop_loss": 10.0},
    )

    assert fused["action"] == "hold"
    assert fused["model_influence"]["applied"] is False
    assert fused["model_influence"]["reason"] == "position_supervisor_evidence_boundary"


def test_open_model_is_veto_only_and_inactive_model_does_not_duplicate_audit(tmp_path):
    db_path = tmp_path / "state.db"
    service = ModelInfluenceService(db_path)
    cfg = _cfg("open_quality_lightgbm", {"allowed_effects": ["veto"], "veto_threshold": 0.25})
    result = service.evaluate_open_veto(
        score={"ok": True, "model_version": "2.0", "quality_score": 0.1},
        subject_id="bar:1",
        cfg=cfg,
        rule_decision={"passed": True},
    )
    assert result["passed"] is False
    assert result["reason"] == "model_open_quality_veto"

    cfg.demo_model_influence_enabled = False
    inactive = service.evaluate_open_veto(
        score={"ok": True, "quality_score": 0.1},
        subject_id="bar:2",
        cfg=cfg,
        rule_decision={"passed": True},
    )
    assert inactive["passed"] is True
    count = sqlite3.connect(str(db_path)).execute("SELECT COUNT(*) FROM model_influence_decision").fetchone()[0]
    assert count == 1


def test_model_influence_is_inactive_outside_demo_autonomy(tmp_path):
    service = ModelInfluenceService(tmp_path / "state.db")
    cfg = _cfg("open_quality_lightgbm", {"allowed_effects": ["veto"], "veto_threshold": 0.25})
    cfg.autonomy_mode = "manual"

    assert ModelInfluenceService.active_policy("open_quality_lightgbm", cfg) is None
    result = service.evaluate_open_veto(
        score={"ok": True, "quality_score": 0.1},
        subject_id="manual:1",
        cfg=cfg,
        rule_decision={"passed": True},
    )
    assert result["passed"] is True
    assert result["reason"] == "model_open_veto_not_applied"


def test_model_promotion_policy_requires_demo_pit_gate_and_safe_capabilities():
    verdict = RiskPolicyService().evaluate("promote_model_influence", {
        "autonomy_mode": "demo_nursery",
        "runtime_incident_mode": "normal",
        "demo_model_influence_enabled": True,
        "model_type": "open_quality_lightgbm",
        "feature_schema_version": "pit.v2",
        "promotion_gate_passed": True,
        "allowed_effects": ["veto"],
        "capabilities": {"can_place_orders": False, "can_close_positions": False},
    })
    assert verdict.allowed is True

    unsafe = RiskPolicyService().evaluate("promote_model_influence", {
        "autonomy_mode": "demo_nursery",
        "runtime_incident_mode": "normal",
        "demo_model_influence_enabled": True,
        "feature_schema_version": "pit.v2",
        "promotion_gate_passed": True,
        "capabilities": {"can_place_orders": True},
    })
    assert unsafe.allowed is False
    assert unsafe.reason == "unsafe_model_influence_capability"


def test_model_stages_are_demo_only_and_live_stage_promotion_is_rejected(tmp_path):
    assert MODEL_STAGES == {"shadow", "demo_canary", "demo_active", "quarantined"}
    normalized = normalized_model_influence_config({
        "models": {
            "open_quality_lightgbm": {"stage": "live_" + "active"},
        }
    })
    assert normalized["models"]["open_quality_lightgbm"]["stage"] == "quarantined"

    result = ModelInfluenceGovernanceService(tmp_path / "state.db").promote(
        tmp_path / "missing-artifact.json",
        stage="live_" + "canary",
    )
    assert result["ok"] is False
    assert result["status"] == "invalid_stage"
    assert result["allowed_stages"] == ["demo_canary"]


def test_model_promotion_gate_rejects_legacy_generic_pit_v2_schema(tmp_path):
    model_file = tmp_path / "model.joblib"
    model_file.write_bytes(b"test-model")
    artifact_path = tmp_path / "artifact.json"
    artifact_path.write_text(json.dumps({
        "model_type": "position_quality_lightgbm",
        "feature_schema_version": "pit.v2",
        "created_at": __import__("time").time(),
        "model_file": str(model_file),
        "model_file_sha256": __import__("hashlib").sha256(b"test-model").hexdigest(),
        "metrics": {"split": "time_ordered_grouped_purged"},
    }), encoding="utf-8")

    gate = ModelInfluenceGovernanceService(tmp_path / "state.db").evaluate_artifact(artifact_path)

    assert gate["passed"] is False
    assert "feature_schema" in gate["failed_checks"]


def test_factor_governance_v5_artifact_passes_feature_schema_gate(tmp_path):
    """Batch A upgraded factor_governance_lightgbm to pit.v3.factor_regime_rolling_lineage
    (MODEL_VERSION 5.0 / FEATURE_NAMES +3 regime features); the promotion gate must
    accept the new schema so the v5.0 artifact can advance to demo_canary."""
    model_file = tmp_path / "model.joblib"
    model_file.write_bytes(b"test-model")
    artifact_path = tmp_path / "artifact.json"
    artifact_path.write_text(json.dumps({
        "model_type": "factor_governance_lightgbm",
        "feature_schema_version": "pit.v4.factor_regime_decision_lineage",
        "created_at": __import__("time").time(),
        "model_file": str(model_file),
        "model_file_sha256": __import__("hashlib").sha256(b"test-model").hexdigest(),
        "metrics": {
            "split": "time_ordered_grouped_purged",
            "distinct_trade_count": 350,
            "holdout_trade_count": 80,
            "holdout": {
                "accuracy": 0.70,
                "balanced_accuracy": 0.66,
                "auc": 0.70,
                "majority_baseline_accuracy": 0.55,
            },
            "train": {"accuracy": 0.75},
        },
    }), encoding="utf-8")

    gate = ModelInfluenceGovernanceService(tmp_path / "state.db").evaluate_artifact(artifact_path)

    assert gate["passed"] is True
    assert "feature_schema" not in gate["failed_checks"]


def test_factor_governance_v4_artifact_rejected_by_feature_schema_gate(tmp_path):
    """Legacy pit.v2.factor_rolling_lineage artifacts must NOT pass the promotion
    gate anymore: their 15-feature schema cannot be promoted against the v5.0
    regime-aware contract (feature mismatch would break inference)."""
    model_file = tmp_path / "model.joblib"
    model_file.write_bytes(b"test-model")
    artifact_path = tmp_path / "artifact.json"
    artifact_path.write_text(json.dumps({
        "model_type": "factor_governance_lightgbm",
        "feature_schema_version": "pit.v2.factor_rolling_lineage",
        "created_at": __import__("time").time(),
        "model_file": str(model_file),
        "model_file_sha256": __import__("hashlib").sha256(b"test-model").hexdigest(),
        "metrics": {
            "split": "time_ordered_grouped_purged",
            "distinct_trade_count": 350,
            "holdout_trade_count": 80,
            "holdout": {
                "accuracy": 0.70,
                "balanced_accuracy": 0.66,
                "auc": 0.70,
                "majority_baseline_accuracy": 0.55,
            },
            "train": {"accuracy": 0.75},
        },
    }), encoding="utf-8")

    gate = ModelInfluenceGovernanceService(tmp_path / "state.db").evaluate_artifact(artifact_path)

    assert gate["passed"] is False
    assert "feature_schema" in gate["failed_checks"]


def test_v16_only_delegates_model_promotion_after_gate_passes(tmp_path):
    service = V16BrainOrchestratorService(tmp_path / "state.db")
    blocked = service.delegate_model_promotion({
        "passed": False,
        "model_type": "open_quality_lightgbm",
        "failed_checks": ["auc"],
    })
    assert blocked["ok"] is False

    delegated = service.delegate_model_promotion({
        "schema_version": "model_promotion_gate.v1",
        "passed": True,
        "model_type": "open_quality_lightgbm",
        "artifact_sha256": "abc",
        "feature_schema_version": "pit.v2",
        "checks": [{"name": "auc", "passed": True}],
    })
    assert delegated["ok"] is True
    command = delegated["command"]
    assert command["scope_type"] == "model_stage"
    assert command["action"] == "promote_model_influence"
    assert command["decision"] == "delegate"
