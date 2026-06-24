# Self-Evolving Trading System Upgrade Plan

> Version: 1.0
> Date: 2026-06-24
> Scope: Current `quant_trading` architecture upgrade plan for full decision traceability, post-trade learning, and future-ready model integration

---

## 1. Goal

This document defines the upgrade path from the current factor-driven trading system to a true self-evolving system.

The target is not merely:

`factor -> signal -> order -> pnl`

The target is:

`factor generation -> candidate evaluation -> decision formation -> order execution -> position lifecycle -> exit outcome attribution -> experience accumulation -> policy update -> safer next decision`

The system should gradually improve because it can:

1. remember what it saw,
2. remember why it acted,
3. remember what happened,
4. learn what worked and what failed,
5. update future decisions under controlled guardrails.

---

## 2. Design Principles

### 2.1 Hard requirements

1. Every open, hold, reduce, and close decision must be traceable.
2. Every closed trade must have a machine-readable outcome review.
3. Learning must be conservative, reviewable, and reversible.
4. Execution safety must remain rule-driven even after model integration.
5. Future statistical models and LLM-based components must plug in through stable interfaces rather than ad hoc code paths.

### 2.2 Non-goals for this phase

1. No direct LLM-controlled live trading.
2. No unconstrained reinforcement learning on live capital.
3. No bypass of current risk, governor, canary, or execution gates.
4. No large refactor that breaks current working live loop unless a compatibility layer is provided.

---

## 3. Target Architecture

This upgrade adds three missing layers to the current layer map:

1. `Ledger Layer`: complete decision and execution trace
2. `Reflection Layer`: post-trade attribution and review
3. `Learning Layer`: conservative experience-driven update engine

Target flow:

```text
Data Layer
  -> Alpha Layer
  -> Decision Layer
  -> Execution Layer
  -> Ledger Layer
  -> Reflection Layer
  -> Learning Layer
  -> Governance Layer
  -> back into Decision Layer / Factor lifecycle
```

### 3.1 Recommended ownership

| Layer | Responsibility | Recommended path |
|------|------|------|
| Ledger | Decision snapshots, factor evidence, order/position lifecycle | `backend/ledger/` |
| Reflection | Close-trade review, attribution, failure taxonomy | `alpha/reflection/` |
| Learning | Experience memory, policy suggestions, promotion inputs | `research/learning/` |
| Governance | Approval, canary, rollback, versioning | existing `backend/runtime/`, `deployment/`, `risk/` |

---

## 4. Core Gap Analysis Against Current System

The current project already has strong foundations:

1. factor generation and normalization,
2. weight fusion in `alpha/decision_policy.py`,
3. shadow evaluation in `alpha/shadow_trader.py`,
4. evolution orchestration in `backend/runtime/evolution_orchestrator.py`,
5. runtime kernel and scheduler integration.

However, the self-evolving loop is still incomplete in three places:

1. There is no unified decision ledger for every trade lifecycle event.
2. There is no standardized post-close outcome review artifact.
3. There is no dedicated experience memory that converts outcomes into controlled policy updates.

This plan focuses on filling those three gaps without discarding the current framework.

---

## 5. Upgrade Strategy

The upgrade should be done in four phases.

### Phase 1. Build the Decision Ledger

Goal: make every decision reconstructable.

Deliverables:

1. `decision_ledger` table
2. `decision_factor_snapshot` table
3. `order_lifecycle_event` table
4. `position_lifecycle_event` table
5. ledger write service and event schema

At the end of this phase, the system must be able to answer:

- Why was this trade opened?
- Which factors contributed?
- What weight and score did each factor have?
- Which risk gates were active?
- Why was this trade closed?

### Phase 2. Build the Reflection Layer

Goal: make every closed trade reviewable.

Deliverables:

1. `trade_outcome_review` table
2. `factor_contribution_review` table
3. rule-based review engine
4. failure taxonomy
5. outcome labels for future model training

At the end of this phase, the system must be able to answer:

- Why did this trade win or lose?
- Was the problem entry timing, hold logic, exit logic, regime mismatch, or factor noise?
- Which factor helped and which factor hurt?

### Phase 3. Build the Experience Memory

Goal: turn repeated outcomes into reusable knowledge.

Deliverables:

1. `experience_memory` table
2. `experience_pattern_stats` table
3. conservative policy suggestion engine
4. versioned learning artifacts
5. promotion and rollback hooks

At the end of this phase, the system must be able to answer:

- What mistakes repeat most often?
- Which factor combinations work in which regimes?
- Which settings should be reduced, boosted, quarantined, or monitored?

### Phase 4. Model-Ready Integration

Goal: preserve current rule-driven safety while exposing stable inputs for future models.

Deliverables:

1. feature views for supervised learning
2. model adapter interface
3. offline trainer interface
4. inference contract
5. approval gate between model suggestions and live policy changes

At the end of this phase, real data will be able to feed:

- statistical ranking models,
- meta-decision models,
- anomaly detectors,
- future LLM-assisted review components.

---

## 6. Data Model

### 6.1 `decision_ledger`

One row per system decision, including non-trade actions such as hold or reduce.

Suggested fields:

| Field | Type | Meaning |
|------|------|------|
| `decision_id` | TEXT PK | Unique decision id |
| `trade_id` | TEXT | Nullable before fill; linked after order creation |
| `position_id` | TEXT | Position lifecycle id |
| `event_type` | TEXT | `open`, `hold`, `add`, `reduce`, `close`, `skip` |
| `symbol` | TEXT | Trading symbol |
| `timeframe` | TEXT | Decision timeframe |
| `decision_ts` | REAL | Timestamp |
| `regime_id` | TEXT | Current market regime |
| `regime_confidence` | REAL | Regime confidence |
| `portfolio_state_json` | TEXT | Exposure, drawdown, risk budget snapshot |
| `risk_state_json` | TEXT | Risk checks and budget usage |
| `policy_version` | TEXT | Decision policy version |
| `factor_set_version` | TEXT | Factor registry snapshot |
| `action_score` | REAL | Final action score |
| `action_reason` | TEXT | Short reason text |
| `action_json` | TEXT | Full action payload |
| `created_at` | REAL | Insert timestamp |

### 6.2 `decision_factor_snapshot`

One row per factor participating in one decision.

Suggested fields:

| Field | Type | Meaning |
|------|------|------|
| `decision_id` | TEXT | Foreign key |
| `factor` | TEXT | Factor name |
| `source` | TEXT | `registry`, `discovered`, `shadow`, `canary`, `retained_ml` |
| `raw_value` | REAL | Raw factor output |
| `normalized_value` | REAL | Normalized score |
| `direction` | REAL | Signed influence |
| `base_weight` | REAL | Pre-policy weight |
| `policy_weight` | REAL | Post-policy weight |
| `shadow_score` | REAL | Shadow performance score |
| `health_score` | REAL | Health / quality score |
| `gated` | INTEGER | Whether blocked by a gate |
| `gated_reason` | TEXT | Gate reason |
| `contribution_score` | REAL | Final contribution used by decision |

### 6.3 `order_lifecycle_event`

Tracks order placement, modification, cancellation, fill, rejection.

Suggested fields:

`event_id`, `decision_id`, `trade_id`, `order_id`, `broker_order_id`, `event_type`, `event_ts`, `price`, `volume`, `status`, `details_json`

### 6.4 `position_lifecycle_event`

Tracks position-level evolution.

Suggested fields:

`event_id`, `position_id`, `trade_id`, `symbol`, `event_type`, `event_ts`, `net_volume`, `avg_price`, `unrealized_pnl`, `realized_pnl`, `details_json`

### 6.5 `trade_outcome_review`

One row per closed trade.

Suggested fields:

| Field | Type | Meaning |
|------|------|------|
| `review_id` | TEXT PK | Review id |
| `trade_id` | TEXT | Closed trade id |
| `position_id` | TEXT | Position id |
| `entry_decision_id` | TEXT | Open source decision |
| `exit_decision_id` | TEXT | Close source decision |
| `entry_quality` | REAL | Entry rating |
| `hold_quality` | REAL | Hold management rating |
| `exit_quality` | REAL | Exit rating |
| `regime_fit_score` | REAL | Whether trade matched environment |
| `execution_quality` | REAL | Fill/slippage quality |
| `pnl` | REAL | Realized pnl |
| `mae` | REAL | Max adverse excursion |
| `mfe` | REAL | Max favorable excursion |
| `outcome_label` | TEXT | `good_win`, `lucky_win`, `good_loss`, `bad_loss`, etc. |
| `failure_tags_json` | TEXT | Failure taxonomy |
| `summary_text` | TEXT | Machine summary |
| `review_json` | TEXT | Full structured review |
| `created_at` | REAL | Timestamp |

### 6.6 `factor_contribution_review`

Per closed trade, per factor contribution summary.

Suggested fields:

`review_id`, `trade_id`, `factor`, `entry_contribution`, `hold_contribution`, `exit_contribution`, `net_contribution`, `confidence`, `notes`

### 6.7 `experience_memory`

Stores normalized experience samples for learning.

Suggested fields:

| Field | Type | Meaning |
|------|------|------|
| `experience_id` | TEXT PK | Experience id |
| `trade_id` | TEXT | Source trade |
| `regime_id` | TEXT | Market regime bucket |
| `setup_hash` | TEXT | Canonicalized setup fingerprint |
| `decision_context_json` | TEXT | Compact context snapshot |
| `outcome_label` | TEXT | Learning label |
| `reward_score` | REAL | Conservative reward |
| `failure_tags_json` | TEXT | Error taxonomy |
| `recommended_action` | TEXT | `downweight`, `quarantine`, `boost`, `watch` |
| `evidence_strength` | REAL | Confidence / support |
| `artifact_version` | TEXT | Learning schema version |
| `created_at` | REAL | Timestamp |

---

## 7. Event Contracts

To keep future integrations fast, all learning-related components should consume events rather than directly depend on live service internals.

### 7.1 Recommended event names

1. `decision.created`
2. `decision.skipped`
3. `order.submitted`
4. `order.filled`
5. `order.rejected`
6. `position.opened`
7. `position.updated`
8. `position.closed`
9. `trade.reviewed`
10. `experience.created`
11. `policy.suggestion.created`
12. `policy.update.approved`
13. `policy.update.rejected`

### 7.2 Event envelope

```python
{
    "event_id": "...",
    "event_type": "decision.created",
    "entity_id": "...",
    "entity_type": "decision",
    "ts": 0.0,
    "schema_version": "v1",
    "source": "backend.services.live_service",
    "payload": {...}
}
```

### 7.3 Why events matter

They allow:

1. current rule-based review to subscribe now,
2. future trainers to backfill from stored history,
3. future models to plug in without rewriting live execution logic.

---

## 8. Service Interfaces to Reserve Now

These interfaces should be added even if early implementations are simple and rule-based.

### 8.1 Decision logging interface

Suggested path: `backend/ledger/decision_logger.py`

```python
class DecisionLogger:
    def log_decision(self, ctx: "DecisionContext", result: "DecisionResult") -> str:
        ...

    def log_skip(self, ctx: "DecisionContext", reason: str, details: dict | None = None) -> str:
        ...
```

### 8.2 Outcome review interface

Suggested path: `alpha/reflection/reviewer.py`

```python
class TradeReviewer:
    def review_closed_trade(self, trade_id: str) -> "TradeOutcomeReview":
        ...
```

### 8.3 Experience builder interface

Suggested path: `research/learning/experience_builder.py`

```python
class ExperienceBuilder:
    def build_from_review(self, review: "TradeOutcomeReview") -> "ExperienceSample":
        ...
```

### 8.4 Policy suggestion interface

Suggested path: `research/learning/policy_suggester.py`

```python
class PolicySuggester:
    def suggest(self, window: list["ExperienceSample"]) -> list["PolicySuggestion"]:
        ...
```

### 8.5 Model adapter interface

Suggested path: `research/model_adapter.py`

```python
class ModelAdapter(Protocol):
    name: str
    version: str

    def fit(self, dataset_ref: str, **kwargs) -> dict:
        ...

    def predict(self, features: dict) -> dict:
        ...

    def explain(self, features: dict, prediction: dict) -> dict:
        ...
```

This adapter can later support:

1. tree models,
2. logistic / linear models,
3. anomaly detectors,
4. ranking models,
5. offline LLM-based review summarizers.

### 8.6 Feature provider interface

Suggested path: `research/features/feature_provider.py`

```python
class FeatureProvider:
    def build_decision_features(self, decision_id: str) -> dict:
        ...

    def build_trade_features(self, trade_id: str) -> dict:
        ...

    def build_experience_features(self, experience_id: str) -> dict:
        ...
```

This prevents model code from reading scattered runtime modules directly.

---

## 9. Rule-Based First, Model-Ready Later

The first implementation should remain rule-driven for safety and data quality reasons.

### 9.1 Why rules first

1. Current live learning labels are still immature.
2. Decision trace and failure taxonomy must stabilize first.
3. Early real trade sample size will be small and noisy.
4. Risk, canary, and governor logic must remain deterministic.

### 9.2 What should stay rule-driven long term

These components should remain primarily rules plus statistical thresholds:

1. max loss / drawdown protection,
2. order sizing hard caps,
3. trading session restrictions,
4. kill switches,
5. deployment approval gates,
6. canary promotion and retirement guardrails.

### 9.3 What can later become model-assisted

1. factor ranking under a given regime,
2. entry quality scoring,
3. exit timing assistance,
4. error pattern detection,
5. setup similarity retrieval,
6. post-trade explanation quality,
7. policy suggestion prioritization.

---

## 10. Training Readiness Plan

Once real data is sufficient, model integration should follow readiness gates instead of calendar deadlines.

### 10.1 Minimum data readiness

Suggested minimum before supervised learning begins:

1. at least 500-1000 closed trades with valid ledger linkage,
2. stable outcome labels for at least 3-5 review categories,
3. regime labels available for most decisions,
4. factor snapshot coverage above 95%,
5. fill and execution quality fields populated,
6. no major schema churn for at least one review cycle.

### 10.2 Minimum governance readiness

1. offline train / validate / promote workflow exists,
2. model artifact registry exists,
3. rollback path exists,
4. canary for model-assisted suggestions exists,
5. model output never bypasses risk or governor.

### 10.3 First recommended model types

Order of introduction:

1. logistic regression / elastic net for outcome probability,
2. gradient boosting or xgboost for ranking setups,
3. anomaly detector for execution or regime mismatch,
4. retrieval-based case matching for similar past failures,
5. LLM-assisted review summarizer after structured labels are stable.

Reinforcement learning should remain late-stage and offline-first.

---

## 11. Integration With Existing Modules

### 11.1 Current modules to extend, not replace

| Current module | Upgrade role |
|------|------|
| `alpha/decision_policy.py` | emit structured factor decision details |
| `backend/services/live_service.py` | emit decision and order lifecycle events |
| `execution/oms.py` | emit fills and execution transitions |
| `backend/runtime/evolution_orchestrator.py` | consume reviewed outcomes and experience stats |
| `alpha/shadow_trader.py` | remain shadow evidence source, later feed ledger-compatible snapshots |
| `deployment/canary.py` | consume richer review and experience evidence |

### 11.2 Recommended insertion points

1. After final decision result is created: write `decision_ledger`
2. After order submit/fill/reject: write `order_lifecycle_event`
3. After close is confirmed: trigger `TradeReviewer`
4. After review is stored: trigger `ExperienceBuilder`
5. During scheduled evolution cycle: run `PolicySuggester`
6. Before policy apply: require governor/canary approval

---

## 12. Suggested Failure Taxonomy

Use a controlled tag vocabulary instead of free text.

Suggested tags:

1. `late_entry`
2. `early_entry`
3. `false_breakout`
4. `regime_mismatch`
5. `factor_conflict`
6. `overweight_noise_factor`
7. `underweight_key_factor`
8. `stop_too_tight`
9. `stop_too_loose`
10. `exit_too_early`
11. `exit_too_late`
12. `execution_slippage`
13. `liquidity_mismatch`
14. `risk_budget_exceeded`
15. `lucky_win`
16. `good_loss`
17. `bad_loss`
18. `unavoidable_noise`

This taxonomy should be versioned and remain backward compatible.

---

## 13. Learning Policy Guardrails

The system should not directly rewrite live policy after a single trade.

Recommended guardrails:

1. updates only happen on scheduled windows,
2. suggestions require minimum evidence count,
3. suggestions are versioned and diffable,
4. live application must pass canary or shadow validation,
5. high-impact changes require governor approval,
6. every applied update must be reversible.

Suggested learning actions:

1. `watch`
2. `downweight`
3. `boost_small`
4. `quarantine_candidate`
5. `retire_candidate`
6. `tighten_gate`
7. `loosen_gate_small`

---

## 14. Delivery Plan

### Milestone A. Ledger baseline

Build:

1. schema,
2. write service,
3. event hooks,
4. minimal query utilities.

Success criteria:

1. every trade links to entry decision,
2. every close links to exit decision,
3. factor snapshots can be replayed for any sampled trade.

### Milestone B. Reflection baseline

Build:

1. trade review service,
2. factor contribution review,
3. failure taxonomy,
4. review reports.

Success criteria:

1. every closed trade gets a review,
2. reviews are queryable by tag and regime,
3. repeated failure clusters become visible.

### Milestone C. Experience baseline

Build:

1. normalized experience samples,
2. suggestion engine,
3. scheduled learning run,
4. approval workflow.

Success criteria:

1. policy suggestions are data-backed,
2. no suggestion can bypass governance,
3. all accepted changes are versioned.

### Milestone D. Model-ready pipeline

Build:

1. feature provider,
2. offline dataset builder,
3. model adapter,
4. artifact registry integration.

Success criteria:

1. real trade history can become a supervised dataset in one step,
2. first statistical models can be trained without live service rewrites,
3. later LLM-based review can subscribe to the same structured artifacts.

---

## 15. Immediate Next Actions

Recommended implementation order:

1. create `decision_ledger` and `decision_factor_snapshot`,
2. add decision logging hooks to `decision_policy` and `live_service`,
3. add order and position lifecycle logging in execution path,
4. create `trade_outcome_review` and a rule-based `TradeReviewer`,
5. create `experience_memory` and scheduled `PolicySuggester`,
6. expose `FeatureProvider` and `ModelAdapter` before any real model integration.

This order gives the system a durable spine first, then adds reflection, then learning, then model acceleration.

---

## 16. Final Recommendation

The current framework should not be replaced. It should be extended along the following path:

1. keep the existing factor and execution pipeline,
2. add a rigorous trace ledger,
3. add structured post-trade reflection,
4. add conservative experience learning,
5. reserve stable interfaces for future models.

When real data becomes sufficient, this architecture will allow fast integration of stronger models without re-architecting the live system.

That is the correct path for a trading system that aims not just to automate decisions, but to grow from experience.

---

## 17. Current Implementation Status (2026-06-24)

This section records what has already been implemented in the current repository, what is partially complete, and what remains before live testing and later model integration.

### 17.1 Completed in current repo

#### A. Rule-driven learning loop baseline

Implemented:

1. learning-related schema extensions in `backend/core/db.py`
2. decision / position review persistence and experience memory persistence
3. policy suggestion persistence and learning application log persistence
4. rule-driven evolution integration in `backend/runtime/evolution_orchestrator.py`

Outcome:

The system can now move through:

`close -> review -> experience -> suggestion -> governance -> weight application log`

#### B. Reflection layer baseline

Implemented:

1. `alpha/reflection/reviewer.py`
2. structured outcome review generation after position close
3. review labels, summary text, MAE / MFE fields, failure tags

Outcome:

Closed positions can now produce machine-readable review artifacts that are suitable for later analytics and model training.

#### C. Experience and policy suggestion baseline

Implemented:

1. `research/learning/experience_builder.py`
2. `research/learning/policy_suggester.py`
3. `research/learning/governor.py`
4. scheduled governance-compatible suggestion application flow

Outcome:

The system can now accumulate repeated patterns and convert them into conservative rule suggestions instead of directly mutating policy from a single trade.

#### D. Learning API surface

Implemented:

1. `backend/api/learning.py`
2. summary endpoint
3. suggestions endpoint
4. review endpoint
5. governance run endpoint
6. applications endpoint
7. reviews list endpoint

Outcome:

Frontend and later external monitoring tools can inspect the self-evolving loop through stable API contracts.

#### E. New mini-program frontend (`miniprogram_v2`)

Implemented:

1. brand-new mini-program structure instead of patching the old panel
2. overview / trading / learning / factors / ops pages
3. iOS-like bright visual system
4. learning loop visualization
5. factor governance view
6. operations and scheduler view

Outcome:

The new frontend can already present the full rule-learning loop and is structurally ready for real API testing.

### 17.2 Partially completed

#### A. Full decision ledger

Status: partial

Already present:

1. close-review artifacts
2. experience memory
3. suggestion and application logs

Still missing or not fully unified:

1. one canonical `decision_ledger` row for every open / hold / reduce / close / skip decision
2. one canonical `decision_factor_snapshot` for every decision
3. complete order lifecycle event history
4. complete position lifecycle event history

Impact:

The learning loop is working at the review-and-weights layer, but the pre-trade and intra-trade reasoning chain is not yet fully replayable from one ledger spine.

#### B. Frontend verification

Status: partial

Already present:

1. new mini-program pages and services
2. improved information hierarchy and traceability-oriented layout

Still pending:

1. real-device / WeChat DevTools rendering verification
2. empty / degraded response handling walkthrough
3. live demo-account end-to-end validation

#### C. Model-ready adapter layer

Status: partial

Already present:

1. document-level interface reservations
2. structured review and experience outputs that are model-friendly

Still pending:

1. concrete `ModelAdapter` implementation
2. `FeatureProvider` implementation
3. offline dataset builder
4. artifact registry and model promotion flow

### 17.3 Not yet implemented

1. full supervised training pipeline
2. model-assisted inference in the live decision path
3. retrieval or case-based similarity layer for past mistakes
4. LLM-assisted review summarization
5. fully versioned policy artifact registry with one-click rollback dashboard

### 17.4 Recommended immediate test sequence

Before expanding the architecture again, the next step should be controlled testing on the demo account stack:

1. verify backend learning endpoints and close-review creation
2. verify governance run creates suggestions and application logs
3. verify mini-program `miniprogram_v2` renders correctly in WeChat DevTools
4. verify one full trade lifecycle from factor decision to close review
5. verify repeated bad patterns actually alter later rule bias conservatively

### 17.5 Summary

Current state:

1. the rule-driven self-evolving baseline is already landed,
2. the new frontend shell for traceability is already landed,
3. the full canonical decision ledger is not fully landed yet,
4. model integration interfaces are reserved conceptually but not fully implemented in code,
5. the system is ready for the next phase: demo-account closed-loop testing.
