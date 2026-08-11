#!/usr/bin/env python3
"""Create the disposable pre-migration PostgreSQL baseline used by CI.

This is deliberately test-only.  Production databases must already contain
the historical ``state_v1`` baseline and are migrated exclusively through
``scripts/state_schema_migrate.py``.  The guard below refuses to run unless
GitHub-style CI is active and the target database name is explicitly test-like.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.core.state_schema_migrations import (  # noqa: E402
    STATE_SCHEMA_MIN_VERSION,
    require_state_schema_version,
    run_state_schema_migrations,
)
from backend.core.state_store import (  # noqa: E402
    STATE_SCHEMA,
    connect_state_migration_store,
)


_BASELINE_DDL = (
    """CREATE TABLE autonomous_learning_sample (
        sample_id TEXT PRIMARY KEY,
        sample_type TEXT NOT NULL DEFAULT '',
        event_ts DOUBLE PRECISION NOT NULL DEFAULT 0.0
    )""",
    "CREATE TABLE decision_ledger (seed INTEGER)",
    """CREATE TABLE learning_application_log (
        application_id TEXT PRIMARY KEY,
        cycle_ts DOUBLE PRECISION NOT NULL DEFAULT 0.0,
        scope_type TEXT NOT NULL,
        scope_key TEXT NOT NULL,
        action TEXT NOT NULL,
        bias_multiplier DOUBLE PRECISION DEFAULT 1.0,
        old_weight DOUBLE PRECISION DEFAULT 0.0,
        new_weight DOUBLE PRECISION DEFAULT 0.0,
        suggestion_ids_json TEXT DEFAULT '[]',
        status TEXT DEFAULT 'applied',
        details_json TEXT DEFAULT '{}',
        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
    )""",
    """CREATE TABLE learning_application_effect (
        application_id TEXT PRIMARY KEY,
        scope_type TEXT NOT NULL,
        scope_key TEXT NOT NULL,
        action TEXT NOT NULL,
        status TEXT DEFAULT 'observing',
        observed_trade_count INTEGER DEFAULT 0,
        baseline_trade_count INTEGER DEFAULT 0,
        post_avg_reward DOUBLE PRECISION DEFAULT 0.0,
        baseline_avg_reward DOUBLE PRECISION DEFAULT 0.0,
        delta_avg_reward DOUBLE PRECISION DEFAULT 0.0,
        post_win_rate DOUBLE PRECISION DEFAULT 0.0,
        baseline_win_rate DOUBLE PRECISION DEFAULT 0.0,
        decision_json TEXT DEFAULT '{}',
        last_review_at DOUBLE PRECISION DEFAULT 0.0,
        updated_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
    )""",
    """CREATE TABLE learning_experiment_reservation (
        reservation_id TEXT PRIMARY KEY,
        scope_type TEXT NOT NULL DEFAULT '',
        scope_key TEXT NOT NULL DEFAULT '',
        action TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'reserved',
        application_id TEXT NOT NULL DEFAULT '',
        expires_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
        updated_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
    )""",
    """CREATE TABLE order_lifecycle_event (
        event_id TEXT PRIMARY KEY,
        event_ts DOUBLE PRECISION NOT NULL DEFAULT 0.0
    )""",
    """CREATE TABLE trade_outcome_review (
        review_id TEXT PRIMARY KEY,
        trade_id TEXT DEFAULT '',
        position_id TEXT DEFAULT '',
        entry_decision_id TEXT DEFAULT '',
        exit_decision_id TEXT DEFAULT '',
        entry_quality DOUBLE PRECISION DEFAULT 0.0,
        hold_quality DOUBLE PRECISION DEFAULT 0.0,
        exit_quality DOUBLE PRECISION DEFAULT 0.0,
        regime_fit_score DOUBLE PRECISION DEFAULT 0.0,
        execution_quality DOUBLE PRECISION DEFAULT 0.0,
        pnl DOUBLE PRECISION DEFAULT 0.0,
        mae DOUBLE PRECISION DEFAULT 0.0,
        mfe DOUBLE PRECISION DEFAULT 0.0,
        outcome_label TEXT DEFAULT '',
        failure_tags_json TEXT DEFAULT '[]',
        summary_text TEXT DEFAULT '',
        review_json TEXT DEFAULT '{}',
        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
    )""",
    """CREATE TABLE factor_contribution_review (
        id BIGSERIAL PRIMARY KEY,
        review_id TEXT NOT NULL,
        trade_id TEXT DEFAULT '',
        factor TEXT NOT NULL,
        entry_contribution DOUBLE PRECISION DEFAULT 0.0,
        hold_contribution DOUBLE PRECISION DEFAULT 0.0,
        exit_contribution DOUBLE PRECISION DEFAULT 0.0,
        net_contribution DOUBLE PRECISION DEFAULT 0.0,
        confidence DOUBLE PRECISION DEFAULT 0.0,
        notes TEXT DEFAULT ''
    )""",
    """CREATE TABLE factor_health (
        factor TEXT PRIMARY KEY,
        score DOUBLE PRECISION DEFAULT 50.0,
        status TEXT DEFAULT 'UNKNOWN',
        section TEXT DEFAULT 'unknown',
        components_json TEXT DEFAULT '{}',
        n_obs INTEGER DEFAULT 0,
        rolling_ic DOUBLE PRECISION DEFAULT 0.0,
        updated_at DOUBLE PRECISION
    )""",
    """CREATE TABLE decision_factor_snapshot (
        id BIGSERIAL PRIMARY KEY,
        decision_id TEXT NOT NULL,
        factor TEXT NOT NULL,
        source TEXT DEFAULT 'registry',
        raw_value DOUBLE PRECISION DEFAULT 0.0,
        normalized_value DOUBLE PRECISION DEFAULT 0.0,
        direction DOUBLE PRECISION DEFAULT 0.0,
        base_weight DOUBLE PRECISION DEFAULT 0.0,
        policy_weight DOUBLE PRECISION DEFAULT 0.0,
        shadow_score DOUBLE PRECISION DEFAULT 0.0,
        health_score DOUBLE PRECISION DEFAULT 0.0,
        gated INTEGER DEFAULT 0,
        gated_reason TEXT DEFAULT '',
        contribution_score DOUBLE PRECISION DEFAULT 0.0
    )""",
    """CREATE TABLE ctrader_deals (
        deal_id BIGINT PRIMARY KEY,
        position_id BIGINT NOT NULL,
        order_id BIGINT DEFAULT 0,
        symbol_id BIGINT DEFAULT 0,
        volume BIGINT DEFAULT 0,
        filled_volume BIGINT DEFAULT 0,
        exec_price DOUBLE PRECISION DEFAULT 0.0,
        trade_side TEXT DEFAULT '',
        deal_status INTEGER DEFAULT 0,
        exec_timestamp DOUBLE PRECISION DEFAULT 0.0,
        commission DOUBLE PRECISION DEFAULT 0.0,
        entry_price DOUBLE PRECISION DEFAULT 0.0,
        gross_profit DOUBLE PRECISION DEFAULT 0.0,
        swap DOUBLE PRECISION DEFAULT 0.0,
        close_commission DOUBLE PRECISION DEFAULT 0.0,
        balance DOUBLE PRECISION DEFAULT 0.0,
        closed_volume BIGINT DEFAULT 0,
        is_close INTEGER DEFAULT 0,
        fetched_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
    )""",
    """CREATE TABLE policy_suggestion (
        suggestion_id TEXT PRIMARY KEY,
        scope_type TEXT NOT NULL,
        scope_key TEXT NOT NULL,
        action TEXT NOT NULL,
        confidence DOUBLE PRECISION DEFAULT 0.0,
        reason TEXT DEFAULT '',
        evidence_json TEXT DEFAULT '{}',
        status TEXT DEFAULT 'proposed',
        reviewed_at DOUBLE PRECISION DEFAULT 0.0,
        review_note TEXT DEFAULT '',
        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
    )""",
    "CREATE TABLE runtime_config_overlay (seed INTEGER)",
    """CREATE TABLE runtime_config_snapshot (
        config_version BIGSERIAL PRIMARY KEY,
        config_hash TEXT NOT NULL,
        source TEXT DEFAULT '',
        config_json TEXT NOT NULL DEFAULT '{}',
        run_id TEXT DEFAULT '',
        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
    )""",
    """CREATE TABLE evolution_run (
        run_id TEXT PRIMARY KEY,
        run_type TEXT NOT NULL,
        trigger_source TEXT DEFAULT '',
        status TEXT DEFAULT 'running',
        config_version INTEGER DEFAULT 0,
        config_hash TEXT DEFAULT '',
        summary_json TEXT DEFAULT '{}',
        started_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
        ended_at DOUBLE PRECISION DEFAULT 0.0
    )""",
    """CREATE TABLE evolution_decision (
        decision_id TEXT PRIMARY KEY,
        run_id TEXT DEFAULT '',
        decision_type TEXT NOT NULL,
        scope_type TEXT DEFAULT '',
        scope_key TEXT DEFAULT '',
        action TEXT DEFAULT '',
        status TEXT DEFAULT '',
        evidence_json TEXT DEFAULT '{}',
        risk_verdict_json TEXT DEFAULT '{}',
        before_json TEXT DEFAULT '{}',
        after_json TEXT DEFAULT '{}',
        result_json TEXT DEFAULT '{}',
        rollback_json TEXT DEFAULT '{}',
        config_version INTEGER DEFAULT 0,
        config_hash TEXT DEFAULT '',
        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
    )""",
    """CREATE TABLE position_supervisor_trace (
        trace_id TEXT PRIMARY KEY,
        position_id TEXT NOT NULL DEFAULT '',
        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
    )""",
    """CREATE TABLE brain_state_snapshot (
        snapshot_id TEXT PRIMARY KEY,
        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
    )""",
    """CREATE TABLE brain_medium_impact_governance (
        governance_id TEXT PRIMARY KEY,
        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
    )""",
    """CREATE TABLE v16_brain_command (
        command_id TEXT PRIMARY KEY,
        target_agent TEXT NOT NULL DEFAULT '',
        scope_type TEXT NOT NULL DEFAULT '',
        decision TEXT NOT NULL DEFAULT '',
        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
        updated_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
    )""",
    """CREATE TABLE brain_governance_candidate_review (
        review_id TEXT PRIMARY KEY,
        candidate_id TEXT NOT NULL DEFAULT '',
        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
    )""",
    """CREATE TABLE proposal_registry (
        proposal_id TEXT PRIMARY KEY,
        source_agent TEXT NOT NULL DEFAULT '',
        proposal_type TEXT NOT NULL DEFAULT '',
        control_surface TEXT NOT NULL DEFAULT '',
        target_scope TEXT NOT NULL DEFAULT ''
    )""",
    """CREATE TABLE jobs (
        id TEXT PRIMARY KEY,
        kind TEXT DEFAULT '',
        status TEXT DEFAULT 'pending',
        params_json TEXT DEFAULT '{}',
        result_json TEXT DEFAULT '{}',
        progress DOUBLE PRECISION DEFAULT 0.0,
        error TEXT DEFAULT '',
        created_at DOUBLE PRECISION,
        updated_at DOUBLE PRECISION
    )""",
    """CREATE TABLE experience_memory (
        experience_id TEXT PRIMARY KEY,
        source_table TEXT NOT NULL DEFAULT '',
        source_id TEXT NOT NULL DEFAULT '',
        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
    )""",
    """CREATE TABLE experience_pattern_stats (
        scope_type TEXT NOT NULL,
        scope_key TEXT NOT NULL,
        PRIMARY KEY (scope_type, scope_key)
    )""",
    """CREATE TABLE factor_catalog_snapshot (
        snapshot_id TEXT PRIMARY KEY,
        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
    )""",
)


def _require_disposable_ci_database(dsn: str) -> str:
    if os.environ.get("CI", "").strip().lower() != "true":
        raise RuntimeError("refusing PostgreSQL test bootstrap outside CI")
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as probe:
        database = str(
            probe.execute("SELECT current_database() AS name").fetchone()["name"] or ""
        )
    normalized = database.lower()
    if not (normalized.endswith("_test") or normalized.startswith("test_")):
        raise RuntimeError(
            f"refusing PostgreSQL test bootstrap for non-test database {database!r}"
        )
    return database


def main() -> int:
    dsn = os.environ.get("QUANT_STATE_PG_DSN", "").strip()
    if not dsn:
        raise RuntimeError("QUANT_STATE_PG_DSN is required")
    database = _require_disposable_ci_database(dsn)

    conn = connect_state_migration_store(dsn, schema=STATE_SCHEMA)
    try:
        existing = {
            str(row["table_name"])
            for row in conn.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema=current_schema()
                """
            ).fetchall()
        }
        if existing:
            status = require_state_schema_version(
                conn,
                minimum_version=STATE_SCHEMA_MIN_VERSION,
            )
            payload = {
                "ok": True,
                "database": database,
                "schema": STATE_SCHEMA,
                "bootstrap": "already_current",
                "status": status,
            }
            print(json.dumps(payload, sort_keys=True))
            return 0

        for statement in _BASELINE_DDL:
            conn.execute(statement)
        conn.commit()

        migrated = run_state_schema_migrations(conn, runner_id="github-actions-ci")
        status = require_state_schema_version(
            conn,
            minimum_version=STATE_SCHEMA_MIN_VERSION,
        )
        payload = {
            "ok": True,
            "database": database,
            "schema": STATE_SCHEMA,
            "bootstrap": "created_and_migrated",
            "migration": migrated,
            "status": status,
        }
        print(json.dumps(payload, sort_keys=True))
        return 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
