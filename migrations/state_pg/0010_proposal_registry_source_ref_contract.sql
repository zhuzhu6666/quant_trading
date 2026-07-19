-- Restore the proposal-registry runtime schema contract without mutating or
-- dropping the historical idx_proposal_registry_source_ref_updated index.
--
-- Some deployed databases created that legacy name with updated_at DESC while
-- the runtime declaration expected the default ascending order. Runtime schema
-- validation intentionally rejects such same-name drift, so use a new additive
-- name with an explicit order.

ALTER TABLE proposal_registry
    ADD COLUMN IF NOT EXISTS source_ref_id TEXT DEFAULT '',
    ADD COLUMN IF NOT EXISTS updated_at DOUBLE PRECISION NOT NULL DEFAULT 0.0;

CREATE INDEX IF NOT EXISTS idx_proposal_registry_source_ref_updated_v2
    ON proposal_registry(source_ref_id, updated_at DESC);
