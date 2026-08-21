-- 0030: remove the retired runtime fact projections after canonical_v2 cutover.
--
-- This migration is deliberately guarded.  The old projections are no longer
-- read or written by the application, but a non-empty table is an operator
-- cleanup problem, not permission to silently discard evidence.  The
-- transaction aborts until the retired rows have been explicitly cleared or
-- migrated into canonical_v2 by the operator.

SELECT 1 / CASE
    WHEN EXISTS (SELECT 1 FROM runtime.decision_ledger)
      OR EXISTS (SELECT 1 FROM runtime.decision_factor_snapshot)
      OR EXISTS (SELECT 1 FROM runtime.autonomous_learning_sample)
      OR EXISTS (SELECT 1 FROM runtime.order_lifecycle_event)
      OR EXISTS (SELECT 1 FROM runtime.position_lifecycle_event)
      OR EXISTS (SELECT 1 FROM runtime.trade_outcome_review)
      OR EXISTS (SELECT 1 FROM runtime.position_supervisor_trace)
      OR EXISTS (SELECT 1 FROM runtime.supervisor_counterfactual_review)
      OR EXISTS (SELECT 1 FROM runtime.supervisor_counterfactual_history)
      OR EXISTS (SELECT 1 FROM runtime.decision_log)
      OR EXISTS (SELECT 1 FROM runtime.lifecycle_events)
    THEN 0
    ELSE 1
END AS retired_fact_rows_must_be_empty;

DROP TABLE IF EXISTS runtime.decision_factor_snapshot;
DROP TABLE IF EXISTS runtime.decision_ledger;
DROP TABLE IF EXISTS runtime.autonomous_learning_sample;
DROP TABLE IF EXISTS runtime.order_lifecycle_event;
DROP TABLE IF EXISTS runtime.position_lifecycle_event;
DROP TABLE IF EXISTS runtime.trade_outcome_review;
DROP TABLE IF EXISTS runtime.position_supervisor_trace;
DROP TABLE IF EXISTS runtime.supervisor_counterfactual_review;
DROP TABLE IF EXISTS runtime.supervisor_counterfactual_history;
DROP TABLE IF EXISTS runtime.state_payload_archive;
DROP TABLE IF EXISTS runtime.decision_log;
DROP TABLE IF EXISTS runtime.lifecycle_events;
DROP TABLE IF EXISTS canonical_v2.legacy_mapping;
