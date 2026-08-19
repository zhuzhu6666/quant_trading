-- 0023: rebuild idx_proposal_registry_source_ref_updated_v2 to match its migrated
-- contract exactly. pact (migration 0010): ON proposal_registry(source_ref_id, updated_at DESC).
-- The live PG index has the non-DESC variant (historical leftover), which fails the
-- fail-closed runtime schema contract comparison (index definition mismatch). This is
-- a pure index metadata repair — table data untouched.
DROP INDEX IF EXISTS idx_proposal_registry_source_ref_updated_v2;
CREATE INDEX IF NOT EXISTS idx_proposal_registry_source_ref_updated_v2
    ON proposal_registry(source_ref_id, updated_at DESC);
