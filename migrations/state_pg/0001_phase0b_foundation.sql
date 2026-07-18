-- Phase 0B: additive schema foundations for execution idempotency,
-- governance mutation coordination, factor lifecycle projection, and auth
-- session revocation.  This migration intentionally changes no live writer.

CREATE TABLE broker_execution_intent (
    intent_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    decision_id TEXT NOT NULL DEFAULT '',
    trade_id TEXT NOT NULL DEFAULT '',
    position_id TEXT NOT NULL DEFAULT '',
    broker TEXT NOT NULL DEFAULT 'ctrader',
    account_id TEXT NOT NULL DEFAULT '',
    symbol TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',
    side TEXT NOT NULL DEFAULT '',
    requested_volume DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    requested_price DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    target_stop_loss DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    target_take_profit DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    status TEXT NOT NULL DEFAULT 'prepared',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    broker_order_id TEXT NOT NULL DEFAULT '',
    request_json TEXT NOT NULL DEFAULT '{}',
    risk_verdict_json TEXT NOT NULL DEFAULT '{}',
    broker_response_json TEXT NOT NULL DEFAULT '{}',
    error_json TEXT NOT NULL DEFAULT '{}',
    config_version BIGINT NOT NULL DEFAULT 0,
    config_hash TEXT NOT NULL DEFAULT '',
    prepared_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    submitted_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    completed_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    updated_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
);

CREATE INDEX idx_broker_execution_intent_status
    ON broker_execution_intent(status, updated_at);
CREATE INDEX idx_broker_execution_intent_decision
    ON broker_execution_intent(decision_id, created_at);
CREATE INDEX idx_broker_execution_intent_position
    ON broker_execution_intent(position_id, created_at);

CREATE TABLE governance_mutation_intent (
    mutation_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    control_surface TEXT NOT NULL DEFAULT '',
    scope_type TEXT NOT NULL DEFAULT '',
    scope_key TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    producer TEXT NOT NULL DEFAULT '',
    run_id TEXT NOT NULL DEFAULT '',
    risk_class TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'reserved',
    projection_status TEXT NOT NULL DEFAULT 'pending',
    before_json TEXT NOT NULL DEFAULT '{}',
    target_json TEXT NOT NULL DEFAULT '{}',
    patch_json TEXT NOT NULL DEFAULT '{}',
    rollback_json TEXT NOT NULL DEFAULT '{}',
    evidence_refs_json TEXT NOT NULL DEFAULT '{}',
    evidence_fingerprint TEXT NOT NULL DEFAULT '',
    v16_command_id TEXT NOT NULL DEFAULT '',
    target_config_version BIGINT NOT NULL DEFAULT 0,
    target_config_hash TEXT NOT NULL DEFAULT '',
    committed_config_version BIGINT NOT NULL DEFAULT 0,
    committed_config_hash TEXT NOT NULL DEFAULT '',
    domain_hash TEXT NOT NULL DEFAULT '',
    error_stage TEXT NOT NULL DEFAULT '',
    error_type TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    reserved_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    prepared_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    committed_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    aborted_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    updated_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
);

CREATE INDEX idx_governance_mutation_surface_status
    ON governance_mutation_intent(control_surface, status, updated_at);
CREATE INDEX idx_governance_mutation_scope_status
    ON governance_mutation_intent(scope_type, scope_key, status, updated_at);
CREATE INDEX idx_governance_mutation_projection
    ON governance_mutation_intent(projection_status, committed_at);
CREATE INDEX idx_governance_mutation_v16
    ON governance_mutation_intent(v16_command_id, created_at);

CREATE TABLE factor_lifecycle_state (
    factor_id TEXT PRIMARY KEY,
    factor_name TEXT NOT NULL DEFAULT '',
    definition_fingerprint TEXT NOT NULL DEFAULT '',
    artifact_hash TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL DEFAULT '',
    lifecycle_stage TEXT NOT NULL DEFAULT 'SHADOW',
    generation INTEGER NOT NULL DEFAULT 0,
    runtime_admission TEXT NOT NULL DEFAULT 'blocked',
    mutation_id TEXT NOT NULL DEFAULT '',
    config_version BIGINT NOT NULL DEFAULT 0,
    config_hash TEXT NOT NULL DEFAULT '',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    activated_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    retired_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    updated_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
);

CREATE INDEX idx_factor_lifecycle_name_stage
    ON factor_lifecycle_state(factor_name, lifecycle_stage, updated_at);
CREATE INDEX idx_factor_lifecycle_admission
    ON factor_lifecycle_state(runtime_admission, lifecycle_stage, updated_at);
CREATE INDEX idx_factor_lifecycle_mutation
    ON factor_lifecycle_state(mutation_id, updated_at);

CREATE TABLE factor_runtime_projection (
    projection_id TEXT PRIMARY KEY,
    factor_id TEXT NOT NULL,
    factor_name TEXT NOT NULL DEFAULT '',
    process_role TEXT NOT NULL DEFAULT '',
    process_id TEXT NOT NULL DEFAULT '',
    boot_id TEXT NOT NULL DEFAULT '',
    generation INTEGER NOT NULL DEFAULT 0,
    artifact_hash TEXT NOT NULL DEFAULT '',
    mutation_id TEXT NOT NULL DEFAULT '',
    config_version BIGINT NOT NULL DEFAULT 0,
    config_hash TEXT NOT NULL DEFAULT '',
    loaded INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    error_message TEXT NOT NULL DEFAULT '',
    heartbeat_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    loaded_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    updated_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
);

CREATE UNIQUE INDEX idx_factor_runtime_projection_identity
    ON factor_runtime_projection(factor_id, process_role, process_id, boot_id);
CREATE INDEX idx_factor_runtime_projection_health
    ON factor_runtime_projection(process_role, status, heartbeat_at);
CREATE INDEX idx_factor_runtime_projection_factor
    ON factor_runtime_projection(factor_id, generation, heartbeat_at);

CREATE TABLE auth_session (
    session_id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    token_jti TEXT NOT NULL DEFAULT '',
    token_hash TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    client_fingerprint TEXT NOT NULL DEFAULT '',
    ip_hash TEXT NOT NULL DEFAULT '',
    user_agent TEXT NOT NULL DEFAULT '',
    issued_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    expires_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    last_seen_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    revoked_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    revoked_by TEXT NOT NULL DEFAULT '',
    revoke_reason TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    updated_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
);

CREATE UNIQUE INDEX idx_auth_session_token_jti
    ON auth_session(token_jti) WHERE token_jti <> '';
CREATE INDEX idx_auth_session_subject_status
    ON auth_session(subject, status, expires_at);
CREATE INDEX idx_auth_session_expiry
    ON auth_session(status, expires_at);

ALTER TABLE order_lifecycle_event
    ADD COLUMN execution_intent_id TEXT NOT NULL DEFAULT '';
CREATE INDEX idx_order_lifecycle_execution_intent
    ON order_lifecycle_event(execution_intent_id, event_ts);

ALTER TABLE runtime_config_snapshot
    ADD COLUMN mutation_id TEXT NOT NULL DEFAULT '';
ALTER TABLE runtime_config_overlay
    ADD COLUMN mutation_id TEXT NOT NULL DEFAULT '';

ALTER TABLE learning_application_log
    ADD COLUMN mutation_id TEXT NOT NULL DEFAULT '',
    ADD COLUMN governance_eligibility_version TEXT NOT NULL DEFAULT '';
ALTER TABLE learning_application_effect
    ADD COLUMN mutation_id TEXT NOT NULL DEFAULT '',
    ADD COLUMN governance_eligibility_version TEXT NOT NULL DEFAULT '';
ALTER TABLE learning_experiment_reservation
    ADD COLUMN mutation_id TEXT NOT NULL DEFAULT '';

ALTER TABLE v16_brain_command
    ADD COLUMN finalized_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    ADD COLUMN finalized_mutation_id TEXT NOT NULL DEFAULT '',
    ADD COLUMN finalized_config_hash TEXT NOT NULL DEFAULT '',
    ADD COLUMN finalized_domain_hash TEXT NOT NULL DEFAULT '',
    ADD COLUMN claim_attempts INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN failure_reason TEXT NOT NULL DEFAULT '';

ALTER TABLE autonomous_learning_sample
    ADD COLUMN system_contaminated INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN governance_eligible INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN governance_effective_weight DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    ADD COLUMN governance_eligibility_version TEXT NOT NULL DEFAULT '',
    ADD COLUMN governance_ineligible_reason TEXT NOT NULL DEFAULT '';

ALTER TABLE policy_suggestion
    ADD COLUMN applied_mutation_id TEXT NOT NULL DEFAULT '',
    ADD COLUMN governance_eligible INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN governance_eligibility_version TEXT NOT NULL DEFAULT '',
    ADD COLUMN governance_ineligible_reason TEXT NOT NULL DEFAULT '';
