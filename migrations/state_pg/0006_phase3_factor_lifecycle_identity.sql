-- Phase 3: one durable lifecycle identity per public factor name.
--
-- factor_id already owns the canonical DSL-AST SHA-256 primary key.  The
-- additional name constraint prevents two definitions from racing through
-- separate workers and leaving ambiguous name-based runtime projections.

CREATE UNIQUE INDEX idx_factor_lifecycle_unique_name
    ON factor_lifecycle_state(factor_name);
