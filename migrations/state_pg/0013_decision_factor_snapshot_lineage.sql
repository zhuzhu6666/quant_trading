ALTER TABLE decision_factor_snapshot ADD COLUMN generation BIGINT NOT NULL DEFAULT 0;
ALTER TABLE decision_factor_snapshot ADD COLUMN artifact_hash TEXT NOT NULL DEFAULT '';
ALTER TABLE decision_factor_snapshot ADD COLUMN definition_fingerprint TEXT NOT NULL DEFAULT '';
ALTER TABLE decision_factor_snapshot ADD COLUMN runtime_selection_fingerprint TEXT NOT NULL DEFAULT '';
ALTER TABLE decision_factor_snapshot ADD COLUMN config_hash TEXT NOT NULL DEFAULT '';
ALTER TABLE decision_factor_snapshot ADD COLUMN lineage_status TEXT NOT NULL DEFAULT 'lineage_missing';
CREATE INDEX idx_decision_factor_snapshot_lineage_status ON decision_factor_snapshot(lineage_status, id DESC);
