-- 0034: bind overlay authority to committed overlay content hash.
--
-- Background: startup authority bound the intent's target/committed hashes to
-- the hash of the FULL effective config.  Every settings.yaml/base change
-- between deploys therefore failed the binding and latched new risk, even
-- when the committed overlay itself was untouched (incidents 2026-09-02 /
-- 09-05 / 09-06 / 09-07, each needing a manual Coordinator rebind).
--
-- New contract: the coordinator stores the hash of the overlay row it
-- commits (committed_overlay_hash).  Startup verifies the live overlay row
-- content against that hash plus intent committed/current.  Base-only drift
-- no longer affects the verdict. Direct overlay writes still fail closed via
-- the row-hash mismatch.  Old intents without the column value fall back to
-- the previous full-config-hash comparison (transition only).

ALTER TABLE governance_mutation_intent
    ADD COLUMN IF NOT EXISTS committed_overlay_hash TEXT NOT NULL DEFAULT '';
