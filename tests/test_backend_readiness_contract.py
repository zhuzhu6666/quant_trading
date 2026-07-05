from backend.services import config_service
from backend.services.backend_readiness import BackendReadinessService
from backend.services.evolution_ledger import persist_runtime_config_snapshot
from config import runtime_config as rc


def test_readiness_reports_config_runtime_drift(monkeypatch, tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text("system:\n  mode: live\nctrader:\n  send_orders: true\n", encoding="utf-8")
    monkeypatch.setattr(config_service, "SETTINGS_PATH", path)
    rc.replace(rc.RuntimeConfig(ctrader_send_orders=False, factor_dry_run=False))

    status = BackendReadinessService._config_runtime_drift_status()

    assert status["drift"] is True
    assert status["semantic_drift"] is True


def test_readiness_exposes_mutation_policy_and_audit_health():
    policy = BackendReadinessService._mutation_policy_status()
    audit = BackendReadinessService._audit_health_status()

    assert "live_dangerous" in policy["classes"]
    assert "governance_mutation" in policy["classes"]
    assert "ok" in audit


def test_readiness_stability_status_reports_phase_h_guards(tmp_path):
    db_path = tmp_path / "state.db"
    persist_runtime_config_snapshot({"risk_per_trade": 0.01}, source="test", db_path=db_path)
    service = BackendReadinessService(db_path=db_path)

    status = service._stability_status(
        governance_freshness={
            "tables": {
                "meta_model_shadow_audit": {"status": "fresh", "age_seconds": 10.0},
                "factor_health": {"status": "stale_or_empty", "age_seconds": 400000.0},
            }
        },
        model_status={"meta_lightgbm": {"report": {"evaluated_count": 40}}},
    )

    assert status["schema_version"] == "backend_stability.v1"
    assert status["runtime_config_snapshot"]["ok"] is True
    assert status["freshness_watchdog"]["status"] == "degraded"
    assert status["freshness_watchdog"]["stale_tables"] == ["factor_health"]
    assert status["freshness_watchdog"]["blocks_live_model_permission"] is True
    assert status["rollback_policy"]["hard_risk_limits_mutable"] is False
