from backend.services import config_service
from backend.services.backend_readiness import BackendReadinessService
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
