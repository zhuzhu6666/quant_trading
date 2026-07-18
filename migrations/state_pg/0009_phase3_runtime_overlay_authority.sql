-- Phase 3: bind startup/runtime RuntimeConfig projection to committed
-- governance facts. Legacy controls are reviewed per top-level overlay key;
-- the JSON manifest is hash-bound and may only describe tightening controls.

ALTER TABLE runtime_config_overlay
    ADD COLUMN IF NOT EXISTS legacy_authority_json TEXT NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS updated_at DOUBLE PRECISION NOT NULL DEFAULT 0.0;

CREATE INDEX IF NOT EXISTS idx_runtime_config_overlay_mutation
    ON runtime_config_overlay(mutation_id, updated_at);
