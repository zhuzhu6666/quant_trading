-- Clean-install legacy baseline for the versioned runtime migration chain.
-- This file is used only when the target runtime schema has no tables.
-- Existing installations never replay it and migrations 0001+ remain checksum-stable.

CREATE TABLE autonomous_learning_sample (
        sample_id TEXT PRIMARY KEY,
        sample_type TEXT NOT NULL DEFAULT '',
        event_ts DOUBLE PRECISION NOT NULL DEFAULT 0.0
    );

CREATE TABLE decision_ledger (seed INTEGER);

CREATE TABLE learning_application_log (
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
    );

CREATE TABLE learning_application_effect (
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
    );

CREATE TABLE learning_experiment_reservation (
        reservation_id TEXT PRIMARY KEY,
        scope_type TEXT NOT NULL DEFAULT '',
        scope_key TEXT NOT NULL DEFAULT '',
        action TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'reserved',
        application_id TEXT NOT NULL DEFAULT '',
        expires_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
        updated_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
    );

CREATE TABLE order_lifecycle_event (
        event_id TEXT PRIMARY KEY,
        event_ts DOUBLE PRECISION NOT NULL DEFAULT 0.0
    );

CREATE TABLE trade_outcome_review (
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
    );

CREATE TABLE factor_contribution_review (
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
    );

CREATE TABLE factor_health (
        factor TEXT PRIMARY KEY,
        score DOUBLE PRECISION DEFAULT 50.0,
        status TEXT DEFAULT 'UNKNOWN',
        section TEXT DEFAULT 'unknown',
        components_json TEXT DEFAULT '{}',
        n_obs INTEGER DEFAULT 0,
        rolling_ic DOUBLE PRECISION DEFAULT 0.0,
        updated_at DOUBLE PRECISION
    );

CREATE TABLE decision_factor_snapshot (
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
    );

CREATE TABLE ctrader_deals (
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
    );

CREATE TABLE policy_suggestion (
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
    );

CREATE TABLE runtime_config_overlay (seed INTEGER);

CREATE TABLE runtime_config_snapshot (
        config_version BIGSERIAL PRIMARY KEY,
        config_hash TEXT NOT NULL,
        source TEXT DEFAULT '',
        config_json TEXT NOT NULL DEFAULT '{}',
        run_id TEXT DEFAULT '',
        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
    );

CREATE TABLE evolution_run (
        run_id TEXT PRIMARY KEY,
        run_type TEXT NOT NULL,
        trigger_source TEXT DEFAULT '',
        status TEXT DEFAULT 'running',
        config_version INTEGER DEFAULT 0,
        config_hash TEXT DEFAULT '',
        summary_json TEXT DEFAULT '{}',
        started_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
        ended_at DOUBLE PRECISION DEFAULT 0.0
    );

CREATE TABLE evolution_decision (
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
    );

CREATE TABLE position_supervisor_trace (
        trace_id TEXT PRIMARY KEY,
        position_id TEXT NOT NULL DEFAULT '',
        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
    );

CREATE TABLE brain_state_snapshot (
        snapshot_id TEXT PRIMARY KEY,
        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
    );

CREATE TABLE brain_medium_impact_governance (
        governance_id TEXT PRIMARY KEY,
        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
    );

CREATE TABLE v16_brain_command (
        command_id TEXT PRIMARY KEY,
        target_agent TEXT NOT NULL DEFAULT '',
        scope_type TEXT NOT NULL DEFAULT '',
        decision TEXT NOT NULL DEFAULT '',
        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
        updated_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
    );

CREATE TABLE brain_governance_candidate_review (
        review_id TEXT PRIMARY KEY,
        candidate_id TEXT NOT NULL DEFAULT '',
        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
    );

CREATE TABLE proposal_registry (
        proposal_id TEXT PRIMARY KEY,
        source_agent TEXT NOT NULL DEFAULT '',
        proposal_type TEXT NOT NULL DEFAULT '',
        control_surface TEXT NOT NULL DEFAULT '',
        target_scope TEXT NOT NULL DEFAULT ''
    );

CREATE TABLE jobs (
        id TEXT PRIMARY KEY,
        kind TEXT DEFAULT '',
        status TEXT DEFAULT 'pending',
        params_json TEXT DEFAULT '{}',
        result_json TEXT DEFAULT '{}',
        progress DOUBLE PRECISION DEFAULT 0.0,
        error TEXT DEFAULT '',
        created_at DOUBLE PRECISION,
        updated_at DOUBLE PRECISION
    );

CREATE TABLE experience_memory (
        experience_id TEXT PRIMARY KEY,
        source_table TEXT NOT NULL DEFAULT '',
        source_id TEXT NOT NULL DEFAULT '',
        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
    );

CREATE TABLE experience_pattern_stats (
        scope_type TEXT NOT NULL,
        scope_key TEXT NOT NULL,
        PRIMARY KEY (scope_type, scope_key)
    );

CREATE TABLE factor_catalog_snapshot (
        snapshot_id TEXT PRIMARY KEY,
        created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
    );

-- Tables formerly created by application ensure paths.  Historical
-- migrations reference them before any catalogued CREATE, so clean installs
-- must materialize their frozen pre-migration shapes here.

CREATE TABLE brain_action_plan_eval (
    eval_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL DEFAULT '',
    created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS autonomy_health_snapshot (
    "snapshot_id" text NOT NULL,
    "score" double precision NOT NULL DEFAULT 0.0,
    "posture" text DEFAULT ''::text,
    "blockers_json" text NOT NULL DEFAULT '[]'::text,
    "metrics_json" text NOT NULL DEFAULT '{}'::text,
    "trend_json" text NOT NULL DEFAULT '{}'::text,
    "source" text DEFAULT ''::text,
    "created_at" double precision NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS autonomy_scope_approval_event (
    "event_id" text NOT NULL,
    "snapshot_id" text DEFAULT ''::text,
    "posture" text DEFAULT ''::text,
    "recommendation_json" text NOT NULL DEFAULT '{}'::text,
    "actor" text DEFAULT ''::text,
    "decision" text DEFAULT ''::text,
    "reason" text DEFAULT ''::text,
    "boundary_json" text NOT NULL DEFAULT '{}'::text,
    "created_at" double precision NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS autonomy_scope_enforcement_event (
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
);

CREATE TABLE IF NOT EXISTS brain_action_plan (
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
);

CREATE TABLE IF NOT EXISTS brain_governance_candidate (
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
);

CREATE TABLE IF NOT EXISTS brain_live_ready_guardrail (
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
);

CREATE TABLE IF NOT EXISTS brain_low_impact_execution (
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
);

CREATE TABLE IF NOT EXISTS brain_memory (
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
);

CREATE TABLE IF NOT EXISTS evolution_events (
    "id" integer NOT NULL,
    "timestamp" double precision NOT NULL,
    "event_type" text NOT NULL,
    "payload_json" text NOT NULL DEFAULT '{}'::text
);

CREATE TABLE IF NOT EXISTS experiments (
    "run_id" text NOT NULL,
    "experiment_type" text,
    "params_json" text DEFAULT '{}'::text,
    "metrics_json" text DEFAULT '{}'::text,
    "tags_json" text DEFAULT '[]'::text,
    "artifacts_json" text DEFAULT '[]'::text,
    "status" text DEFAULT 'running',
    "timestamp" real,
    "created_at" real
);

CREATE TABLE IF NOT EXISTS incident_playbook_event (
    "event_id" text NOT NULL,
    "playbook_id" text NOT NULL,
    "event_type" text DEFAULT ''::text,
    "actor" text DEFAULT ''::text,
    "status" text DEFAULT ''::text,
    "evidence_refs_json" text NOT NULL DEFAULT '{}'::text,
    "notes" text DEFAULT ''::text,
    "boundary_json" text NOT NULL DEFAULT '{}'::text,
    "created_at" double precision NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS incident_playbook_run (
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
);

CREATE TABLE IF NOT EXISTS live_autonomy_unlock_event (
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
);

CREATE TABLE IF NOT EXISTS model_canary_review (
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
);

CREATE TABLE IF NOT EXISTS model_canary_trial (
    "trial_id" text NOT NULL,
    "candidate_id" text NOT NULL,
    "status" text NOT NULL,
    "metrics_json" text DEFAULT '{}'::text,
    "thresholds_json" text DEFAULT '{}'::text,
    "details_json" text DEFAULT '{}'::text,
    "created_at" double precision DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS model_inference_audit (
    "inference_id" text NOT NULL,
    "candidate_id" text NOT NULL,
    "model_type" text NOT NULL,
    "mode" text DEFAULT 'advisory',
    "score" double precision DEFAULT 0.0,
    "prediction" integer DEFAULT 0,
    "payload_json" text DEFAULT '{}'::text,
    "result_json" text DEFAULT '{}'::text,
    "created_at" double precision DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS model_permission_audit (
    "audit_id" text NOT NULL,
    "model_type" text DEFAULT ''::text,
    "artifact_path" text DEFAULT ''::text,
    "status" text DEFAULT ''::text,
    "reason" text DEFAULT ''::text,
    "capabilities_json" text DEFAULT '{}'::text,
    "violations_json" text DEFAULT '[]'::text,
    "context_json" text DEFAULT '{}'::text,
    "created_at" double precision NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS model_shadow_candidate (
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
);

CREATE TABLE IF NOT EXISTS parameter_template_active (
    "factor_id" text NOT NULL,
    "regime_key" text NOT NULL DEFAULT ''::text,
    "template_id" text NOT NULL,
    "template_version" text NOT NULL,
    "status" text DEFAULT 'active',
    "suggestion_id" text DEFAULT ''::text,
    "context_json" text DEFAULT '{}'::text,
    "activated_at" double precision NOT NULL DEFAULT 0.0,
    "updated_at" double precision NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS parameter_template_registry (
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
);

CREATE TABLE IF NOT EXISTS parameter_template_release_candidate (
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
);

CREATE TABLE IF NOT EXISTS parameter_template_switch_log (
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
);

CREATE TABLE IF NOT EXISTS release_approval_event (
    "event_id" text NOT NULL,
    "run_id" text NOT NULL,
    "action" text DEFAULT ''::text,
    "actor" text DEFAULT ''::text,
    "decision" text DEFAULT ''::text,
    "reason" text DEFAULT ''::text,
    "evidence_refs_json" text NOT NULL DEFAULT '{}'::text,
    "boundary_json" text NOT NULL DEFAULT '{}'::text,
    "created_at" real NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS release_run (
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
);

CREATE TABLE IF NOT EXISTS replay_report (
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
);

CREATE TABLE IF NOT EXISTS shadow_factor_perf (
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
);

CREATE TABLE IF NOT EXISTS supervisor_counterfactual_review (
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
);

CREATE TABLE IF NOT EXISTS position_lifecycle_event (
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
);

CREATE TABLE IF NOT EXISTS recovery_position_state (
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
);

ALTER TABLE autonomous_learning_sample ADD COLUMN IF NOT EXISTS event_ts real;

ALTER TABLE autonomous_learning_sample ADD COLUMN IF NOT EXISTS label_status text;

ALTER TABLE autonomous_learning_sample ADD COLUMN IF NOT EXISTS sample_type text;

ALTER TABLE autonomous_learning_sample ADD COLUMN IF NOT EXISTS source_id text;

ALTER TABLE autonomous_learning_sample ADD COLUMN IF NOT EXISTS source_table text;

ALTER TABLE autonomous_learning_sample ADD COLUMN IF NOT EXISTS updated_at real;

ALTER TABLE brain_action_plan_eval ADD COLUMN IF NOT EXISTS scope_type text;

ALTER TABLE brain_action_plan_eval ADD COLUMN IF NOT EXISTS status text;

ALTER TABLE brain_governance_candidate_review ADD COLUMN IF NOT EXISTS review_status text;

ALTER TABLE brain_medium_impact_governance ADD COLUMN IF NOT EXISTS created_at double precision;

ALTER TABLE brain_medium_impact_governance ADD COLUMN IF NOT EXISTS eval_id text;

ALTER TABLE brain_medium_impact_governance ADD COLUMN IF NOT EXISTS plan_id text;

ALTER TABLE brain_medium_impact_governance ADD COLUMN IF NOT EXISTS scope_type text;

ALTER TABLE brain_medium_impact_governance ADD COLUMN IF NOT EXISTS status text;

ALTER TABLE brain_state_snapshot ADD COLUMN IF NOT EXISTS created_at double precision;

ALTER TABLE brain_state_snapshot ADD COLUMN IF NOT EXISTS status text;

ALTER TABLE ctrader_deals ADD COLUMN IF NOT EXISTS exec_timestamp double precision;

ALTER TABLE ctrader_deals ADD COLUMN IF NOT EXISTS position_id integer;

ALTER TABLE decision_ledger ADD COLUMN IF NOT EXISTS decision_ts double precision;

ALTER TABLE evolution_decision ADD COLUMN IF NOT EXISTS decision_type text;

ALTER TABLE evolution_decision ADD COLUMN IF NOT EXISTS run_id text;

ALTER TABLE experience_memory ADD COLUMN IF NOT EXISTS regime_id text;

ALTER TABLE experience_memory ADD COLUMN IF NOT EXISTS trade_id text;

ALTER TABLE learning_application_effect ADD COLUMN IF NOT EXISTS created_at double precision;

ALTER TABLE learning_application_effect ADD COLUMN IF NOT EXISTS scope text;

ALTER TABLE learning_application_log ADD COLUMN IF NOT EXISTS created_at double precision;

ALTER TABLE policy_suggestion ADD COLUMN IF NOT EXISTS scope_key text;

ALTER TABLE policy_suggestion ADD COLUMN IF NOT EXISTS scope_type text;

ALTER TABLE position_supervisor_trace ADD COLUMN IF NOT EXISTS action text;

ALTER TABLE position_supervisor_trace ADD COLUMN IF NOT EXISTS event_ts real;

ALTER TABLE position_supervisor_trace ADD COLUMN IF NOT EXISTS outcome text;

ALTER TABLE position_supervisor_trace ADD COLUMN IF NOT EXISTS position_id text;

ALTER TABLE proposal_registry ADD COLUMN IF NOT EXISTS source_ref_type text;

ALTER TABLE proposal_registry ADD COLUMN IF NOT EXISTS status text;

ALTER TABLE runtime_config_snapshot ADD COLUMN IF NOT EXISTS config_hash text;

ALTER TABLE v16_brain_command ADD COLUMN IF NOT EXISTS scope_key text;

ALTER TABLE v16_brain_command ADD COLUMN IF NOT EXISTS status text;

ALTER TABLE factor_health
    ADD COLUMN IF NOT EXISTS factor_id TEXT,
    ADD COLUMN IF NOT EXISTS health_score DOUBLE PRECISION DEFAULT 0.0;
