import sqlite3

from backend.services.backend_readiness import BackendReadinessService
from backend.services.meta_governance import MetaGovernanceService


def _fake_report():
    return {
        "ok": True,
        "schema_version": "meta_model_shadow_report.v1",
        "model_type": "meta_model_lightgbm",
        "audit_count": 40,
        "evaluated_count": 40,
        "accuracy": 0.42,
        "confusion_matrix": {
            "contract": {"contract": 4, "observe": 3, "recover": 1},
            "observe": {"contract": 5, "observe": 9, "recover": 4},
            "recover": {"contract": 2, "observe": 8, "recover": 4},
        },
        "posture_distribution": {"contract": 11, "observe": 20, "recover": 9},
        "label_distribution": {"contract": 8, "observe": 18, "recover": 14},
        "rule_comparison": {
            "compared_count": 40,
            "agreement_rate": 0.35,
            "rule_accuracy": 0.3,
            "model_accuracy_on_compared": 0.42,
        },
        "artifact_summary": {
            "artifact_path": "/tmp/meta.json",
            "model_version": "1.1",
            "metrics": {
                "safe_for_live_trading": False,
                "holdout": {"accuracy": 0.36, "count": 10},
            },
        },
    }


def test_meta_governance_snapshots_and_materializes_proposed_suggestion(tmp_path):
    db_path = tmp_path / "state.db"
    service = MetaGovernanceService(db_path)

    snapshot = service.create_shadow_report_snapshot(report=_fake_report(), source="test")
    assert snapshot["ok"] is True
    assert snapshot["report_id"].startswith("msr_")

    listed = service.list_shadow_report_snapshots(limit=10)
    assert listed["count"] == 1
    assert listed["items"][0]["accuracy"] == 0.42

    result = service.materialize_meta_governance_suggestion(
        report=_fake_report(),
        source="test",
    )
    assert result["ok"] is True
    assert result["advisory_only"] is True
    assert result["requires_review"] is True
    assert result["suggestion"]["action"] == "block_meta_model_promotion"
    assert result["capabilities"]["can_place_orders"] is False

    conn = sqlite3.connect(db_path)
    try:
        suggestion = conn.execute(
            "SELECT scope_type, scope_key, action, status FROM policy_suggestion"
        ).fetchone()
        assert suggestion == (
            "meta_model",
            "meta_model_lightgbm",
            "block_meta_model_promotion",
            "proposed",
        )
        ledger = conn.execute(
            "SELECT event_type, action_reason FROM decision_ledger"
        ).fetchone()
        assert ledger == ("meta_model_governance_suggestion", "block_meta_model_promotion")
    finally:
        conn.close()


def test_backend_readiness_contract(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    MetaGovernanceService(db_path).create_shadow_report_snapshot(report=_fake_report(), source="test")

    monkeypatch.setattr(
        "backend.services.backend_readiness.BackendReadinessService._live_status",
        staticmethod(
            lambda: {
                "ctrader": {"status": "connected"},
                "loop": {"running": True},
                "readiness": {"ok": True},
                "market_session": {
                    "status": "closed_pending_positions",
                    "high_load_allowed": True,
                    "high_load_profile": "limited_with_positions",
                },
            }
        ),
    )
    monkeypatch.setattr(
        "backend.services.backend_readiness.BackendReadinessService._system_health",
        staticmethod(
            lambda: {
                "overall": "critical",
                "display_overall": "degraded",
                "score": 0.8,
                "components": {"l2_depth": "critical"},
                "blocking_components": [],
                "known_observations": [{"component": "l2_depth", "status": "critical"}],
            }
        ),
    )
    monkeypatch.setattr(
        "backend.services.backend_readiness.MetaModelLightGBMService.build_shadow_report",
        lambda self, **kwargs: _fake_report(),
    )

    result = BackendReadinessService(db_path=db_path).build()

    assert result["schema_version"] == "backend_readiness.v1"
    assert result["ready_for_frontend"] is True
    assert result["high_load"]["can_run_training_with_positions"] is True
    assert result["models"]["meta_lightgbm"]["promotion_gate"]["eligible_for_live"] is False
    assert result["frontend_contract"]["preferred_entry"] == "/api/ops/backend-readiness"
    assert result["governance"]["automatic_execution_enabled"] is True
    assert result["governance"]["autonomy_mode"] == "demo_autonomous"
