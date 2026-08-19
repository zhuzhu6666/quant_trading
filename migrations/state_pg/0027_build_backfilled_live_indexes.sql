-- 0027: build B-category live-table indexes whose columns now exist (0020-0026 backfilled
-- the columns they reference, which had kept them out of 0019).
-- Only indexes whose columns verified present in the runtime schema are added.
-- NOT included: idx_factor_lifecycle_name_stage/admission/mutation/unique_name reference
-- legacy column names (factor_name/lifecycle_stage/runtime_admission) that exist in no
-- standard definition — they are stale contracts, not buildable gaps.
CREATE INDEX IF NOT EXISTS idx_brain_candidate_review_fingerprint
    ON brain_governance_candidate_review(candidate_id, evidence_fingerprint, created_at);
CREATE INDEX IF NOT EXISTS idx_proposal_registry_projection_key
    ON proposal_registry(source_agent, proposal_type, control_surface, target_scope, proposal_action);
CREATE INDEX IF NOT EXISTS idx_decision_factor_snapshot_lineage_status
    ON decision_factor_snapshot(lineage_status, id DESC);
