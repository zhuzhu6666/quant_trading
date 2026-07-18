-- Phase 5: materialize every PostgreSQL object that was still created by
-- backend/learning-worker compatibility ``ensure`` paths.  Runtime
-- connections reinterpret those legacy idempotent statements as catalog
-- validation after this migration, and only the explicit migration CLI executes
-- this DDL.  Existing production objects are retained with IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS factor_governance_shadow_audit (
    inference_id TEXT PRIMARY KEY,
    model_type TEXT NOT NULL,
    model_version TEXT DEFAULT '',
    artifact_path TEXT DEFAULT '',
    review_id TEXT DEFAULT '',
    trade_id TEXT DEFAULT '',
    position_id TEXT DEFAULT '',
    factor TEXT DEFAULT '',
    mode TEXT DEFAULT 'shadow',
    positive_score REAL DEFAULT 0.0,
    weakness_score REAL DEFAULT 0.0,
    prediction INTEGER DEFAULT 0,
    payload_json TEXT DEFAULT '{}',
    result_json TEXT DEFAULT '{}',
    created_at REAL NOT NULL DEFAULT 0.0
);

CREATE INDEX IF NOT EXISTS idx_factor_governance_audit_created
    ON factor_governance_shadow_audit(created_at);
CREATE INDEX IF NOT EXISTS idx_factor_governance_audit_factor
    ON factor_governance_shadow_audit(factor, created_at);

CREATE TABLE IF NOT EXISTS llm_advisory_audit (
    audit_id TEXT PRIMARY KEY,
    provider TEXT DEFAULT '',
    model TEXT DEFAULT '',
    task_type TEXT DEFAULT '',
    target_type TEXT DEFAULT '',
    target_id TEXT DEFAULT '',
    status TEXT DEFAULT '',
    prompt_json TEXT DEFAULT '{}',
    response_json TEXT DEFAULT '{}',
    result_json TEXT DEFAULT '{}',
    error TEXT DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0.0
);

CREATE INDEX IF NOT EXISTS idx_llm_advisory_audit_created
    ON llm_advisory_audit(created_at);
CREATE INDEX IF NOT EXISTS idx_llm_advisory_audit_target
    ON llm_advisory_audit(target_type, target_id, created_at);

CREATE TABLE IF NOT EXISTS meta_model_shadow_audit (
    inference_id TEXT PRIMARY KEY,
    model_type TEXT NOT NULL,
    model_version TEXT DEFAULT '',
    artifact_path TEXT DEFAULT '',
    mode TEXT DEFAULT 'shadow',
    posture TEXT DEFAULT '',
    posture_score REAL DEFAULT 0.0,
    contract_score REAL DEFAULT 0.0,
    observe_score REAL DEFAULT 0.0,
    recover_score REAL DEFAULT 0.0,
    ledger_decision_id TEXT DEFAULT '',
    payload_json TEXT DEFAULT '{}',
    result_json TEXT DEFAULT '{}',
    created_at REAL NOT NULL DEFAULT 0.0
);

CREATE INDEX IF NOT EXISTS idx_meta_model_shadow_audit_created
    ON meta_model_shadow_audit(created_at);

CREATE TABLE IF NOT EXISTS meta_shadow_report_snapshot (
    report_id TEXT PRIMARY KEY,
    model_type TEXT NOT NULL,
    model_version TEXT DEFAULT '',
    source TEXT DEFAULT '',
    accuracy REAL DEFAULT 0.0,
    evaluated_count INTEGER DEFAULT 0,
    audit_count INTEGER DEFAULT 0,
    artifact_path TEXT DEFAULT '',
    payload_json TEXT DEFAULT '{}',
    created_at REAL NOT NULL DEFAULT 0.0
);

CREATE INDEX IF NOT EXISTS idx_meta_shadow_report_snapshot_created
    ON meta_shadow_report_snapshot(created_at);

CREATE TABLE IF NOT EXISTS model_influence_decision (
    influence_id TEXT PRIMARY KEY,
    model_type TEXT NOT NULL,
    model_version TEXT DEFAULT '',
    artifact_sha256 TEXT DEFAULT '',
    stage TEXT NOT NULL,
    control_surface TEXT NOT NULL,
    subject_id TEXT DEFAULT '',
    rule_decision_json TEXT NOT NULL DEFAULT '{}',
    model_result_json TEXT NOT NULL DEFAULT '{}',
    fused_decision_json TEXT NOT NULL DEFAULT '{}',
    applied INTEGER NOT NULL DEFAULT 0,
    reason TEXT DEFAULT '',
    created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
);

CREATE INDEX IF NOT EXISTS idx_model_influence_decision_model_ts
    ON model_influence_decision(model_type, created_at);
CREATE INDEX IF NOT EXISTS idx_model_influence_decision_subject_ts
    ON model_influence_decision(subject_id, created_at);

CREATE TABLE IF NOT EXISTS model_influence_effect (
    effect_id TEXT PRIMARY KEY,
    influence_id TEXT NOT NULL,
    model_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    utility_delta DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    outcome_json TEXT NOT NULL DEFAULT '{}',
    created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    matured_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS offmarket_high_load_job_audit (
    audit_id TEXT PRIMARY KEY,
    job_name TEXT NOT NULL,
    status TEXT NOT NULL,
    session_status TEXT DEFAULT '',
    high_load_profile TEXT DEFAULT '',
    payload_json TEXT DEFAULT '{}',
    result_json TEXT DEFAULT '{}',
    error TEXT DEFAULT '',
    started_at REAL NOT NULL,
    finished_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS open_quality_shadow_audit (
    inference_id TEXT PRIMARY KEY,
    model_type TEXT NOT NULL,
    model_version TEXT DEFAULT '',
    artifact_path TEXT DEFAULT '',
    sample_id TEXT DEFAULT '',
    decision_id TEXT DEFAULT '',
    trade_id TEXT DEFAULT '',
    position_id TEXT DEFAULT '',
    mode TEXT DEFAULT 'shadow',
    quality_score REAL DEFAULT 0.0,
    risk_score REAL DEFAULT 0.0,
    prediction INTEGER DEFAULT 0,
    payload_json TEXT DEFAULT '{}',
    result_json TEXT DEFAULT '{}',
    created_at REAL NOT NULL DEFAULT 0.0
);

CREATE INDEX IF NOT EXISTS idx_open_quality_shadow_audit_created
    ON open_quality_shadow_audit(created_at);
CREATE INDEX IF NOT EXISTS idx_open_quality_shadow_audit_position
    ON open_quality_shadow_audit(position_id, created_at);

CREATE TABLE IF NOT EXISTS position_quality_shadow_audit (
    inference_id TEXT PRIMARY KEY,
    model_type TEXT NOT NULL,
    model_version TEXT DEFAULT '',
    artifact_path TEXT DEFAULT '',
    review_id TEXT DEFAULT '',
    trade_id TEXT DEFAULT '',
    position_id TEXT DEFAULT '',
    mode TEXT DEFAULT 'shadow',
    hold_score REAL DEFAULT 0.0,
    exit_risk_score REAL DEFAULT 0.0,
    prediction INTEGER DEFAULT 0,
    payload_json TEXT DEFAULT '{}',
    result_json TEXT DEFAULT '{}',
    created_at REAL NOT NULL DEFAULT 0.0
);

CREATE INDEX IF NOT EXISTS idx_position_quality_shadow_audit_created
    ON position_quality_shadow_audit(created_at);
CREATE INDEX IF NOT EXISTS idx_position_quality_shadow_audit_position
    ON position_quality_shadow_audit(position_id, created_at);

CREATE INDEX IF NOT EXISTS idx_experience_memory_source
    ON experience_memory(source_table, source_id, created_at);
CREATE INDEX IF NOT EXISTS idx_factor_catalog_snapshot_created
    ON factor_catalog_snapshot(created_at);
