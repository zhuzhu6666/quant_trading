-- State payload interning and mutation projection lineage.
-- This migration is schema-only. Historical backfill and table compaction are
-- intentionally owned by scripts/state_payload_compact.py.

CREATE TABLE IF NOT EXISTS runtime_config_payload (
    payload_hash TEXT PRIMARY KEY,
    config_json TEXT NOT NULL,
    byte_length BIGINT NOT NULL DEFAULT 0,
    created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS brain_action_plan_eval_payload (
    payload_hash TEXT PRIMARY KEY,
    comparison_json TEXT NOT NULL DEFAULT '{}',
    evidence_refs_json TEXT NOT NULL DEFAULT '{}',
    boundary_json TEXT NOT NULL DEFAULT '{}',
    byte_length BIGINT NOT NULL DEFAULT 0,
    created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS mutation_payload (
    payload_hash TEXT PRIMARY KEY,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    risk_verdict_json TEXT NOT NULL DEFAULT '{}',
    before_json TEXT NOT NULL DEFAULT '{}',
    after_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    rollback_json TEXT NOT NULL DEFAULT '{}',
    byte_length BIGINT NOT NULL DEFAULT 0,
    created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
);

ALTER TABLE runtime_config_snapshot
    ADD COLUMN IF NOT EXISTS payload_hash TEXT NOT NULL DEFAULT '';

ALTER TABLE brain_action_plan_eval
    ADD COLUMN IF NOT EXISTS payload_hash TEXT NOT NULL DEFAULT '';

ALTER TABLE brain_action_plan_eval
    ADD COLUMN IF NOT EXISTS evaluation_run_id TEXT NOT NULL DEFAULT '';

ALTER TABLE evolution_decision
    ADD COLUMN IF NOT EXISTS payload_hash TEXT NOT NULL DEFAULT '';

ALTER TABLE evolution_decision
    ADD COLUMN IF NOT EXISTS canonical_event_id TEXT NOT NULL DEFAULT '';

ALTER TABLE evolution_decision
    ADD COLUMN IF NOT EXISTS projection_type TEXT NOT NULL DEFAULT 'legacy';

ALTER TABLE autonomous_learning_sample
    ADD COLUMN IF NOT EXISTS content_fingerprint TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_runtime_config_payload_created
    ON runtime_config_payload(created_at);

CREATE INDEX IF NOT EXISTS idx_brain_action_plan_eval_payload_created
    ON brain_action_plan_eval_payload(created_at);

CREATE INDEX IF NOT EXISTS idx_mutation_payload_created
    ON mutation_payload(created_at);

CREATE INDEX IF NOT EXISTS idx_runtime_config_snapshot_payload
    ON runtime_config_snapshot(payload_hash, config_version);

CREATE INDEX IF NOT EXISTS idx_brain_action_plan_eval_payload
    ON brain_action_plan_eval(payload_hash, created_at);

CREATE INDEX IF NOT EXISTS idx_brain_action_plan_eval_run_plan
    ON brain_action_plan_eval(evaluation_run_id, plan_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_brain_action_plan_eval_run_plan_unique
    ON brain_action_plan_eval(evaluation_run_id, plan_id)
    WHERE evaluation_run_id <> '';

CREATE INDEX IF NOT EXISTS idx_evolution_decision_canonical
    ON evolution_decision(canonical_event_id, projection_type, created_at);

CREATE INDEX IF NOT EXISTS idx_autonomous_learning_sample_fingerprint
    ON autonomous_learning_sample(content_fingerprint, updated_at);
