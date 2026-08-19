-- Canonical v2 foundation.
-- Schema-only: no legacy reads, historical data processing, or physical rewrite.
-- Runtime writers remain on state_v1 until the canonical_v2 cutover gate passes.

CREATE SCHEMA IF NOT EXISTS canonical_v2;

CREATE TABLE IF NOT EXISTS canonical_v2.payload_blob (
    payload_hash TEXT PRIMARY KEY CHECK (payload_hash <> ''),
    payload_kind TEXT NOT NULL CHECK (payload_kind <> ''),
    schema_version TEXT NOT NULL CHECK (schema_version <> ''),
    canonical_bytes BYTEA NOT NULL,
    codec TEXT NOT NULL DEFAULT 'gzip' CHECK (codec IN ('identity', 'gzip')),
    raw_sha256 TEXT NOT NULL CHECK (raw_sha256 <> ''),
    raw_bytes BIGINT NOT NULL CHECK (raw_bytes >= 0),
    compressed_bytes BIGINT NOT NULL CHECK (compressed_bytes >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_canonical_v2_payload_kind_created
    ON canonical_v2.payload_blob(payload_kind, created_at);

CREATE TABLE IF NOT EXISTS canonical_v2.event (
    event_id TEXT PRIMARY KEY CHECK (event_id <> ''),
    event_type TEXT NOT NULL CHECK (
        event_type IN (
            'market_observation',
            'broker_execution',
            'position_transition',
            'risk_decision',
            'factor_observation',
            'governance_proposal',
            'governance_command',
            'governance_effect',
            'trade_review',
            'label_observation',
            'training_run'
        )
    ),
    entity_type TEXT NOT NULL CHECK (entity_type <> ''),
    entity_id TEXT NOT NULL CHECK (entity_id <> ''),
    observed_at TIMESTAMPTZ NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    producer TEXT NOT NULL CHECK (producer <> ''),
    producer_version TEXT NOT NULL DEFAULT '',
    schema_version TEXT NOT NULL CHECK (schema_version <> ''),
    correlation_id TEXT NOT NULL DEFAULT '',
    causation_id TEXT NOT NULL DEFAULT '',
    parent_event_id TEXT,
    idempotency_key TEXT NOT NULL DEFAULT '',
    payload_hash TEXT NOT NULL REFERENCES canonical_v2.payload_blob(payload_hash),
    status TEXT NOT NULL DEFAULT 'recorded' CHECK (status <> ''),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT canonical_v2_event_parent_fk
        FOREIGN KEY (parent_event_id)
        REFERENCES canonical_v2.event(event_id)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE INDEX IF NOT EXISTS idx_canonical_v2_event_entity_time
    ON canonical_v2.event(entity_type, entity_id, observed_at, event_id);

CREATE INDEX IF NOT EXISTS idx_canonical_v2_event_payload
    ON canonical_v2.event(payload_hash, recorded_at);

CREATE INDEX IF NOT EXISTS idx_canonical_v2_event_causation
    ON canonical_v2.event(causation_id, recorded_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_canonical_v2_event_idempotency
    ON canonical_v2.event(producer, idempotency_key)
    WHERE idempotency_key <> '';

CREATE TABLE IF NOT EXISTS canonical_v2.event_relation (
    from_event_id TEXT NOT NULL REFERENCES canonical_v2.event(event_id),
    to_event_id TEXT NOT NULL REFERENCES canonical_v2.event(event_id),
    relation_type TEXT NOT NULL CHECK (
        relation_type IN (
            'caused_by',
            'derived_from',
            'reviews',
            'labels',
            'uses_config',
            'uses_factor_state',
            'produced_sample',
            'included_in_dataset',
            'produced_artifact',
            'governed_by'
        )
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (from_event_id, to_event_id, relation_type)
);

CREATE INDEX IF NOT EXISTS idx_canonical_v2_relation_to
    ON canonical_v2.event_relation(to_event_id, relation_type);

CREATE TABLE IF NOT EXISTS canonical_v2.state_version (
    state_version_id TEXT PRIMARY KEY CHECK (state_version_id <> ''),
    entity_type TEXT NOT NULL CHECK (entity_type <> ''),
    entity_id TEXT NOT NULL CHECK (entity_id <> ''),
    version BIGINT NOT NULL CHECK (version > 0),
    valid_from TIMESTAMPTZ NOT NULL,
    valid_to TIMESTAMPTZ,
    source_event_id TEXT NOT NULL REFERENCES canonical_v2.event(event_id),
    payload_hash TEXT NOT NULL REFERENCES canonical_v2.payload_blob(payload_hash),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (entity_type, entity_id, version),
    CHECK (valid_to IS NULL OR valid_to > valid_from)
);

CREATE INDEX IF NOT EXISTS idx_canonical_v2_state_version_entity
    ON canonical_v2.state_version(entity_type, entity_id, version DESC);

CREATE TABLE IF NOT EXISTS canonical_v2.training_sample (
    sample_id TEXT PRIMARY KEY CHECK (sample_id <> ''),
    sample_type TEXT NOT NULL CHECK (sample_type <> ''),
    source_event_ids TEXT[] NOT NULL CHECK (cardinality(source_event_ids) > 0),
    feature_hash TEXT NOT NULL CHECK (feature_hash <> ''),
    feature_schema_hash TEXT NOT NULL CHECK (feature_schema_hash <> ''),
    label_hash TEXT NOT NULL CHECK (label_hash <> ''),
    trace_hash TEXT NOT NULL CHECK (trace_hash <> ''),
    evidence_contract JSONB NOT NULL DEFAULT '{}'::jsonb,
    config_version BIGINT NOT NULL DEFAULT 0 CHECK (config_version >= 0),
    config_hash TEXT NOT NULL DEFAULT '',
    horizon_minutes INTEGER NOT NULL CHECK (horizon_minutes > 0),
    target_source TEXT NOT NULL CHECK (target_source <> ''),
    sample_status TEXT NOT NULL DEFAULT 'candidate' CHECK (
        sample_status IN ('candidate', 'ready', 'quarantined', 'invalid')
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_canonical_v2_training_sample_type_status
    ON canonical_v2.training_sample(sample_type, sample_status, updated_at);

CREATE INDEX IF NOT EXISTS idx_canonical_v2_training_sample_source
    ON canonical_v2.training_sample USING GIN(source_event_ids);

CREATE TABLE IF NOT EXISTS canonical_v2.dataset_manifest (
    dataset_id TEXT PRIMARY KEY CHECK (dataset_id <> ''),
    purpose TEXT NOT NULL CHECK (purpose <> ''),
    training_window TEXT NOT NULL CHECK (training_window <> ''),
    horizon_minutes INTEGER NOT NULL CHECK (horizon_minutes > 0),
    query_contract_hash TEXT NOT NULL CHECK (query_contract_hash <> ''),
    sample_digest TEXT NOT NULL CHECK (sample_digest <> ''),
    feature_schema_hash TEXT NOT NULL CHECK (feature_schema_hash <> ''),
    label_contract_hash TEXT NOT NULL CHECK (label_contract_hash <> ''),
    target_source TEXT NOT NULL CHECK (target_source <> ''),
    config_hash TEXT NOT NULL DEFAULT '',
    source_watermark TEXT NOT NULL CHECK (source_watermark <> ''),
    code_commit TEXT NOT NULL CHECK (code_commit <> ''),
    artifact_hash TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'created' CHECK (status <> ''),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS canonical_v2.dataset_manifest_member (
    dataset_id TEXT NOT NULL REFERENCES canonical_v2.dataset_manifest(dataset_id),
    sample_id TEXT NOT NULL REFERENCES canonical_v2.training_sample(sample_id),
    sample_order BIGINT NOT NULL CHECK (sample_order >= 0),
    sample_digest TEXT NOT NULL CHECK (sample_digest <> ''),
    PRIMARY KEY (dataset_id, sample_id),
    UNIQUE (dataset_id, sample_order)
);

CREATE TABLE IF NOT EXISTS canonical_v2.projection_run (
    projection_run_id TEXT PRIMARY KEY CHECK (projection_run_id <> ''),
    run_kind TEXT NOT NULL DEFAULT 'projection' CHECK (run_kind IN ('projection', 'backfill')),
    projection_name TEXT NOT NULL CHECK (projection_name <> ''),
    source_watermark TEXT NOT NULL CHECK (source_watermark <> ''),
    code_version TEXT NOT NULL CHECK (code_version <> ''),
    input_digest TEXT NOT NULL CHECK (input_digest <> ''),
    output_digest TEXT NOT NULL DEFAULT '',
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'completed', 'failed', 'aborted')),
    error_code TEXT NOT NULL DEFAULT '',
    UNIQUE (run_kind, projection_name, source_watermark, code_version, input_digest)
);

CREATE INDEX IF NOT EXISTS idx_canonical_v2_projection_run_status
    ON canonical_v2.projection_run(projection_name, status, started_at DESC);

CREATE TABLE IF NOT EXISTS canonical_v2.legacy_mapping (
    legacy_table TEXT NOT NULL CHECK (legacy_table <> ''),
    legacy_primary_key TEXT NOT NULL CHECK (legacy_primary_key <> ''),
    canonical_event_id TEXT REFERENCES canonical_v2.event(event_id),
    canonical_payload_hash TEXT REFERENCES canonical_v2.payload_blob(payload_hash),
    classification TEXT NOT NULL CHECK (classification <> ''),
    mapping_confidence TEXT NOT NULL CHECK (
        mapping_confidence IN ('exact', 'strong', 'weak', 'unresolved')
    ),
    unresolved_reason TEXT NOT NULL DEFAULT '',
    migration_run_id TEXT NOT NULL REFERENCES canonical_v2.projection_run(projection_run_id),
    PRIMARY KEY (legacy_table, legacy_primary_key, migration_run_id)
);

CREATE INDEX IF NOT EXISTS idx_canonical_v2_legacy_mapping_event
    ON canonical_v2.legacy_mapping(canonical_event_id, mapping_confidence);
