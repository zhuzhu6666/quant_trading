-- Training-window crash guard and semantic-lossless supervisor/review archives.
-- Schema only: no historical backfill, row removal, or table rewrite.

ALTER TABLE offmarket_high_load_job_audit
    ADD COLUMN IF NOT EXISTS training_window_key TEXT NOT NULL DEFAULT '';

ALTER TABLE offmarket_high_load_job_audit
    ADD COLUMN IF NOT EXISTS phase TEXT NOT NULL DEFAULT '';

ALTER TABLE offmarket_high_load_job_audit
    ADD COLUMN IF NOT EXISTS worker_instance_id TEXT NOT NULL DEFAULT '';

ALTER TABLE offmarket_high_load_job_audit
    ADD COLUMN IF NOT EXISTS heartbeat_at DOUBLE PRECISION NOT NULL DEFAULT 0.0;

ALTER TABLE offmarket_high_load_job_audit
    ADD COLUMN IF NOT EXISTS input_bytes_estimate BIGINT NOT NULL DEFAULT 0;

CREATE UNIQUE INDEX IF NOT EXISTS idx_offmarket_training_window_unique
    ON offmarket_high_load_job_audit(job_name, training_window_key)
    WHERE training_window_key <> '';

CREATE TABLE IF NOT EXISTS state_payload_archive (
    archive_hash TEXT PRIMARY KEY,
    source_table TEXT NOT NULL,
    source_id TEXT NOT NULL,
    payload_kind TEXT NOT NULL,
    codec TEXT NOT NULL DEFAULT 'gzip',
    raw_sha256 TEXT NOT NULL,
    raw_bytes BIGINT NOT NULL DEFAULT 0,
    compressed_bytes BIGINT NOT NULL DEFAULT 0,
    payload_bytes BYTEA NOT NULL,
    created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_state_payload_archive_source
    ON state_payload_archive(source_table, source_id, payload_kind, raw_sha256);

CREATE INDEX IF NOT EXISTS idx_state_payload_archive_created
    ON state_payload_archive(created_at);

ALTER TABLE position_supervisor_trace
    ADD COLUMN IF NOT EXISTS verdict_archive_hash TEXT NOT NULL DEFAULT '';

ALTER TABLE position_supervisor_trace
    ADD COLUMN IF NOT EXISTS verdict_raw_sha256 TEXT NOT NULL DEFAULT '';

ALTER TABLE position_supervisor_trace
    ADD COLUMN IF NOT EXISTS verdict_raw_bytes BIGINT NOT NULL DEFAULT 0;

ALTER TABLE trade_outcome_review
    ADD COLUMN IF NOT EXISTS review_archive_hash TEXT NOT NULL DEFAULT '';

ALTER TABLE trade_outcome_review
    ADD COLUMN IF NOT EXISTS review_raw_sha256 TEXT NOT NULL DEFAULT '';

ALTER TABLE trade_outcome_review
    ADD COLUMN IF NOT EXISTS review_raw_bytes BIGINT NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_position_supervisor_trace_verdict_archive
    ON position_supervisor_trace(verdict_archive_hash);

CREATE INDEX IF NOT EXISTS idx_trade_outcome_review_archive
    ON trade_outcome_review(review_archive_hash);
