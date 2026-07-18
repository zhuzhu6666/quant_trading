-- Phase 3: separate V16 delegation authority time from mutable claim state.
--
-- claim/release/expiry/finalize continue updating updated_at for operational
-- observability, but can no longer extend the delegation authorization TTL.

ALTER TABLE v16_brain_command
    ADD COLUMN IF NOT EXISTS authority_issued_at DOUBLE PRECISION NOT NULL DEFAULT 0.0;

UPDATE v16_brain_command
SET authority_issued_at = CASE
    WHEN created_at > 0.0 THEN created_at
    ELSE updated_at
END
WHERE authority_issued_at <= 0.0;

CREATE INDEX IF NOT EXISTS idx_v16_brain_command_authority
    ON v16_brain_command(target_agent, decision, authority_issued_at DESC);
