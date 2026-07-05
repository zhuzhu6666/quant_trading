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
