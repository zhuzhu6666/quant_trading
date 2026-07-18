-- Phase 3: durable coordinator recovery/projection metadata.  The primary
-- governance intent and V16 finalize fields were introduced additively in v1.

ALTER TABLE governance_mutation_intent
    ADD COLUMN IF NOT EXISTS projection_attempts INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_projection_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS projection_error_json TEXT NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS rolled_back_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS rollback_mutation_id TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS superseded_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS superseded_by_mutation_id TEXT NOT NULL DEFAULT '';

CREATE UNIQUE INDEX IF NOT EXISTS idx_governance_mutation_active_scope
    ON governance_mutation_intent(control_surface, scope_type, scope_key)
    WHERE status IN ('reserved', 'prepared');

CREATE INDEX IF NOT EXISTS idx_governance_mutation_recovery
    ON governance_mutation_intent(status, projection_status, updated_at);
