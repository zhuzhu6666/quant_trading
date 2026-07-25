ALTER TABLE ctrader_deals
    ADD COLUMN IF NOT EXISTS raw_execution_price DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS price_contract TEXT NOT NULL DEFAULT 'legacy_unknown',
    ADD COLUMN IF NOT EXISTS price_quality TEXT NOT NULL DEFAULT 'unknown',
    ADD COLUMN IF NOT EXISTS repair_run_id TEXT NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS data_repair_run (
    repair_run_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    repair_type TEXT NOT NULL,
    source_artifact TEXT NOT NULL,
    source_payload_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    summary_json TEXT NOT NULL DEFAULT '{}',
    created_at DOUBLE PRECISION NOT NULL,
    completed_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS data_repair_item (
    repair_run_id TEXT NOT NULL,
    table_name TEXT NOT NULL,
    primary_key TEXT NOT NULL,
    field_path TEXT NOT NULL,
    before_value TEXT NOT NULL,
    after_value TEXT NOT NULL,
    source TEXT NOT NULL,
    source_payload_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    correction_version TEXT NOT NULL,
    corrected_at DOUBLE PRECISION NOT NULL,
    rolled_back_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    PRIMARY KEY (repair_run_id, table_name, primary_key, field_path)
);

CREATE INDEX IF NOT EXISTS idx_data_repair_item_primary_key
    ON data_repair_item(table_name, primary_key);
