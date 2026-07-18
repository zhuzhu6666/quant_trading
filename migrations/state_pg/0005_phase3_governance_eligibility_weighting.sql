-- Phase 3: make executable-governance sample weighting durable and auditable.
-- Historical rows remain fail-closed until an explicit eligibility repair run
-- records the current version and deterministic fingerprint.

ALTER TABLE autonomous_learning_sample
    ADD COLUMN governance_eligibility_fingerprint TEXT NOT NULL DEFAULT '';

ALTER TABLE policy_suggestion
    ADD COLUMN governance_eligibility_fingerprint TEXT NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS experience_pattern_stats (
    scope_type TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    sample_count INTEGER DEFAULT 0,
    win_count INTEGER DEFAULT 0,
    bad_loss_count INTEGER DEFAULT 0,
    avg_reward DOUBLE PRECISION DEFAULT 0.0,
    last_outcome_label TEXT DEFAULT '',
    recommended_action TEXT DEFAULT '',
    updated_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    PRIMARY KEY (scope_type, scope_key)
);

ALTER TABLE experience_pattern_stats
    ADD COLUMN effective_sample_count DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    ADD COLUMN weighted_win_count DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    ADD COLUMN weighted_bad_loss_count DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    ADD COLUMN weighted_avg_reward DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    ADD COLUMN governance_eligibility_version TEXT NOT NULL DEFAULT '',
    ADD COLUMN governance_eligibility_fingerprint TEXT NOT NULL DEFAULT '';

CREATE INDEX idx_autonomous_learning_governance_eligible
    ON autonomous_learning_sample(
        sample_type,
        governance_eligibility_version,
        governance_eligible,
        governance_effective_weight,
        event_ts
    );

CREATE INDEX idx_policy_suggestion_governance_eligible
    ON policy_suggestion(
        status,
        governance_eligibility_version,
        governance_eligible,
        created_at
    );
