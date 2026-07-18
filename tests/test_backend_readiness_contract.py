import json
import time

from backend.services import config_service
from backend.core.db import STATE_DB_DDL, connect_sqlite
from backend.services.backend_readiness import BackendReadinessService
from backend.services.evolution_ledger import persist_runtime_config_snapshot
from backend.services.policy_suggestion_status import normalize_policy_suggestion_status
from config import runtime_config as rc


def test_readiness_reports_config_runtime_drift(monkeypatch, tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text("system:\n  mode: live\nctrader:\n  send_orders: true\n", encoding="utf-8")
    monkeypatch.setattr(config_service, "SETTINGS_PATH", path)
    rc.replace(rc.RuntimeConfig(ctrader_send_orders=False, factor_dry_run=False))

    status = BackendReadinessService._config_runtime_drift_status()

    assert status["drift"] is True
    assert status["semantic_drift"] is True


def test_config_runtime_drift_treats_persisted_overlay_as_authority(monkeypatch):
    from backend.services import runtime_config_overlay

    parsed = {"system": {"mode": "live"}, "ctrader": {"send_orders": False}}
    base = rc.RuntimeConfig.from_yaml(parsed)
    rc.register_overlay_base(base, replace_existing=True)
    effective = rc.config_from_overlay({"ctrader_send_orders": True})
    rc.replace(effective)

    class _FakeOverlayService:
        def latest(self):
            return {
                "ok": True,
                "status": "available",
                "overlay": {"ctrader_send_orders": True},
            }

    monkeypatch.setattr(runtime_config_overlay, "RuntimeConfigOverlayService", _FakeOverlayService)

    status = config_service.config_runtime_drift(parsed, include_overlay=True)

    assert status["drift"] is False
    assert status["authority"] == "yaml_plus_runtime_overlay"
    assert "ctrader_send_orders" in status["overlay_changed_keys"]
    rc.reset_for_tests()


def test_readiness_exposes_mutation_policy_and_audit_health():
    policy = BackendReadinessService._mutation_policy_status()
    audit = BackendReadinessService._audit_health_status()

    assert "live_dangerous" in policy["classes"]
    assert "governance_mutation" in policy["classes"]
    assert "ok" in audit


def test_learning_repair_scopes_maturity_to_current_canary_cohort(tmp_path):
    db_path = tmp_path / "state.db"
    candidate_started_at = 1_700_000_000.0
    now = time.time()
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.execute(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence, status, created_at)
            VALUES ('canary_1', 'position_supervisor_template', 'position_supervisor:test.v1',
                    'switch_position_supervisor_template', 0.9, 'approved', ?)
            """,
            (candidate_started_at,),
        )
        rows = [
            ("mature_asia", candidate_started_at + 3600, True, "regime_a", True),
            ("mature_us", candidate_started_at + 8 * 3600, True, "regime_b", True),
            ("candidate_market_close_gap", candidate_started_at + 16 * 3600, False, "regime_b", True),
            ("historical_incomplete", candidate_started_at - 86400, False, "legacy", False),
        ]
        for index, (position_id, close_ts, eligible, regime, has_shadow) in enumerate(rows):
            if has_shadow:
                conn.execute(
                    """
                    INSERT INTO position_supervisor_trace
                    (trace_id, position_id, template_id, stage, outcome, event_ts, created_at)
                    VALUES (?, ?, 'position_supervisor:test.v1', 'canary_shadow', 'shadow', ?, ?)
                    """,
                    (f"trace_{index}", position_id, close_ts - 60, close_ts - 60),
                )
            evidence = {
                "regime": regime,
                "maturity": {
                    "status": "governance_ready" if eligible else "partially_matured",
                    "governance_eligible": eligible,
                },
            }
            conn.execute(
                """
                INSERT INTO supervisor_counterfactual_review
                (counterfactual_id, position_id, close_ts, evidence_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (f"cf_{index}", position_id, close_ts, json.dumps(evidence), now, now),
            )
        conn.commit()
    finally:
        conn.close()

    rc.replace(
        rc.RuntimeConfig(
            autonomy_expansion_frozen=True,
            supervisor_canary_mature_trade_count=2,
            supervisor_counterfactual_governance_horizon_minutes=60,
        )
    )
    try:
        status = BackendReadinessService(db_path=db_path)._learning_repair_status()
    finally:
        rc.reset_for_tests()

    assert status["checks"]["counterfactual_maturity"] is True
    assert status["checks"]["canary_sample_count"] is True
    assert status["checks"]["canary_session_coverage"] is True
    assert status["checks"]["canary_regime_coverage"] is True
    assert status["configured_expansion_frozen"] is True
    assert status["expansion_frozen"] is False
    assert status["blocks_demo_governance"] is False
    assert status["immature_counterfactual_count"] == 1
    assert status["historical_immature_excluded_count"] == 1
    assert status["canary"]["mature_trade_count"] == 2
    assert status["canary"]["reviewed_position_count"] == 3
    assert status["ok"] is True


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


def test_governance_freshness_accepts_lifecycle_timestamp_column(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.execute(
            """
            INSERT INTO lifecycle_events
            (timestamp, event, factor, source, description, score, status, reason)
            VALUES (?, 'register', 'alpha_x', 'shadow', '', 0.0, 'ACTIVE', '')
            """,
            (9999999999.0,),
        )
        conn.commit()
    finally:
        conn.close()

    status = BackendReadinessService(db_path=db_path)._governance_freshness_status()

    assert status["tables"]["lifecycle_events"]["status"] == "fresh"
    assert status["tables"]["lifecycle_events"]["latest_ts"] == 9999999999.0


def test_factor_governance_runtime_reports_missing_run(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.commit()
    finally:
        conn.close()

    status = BackendReadinessService(db_path=db_path)._factor_governance_runtime_status()

    assert status["ok"] is False
    assert status["status"] == "missing_run"
    assert status["stale"] is True


def test_factor_governance_runtime_reports_fresh_run_and_catalog_snapshot(tmp_path):
    db_path = tmp_path / "state.db"
    now = time.time()
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.execute(
            """
            INSERT INTO evolution_run
            (run_id, run_type, trigger_source, status, config_version, config_hash,
             summary_json, started_at, ended_at)
            VALUES ('fg_run_1', 'factor_governance_autonomous', 'scheduler',
                    'completed', 1, 'hash', '{"status":"ok"}', ?, ?)
            """,
            (now - 10.0, now - 5.0),
        )
        conn.execute(
            """
            INSERT INTO factor_catalog_snapshot
            (snapshot_id, run_id, catalog_hash, catalog_json, source, created_at)
            VALUES ('snap_1', 'fg_run_1', 'catalog_hash', '[]', 'factor_governance_cycle', ?)
            """,
            (now - 4.0,),
        )
        conn.commit()
    finally:
        conn.close()

    status = BackendReadinessService(db_path=db_path)._factor_governance_runtime_status()

    assert status["ok"] is True
    assert status["status"] == "fresh"
    assert status["latest_run"]["run_id"] == "fg_run_1"
    assert status["latest_catalog_snapshot"]["snapshot_id"] == "snap_1"


def test_policy_suggestion_status_normalization_separates_legacy_and_autonomous():
    assert normalize_policy_suggestion_status(
        {"status": "approved", "action": "demo_auto_approve", "review_note": "auto-approved by demo_autonomous"}
    ) == "auto_approved"
    assert normalize_policy_suggestion_status({"status": "approved", "action": "manual_review"}) == "legacy_approved"
    assert normalize_policy_suggestion_status({"status": "pending_review"}) == "proposed"


def test_governance_status_exposes_raw_and_normalized_policy_counts(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.executemany(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence, reason,
             evidence_json, status, reviewed_at, review_note, created_at)
            VALUES (?, 'factor', ?, ?, 0.8, ?, '{}', ?, 0.0, ?, ?)
            """,
            [
                ("s_auto", "rsi_14", "demo_auto_approve", "autonomous", "approved", "auto-approved by demo_autonomous", 10.0),
                ("s_manual", "ema_slope", "manual_review", "manual", "approved", "manual approve", 11.0),
                ("s_pending", "macd_hist", "review", "test", "pending_review", "", 12.0),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    status = BackendReadinessService(db_path=db_path)._governance_status()

    assert status["policy_suggestion_counts_raw"]["approved"] == 2
    assert status["policy_suggestion_counts_normalized"]["auto_approved"] == 1
    assert status["policy_suggestion_counts_normalized"]["legacy_approved"] == 1
    assert status["policy_suggestion_counts_normalized"]["proposed"] == 1
