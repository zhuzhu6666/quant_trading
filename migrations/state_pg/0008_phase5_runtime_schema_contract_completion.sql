-- Phase 5: complete the runtime schema-writer retirement contract without
-- changing or dropping the legacy idx_experience_memory_source index.
--
-- Migration 0004 materialized that historical name with created_at as the
-- third key, while the learning/backfill idempotency paths query append_source.
-- A new name keeps the migration additive and makes the required definition
-- unambiguous for catalog validation.

CREATE TABLE IF NOT EXISTS runtime_kv (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL DEFAULT '{}',
    updated_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
);

ALTER TABLE runtime_kv
    ADD COLUMN IF NOT EXISTS value_json TEXT NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS updated_at DOUBLE PRECISION NOT NULL DEFAULT 0.0;

CREATE INDEX IF NOT EXISTS idx_runtime_kv_updated
    ON runtime_kv(updated_at);

CREATE TABLE IF NOT EXISTS canary_state (
    factor_name TEXT PRIMARY KEY,
    stage TEXT NOT NULL DEFAULT 'SHADOW',
    oos_bars INTEGER NOT NULL DEFAULT 0,
    cumulative_pnl DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    promote_time DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    rollback_count INTEGER NOT NULL DEFAULT 0,
    evidence_hash TEXT NOT NULL DEFAULT '',
    dataset_hash TEXT NOT NULL DEFAULT '',
    evidence_end_at TEXT NOT NULL DEFAULT '',
    stage_evidence_hash TEXT NOT NULL DEFAULT '',
    fresh_evidence_bars INTEGER NOT NULL DEFAULT 0,
    events_json TEXT NOT NULL DEFAULT '[]',
    updated_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
);

ALTER TABLE canary_state
    ADD COLUMN IF NOT EXISTS rollback_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS evidence_hash TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS dataset_hash TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS evidence_end_at TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS stage_evidence_hash TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS fresh_evidence_bars INTEGER NOT NULL DEFAULT 0;

ALTER TABLE autonomous_learning_sample
    ADD COLUMN IF NOT EXISTS evidence_contract_json TEXT NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS config_version INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS config_hash TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS evolution_run_id TEXT NOT NULL DEFAULT '';

ALTER TABLE position_supervisor_trace
    ADD COLUMN IF NOT EXISTS trace_integrity TEXT NOT NULL DEFAULT 'full',
    ADD COLUMN IF NOT EXISTS config_version INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS config_hash TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS evolution_run_id TEXT NOT NULL DEFAULT '';

ALTER TABLE proposal_registry
    ADD COLUMN IF NOT EXISTS source_reliability_json TEXT NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS evidence_freshness_json TEXT NOT NULL DEFAULT '{}';

ALTER TABLE brain_state_snapshot
    ADD COLUMN IF NOT EXISTS memory_json TEXT NOT NULL DEFAULT '{}';

ALTER TABLE brain_medium_impact_governance
    ADD COLUMN IF NOT EXISTS candidate_id TEXT NOT NULL DEFAULT '';

ALTER TABLE experience_memory
    ADD COLUMN IF NOT EXISTS source_table TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS source_id TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS append_source TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS evolution_run_id TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_experience_memory_source_append
    ON experience_memory(source_table, source_id, append_source);
