from __future__ import annotations

import os
import uuid

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row


pytestmark = pytest.mark.postgres_integration


def test_postgres_state_store_connects_and_uses_state_schema() -> None:
    dsn = os.environ.get("QUANT_STATE_PG_DSN", "").strip()
    if not dsn:
        pytest.skip("QUANT_STATE_PG_DSN is not configured")

    from backend.core.state_store import RuntimeStateSchemaMissingError, connect_state_store
    from backend.core.state_schema_migrations import (
        STATE_SCHEMA_MIN_VERSION,
        require_state_schema_version,
    )

    conn = connect_state_store(dsn)
    try:
        schema = conn.execute("SELECT current_schema() AS schema").fetchone()["schema"]
        assert schema == "state_v1"
        status = require_state_schema_version(conn)
        assert status["current_version"] >= STATE_SCHEMA_MIN_VERSION
        with pytest.raises(RuntimeStateSchemaMissingError):
            conn.execute(
                "CREATE TEMP TABLE phase0b_pg_smoke (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
            )
        conn.rollback()
    finally:
        conn.close()


def test_postgres_read_only_state_connection_stays_read_only_after_commit() -> None:
    dsn = os.environ.get("QUANT_STATE_PG_DSN", "").strip()
    if not dsn:
        pytest.skip("QUANT_STATE_PG_DSN is not configured")

    from backend.core.state_store import connect_state_store

    schema = f"pytest_state_readonly_{uuid.uuid4().hex}"
    admin = psycopg.connect(dsn, autocommit=False, row_factory=dict_row)
    admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    admin.execute(
        sql.SQL("CREATE TABLE {}.probe (value INTEGER)").format(sql.Identifier(schema))
    )
    admin.commit()
    conn = None
    try:
        conn = connect_state_store(dsn, read_only=True, schema=schema)
        assert conn.execute("SELECT COUNT(*) AS count FROM probe").fetchone()["count"] == 0
        conn.commit()
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("INSERT INTO probe(value) VALUES (1)")
        conn.rollback()
        # Rollback also starts a fresh default transaction on the next query;
        # the session default must keep that transaction read-only as well.
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("INSERT INTO probe(value) VALUES (2)")
        conn.rollback()
    finally:
        if conn is not None:
            conn.close()
        admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        admin.commit()
        admin.close()


def test_versioned_state_migration_executes_in_disposable_pg_temp_schema() -> None:
    from backend.core.db import state_pg_dsn

    dsn = state_pg_dsn().strip()
    if not dsn:
        pytest.skip("QUANT_STATE_PG_DSN is not configured")

    from backend.core.state_schema_migrations import (
        STATE_SCHEMA_MIGRATIONS,
        require_state_schema_version,
        run_state_schema_migrations,
    )

    from backend.core.state_store import connect_state_migration_store

    conn = connect_state_migration_store(dsn)

    class _DeferredCommitConnection:
        """Keep runner DDL inside the test transaction for final rollback."""

        def __init__(self, inner):
            self.inner = inner
            self.commit_calls = 0

        def execute(self, sql, params=None):
            return self.inner.execute(sql, params)

        def commit(self):
            self.commit_calls += 1

        def rollback(self):
            return self.inner.rollback()

    wrapped = _DeferredCommitConnection(conn)
    try:
        conn.execute("SET search_path TO pg_temp")
        conn.execute("CREATE TEMP TABLE migration_connection_ddl_smoke (value INTEGER)")
        # STATE_SCHEMA_BASELINE_TABLES 已于 2026-08-18 清空（生产表由 ensure_*
        # 预建）；migration 0001-0018 仍假设这些运行表在干净 schema 中预存，
        # 因此集成测试使用迁移期冻结的预存表清单（order_lifecycle_event 由下方独立补建）。
        _MIGRATION_PREDEPOSIT_TABLES = (
            "autonomous_learning_sample", "brain_governance_candidate_review",
            "brain_medium_impact_governance", "brain_state_snapshot", "ctrader_deals",
            "decision_ledger", "decision_factor_snapshot", "brain_action_plan_eval",
            "evolution_decision", "experience_memory", "experience_pattern_stats",
            "factor_catalog_snapshot", "jobs", "learning_application_effect",
            "learning_application_log", "learning_experiment_reservation",
            "policy_suggestion", "position_supervisor_trace", "proposal_registry",
            "runtime_config_overlay", "runtime_config_snapshot", "trade_outcome_review",
            "v16_brain_command",
        )
        for table in _MIGRATION_PREDEPOSIT_TABLES:
            if table == "autonomous_learning_sample":
                conn.execute(
                    """CREATE TEMP TABLE autonomous_learning_sample (
                        sample_id TEXT PRIMARY KEY,
                        sample_type TEXT NOT NULL DEFAULT '',
                        event_ts DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                        updated_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
                    )"""
                )
            elif table == "policy_suggestion":
                conn.execute(
                    """CREATE TEMP TABLE policy_suggestion (
                        suggestion_id TEXT PRIMARY KEY,
                        status TEXT NOT NULL DEFAULT 'proposed',
                        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
                    )"""
                )
            elif table == "order_lifecycle_event":
                conn.execute(
                    "CREATE TEMP TABLE order_lifecycle_event "
                    "(event_id TEXT PRIMARY KEY, event_ts DOUBLE PRECISION NOT NULL DEFAULT 0.0)"
                )
            elif table == "learning_experiment_reservation":
                conn.execute(
                    """CREATE TEMP TABLE learning_experiment_reservation (
                        reservation_id TEXT PRIMARY KEY,
                        scope_type TEXT NOT NULL DEFAULT '',
                        scope_key TEXT NOT NULL DEFAULT '',
                        action TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'reserved',
                        application_id TEXT NOT NULL DEFAULT '',
                        expires_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                        updated_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
                    )"""
                )
            elif table == "nursery_exploration_reservation":
                conn.execute(
                    """CREATE TEMP TABLE nursery_exploration_reservation (
                        reservation_id TEXT NOT NULL,
                        trade_date TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        setup_fingerprint TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'reserved',
                        expires_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                        updated_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                        PRIMARY KEY (reservation_id, reason)
                    )"""
                )
            elif table == "v16_brain_command":
                conn.execute(
                    """CREATE TEMP TABLE v16_brain_command (
                        command_id TEXT PRIMARY KEY,
                        target_agent TEXT NOT NULL DEFAULT '',
                        scope_type TEXT NOT NULL DEFAULT '',
                        decision TEXT NOT NULL DEFAULT '',
                        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                        updated_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
                    )"""
                )
            elif table == "brain_governance_candidate_review":
                conn.execute(
                    """CREATE TEMP TABLE brain_governance_candidate_review (
                        review_id TEXT PRIMARY KEY,
                        candidate_id TEXT NOT NULL DEFAULT '',
                        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
                    )"""
                )
            elif table == "proposal_registry":
                conn.execute(
                    """CREATE TEMP TABLE proposal_registry (
                        proposal_id TEXT PRIMARY KEY,
                        source_agent TEXT NOT NULL DEFAULT '',
                        proposal_type TEXT NOT NULL DEFAULT '',
                        control_surface TEXT NOT NULL DEFAULT '',
                        target_scope TEXT NOT NULL DEFAULT ''
                    )"""
                )
            elif table == "jobs":
                conn.execute(
                    """CREATE TEMP TABLE jobs (
                        id TEXT PRIMARY KEY,
                        kind TEXT DEFAULT '',
                        status TEXT DEFAULT 'pending',
                        params_json TEXT DEFAULT '{}',
                        result_json TEXT DEFAULT '{}',
                        progress DOUBLE PRECISION DEFAULT 0.0,
                        error TEXT DEFAULT '',
                        created_at DOUBLE PRECISION,
                        updated_at DOUBLE PRECISION
                    )"""
                )
            elif table == "experience_memory":
                conn.execute(
                    """CREATE TEMP TABLE experience_memory (
                        experience_id TEXT PRIMARY KEY,
                        source_table TEXT NOT NULL DEFAULT '',
                        source_id TEXT NOT NULL DEFAULT '',
                        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
                    )"""
                )
            elif table == "decision_factor_snapshot":
                conn.execute(
                    """CREATE TEMP TABLE decision_factor_snapshot (
                        id BIGSERIAL PRIMARY KEY,
                        decision_id TEXT NOT NULL DEFAULT '',
                        factor TEXT NOT NULL DEFAULT ''
                    )"""
                )
            elif table == "factor_catalog_snapshot":
                conn.execute(
                    """CREATE TEMP TABLE factor_catalog_snapshot (
                        snapshot_id TEXT PRIMARY KEY,
                        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
                    )"""
                )
            elif table == "brain_action_plan_eval":
                conn.execute(
                    """CREATE TEMP TABLE brain_action_plan_eval (
                        eval_id TEXT PRIMARY KEY,
                        plan_id TEXT NOT NULL DEFAULT '',
                        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
                    )"""
                )
            elif table == "evolution_decision":
                conn.execute(
                    """CREATE TEMP TABLE evolution_decision (
                        decision_id TEXT PRIMARY KEY,
                        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
                    )"""
                )
            elif table == "trade_outcome_review":
                conn.execute(
                    """CREATE TEMP TABLE trade_outcome_review (
                        review_id TEXT PRIMARY KEY
                    )"""
                )
            elif table == "runtime_config_snapshot":
                conn.execute(
                    """CREATE TEMP TABLE runtime_config_snapshot (
                        config_version TEXT NOT NULL DEFAULT '',
                        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
                    )"""
                )
            else:
                conn.execute(f'CREATE TEMP TABLE "{table}" (seed INTEGER)')

        # baseline 已于 2026-08-18 清空（表由生产 ensure_* 预建）；migration
        # 0001 仍会对 order_lifecycle_event 做 ADD COLUMN + INDEX，干净 schema
        # 下需先补建该依赖表（含 index 所需 event_ts 列）。
        conn.execute(
            """CREATE TEMP TABLE order_lifecycle_event (
                event_id TEXT PRIMARY KEY,
                event_ts DOUBLE PRECISION NOT NULL DEFAULT 0.0
            )"""
        )

        # 0019 二级索引回填引用了一大批"生产代码 DDL 预建、迁移链从不建表"的
        # 表（Test 环境既无夹具 create 也无迁移 CREATE，否则会 UndefinedTable）。
        # 按生产 ensure_* 预建的真实列建 TEMP 表（列名/类型/DEFAULT 与 runtime
        # schema 对齐，跳过序列/函数默认值），使 0019 在 pg_temp 模拟的生产
        # 形态上可重放。此清单来自 S7.3 重建的 runtime 表减去迁移 CREATE 的表。
        conn.execute("""CREATE TEMP TABLE autonomy_health_snapshot (
    "snapshot_id" text NOT NULL,
    "score" double precision NOT NULL DEFAULT 0.0,
    "posture" text DEFAULT ''::text,
    "blockers_json" text NOT NULL DEFAULT '[]'::text,
    "metrics_json" text NOT NULL DEFAULT '{}'::text,
    "trend_json" text NOT NULL DEFAULT '{}'::text,
    "source" text DEFAULT ''::text,
    "created_at" double precision NOT NULL DEFAULT 0.0
)""")
        conn.execute("""CREATE TEMP TABLE autonomy_scope_approval_event (
    "event_id" text NOT NULL,
    "snapshot_id" text DEFAULT ''::text,
    "posture" text DEFAULT ''::text,
    "recommendation_json" text NOT NULL DEFAULT '{}'::text,
    "actor" text DEFAULT ''::text,
    "decision" text DEFAULT ''::text,
    "reason" text DEFAULT ''::text,
    "boundary_json" text NOT NULL DEFAULT '{}'::text,
    "created_at" double precision NOT NULL DEFAULT 0.0
)""")
        conn.execute("""CREATE TEMP TABLE autonomy_scope_enforcement_event (
    "event_id" text NOT NULL,
    "snapshot_id" text DEFAULT ''::text,
    "posture" text DEFAULT ''::text,
    "recommendation_json" text NOT NULL DEFAULT '{}'::text,
    "current_mode" text DEFAULT ''::text,
    "target_mode" text DEFAULT ''::text,
    "status" text DEFAULT ''::text,
    "risk_verdict_json" text NOT NULL DEFAULT '{}'::text,
    "mutation_json" text NOT NULL DEFAULT '{}'::text,
    "actor" text DEFAULT ''::text,
    "reason" text DEFAULT ''::text,
    "boundary_json" text NOT NULL DEFAULT '{}'::text,
    "created_at" double precision NOT NULL DEFAULT 0.0
)""")
        conn.execute("""CREATE TEMP TABLE brain_action_plan (
    "plan_id" text NOT NULL,
    "snapshot_id" text DEFAULT ''::text,
    "hypothesis_id" text DEFAULT ''::text,
    "action_type" text DEFAULT ''::text,
    "status" text DEFAULT 'shadow_recorded',
    "scope_json" text NOT NULL DEFAULT '{}'::text,
    "max_impact" text DEFAULT 'none_shadow_only',
    "risk_class" text DEFAULT ''::text,
    "critic_verdict" text DEFAULT ''::text,
    "validation_refs_json" text NOT NULL DEFAULT '{}'::text,
    "rollback_plan_json" text NOT NULL DEFAULT '{}'::text,
    "required_services_json" text NOT NULL DEFAULT '[]'::text,
    "shadow_eval_json" text NOT NULL DEFAULT '{}'::text,
    "boundary_json" text NOT NULL DEFAULT '{}'::text,
    "created_at" double precision NOT NULL DEFAULT 0.0
)""")
        conn.execute("""CREATE TEMP TABLE brain_governance_candidate (
    "candidate_id" text NOT NULL,
    "source_agent" text DEFAULT ''::text,
    "source_kind" text DEFAULT ''::text,
    "source_ref_type" text DEFAULT ''::text,
    "source_ref_id" text DEFAULT ''::text,
    "proposal_stage" text DEFAULT 'brain_candidate',
    "capability_scope" text DEFAULT ''::text,
    "scope_type" text DEFAULT ''::text,
    "scope_key" text DEFAULT ''::text,
    "action" text DEFAULT ''::text,
    "confidence" real DEFAULT 0.0,
    "evidence_score" real DEFAULT 0.0,
    "risk_class" text DEFAULT ''::text,
    "max_impact" text DEFAULT ''::text,
    "expected_effect_json" text NOT NULL DEFAULT '{}'::text,
    "evidence_refs_json" text NOT NULL DEFAULT '{}'::text,
    "counter_evidence_refs_json" text NOT NULL DEFAULT '{}'::text,
    "risk_verdict_json" text NOT NULL DEFAULT '{}'::text,
    "decision_policy_json" text NOT NULL DEFAULT '{}'::text,
    "rollback_plan_json" text NOT NULL DEFAULT '{}'::text,
    "lineage_json" text NOT NULL DEFAULT '{}'::text,
    "status" text DEFAULT 'active',
    "submitted_suggestion_id" text DEFAULT ''::text,
    "submitted_at" real DEFAULT 0.0,
    "expires_at" real DEFAULT 0.0,
    "created_at" real NOT NULL DEFAULT 0.0,
    "updated_at" real NOT NULL DEFAULT 0.0
)""")
        conn.execute("""CREATE TEMP TABLE brain_live_ready_guardrail (
    "guardrail_id" text NOT NULL,
    "status" text DEFAULT ''::text,
    "live_capability_lock_json" text NOT NULL DEFAULT '{}'::text,
    "broker_local_divergence_json" text NOT NULL DEFAULT '{}'::text,
    "incident_control_json" text NOT NULL DEFAULT '{}'::text,
    "incident_memory_json" text NOT NULL DEFAULT '{}'::text,
    "release_rollback_json" text NOT NULL DEFAULT '{}'::text,
    "p3_p4_evidence_json" text NOT NULL DEFAULT '{}'::text,
    "action_recommendation_json" text NOT NULL DEFAULT '{}'::text,
    "risk_precheck_json" text NOT NULL DEFAULT '{}'::text,
    "boundary_json" text NOT NULL DEFAULT '{}'::text,
    "created_at" double precision NOT NULL DEFAULT 0.0,
    "updated_at" double precision NOT NULL DEFAULT 0.0
)""")
        conn.execute("""CREATE TEMP TABLE brain_low_impact_execution (
    "execution_id" text NOT NULL,
    "plan_id" text DEFAULT ''::text,
    "eval_id" text DEFAULT ''::text,
    "action_type" text DEFAULT ''::text,
    "execution_action" text DEFAULT ''::text,
    "status" text DEFAULT ''::text,
    "evidence_score" double precision NOT NULL DEFAULT 0.0,
    "critic_verdict" text DEFAULT ''::text,
    "comparison_verdict" text DEFAULT ''::text,
    "risk_verdict_json" text NOT NULL DEFAULT '{}'::text,
    "rollback_plan_json" text NOT NULL DEFAULT '{}'::text,
    "result_json" text NOT NULL DEFAULT '{}'::text,
    "posterior_monitor_json" text NOT NULL DEFAULT '{}'::text,
    "boundary_json" text NOT NULL DEFAULT '{}'::text,
    "created_at" double precision NOT NULL DEFAULT 0.0,
    "updated_at" double precision NOT NULL DEFAULT 0.0
)""")
        conn.execute("""CREATE TEMP TABLE brain_memory (
    "memory_id" text NOT NULL,
    "memory_type" text DEFAULT ''::text,
    "source_table" text DEFAULT ''::text,
    "source_id" text DEFAULT ''::text,
    "symbol" text DEFAULT ''::text,
    "timeframe" text DEFAULT ''::text,
    "regime" text DEFAULT ''::text,
    "text_summary" text DEFAULT ''::text,
    "structured_json" text NOT NULL DEFAULT '{}'::text,
    "evidence_score" double precision NOT NULL DEFAULT 0.0,
    "similarity_score" double precision NOT NULL DEFAULT 0.0,
    "polarity" text DEFAULT 'neutral',
    "created_at" double precision NOT NULL DEFAULT 0.0,
    "last_used_at" double precision NOT NULL DEFAULT 0.0
)""")
        conn.execute("""CREATE TEMP TABLE evolution_events (
    "id" integer NOT NULL,
    "timestamp" double precision NOT NULL,
    "event_type" text NOT NULL,
    "payload_json" text NOT NULL DEFAULT '{}'::text
)""")
        conn.execute("""CREATE TEMP TABLE evolution_run (
    "run_id" text NOT NULL,
    "run_type" text NOT NULL,
    "trigger_source" text DEFAULT ''::text,
    "status" text DEFAULT 'running',
    "config_version" integer DEFAULT 0,
    "config_hash" text DEFAULT ''::text,
    "summary_json" text DEFAULT '{}'::text,
    "started_at" double precision NOT NULL DEFAULT 0.0,
    "ended_at" double precision DEFAULT 0.0
)""")
        conn.execute("""CREATE TEMP TABLE experiments (
    "run_id" text NOT NULL,
    "experiment_type" text,
    "params_json" text DEFAULT '{}'::text,
    "metrics_json" text DEFAULT '{}'::text,
    "tags_json" text DEFAULT '[]'::text,
    "artifacts_json" text DEFAULT '[]'::text,
    "status" text DEFAULT 'running',
    "timestamp" real,
    "created_at" real
)""")
        conn.execute("""CREATE TEMP TABLE incident_playbook_event (
    "event_id" text NOT NULL,
    "playbook_id" text NOT NULL,
    "event_type" text DEFAULT ''::text,
    "actor" text DEFAULT ''::text,
    "status" text DEFAULT ''::text,
    "evidence_refs_json" text NOT NULL DEFAULT '{}'::text,
    "notes" text DEFAULT ''::text,
    "boundary_json" text NOT NULL DEFAULT '{}'::text,
    "created_at" double precision NOT NULL DEFAULT 0.0
)""")
        conn.execute("""CREATE TEMP TABLE incident_playbook_run (
    "playbook_id" text NOT NULL,
    "scenario" text DEFAULT ''::text,
    "severity" text DEFAULT ''::text,
    "current_mode" text DEFAULT ''::text,
    "target_mode" text DEFAULT ''::text,
    "status" text DEFAULT ''::text,
    "steps_json" text NOT NULL DEFAULT '[]'::text,
    "risk_precheck_json" text NOT NULL DEFAULT '{}'::text,
    "release_ref_json" text NOT NULL DEFAULT '{}'::text,
    "boundary_json" text NOT NULL DEFAULT '{}'::text,
    "created_by" text DEFAULT ''::text,
    "created_at" double precision NOT NULL DEFAULT 0.0
)""")
        conn.execute("""CREATE TEMP TABLE live_autonomy_unlock_event (
    "event_id" text NOT NULL,
    "action" text DEFAULT ''::text,
    "status" text DEFAULT ''::text,
    "actor" text DEFAULT ''::text,
    "reason" text DEFAULT ''::text,
    "autonomy_mode_before" text DEFAULT ''::text,
    "autonomy_mode_after" text DEFAULT ''::text,
    "readiness_json" text NOT NULL DEFAULT '{}'::text,
    "proposal_registry_json" text NOT NULL DEFAULT '{}'::text,
    "risk_verdict_json" text NOT NULL DEFAULT '{}'::text,
    "blockers_json" text NOT NULL DEFAULT '[]'::text,
    "mutation_json" text NOT NULL DEFAULT '{}'::text,
    "boundary_json" text NOT NULL DEFAULT '{}'::text,
    "created_at" double precision NOT NULL DEFAULT 0.0
)""")
        conn.execute("""CREATE TEMP TABLE model_canary_review (
    "review_id" text NOT NULL,
    "candidate_id" text NOT NULL,
    "model_type" text NOT NULL,
    "decision" text NOT NULL,
    "report_path" text DEFAULT ''::text,
    "metrics_json" text DEFAULT '{}'::text,
    "thresholds_json" text DEFAULT '{}'::text,
    "issues_json" text DEFAULT '[]'::text,
    "note" text DEFAULT ''::text,
    "created_at" double precision DEFAULT 0.0
)""")
        conn.execute("""CREATE TEMP TABLE model_canary_trial (
    "trial_id" text NOT NULL,
    "candidate_id" text NOT NULL,
    "status" text NOT NULL,
    "metrics_json" text DEFAULT '{}'::text,
    "thresholds_json" text DEFAULT '{}'::text,
    "details_json" text DEFAULT '{}'::text,
    "created_at" double precision DEFAULT 0.0
)""")
        conn.execute("""CREATE TEMP TABLE model_inference_audit (
    "inference_id" text NOT NULL,
    "candidate_id" text NOT NULL,
    "model_type" text NOT NULL,
    "mode" text DEFAULT 'advisory',
    "score" double precision DEFAULT 0.0,
    "prediction" integer DEFAULT 0,
    "payload_json" text DEFAULT '{}'::text,
    "result_json" text DEFAULT '{}'::text,
    "created_at" double precision DEFAULT 0.0
)""")
        conn.execute("""CREATE TEMP TABLE model_permission_audit (
    "audit_id" text NOT NULL,
    "model_type" text DEFAULT ''::text,
    "artifact_path" text DEFAULT ''::text,
    "status" text DEFAULT ''::text,
    "reason" text DEFAULT ''::text,
    "capabilities_json" text DEFAULT '{}'::text,
    "violations_json" text DEFAULT '[]'::text,
    "context_json" text DEFAULT '{}'::text,
    "created_at" double precision NOT NULL DEFAULT 0.0
)""")
        conn.execute("""CREATE TEMP TABLE model_shadow_candidate (
    "candidate_id" text NOT NULL,
    "model_type" text NOT NULL,
    "artifact_path" text NOT NULL,
    "artifact_sha256" text NOT NULL,
    "symbol" text DEFAULT 'XAUUSD+',
    "timeframe" text DEFAULT 'M5',
    "status" text DEFAULT 'queued',
    "gate_decision" text DEFAULT ''::text,
    "gate_json" text DEFAULT '{}'::text,
    "registry_version_json" text DEFAULT 'null',
    "note" text DEFAULT ''::text,
    "created_at" double precision DEFAULT 0.0,
    "updated_at" double precision DEFAULT 0.0
)""")
        conn.execute("""CREATE TEMP TABLE parameter_template_active (
    "factor_id" text NOT NULL,
    "regime_key" text NOT NULL DEFAULT ''::text,
    "template_id" text NOT NULL,
    "template_version" text NOT NULL,
    "status" text DEFAULT 'active',
    "suggestion_id" text DEFAULT ''::text,
    "context_json" text DEFAULT '{}'::text,
    "activated_at" double precision NOT NULL DEFAULT 0.0,
    "updated_at" double precision NOT NULL DEFAULT 0.0
)""")
        conn.execute("""CREATE TEMP TABLE parameter_template_registry (
    "template_id" text NOT NULL,
    "factor_id" text NOT NULL,
    "regime_key" text DEFAULT ''::text,
    "template_version" text NOT NULL,
    "template_role" text DEFAULT 'default',
    "factor_family" text DEFAULT ''::text,
    "formula_version" text DEFAULT ''::text,
    "base_parameter_version" text DEFAULT ''::text,
    "parameters_json" text DEFAULT '{}'::text,
    "applicable_regimes_json" text DEFAULT '[]'::text,
    "avoid_regimes_json" text DEFAULT '[]'::text,
    "holding_profile_hint_json" text DEFAULT '{}'::text,
    "evidence_json" text DEFAULT '{}'::text,
    "source" text DEFAULT 'derived',
    "active" integer DEFAULT 0,
    "created_at" double precision NOT NULL DEFAULT 0.0,
    "updated_at" double precision NOT NULL DEFAULT 0.0
)""")
        conn.execute("""CREATE TEMP TABLE parameter_template_release_candidate (
    "candidate_id" text NOT NULL,
    "factor_id" text NOT NULL,
    "template_id" text NOT NULL,
    "regime_key" text DEFAULT ''::text,
    "status" text DEFAULT 'pending_review',
    "boundary_json" text DEFAULT '{}'::text,
    "validation_summary_json" text DEFAULT '{}'::text,
    "validation_report_path" text DEFAULT ''::text,
    "created_at" double precision NOT NULL DEFAULT 0.0,
    "updated_at" double precision NOT NULL DEFAULT 0.0
)""")
        conn.execute("""CREATE TEMP TABLE parameter_template_switch_log (
    "switch_id" text NOT NULL,
    "factor_id" text NOT NULL,
    "regime_key" text DEFAULT ''::text,
    "old_template_id" text DEFAULT ''::text,
    "new_template_id" text NOT NULL,
    "suggestion_id" text DEFAULT ''::text,
    "risk_verdict_json" text DEFAULT '{}'::text,
    "context_json" text DEFAULT '{}'::text,
    "status" text DEFAULT 'applied',
    "created_at" double precision NOT NULL DEFAULT 0.0
)""")
        conn.execute("""CREATE TEMP TABLE release_approval_event (
    "event_id" text NOT NULL,
    "run_id" text NOT NULL,
    "action" text DEFAULT ''::text,
    "actor" text DEFAULT ''::text,
    "decision" text DEFAULT ''::text,
    "reason" text DEFAULT ''::text,
    "evidence_refs_json" text NOT NULL DEFAULT '{}'::text,
    "boundary_json" text NOT NULL DEFAULT '{}'::text,
    "created_at" real NOT NULL DEFAULT 0.0
)""")
        conn.execute("""CREATE TEMP TABLE release_run (
    "run_id" text NOT NULL,
    "release_class" text DEFAULT ''::text,
    "status" text DEFAULT 'started',
    "summary_json" text NOT NULL DEFAULT '{}'::text,
    "checklist_json" text NOT NULL DEFAULT '{}'::text,
    "runtime_config_hash" text DEFAULT ''::text,
    "replay_run_id" text DEFAULT ''::text,
    "replay_artifact_hash" text DEFAULT ''::text,
    "incident_mode" text DEFAULT ''::text,
    "readiness_posture" text DEFAULT ''::text,
    "tests_json" text NOT NULL DEFAULT '[]'::text,
    "rollback_ref_json" text NOT NULL DEFAULT '{}'::text,
    "created_by" text DEFAULT ''::text,
    "created_at" real NOT NULL DEFAULT 0.0,
    "updated_at" real NOT NULL DEFAULT 0.0
)""")
        conn.execute("""CREATE TEMP TABLE replay_report (
    "replay_run_id" text NOT NULL,
    "scope_json" text NOT NULL DEFAULT '{}'::text,
    "input_dataset_hash" text DEFAULT ''::text,
    "runtime_config_hash" text DEFAULT ''::text,
    "code_version" text DEFAULT ''::text,
    "decision_count" integer DEFAULT 0,
    "matched_live_count" integer DEFAULT 0,
    "mismatch_count" integer DEFAULT 0,
    "metric_summary_json" text NOT NULL DEFAULT '{}'::text,
    "replay_error" text DEFAULT ''::text,
    "evidence_grade" text DEFAULT ''::text,
    "artifact_path" text DEFAULT ''::text,
    "artifact_hash" text DEFAULT ''::text,
    "status" text DEFAULT 'completed',
    "created_at" double precision NOT NULL DEFAULT 0.0
)""")
        conn.execute("""CREATE TEMP TABLE shadow_factor_perf (
    "factor" text NOT NULL,
    "source" text DEFAULT 'shadow',
    "symbol" text DEFAULT ''::text,
    "timeframe" text DEFAULT ''::text,
    "oos_bars" integer DEFAULT 0,
    "cumulative_pnl" double precision DEFAULT 0.0,
    "hit_rate" double precision DEFAULT 0.0,
    "max_drawdown" double precision DEFAULT 0.0,
    "last_signal" double precision DEFAULT 0.0,
    "metrics_json" text DEFAULT '{}'::text,
    "updated_at" double precision DEFAULT 0.0
)""")
        conn.execute("""CREATE TEMP TABLE supervisor_counterfactual_review (
    "counterfactual_id" text NOT NULL,
    "review_id" text DEFAULT ''::text,
    "trade_id" text DEFAULT ''::text,
    "position_id" text NOT NULL,
    "close_ts" double precision NOT NULL DEFAULT 0.0,
    "close_reason" text DEFAULT ''::text,
    "supervisor_event_type" text DEFAULT ''::text,
    "supervisor_reason" text DEFAULT ''::text,
    "label" text DEFAULT ''::text,
    "confidence" double precision DEFAULT 0.0,
    "horizons_json" text DEFAULT '[]'::text,
    "evidence_json" text DEFAULT '{}'::text,
    "created_at" double precision NOT NULL DEFAULT 0.0,
    "updated_at" double precision NOT NULL DEFAULT 0.0
)""")

        # 0020 补列迁移同样作用于这三张"生产 ensure_* 预建、迁移链从不建表"
        # 的表（factor_health / position_lifecycle_event / recovery_position_state），
        # 干净 schema 下测试需按其生产预建形态建出（含 0020 新增列，幂等）。
        conn.execute("""CREATE TEMP TABLE factor_health (
    "factor_id" text NOT NULL,
    "health_score" double precision DEFAULT 0.0,
    "status" text DEFAULT 'UNKNOWN'::text,
    "rolling_ic" double precision DEFAULT 0.0,
    "components_json" text NOT NULL DEFAULT '{}'::text,
    "updated_at" double precision NOT NULL DEFAULT 0.0,
    "factor" text,
    "n_obs" integer DEFAULT 0,
    "score" double precision DEFAULT 50.0,
    "section" text DEFAULT 'unknown'::text
)""")
        conn.execute("""CREATE TEMP TABLE position_lifecycle_event (
    "event_id" text NOT NULL,
    "event_type" text DEFAULT ''::text,
    "event_json" text NOT NULL DEFAULT '{}'::text,
    "event_ts" double precision DEFAULT 0.0,
    "created_at" double precision NOT NULL DEFAULT 0.0,
    "avg_price" double precision DEFAULT 0.0,
    "details_json" text DEFAULT '{}'::text,
    "net_volume" double precision DEFAULT 0.0,
    "position_id" text,
    "realized_pnl" double precision DEFAULT 0.0,
    "symbol" text DEFAULT ''::text,
    "trade_id" text DEFAULT ''::text,
    "unrealized_pnl" double precision DEFAULT 0.0
)""")
        conn.execute("""CREATE TEMP TABLE recovery_position_state (
    "position_id" text NOT NULL,
    "recovery_json" text NOT NULL DEFAULT '{}'::text,
    "last_seen_at" double precision DEFAULT 0.0,
    "updated_at" double precision NOT NULL DEFAULT 0.0,
    "broker" text DEFAULT 'ctrader'::text,
    "close_pnl" double precision DEFAULT 0.0,
    "close_reason" text DEFAULT ''::text,
    "closed_at" double precision DEFAULT 0.0,
    "context_integrity" text DEFAULT 'full'::text,
    "direction" integer DEFAULT 0,
    "entry_decision_id" text DEFAULT ''::text,
    "first_seen_at" double precision DEFAULT 0.0,
    "open_price" double precision DEFAULT 0.0,
    "recovery_meta_json" text DEFAULT '{}'::text,
    "status" text DEFAULT 'open'::text,
    "strategy_name" text DEFAULT ''::text,
    "symbol" text DEFAULT ''::text,
    "volume" double precision DEFAULT 0.0
)""")

        # 0019 二级索引回填同时引用了一批"生产代码 DDL 预建、迁移链从未 ADD"的列
        # （autonomous_learning_sample.source_table/source_id 等）。夹具预建的
        # 极简/seed 版不含这些列，迁移在干净 schema 上重放时会因缺列失败。
        # 按生产 ensure_* 预建的真实列对齐夹具（ADD COLUMN IF NOT EXISTS 幂等，
        # 与生产 DDL 类型一致，且与迁移链已 ADD 的列不重复），
        # 使 0019 在 pg_temp 模拟的生产形态上可重放。
        conn.execute("ALTER TABLE autonomous_learning_sample ADD COLUMN IF NOT EXISTS event_ts real")
        conn.execute("ALTER TABLE autonomous_learning_sample ADD COLUMN IF NOT EXISTS label_status text")
        conn.execute("ALTER TABLE autonomous_learning_sample ADD COLUMN IF NOT EXISTS sample_type text")
        conn.execute("ALTER TABLE autonomous_learning_sample ADD COLUMN IF NOT EXISTS source_id text")
        conn.execute("ALTER TABLE autonomous_learning_sample ADD COLUMN IF NOT EXISTS source_table text")
        conn.execute("ALTER TABLE autonomous_learning_sample ADD COLUMN IF NOT EXISTS updated_at real")
        conn.execute("ALTER TABLE brain_action_plan_eval ADD COLUMN IF NOT EXISTS scope_type text")
        conn.execute("ALTER TABLE brain_action_plan_eval ADD COLUMN IF NOT EXISTS status text")
        conn.execute("ALTER TABLE brain_governance_candidate_review ADD COLUMN IF NOT EXISTS review_status text")
        conn.execute("ALTER TABLE brain_medium_impact_governance ADD COLUMN IF NOT EXISTS created_at double precision")
        conn.execute("ALTER TABLE brain_medium_impact_governance ADD COLUMN IF NOT EXISTS eval_id text")
        conn.execute("ALTER TABLE brain_medium_impact_governance ADD COLUMN IF NOT EXISTS plan_id text")
        conn.execute("ALTER TABLE brain_medium_impact_governance ADD COLUMN IF NOT EXISTS scope_type text")
        conn.execute("ALTER TABLE brain_medium_impact_governance ADD COLUMN IF NOT EXISTS status text")
        conn.execute("ALTER TABLE brain_state_snapshot ADD COLUMN IF NOT EXISTS created_at double precision")
        conn.execute("ALTER TABLE brain_state_snapshot ADD COLUMN IF NOT EXISTS status text")
        conn.execute("ALTER TABLE ctrader_deals ADD COLUMN IF NOT EXISTS exec_timestamp double precision")
        conn.execute("ALTER TABLE ctrader_deals ADD COLUMN IF NOT EXISTS position_id integer")
        conn.execute("ALTER TABLE decision_ledger ADD COLUMN IF NOT EXISTS decision_ts double precision")
        conn.execute("ALTER TABLE evolution_decision ADD COLUMN IF NOT EXISTS decision_type text")
        conn.execute("ALTER TABLE evolution_decision ADD COLUMN IF NOT EXISTS run_id text")
        conn.execute("ALTER TABLE experience_memory ADD COLUMN IF NOT EXISTS regime_id text")
        conn.execute("ALTER TABLE experience_memory ADD COLUMN IF NOT EXISTS trade_id text")
        conn.execute("ALTER TABLE learning_application_effect ADD COLUMN IF NOT EXISTS created_at double precision")
        conn.execute("ALTER TABLE learning_application_effect ADD COLUMN IF NOT EXISTS scope text")
        conn.execute("ALTER TABLE learning_application_log ADD COLUMN IF NOT EXISTS created_at double precision")
        conn.execute("ALTER TABLE policy_suggestion ADD COLUMN IF NOT EXISTS scope_key text")
        conn.execute("ALTER TABLE policy_suggestion ADD COLUMN IF NOT EXISTS scope_type text")
        conn.execute("ALTER TABLE position_supervisor_trace ADD COLUMN IF NOT EXISTS action text")
        conn.execute("ALTER TABLE position_supervisor_trace ADD COLUMN IF NOT EXISTS event_ts real")
        conn.execute("ALTER TABLE position_supervisor_trace ADD COLUMN IF NOT EXISTS outcome text")
        conn.execute("ALTER TABLE position_supervisor_trace ADD COLUMN IF NOT EXISTS position_id text")
        conn.execute("ALTER TABLE proposal_registry ADD COLUMN IF NOT EXISTS source_ref_type text")
        conn.execute("ALTER TABLE proposal_registry ADD COLUMN IF NOT EXISTS status text")
        conn.execute("ALTER TABLE runtime_config_snapshot ADD COLUMN IF NOT EXISTS config_hash text")
        conn.execute("ALTER TABLE v16_brain_command ADD COLUMN IF NOT EXISTS scope_key text")
        conn.execute("ALTER TABLE v16_brain_command ADD COLUMN IF NOT EXISTS status text")

        first = run_state_schema_migrations(wrapped, runner_id="pytest_pg_temp")
        second = run_state_schema_migrations(wrapped, runner_id="pytest_pg_temp")
        status = require_state_schema_version(wrapped)

        assert first["applied_count"] == len(STATE_SCHEMA_MIGRATIONS)
        assert second["applied_count"] == 0
        assert status["ok"] is True
        assert wrapped.commit_calls == 2
        assert conn.execute("SELECT to_regclass('broker_execution_intent') AS name").fetchone()["name"]
        assert conn.execute("SELECT to_regclass('idx_jobs_claim_ready') AS name").fetchone()["name"]
        projection_columns = {
            row["column_name"]
            for row in conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema=current_schema()
                  AND table_name='factor_runtime_projection'
                """
            ).fetchall()
        }
        assert {"mutation_id", "config_version", "config_hash"} <= projection_columns
        sample_columns = {
            row["column_name"]
            for row in conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema=current_schema()
                  AND table_name='autonomous_learning_sample'
                """
            ).fetchall()
        }
        assert {
            "system_contaminated",
            "governance_eligible",
            "governance_effective_weight",
            "governance_eligibility_version",
            "governance_eligibility_fingerprint",
            "governance_ineligible_reason",
        } <= sample_columns
        stats_columns = {
            row["column_name"]
            for row in conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema=current_schema()
                  AND table_name='experience_pattern_stats'
                """
            ).fetchall()
        }
        assert {
            "effective_sample_count",
            "weighted_win_count",
            "weighted_bad_loss_count",
            "weighted_avg_reward",
            "governance_eligibility_version",
            "governance_eligibility_fingerprint",
        } <= stats_columns
        runtime_writer_columns = {
            "factor_governance_shadow_audit": {"inference_id", "factor", "payload_json", "created_at"},
            "llm_advisory_audit": {"audit_id", "target_type", "response_json", "created_at"},
            "meta_model_shadow_audit": {"inference_id", "posture", "ledger_decision_id", "created_at"},
            "meta_shadow_report_snapshot": {"report_id", "model_type", "payload_json", "created_at"},
            "model_influence_decision": {"influence_id", "control_surface", "fused_decision_json", "created_at"},
            "model_influence_effect": {"effect_id", "influence_id", "outcome_json", "matured_at"},
            "offmarket_high_load_job_audit": {
                "audit_id", "job_name", "payload_json", "finished_at",
                "training_window_key", "phase", "worker_instance_id",
                "heartbeat_at", "input_bytes_estimate",
            },
            "state_payload_archive": {
                "archive_hash", "source_table", "source_id", "raw_sha256", "payload_bytes",
            },
            "open_quality_shadow_audit": {"inference_id", "decision_id", "quality_score", "created_at"},
            "position_quality_shadow_audit": {"inference_id", "position_id", "hold_score", "created_at"},
        }
        for table, required_columns in runtime_writer_columns.items():
            actual = {
                row["column_name"]
                for row in conn.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema=current_schema() AND table_name=%s
                    """,
                    (table,),
                ).fetchall()
            }
            assert required_columns <= actual, table
        for index in (
            "idx_factor_governance_audit_created",
            "idx_llm_advisory_audit_target",
            "idx_meta_shadow_report_snapshot_created",
            "idx_model_influence_decision_subject_ts",
            "idx_open_quality_shadow_audit_position",
            "idx_position_quality_shadow_audit_position",
            "idx_experience_memory_source",
            "idx_experience_memory_source_append",
            "idx_factor_catalog_snapshot_created",
        ):
            assert conn.execute("SELECT to_regclass(%s) AS name", (index,)).fetchone()["name"], index
    finally:
        conn.rollback()
        conn.close()


def test_auth_logout_revokes_entire_rotated_family_in_postgres(monkeypatch) -> None:
    dsn = os.environ.get("QUANT_STATE_PG_DSN", "").strip()
    if not dsn:
        pytest.skip("QUANT_STATE_PG_DSN is not configured")

    from backend.core import db as db_module
    from backend.services.auth_sessions import (
        create_refresh_session,
        revoke_refresh_session,
        rotate_refresh_session,
        session_family_ids,
        session_is_active,
        step_up_refresh_session,
    )

    schema = f"pytest_auth_session_{uuid.uuid4().hex}"
    admin = psycopg.connect(dsn, autocommit=False, row_factory=dict_row)
    admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    admin.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema)))
    admin.execute(
        """
        CREATE TABLE auth_session (
            session_id TEXT PRIMARY KEY,
            subject TEXT NOT NULL,
            token_jti TEXT NOT NULL DEFAULT '',
            token_hash TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            client_fingerprint TEXT NOT NULL DEFAULT '',
            ip_hash TEXT NOT NULL DEFAULT '',
            user_agent TEXT NOT NULL DEFAULT '',
            issued_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            expires_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            last_seen_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            revoked_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            revoked_by TEXT NOT NULL DEFAULT '',
            revoke_reason TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            updated_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
        )
        """
    )
    admin.commit()

    def _connect(*, read_only: bool = False):
        del read_only
        conn = psycopg.connect(dsn, autocommit=False, row_factory=dict_row)
        conn.execute(
            sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema))
        )
        return conn

    monkeypatch.delenv("QUANT_AUTH_SESSION_STORE", raising=False)
    monkeypatch.setattr(db_module, "get_state_pg_conn", _connect)
    try:
        first = create_refresh_session("operator", now=1_000.0, auth_time=1_000)
        stepped_up = step_up_refresh_session(
            first.session_id,
            subject="operator",
            family_id=first.family_id,
            now=1_050.0,
        )
        assert stepped_up.auth_time == 1_050
        second = rotate_refresh_session(first.refresh_token, now=1_100.0)
        assert second.auth_time == 1_050
        family_id, members = session_family_ids(second.session_id)
        assert family_id == first.family_id == second.family_id
        assert set(members) == {first.session_id, second.session_id}
        assert session_is_active(second.session_id, subject="operator", now=1_200.0)

        assert revoke_refresh_session(
            session_id=second.session_id,
            token=second.refresh_token,
            now=1_300.0,
        )
        assert session_is_active(first.session_id, subject="operator", now=1_301.0) is False
        assert session_is_active(second.session_id, subject="operator", now=1_301.0) is False
        rows = admin.execute(
            "SELECT session_id, status FROM auth_session ORDER BY session_id"
        ).fetchall()
        assert {str(row["status"]) for row in rows} == {"revoked"}
    finally:
        admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        admin.commit()
        admin.close()
