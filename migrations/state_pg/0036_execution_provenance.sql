-- 0036: execution provenance columns and the one-shot origin backfill.
--
-- origin/provenance turn these facts into stored columns instead of inferred
-- properties.  ctrader_deals rows record whether an execution intent exists
-- for the position, broker_execution_intent rows record the autonomous
-- producer, and canonical_v2.event rows record test versus production writers
-- (L3-5).
-- Backfill window: deals older than 2026-08-21 00:00 CST (epoch 1787241600)
-- without a matching intent are pre-convergence legacy fills.
ALTER TABLE runtime.ctrader_deals
    ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE runtime.broker_execution_intent
    ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE canonical_v2.event
    ADD COLUMN IF NOT EXISTS provenance TEXT NOT NULL DEFAULT 'unknown';
UPDATE runtime.broker_execution_intent
    SET origin = 'autonomous' WHERE origin = 'unknown';
UPDATE runtime.ctrader_deals d
    SET origin = 'autonomous'
    WHERE EXISTS (
        SELECT 1 FROM runtime.broker_execution_intent i
        WHERE i.position_id = d.position_id::text
    );
UPDATE runtime.ctrader_deals d
    SET origin = 'legacy_pre_convergence'
    WHERE d.origin = 'unknown'
      AND d.exec_timestamp < 1787241600
      AND NOT EXISTS (
          SELECT 1 FROM runtime.broker_execution_intent i
          WHERE i.position_id = d.position_id::text
      );
