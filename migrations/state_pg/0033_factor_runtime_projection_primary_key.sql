-- 0033: restore the authoritative projection_id primary key.
--
-- The deployed table was rebuilt with factor_id as its primary key even
-- though the writer is idempotent on the process identity tuple.  Build the
-- new unique index first so duplicate or empty projection identities fail
-- loudly during the explicit migration instead of being silently collapsed.
-- The existing identity unique index remains the per-process upsert boundary.

CREATE UNIQUE INDEX IF NOT EXISTS idx_factor_runtime_projection_projection_id
    ON factor_runtime_projection(projection_id);

ALTER TABLE factor_runtime_projection
    ALTER COLUMN projection_id SET NOT NULL;

ALTER TABLE factor_runtime_projection
    ALTER COLUMN projection_id DROP DEFAULT;

ALTER TABLE factor_runtime_projection
    DROP CONSTRAINT IF EXISTS factor_runtime_projection_pkey;

ALTER TABLE factor_runtime_projection
    ADD CONSTRAINT factor_runtime_projection_pkey
    PRIMARY KEY USING INDEX idx_factor_runtime_projection_projection_id;

CREATE UNIQUE INDEX IF NOT EXISTS idx_factor_runtime_projection_identity
    ON factor_runtime_projection(factor_id, process_role, process_id, boot_id);
