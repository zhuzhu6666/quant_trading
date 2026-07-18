-- Phase 5: durable PostgreSQL worker queue and retirement of application-time
-- state schema compatibility DDL.  Every statement is additive and safe to
-- keep when application code is rolled back.

ALTER TABLE jobs
    ADD COLUMN IF NOT EXISTS priority INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS max_attempts INTEGER NOT NULL DEFAULT 3,
    ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS available_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS claimed_by TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS claim_token TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS claimed_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS heartbeat_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS lease_expires_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS cancel_requested INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS idempotency_key TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS current_step TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS log_tail_json TEXT NOT NULL DEFAULT '[]',
    ADD COLUMN IF NOT EXISTS finished_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    -- Existing pre-v2 rows must never be replayed automatically when the
    -- persistent worker flag is enabled.  New queue writes set v1 explicitly.
    ADD COLUMN IF NOT EXISTS handler_version TEXT NOT NULL DEFAULT 'legacy';

CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_kind_idempotency
    ON jobs(kind, idempotency_key)
    WHERE idempotency_key <> '';

CREATE INDEX IF NOT EXISTS idx_jobs_claim_ready
    ON jobs(status, available_at, priority DESC, created_at)
    WHERE status IN ('pending', 'queued', 'retry_wait');

CREATE INDEX IF NOT EXISTS idx_jobs_running_lease
    ON jobs(status, lease_expires_at, kind)
    WHERE status = 'running';

CREATE INDEX IF NOT EXISTS idx_jobs_kind_status_created
    ON jobs(kind, status, created_at DESC);

ALTER TABLE brain_governance_candidate_review
    ADD COLUMN IF NOT EXISTS evidence_fingerprint TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_brain_candidate_review_fingerprint
    ON brain_governance_candidate_review(candidate_id, evidence_fingerprint, created_at);

CREATE TABLE IF NOT EXISTS learning_experiment_reservation (
    reservation_id TEXT PRIMARY KEY,
    scope_type TEXT NOT NULL DEFAULT '',
    scope_key TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'reserved',
    application_id TEXT NOT NULL DEFAULT '',
    expires_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    updated_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
);

CREATE INDEX IF NOT EXISTS idx_learning_experiment_reservation_status
    ON learning_experiment_reservation(status, expires_at);

CREATE INDEX IF NOT EXISTS idx_learning_experiment_reservation_scope
    ON learning_experiment_reservation(scope_type, scope_key, status);

CREATE TABLE IF NOT EXISTS nursery_exploration_reservation (
    reservation_id TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    reason TEXT NOT NULL,
    setup_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'reserved',
    expires_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    updated_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    PRIMARY KEY (reservation_id, reason)
);

CREATE INDEX IF NOT EXISTS idx_nursery_exploration_budget
    ON nursery_exploration_reservation(trade_date, status, reason, setup_fingerprint);

CREATE TABLE IF NOT EXISTS supervisor_counterfactual_history (
    history_id TEXT PRIMARY KEY,
    counterfactual_id TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    archived_reason TEXT NOT NULL DEFAULT '',
    created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
);

CREATE INDEX IF NOT EXISTS idx_supervisor_counterfactual_history_source
    ON supervisor_counterfactual_history(counterfactual_id, created_at);

ALTER TABLE proposal_registry
    ADD COLUMN IF NOT EXISTS proposal_action TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_proposal_registry_projection_key
    ON proposal_registry(source_agent, proposal_type, control_surface, target_scope, proposal_action);

ALTER TABLE v16_brain_command
    ADD COLUMN IF NOT EXISTS claim_status TEXT NOT NULL DEFAULT 'available',
    ADD COLUMN IF NOT EXISTS claim_token TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS claimed_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS claim_expires_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS apply_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS max_apply_count INTEGER NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS consumed_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS consumed_mutation_id TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS posterior_fingerprint TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS evidence_fingerprint TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS last_release_reason TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_v16_brain_command_claim
    ON v16_brain_command(target_agent, scope_type, claim_status, claim_expires_at);
