-- 0022: backfill the remaining single-column gaps between SQLite full standard
-- (STATE_DB_DDL) and PG runtime (survivors of the 0020 review — these 3 tables each
-- have exactly one column missing, confirmed referenced by active production code):
--   proposal_registry.proposal_action        → root cause of /api/ops/autonomy/proposals 500
--   brain_governance_candidate_review.evidence_fingerprint
--   learning_experiment_reservation.mutation_id
-- (0020 corrected 6 minimal tables. this catches the 3 single-column stragglers found
--  by a full SQLite-vs-PG column delta rescan on 2026-08-19.)
ALTER TABLE proposal_registry
    ADD COLUMN IF NOT EXISTS "proposal_action" text DEFAULT '';
ALTER TABLE brain_governance_candidate_review
    ADD COLUMN IF NOT EXISTS "evidence_fingerprint" text NOT NULL DEFAULT '';
ALTER TABLE learning_experiment_reservation
    ADD COLUMN IF NOT EXISTS "mutation_id" text NOT NULL DEFAULT '';
