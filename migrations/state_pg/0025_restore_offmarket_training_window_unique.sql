-- 0025: restore idx_offmarket_training_window_unique, suspended during 0020 review because its
-- 24 empty-training_window_key rows were mistaken for duplicates. The index is a PARTIAL unique
-- index (WHERE training_window_key <> '') — empty-key 'skipped' audit rows are NOT in its scope,
-- and non-empty keys have 0 duplicate groups (verified). No data change.
CREATE UNIQUE INDEX IF NOT EXISTS idx_offmarket_training_window_unique
    ON offmarket_high_load_job_audit(job_name, training_window_key)
    WHERE training_window_key <> '';
