-- 0037: recovery_position_state attribution integrity.
--
-- L0-0R requires every recovery/restart row to carry an explicit
-- attribution_integrity, otherwise the single learning_eligible predicate has
-- no source on this table at all (P0-2): restart_affected and chain_broken
-- could be decided but never recorded.
--
-- Two different questions, previously collapsed into one:
--   context_integrity     -> is the *entry* context complete?  (entry_decision_id)
--   attribution_integrity -> is the *exit* attribution trustworthy?  (evidence)
--
-- Audit 2026-09-12: 232 of 234 rows do have an entry_decision_id, so their
-- context_integrity='full' is CORRECT and must not be touched.  Only the two
-- rows without a decision chain are downgraded to 'partial'.
--
-- Values mirror backend/core/contracts.py:
--   full              -> fully observed, may enter the learning pool
--   restart_affected  -> crossed a restart (real PnL, but not learnable)
--   chain_broken      -> no closing evidence, exit reason unknown
--   unknown           -> unclassified, refused by learning_eligible (fail-closed)
--
-- NOTE: the migration runner splits on semicolons, so keep them out of comments.
ALTER TABLE runtime.recovery_position_state
    ADD COLUMN IF NOT EXISTS attribution_integrity TEXT NOT NULL DEFAULT 'unknown';

-- 1) Inferred close reasons: no evidence at all, so both the reason and the
--    integrity must say so.  This is the L0-0R-b whitelist fix (P1-1).
UPDATE runtime.recovery_position_state
    SET close_reason = 'chain_broken',
        attribution_integrity = 'chain_broken'
    WHERE close_reason = 'restart_replay';

-- 2) Position disappeared broker-side with no closing evidence.
UPDATE runtime.recovery_position_state
    SET attribution_integrity = 'chain_broken'
    WHERE close_reason = 'chain_broken' AND attribution_integrity = 'unknown';

-- 3) Broker-side close matched by reconciliation: real evidence, but the
--    position crossed a restart window.
UPDATE runtime.recovery_position_state
    SET attribution_integrity = 'restart_affected'
    WHERE close_reason = 'broker_close' AND attribution_integrity = 'unknown';

-- 4) System decision after re-adoption: the decision chain is real, the path
--    metrics have a downtime hole.
UPDATE runtime.recovery_position_state
    SET attribution_integrity = 'restart_affected'
    WHERE attribution_integrity = 'unknown';

-- 5) No entry decision chain -> the context is genuinely incomplete.  These
--    were unconditionally marked 'full' before.
UPDATE runtime.recovery_position_state
    SET context_integrity = 'partial'
    WHERE (entry_decision_id IS NULL OR entry_decision_id = '')
      AND context_integrity = 'full';
