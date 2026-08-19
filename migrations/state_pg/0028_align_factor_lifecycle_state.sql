-- 0028: align factor_lifecycle_state with its authoritative 17-column shape.
--
-- Root cause: S7.3 rebuild created this table from the slim 7-column DDL
-- (backend/core/db.py _PG_BUSINESS_TABLES_DDL: factor_id/stage/origin/...),
-- but the authoritative definition is 0001 + every consumer
-- (factor_lifecycle_service._write_lifecycle_state, factor_catalog.factor_state_rows,
--  ledger service) which all use the 17-column full shape
-- (factor_name/definition_fingerprint/lifecycle_stage/generation/runtime_admission/
--  mutation_id/config_version/config_hash/metadata_json/activated_at/retired_at).
-- The startup warning "column s.mutation_id does not exist" is this same debt.
--
-- We ADD the 11 missing columns and the unique factor_name index (0006 contract).
-- The stale "stage" column is left untouched (harmless, consumers read lifecycle_stage).
-- The table is currently empty, so the backfill is safe.

-- 1) factor_name
ALTER TABLE factor_lifecycle_state ADD COLUMN IF NOT EXISTS factor_name TEXT NOT NULL DEFAULT '';
-- 2) definition_fingerprint
ALTER TABLE factor_lifecycle_state ADD COLUMN IF NOT EXISTS definition_fingerprint TEXT NOT NULL DEFAULT '';
-- 3) lifecycle_stage
ALTER TABLE factor_lifecycle_state ADD COLUMN IF NOT EXISTS lifecycle_stage TEXT NOT NULL DEFAULT 'SHADOW';
-- 4) generation
ALTER TABLE factor_lifecycle_state ADD COLUMN IF NOT EXISTS generation INTEGER NOT NULL DEFAULT 0;
-- 5) runtime_admission
ALTER TABLE factor_lifecycle_state ADD COLUMN IF NOT EXISTS runtime_admission TEXT NOT NULL DEFAULT 'blocked';
-- 6) mutation_id
ALTER TABLE factor_lifecycle_state ADD COLUMN IF NOT EXISTS mutation_id TEXT NOT NULL DEFAULT '';
-- 7) config_version
ALTER TABLE factor_lifecycle_state ADD COLUMN IF NOT EXISTS config_version BIGINT NOT NULL DEFAULT 0;
-- 8) config_hash
ALTER TABLE factor_lifecycle_state ADD COLUMN IF NOT EXISTS config_hash TEXT NOT NULL DEFAULT '';
-- 9) metadata_json
ALTER TABLE factor_lifecycle_state ADD COLUMN IF NOT EXISTS metadata_json TEXT NOT NULL DEFAULT '{}';
-- 10) activated_at
ALTER TABLE factor_lifecycle_state ADD COLUMN IF NOT EXISTS activated_at DOUBLE PRECISION NOT NULL DEFAULT 0.0;
-- 11) retired_at
ALTER TABLE factor_lifecycle_state ADD COLUMN IF NOT EXISTS retired_at DOUBLE PRECISION NOT NULL DEFAULT 0.0;

-- unique factor_name index (0006 contract, was previously impossible without the column)
CREATE UNIQUE INDEX IF NOT EXISTS idx_factor_lifecycle_unique_name
    ON factor_lifecycle_state(factor_name);
