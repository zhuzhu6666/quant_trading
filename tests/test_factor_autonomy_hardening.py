import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from backend.core.db import connect_sqlite
from backend.services.canonical_v2 import ensure_sqlite_schema, record_decision_event
from backend.services.context_policy import ContextPolicyService
from backend.services.factor_catalog import latest_factor_catalog_snapshot, persist_factor_catalog_snapshot
from backend.services.factor_redundancy import RedundancyDetector
from backend.services.incident_controls import RuntimeIncidentControlService
from backend.services.runtime_config_mutation import RuntimeConfigMutationService
from backend.services.runtime_config_overlay import (
    RuntimeConfigOverlayAuthorityError,
    RuntimeConfigOverlayService,
)
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
            "position_supervisor_template_id": "position_supervisor:conservative.v1",
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
    assert live_cfg.position_supervisor_template_id == "position_supervisor:conservative.v1"

    latest = service.latest()
    assert latest["ok"] is True
    assert "risk_per_trade" not in latest["overlay"]
    assert latest["overlay"]["position_supervisor_template_id"] == "position_supervisor:conservative.v1"

    with pytest.raises(RuntimeConfigOverlayAuthorityError) as caught:
        service.restore_on_startup(RuntimeConfig())
    quarantined_cfg = caught.value.quarantined_config
    assert quarantined_cfg is not None
    assert quarantined_cfg.factor_signal_config["auto_alpha"]["role"] == "alpha"
    assert quarantined_cfg.factor_portfolio_weights["auto_alpha"] == 0.22
    assert quarantined_cfg.extra["active_parameter_templates"]["rsi_14"] == "fast_mean_revert"
    assert quarantined_cfg.position_supervisor_template_id == "position_supervisor:conservative.v1"
    assert "risk_per_trade" not in quarantined_cfg.extra
    assert caught.value.report["new_risk_authorized"] is False
    assert caught.value.report["quarantine_projection"] == "legacy_behavior_preserved"


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
    from backend.services.governance_mutation_coordinator import (
        GovernanceMutationCoordinator,
        GovernanceMutationPlan,
    )

    rc.register_overlay_base(RuntimeConfig(), db_path)
    committed = GovernanceMutationCoordinator(db_path).execute(
        GovernanceMutationPlan(
            patch={"factor_portfolio_weights": {"rsi_14": 0.42}},
            source="factor_governance_update_weight",
            action="update_weight",
            control_surface="factor_weight",
            scope_type="factor_weight",
            scope_key="rsi_14",
            run_id="run_overlay",
        )
    )
    assert committed["ok"] is True
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


def test_runtime_config_shared_can_refresh_overlay_written_by_another_process(tmp_path):
    rc.reset_for_tests()
    db_path = tmp_path / "state.db"
    service = RuntimeConfigOverlayService(db_path)
    service.apply_patch(
        {"autonomy_mode": "demo_nursery", "dynamic_sizing_max_api_volume": 1000.0},
        source="demo_nursery_switch",
        run_id="nursery_run",
    )
    rc.replace(RuntimeConfig(autonomy_mode="demo_autonomous", dynamic_sizing_max_api_volume=100.0))

    refreshed = rc.refresh_from_overlay(db_path, force=True)

    assert refreshed is True
    assert rc.shared().autonomy_mode == "demo_nursery"
    assert rc.shared().dynamic_sizing_max_api_volume == 1000.0


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


def test_clear_overlay_to_base_requires_exact_expected_hash(tmp_path):
    rc.reset_for_tests()
    db_path = tmp_path / "state.db"
    service = RuntimeConfigOverlayService(db_path)
    service.apply_patch(
        {"factor_portfolio_weights": {"shadow_alpha_1": 0.3}},
        source="factor_governance_promote_factor",
        run_id="run_overlay",
    )
    original = service.latest()

    with pytest.raises(
        RuntimeConfigOverlayAuthorityError,
        match="overlay_hash_changed",
    ):
        service.clear_overlay_to_base(
            RuntimeConfig(),
            source="operator_overlay_reconstruction",
            run_id="operator_run",
            expected_overlay_hash="stale-hash",
        )

    latest = service.latest()
    assert latest["overlay"] == original["overlay"]
    assert latest["overlay_hash"] == original["overlay_hash"]


def test_clear_overlay_to_base_accepts_exact_expected_hash(tmp_path):
    rc.reset_for_tests()
    db_path = tmp_path / "state.db"
    service = RuntimeConfigOverlayService(db_path)
    service.apply_patch(
        {"factor_portfolio_weights": {"shadow_alpha_1": 0.3}},
        source="factor_governance_promote_factor",
        run_id="run_overlay",
    )
    expected_hash = service.latest()["overlay_hash"]

    result = service.clear_overlay_to_base(
        RuntimeConfig(),
        source="operator_overlay_reconstruction",
        run_id="operator_run",
        expected_overlay_hash=expected_hash,
    )

    assert result["status"] == "cleared"
    assert service.latest()["overlay"] == {}


def test_runtime_config_overlay_serializes_concurrent_partial_patches(tmp_path):
    rc.reset_for_tests()
    db_path = tmp_path / "state.db"
    service = RuntimeConfigOverlayService(db_path)
    rc.register_overlay_base(RuntimeConfig(), db_path)

    def write_factor(name: str, weight: float):
        return service.apply_patch(
            {"factor_portfolio_weights": {name: weight}},
            source="factor_governance_update_weight",
            run_id=f"concurrent_{name}",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda item: write_factor(*item), [("alpha_a", 0.11), ("alpha_b", 0.22)]))

    assert all(item["ok"] for item in results)
    weights = service.latest()["overlay"]["factor_portfolio_weights"]
    assert weights["alpha_a"] == 0.11
    assert weights["alpha_b"] == 0.22


def test_runtime_config_overlay_does_not_publish_failed_transaction(tmp_path, monkeypatch):
    rc.reset_for_tests()
    db_path = tmp_path / "state.db"
    service = RuntimeConfigOverlayService(db_path)
    base = RuntimeConfig(autonomy_mode="demo_autonomous")
    rc.register_overlay_base(base, db_path)
    rc.replace(base)
    version_before = rc.version()

    def fail_persist(*_args, **_kwargs):
        raise RuntimeError("simulated overlay persistence failure")

    monkeypatch.setattr(service, "_persist_overlay_row", fail_persist)
    with pytest.raises(RuntimeError, match="simulated overlay persistence failure"):
        service.apply_patch(
            {"autonomy_mode": "demo_nursery"},
            source="factor_governance_update_weight",
            run_id="failed_transaction",
        )

    assert rc.version() == version_before
    assert rc.shared().autonomy_mode == "demo_autonomous"
    assert service.latest()["ok"] is False


def test_empty_overlay_refresh_rebuilds_yaml_base_and_removes_old_keys(tmp_path):
    rc.reset_for_tests()
    db_path = tmp_path / "state.db"
    service = RuntimeConfigOverlayService(db_path)
    base = RuntimeConfig(autonomy_mode="demo_autonomous")
    rc.register_overlay_base(base, db_path)
    service.apply_patch(
        {
            "autonomy_mode": "demo_nursery",
            "factor_portfolio_weights": {"temporary_alpha": 0.4},
        },
        source="factor_governance_update_weight",
        run_id="before_clear",
    )
    service.clear_overlay_to_base(base, source="operator_clear", run_id="clear")

    # Simulate a second process that still holds the pre-clear value.
    rc.replace(RuntimeConfig(autonomy_mode="demo_nursery", factor_portfolio_weights={"temporary_alpha": 0.4}))
    assert rc.refresh_from_overlay(db_path, force=True) is True
    assert rc.shared().autonomy_mode == "demo_autonomous"
    assert "temporary_alpha" not in rc.shared().factor_portfolio_weights


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


def test_runtime_config_overlay_allows_dynamic_sizing_controls(tmp_path):
    rc.reset_for_tests()
    db_path = tmp_path / "state.db"
    result = RuntimeConfigMutationService(db_path).apply_patch(
        {
            "kelly_risk_per_trade_pct": 0.06,
            "kelly_fraction": 0.5,
            "dynamic_sizing_max_api_volume": 1000.0,
        },
        source="demo_dynamic_sizing_recalibration",
        run_id="sizing_repair",
    )

    assert result["ok"] is True
    assert rc.shared().kelly_risk_per_trade_pct == 0.06
    assert rc.shared().kelly_fraction == 0.5
    assert rc.shared().dynamic_sizing_max_api_volume == 1000.0
    overlay = RuntimeConfigOverlayService(db_path).latest()["overlay"]
    assert overlay["kelly_risk_per_trade_pct"] == 0.06
    assert overlay["kelly_fraction"] == 0.5
    assert overlay["dynamic_sizing_max_api_volume"] == 1000.0


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
            SELECT s.source, p.config_json
            FROM runtime_config_snapshot s
            JOIN runtime_config_payload p ON p.payload_hash = s.payload_hash
            ORDER BY config_version DESC
            LIMIT 1
            """
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "v15_incident_control"
    assert json.loads(row[1])["runtime_incident_mode"] == "normal"


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
    monkeypatch.setattr(
        "backend.runtime.evolution_orchestrator.scheduled_evolution_with_governance_handoff",
        lambda: None,
    )
    monkeypatch.setattr("backend.services.learning_research_jobs.run_feature_engineering_job", lambda: None)
    monkeypatch.setattr("backend.services.learning_research_jobs.run_offmarket_position_quality_job", lambda: None)

    worker._register_heavy_jobs(include_system_health=False)

    names = [item[0] for item in registered]
    assert "factor_governance_autonomous" not in names
    assert "awe_adapt" not in names
    assert (
        "evolution_hourly",
        "23,53 * * * *",
        "coordinated_evolution_hourly",
    ) in registered
    assert any(
        name == "autonomous_evolution_nursery"
        and cron == "7,17,37,47 * * * *"
        for name, cron, _fn in registered
    )
    assert any(
        name == "feature_eng" and cron == "5 3 * * *"
        for name, cron, _fn in registered
    )
    assert any(
        name == "supervisor_learning" and cron == "9,39 * * * *"
        for name, cron, _fn in registered
    )
    assert any(
        name == "autonomous_learning" and cron == "12,42 * * * *"
        for name, cron, _fn in registered
    )
    assert worker.__file__
    assert "backend.services.live_service" not in Path(worker.__file__).read_text(encoding="utf-8")


def test_learning_worker_factor_health_catchup_reuses_governance_freshness(
    monkeypatch,
):
    import scripts.learning_worker as worker
    import backend.runtime.factor_governance_orchestrator as governance

    calls = []
    monkeypatch.setattr(worker, "_factor_health_catchup_thread", None)
    monkeypatch.setattr(worker, "_latest_factor_health_age_seconds", lambda: 301.0)
    monkeypatch.setattr(
        governance,
        "factor_governance_health_max_age_seconds",
        lambda _cfg=None: 300.0,
    )
    monkeypatch.setattr(
        worker,
        "_coordinated_mutation_job",
        lambda name, _fn: lambda: calls.append(name) or {"status": "ok"},
    )

    assert worker._schedule_factor_health_catchup(delay_sec=0.0) is True
    worker._factor_health_catchup_thread.join(timeout=2.0)

    assert calls == ["factor_health_startup_catchup"]


def test_learning_worker_nursery_uses_bounded_demo_step_without_full_learning_cycle(
    monkeypatch,
):
    import scripts.learning_worker as worker

    jobs = {}
    captured = []

    class _FakeScheduler:
        def add_job(self, name, _cron_expr, fn):
            jobs[name] = fn
            return True

        def start(self):
            return None

    class _Nursery:
        def run_once(self, **kwargs):
            captured.append(kwargs)
            return {
                "status": "completed",
                "initial_cycle": {},
                "repaired_cycle": {},
                "final_cycle": {},
                "actions": [],
            }

    monkeypatch.setattr(
        "backend.runtime.scheduler.InProcessScheduler",
        lambda: _FakeScheduler(),
    )
    monkeypatch.setattr(
        worker,
        "_coordinated_mutation_job",
        lambda _name, fn: fn,
    )
    monkeypatch.setattr(
        "backend.services.evolution_work_coordinator.coordinated_job",
        lambda _name, fn: fn,
    )
    monkeypatch.setattr(
        "backend.services.autonomous_evolution_runner.AutonomousEvolutionNurseryRunner",
        _Nursery,
    )

    worker._register_heavy_jobs(include_system_health=False)
    jobs["autonomous_evolution_nursery"]()

    assert captured[0]["full_learning_cycle"] is False
    assert captured[0]["automatic_demo"] is True
    assert captured[0]["consume_recommended_step"] is True
    assert "apply_when_ready" not in captured[0]


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


def test_factor_catalog_snapshot_interns_semantic_payload_but_keeps_occurrences(tmp_path):
    db_path = tmp_path / "state.db"
    first_catalog = [
        {
            "factor_id": "rsi_14",
            "weight": 0.25,
            "details": "x" * 1000,
            "catalog_ts": 100.0,
            "latest_catalog_snapshot_id": "old-snapshot",
            "latest_catalog_snapshot_run_id": "old-run",
        }
    ]
    second_catalog = [
        {
            "factor_id": "rsi_14",
            "weight": 0.25,
            "details": "x" * 1000,
            "catalog_ts": 200.0,
            "latest_catalog_snapshot_id": "new-snapshot",
            "latest_catalog_snapshot_run_id": "new-run",
        }
    ]

    first = persist_factor_catalog_snapshot(
        first_catalog, run_id="old-run", source="test", db_path=db_path
    )
    second = persist_factor_catalog_snapshot(
        second_catalog, run_id="new-run", source="test", db_path=db_path
    )

    conn = connect_sqlite(db_path, read_only=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM factor_catalog_snapshot").fetchone()[0] == 2
        stored = conn.execute(
            "SELECT catalog_json FROM factor_catalog_snapshot ORDER BY created_at"
        ).fetchall()
        assert stored[0][0].startswith("[")
        assert len(stored[1][0]) < len(stored[0][0])
    finally:
        conn.close()
    assert second["payload_interned"] is True
    assert second["catalog_hash"] == first["catalog_hash"]
    latest = latest_factor_catalog_snapshot(db_path)
    assert latest["items"] == second_catalog


def test_context_policy_only_outputs_threshold_and_sizing_effects():
    result = ContextPolicyService().evaluate(
        {
            "volatility_state": "high",
            "event_window_state": "active",
            "trend_strength_state": "strong",
            "session_state": "rollover",
            "macro_context_score": 0.8,
            "macro_evidence_count": 3,
            "calibrated_probability": 0.52,
            "confidence_sizing_multiplier": 0.76,
        }
    ).to_dict()

    assert result["applied"] is True
    assert result["signal_threshold_delta"] > 0
    assert result["position_multiplier"] == 0.5
    assert "direction" not in result
    assert "event_window_active" in result["reason"]
    assert "macro_context_extreme" in result["reason"]
    assert "low_calibrated_confidence" in result["reason"]


def test_redundancy_detector_groups_live_alpha_only_and_chooses_leader(tmp_path):
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    try:
        ensure_sqlite_schema(conn)
        for i in range(220):
            base = (i - 110) / 100.0
            decision_id = f"d{i}"
            record_decision_event(
                conn,
                decision_id=decision_id,
                event_type="open",
                symbol="XAUUSD",
                timeframe="M1",
                decision_ts=float(i),
                created_at=float(i),
                factor_snapshots=[
                    {"decision_id": decision_id, "factor": "alpha_leader", "normalized_value": base},
                    {
                        "decision_id": decision_id,
                        "factor": "alpha_follower",
                        "normalized_value": base * 1.01 + 0.001,
                    },
                    {"decision_id": decision_id, "factor": "context_vol", "normalized_value": base},
                    {
                        "decision_id": decision_id,
                        "factor": "alpha_independent",
                        "normalized_value": 1.0 if i % 2 == 0 else -1.0,
                    },
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
