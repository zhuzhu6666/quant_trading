-- 0035: retire the V16 cognition ledgers whose producers were stopped on 2026-09-11.
--
-- Retired relations (row counts frozen at retirement, verified zero growth after
-- the producer stop and the 2026-09-11 code deletion):
--   brain_action_plan_eval_payload      1,583 rows / 182 MB   (interned plan-eval payloads)
--   brain_medium_impact_governance     36,592 rows / 114 MB   (Phase-4 candidate materialisation)
--   brain_action_plan_eval             69,852 rows /  65 MB   (Phase-2 plan evaluations)
--   brain_action_plan                   6,890 rows /  39 MB   (Phase-2 shadow plans)
--   brain_state_snapshot                   20 rows / 1.1 MB   (orchestrator snapshot)
--   brain_low_impact_execution              0 rows /  40 kB   (Phase-3 ledger, never used)
--
-- Evidence was exported before this migration with
--   pg_dump -Fc -t runtime.<table> ... -> run_artifacts/v16_cognition_retirement_20260911.dump
-- (74.9 MB) and the discard was explicitly authorised by the operator on
-- 2026-09-11.  The application no longer declares or reads these relations:
-- the factor-expansion authority carrier is now the canonical-evidence
-- posterior fingerprint (backend.services.v16_posterior_arbitration), and the
-- remaining live V16 surfaces are v16_brain_command, brain_governance_candidate
-- (+_review), brain_memory and brain_live_ready_guardrail.
--
-- Unlike 0030 this migration does not assert emptiness: the rows are the
-- retired audit surface itself, and the operator decision is recorded above.

DROP TABLE IF EXISTS runtime.brain_action_plan_eval_payload;
DROP TABLE IF EXISTS runtime.brain_medium_impact_governance;
DROP TABLE IF EXISTS runtime.brain_action_plan_eval;
DROP TABLE IF EXISTS runtime.brain_action_plan;
DROP TABLE IF EXISTS runtime.brain_state_snapshot;
DROP TABLE IF EXISTS runtime.brain_low_impact_execution;
