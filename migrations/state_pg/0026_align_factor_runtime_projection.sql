-- 0026: align factor_runtime_projection with its authoritative migration-0001 shape.
-- 0001 CREATEs an 18-column table with indexes on (process_role, status, heartbeat_at) etc,
-- but the S7.3 rebuild used _PG_BUSINESS_TABLES_DDL's 4-column minimal variant, so the
-- runtime table lacks the columns the 0001 indexes reference — hence a non-fatal startup
-- warning "column process_role does not exist" and contract-mismatch debt.
-- This ADDs the missing 15 columns (existing factor_id PK and projection_json kept, the
-- primary-key-ish projection_id is added as a plain column to avoid a DROP/recreate) and
-- builds the three 0001-declared indexes now that their columns exist.
-- Data untouched.
ALTER TABLE factor_runtime_projection
    ADD COLUMN IF NOT EXISTS "projection_id" text DEFAULT '',
    ADD COLUMN IF NOT EXISTS "factor_name" text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS "process_role" text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS "process_id" text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS "boot_id" text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS "generation" integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS "artifact_hash" text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS "mutation_id" text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS "config_version" bigint NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS "config_hash" text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS "loaded" integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS "status" text NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS "error_message" text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS "heartbeat_at" double precision NOT NULL DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS "created_at" double precision NOT NULL DEFAULT 0.0;

CREATE UNIQUE INDEX IF NOT EXISTS idx_factor_runtime_projection_identity
    ON factor_runtime_projection(factor_id, process_role, process_id, boot_id);
CREATE INDEX IF NOT EXISTS idx_factor_runtime_projection_health
    ON factor_runtime_projection(process_role, status, heartbeat_at);
CREATE INDEX IF NOT EXISTS idx_factor_runtime_projection_factor
    ON factor_runtime_projection(factor_id, generation, heartbeat_at);
