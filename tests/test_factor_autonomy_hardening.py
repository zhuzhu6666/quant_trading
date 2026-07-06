import sqlite3

import pytest

from backend.services.context_policy import ContextPolicyService
from backend.services.factor_catalog import latest_factor_catalog_snapshot, persist_factor_catalog_snapshot
from backend.services.factor_redundancy import RedundancyDetector
from backend.services.incident_controls import RuntimeIncidentControlService
from backend.services.runtime_config_mutation import RuntimeConfigMutationService
from backend.services.runtime_config_overlay import RuntimeConfigOverlayService
from backend.services.runtime_config_startup import restore_runtime_config_on_startup
from config import runtime_config as rc
from config.runtime_config import RuntimeConfig


def test_runtime_config_overlay_persists_allowed_autonomous_patch_and_restores(tmp_path):
    rc.reset_for_tests()
    db_path = tmp_path / "state.db"
    service = RuntimeConfigOverlayService(db_path)

    result = service.apply_patch(
        {
            "factor_signal_config": {
                "auto_alpha": {"role": "alpha", "enabled": True, "source": "discovered"},
            },
            "factor_portfolio_weights": {"auto_alpha": 0.22},
            "extra": {"active_parameter_templates": {"rsi_14": "fast_mean_revert"}},
            "risk_per_trade": 0.99,
        },
        source="test_autonomy",
        run_id="run_1",
    )

    assert result["ok"] is True
    live_cfg = rc.shared()
    assert "rsi_14" in live_cfg.factor_signal_config
    assert live_cfg.factor_signal_config["auto_alpha"]["source"] == "discovered"
    assert live_cfg.factor_portfolio_weights["auto_alpha"] == 0.22

    latest = service.latest()
    assert latest["ok"] is True
    assert "risk_per_trade" not in latest["overlay"]

    restored = service.restore_on_startup(RuntimeConfig())
    assert restored["restored"] is True
    restored_cfg = restored["config"]
    assert restored_cfg.factor_signal_config["auto_alpha"]["role"] == "alpha"
    assert restored_cfg.factor_portfolio_weights["auto_alpha"] == 0.22
    assert restored_cfg.extra["active_parameter_templates"]["rsi_14"] == "fast_mean_revert"
    assert "risk_per_trade" not in restored_cfg.extra


def test_runtime_config_overlay_replace_clears_superseded_autonomous_keys(tmp_path):
    rc.reset_for_tests()
    db_path = tmp_path / "state.db"
    service = RuntimeConfigOverlayService(db_path)
    service.apply_patch(
        {
            "factor_signal_config": {"bad_auto_alpha": {"role": "alpha", "enabled": True}},
            "factor_portfolio_weights": {"bad_auto_alpha": 0.3},
        },
        source="test_autonomy",
        run_id="run_before",
    )

    replacement = service.replace_overlay(
        {
            "factor_signal_config": {"rsi_14": {"role": "alpha", "enabled": True}},
            "factor_portfolio_weights": {"rsi_14": 0.1},
        },
        source="test_rollback",
        run_id="run_after",
    )

    assert replacement["ok"] is True
    latest = service.latest()["overlay"]
    assert "bad_auto_alpha" not in latest["factor_signal_config"]
    assert "bad_auto_alpha" not in latest["factor_portfolio_weights"]
    assert "bad_auto_alpha" not in rc.shared().factor_signal_config
    assert "bad_auto_alpha" not in rc.shared().factor_portfolio_weights


def test_runtime_config_overlay_refuses_test_run_on_production_store(monkeypatch, tmp_path):
    rc.reset_for_tests()
    import backend.services.runtime_config_overlay as overlay_mod

    monkeypatch.setattr(overlay_mod, "is_state_db_path", lambda _path: True)
    service = RuntimeConfigOverlayService(tmp_path / "state.db")

    with pytest.raises(RuntimeError, match="refusing to write test"):
        service.replace_overlay(
            {"factor_portfolio_weights": {"shadow_alpha_1": 0.3}},
            source="factor_governance_promote_factor",
            run_id="test-run",
        )


def test_runtime_config_overlay_refuses_pytest_write_to_production_store(monkeypatch, tmp_path):
    rc.reset_for_tests()
    import backend.services.runtime_config_overlay as overlay_mod

    monkeypatch.setattr(overlay_mod, "is_state_db_path", lambda _path: True)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_runtime_overlay.py::test")
    service = RuntimeConfigOverlayService(tmp_path / "state.db")

    with pytest.raises(RuntimeError, match="refusing to write test"):
        service.replace_overlay(
            {"factor_portfolio_weights": {"rsi_14": 0.3}},
            source="awe_decision_policy_update_weight",
            run_id="awe_adapt_123",
        )


def test_startup_restore_applies_overlay_and_writes_snapshot(tmp_path):
    rc.reset_for_tests()
    db_path = tmp_path / "state.db"
    service = RuntimeConfigOverlayService(db_path)
    service.apply_patch(
        {"factor_portfolio_weights": {"rsi_14": 0.42}},
        source="factor_governance_update_weight",
        run_id="run_overlay",
    )
    rc.reset_for_tests()

    restored = restore_runtime_config_on_startup(
        RuntimeConfig(),
        snapshot_source="test_worker_startup",
        db_path=db_path,
        run_id="startup_run",
    )

    assert restored["overlay"]["restored"] is True
    assert rc.shared().factor_portfolio_weights["rsi_14"] == 0.42
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT source, run_id FROM runtime_config_snapshot ORDER BY config_version DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row == ("test_worker_startup", "startup_run")


def test_clear_overlay_to_base_does_not_snapshot_stale_overlay(tmp_path):
    rc.reset_for_tests()
    db_path = tmp_path / "state.db"
    service = RuntimeConfigOverlayService(db_path)
    service.apply_patch(
        {"factor_portfolio_weights": {"shadow_alpha_1": 0.3}},
        source="factor_governance_promote_factor",
        run_id="run_overlay",
    )

    base_cfg = RuntimeConfig()
    result = service.clear_overlay_to_base(
        base_cfg,
        source="cleanup_test_overlay",
        run_id="cleanup_run",
    )

    assert result["status"] == "cleared"
    assert service.latest()["overlay"] == {}
    assert "shadow_alpha_1" not in rc.shared().factor_portfolio_weights
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT source, run_id, config_json FROM runtime_config_snapshot ORDER BY config_version DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row[0:2] == ("cleanup_test_overlay", "cleanup_run")
    assert "shadow_alpha_1" not in row[2]


def test_runtime_config_mutation_service_uses_overlay_without_temp_db_audit(tmp_path):
    rc.reset_for_tests()
    db_path = tmp_path / "state.db"
    result = RuntimeConfigMutationService(db_path).apply_patch(
        {"factor_portfolio_weights": {"rsi_14": 0.21}},
        source="awe_decision_policy_update_weight",
        run_id="awe_run",
    )

    assert result["ok"] is True
    assert result["mutation_source"] == "awe_decision_policy_update_weight"
    assert rc.shared().factor_portfolio_weights["rsi_14"] == 0.21
    overlay = RuntimeConfigOverlayService(db_path).latest()["overlay"]
    assert overlay["factor_portfolio_weights"]["rsi_14"] == 0.21


def test_incident_control_service_persists_via_overlay_and_requires_confirm_to_thaw(tmp_path):
    rc.reset_for_tests()
    db_path = tmp_path / "state.db"
    service = RuntimeIncidentControlService(db_path)

    frozen = service.set_mode("frozen", reason="test freeze")
    assert frozen["ok"] is True
    assert rc.shared().runtime_incident_mode == "frozen"

    blocked_thaw = service.set_mode("normal", reason="test thaw")
    assert blocked_thaw["ok"] is False
    assert blocked_thaw["status"] == "blocked_by_risk"
    assert rc.shared().runtime_incident_mode == "frozen"

    thawed = service.set_mode("normal", reason="test thaw", confirm_thaw=True)
    assert thawed["ok"] is True
    assert rc.shared().runtime_incident_mode == "normal"

    overlay = RuntimeConfigOverlayService(db_path).latest()["overlay"]
    assert overlay["runtime_incident_mode"] == "normal"
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT source, config_json
            FROM runtime_config_snapshot
            ORDER BY config_version DESC
            LIMIT 1
            """
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "v15_incident_control"
    assert '"runtime_incident_mode": "normal"' in row[1]


def test_runtime_config_overlay_status_flags_suspicious_test_factors(tmp_path):
    rc.reset_for_tests()
    db_path = tmp_path / "state.db"
    service = RuntimeConfigOverlayService(db_path)
    service.apply_patch(
        {
            "factor_signal_config": {"shadow_alpha_1": {"role": "alpha"}},
            "factor_portfolio_weights": {"foo": 0.1},
        },
        source="factor_governance_update_weight",
        run_id="awe_adapt_1",
    )

    status = service.status()

    assert status["status"] == "suspicious"
    assert status["suspicious"] is True
    assert status["reasons"] == ["test_like_factor_ids"]
    assert status["suspicious_factors"] == ["foo", "shadow_alpha_1"]


def test_runtime_config_overlay_restore_refuses_suspicious_overlay(tmp_path):
    rc.reset_for_tests()
    db_path = tmp_path / "state.db"
    service = RuntimeConfigOverlayService(db_path)
    service.apply_patch(
        {
            "factor_signal_config": {"model_weak_factor": {"role": "alpha"}},
            "factor_portfolio_weights": {"model_weak_factor": 0.3},
        },
        source="factor_governance_update_weight",
        run_id="awe_adapt_1",
    )

    with pytest.raises(RuntimeError, match="runtime_config_overlay_suspicious"):
        service.restore_on_startup(RuntimeConfig())


def test_learning_worker_registers_factor_governance_job(monkeypatch):
    import scripts.learning_worker as worker
    from config import runtime_config as runtime_cfg

    runtime_cfg.reset_for_tests()
    runtime_cfg.patch({"factor_governance_cron": "*/15 * * * *"})
    registered = []

    class _FakeScheduler:
        def add_job(self, name, cron_expr, fn):
            registered.append((name, cron_expr, getattr(fn, "__name__", "")))
            return True

        def start(self):
            registered.append(("__start__", "", ""))

    monkeypatch.setattr("backend.runtime.scheduler.InProcessScheduler", lambda: _FakeScheduler())
    monkeypatch.setattr("backend.runtime.evolution_orchestrator.scheduled_evolution_cycle", lambda: None)
    monkeypatch.setattr("backend.runtime.factor_governance_orchestrator.run_autonomous_factor_governance_cycle", lambda: None)
    monkeypatch.setattr("backend.services.live_service._scheduled_awe_adapt", lambda: None)
    monkeypatch.setattr("backend.services.live_service._scheduled_feature_engineering", lambda: None)
    monkeypatch.setattr("backend.services.live_service._scheduled_offmarket_position_quality_lightgbm", lambda: None)

    worker._register_heavy_jobs(include_system_health=False)

    names = [item[0] for item in registered]
    assert "factor_governance_autonomous" in names
    assert ("factor_governance_autonomous", "*/15 * * * *", "<lambda>") in registered


def test_factor_catalog_snapshot_round_trips_full_catalog_json(tmp_path):
    db_path = tmp_path / "state.db"
    catalog = [
        {
            "factor_id": "rsi_14",
            "role": "alpha",
            "enabled": True,
            "context_policy_effect": {"position_multiplier": 1.0},
        }
    ]

    snapshot = persist_factor_catalog_snapshot(
        catalog,
        run_id="run_snapshot",
        source="test",
        db_path=db_path,
    )
    latest = latest_factor_catalog_snapshot(db_path)

    assert snapshot["count"] == 1
    assert latest["ok"] is True
    assert latest["run_id"] == "run_snapshot"
    assert latest["items"] == catalog
    assert latest["catalog_hash"] == snapshot["catalog_hash"]


def test_context_policy_only_outputs_threshold_and_sizing_effects():
    result = ContextPolicyService().evaluate(
        {
            "volatility_state": "high",
            "event_window_state": "active",
            "trend_strength_state": "strong",
            "session_state": "rollover",
        }
    ).to_dict()

    assert result["applied"] is True
    assert result["signal_threshold_delta"] > 0
    assert result["position_multiplier"] == 0.5
    assert "direction" not in result
    assert "event_window_active" in result["reason"]


def test_redundancy_detector_groups_live_alpha_only_and_chooses_leader(tmp_path):
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE decision_factor_snapshot (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id TEXT NOT NULL,
                factor TEXT NOT NULL,
                normalized_value REAL DEFAULT 0.0
            )
            """
        )
        for i in range(220):
            base = (i - 110) / 100.0
            conn.executemany(
                """
                INSERT INTO decision_factor_snapshot
                (decision_id, factor, normalized_value)
                VALUES (?, ?, ?)
                """,
                [
                    (f"d{i}", "alpha_leader", base),
                    (f"d{i}", "alpha_follower", base * 1.01 + 0.001),
                    (f"d{i}", "context_vol", base),
                    (f"d{i}", "alpha_independent", 1.0 if i % 2 == 0 else -1.0),
                ],
            )
        conn.commit()
    finally:
        conn.close()

    catalog = [
        {
            "factor_id": "alpha_leader",
            "role": "alpha",
            "enabled": True,
            "eligible_for_live": True,
            "health_score": 0.8,
            "model_positive_score": 0.7,
            "weight": 0.3,
        },
        {
            "factor_id": "alpha_follower",
            "role": "alpha",
            "enabled": True,
            "eligible_for_live": True,
            "health_score": 0.4,
            "model_positive_score": 0.5,
            "weight": 0.3,
        },
        {
            "factor_id": "context_vol",
            "role": "context",
            "enabled": True,
            "eligible_for_live": True,
            "health_score": 1.0,
            "model_positive_score": 1.0,
            "weight": 1.0,
        },
        {
            "factor_id": "alpha_independent",
            "role": "alpha",
            "enabled": True,
            "eligible_for_live": True,
            "health_score": 0.9,
            "model_positive_score": 0.9,
            "weight": 0.3,
        },
    ]

    report = RedundancyDetector(db_path).build_report(catalog, min_samples=200, corr_threshold=0.85)

    assert report["group_count"] == 1
    group = report["groups"][0]
    assert group["leader"] == "alpha_leader"
    assert group["members"] == ["alpha_follower", "alpha_leader"]
    assert "context_vol" not in group["members"]
    assert "alpha_independent" not in group["members"]
