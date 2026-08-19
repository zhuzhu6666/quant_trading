-- 0024: rebuild idx_jobs_claim_ready to match its original migrated contract exactly.
-- Contract (0003): ON jobs(status, available_at, priority DESC, created_at).
-- 0019 backfill created it WITHOUT the DESC on priority (a non-semantic sort-order gap),
-- which trips the fail-closed index-contract comparison when jobs DDL is exercised.
-- Pure index metadata repair — table data untouched.
DROP INDEX IF EXISTS idx_jobs_claim_ready;
CREATE INDEX IF NOT EXISTS idx_jobs_claim_ready
    ON jobs(status, available_at, priority DESC, created_at);
