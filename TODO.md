# TODO - Current Status Board

> Status: active
> Last verified: 2026-07-07
> Scope: current work queue, blockers, and near-term execution only.

本文只保留当前状态和下一步。历史阶段流水、长测试日志、旧审批路线和已完成实现细节不再放在这里，避免后续开发被旧上下文带偏。

长期事实源见：

- [docs/system-source-of-truth.md](docs/system-source-of-truth.md)
- [docs/architecture.md](docs/architecture.md)
- [docs/autonomous-governance-architecture.md](docs/autonomous-governance-architecture.md)
- [docs/change-impact-checklist.md](docs/change-impact-checklist.md)
- [docs/legacy-debt-register.md](docs/legacy-debt-register.md)

## 1. Current System State

当前系统定位：

- XAUUSD+ demo trading system.
- Backend: FastAPI + cTrader demo + PostgreSQL `state_v1`.
- Frontend: `web_frontend` is the full operator console; `miniprogram_v2` is a lightweight mobile status surface.
- Runtime state and learning audit state use PostgreSQL `state_v1`; `data/state.db` is not a live state source.
- cTrader is the only execution and live broker state source.
- Factor data enters through `FactorFrameBuilder` with point-in-time external/event data.
- Factor system uses V2 roles: `alpha`, `context`, `gate`, `sizing`.
- Factor governance uses V3 autonomous governance: Catalog, Orchestrator, DB overlay, snapshots, post-action rollback, redundancy detection, and context policy.
- V15 Phase 0 is complete: backend readiness exposes V15 runtime/overlay/catalog/worker/replay/autonomy health/incident-control/release/phase0 contract; replay harness v1 writes `replay_report` plus artifact JSON; autonomy health v1 is read-only; release checklist, incident controls v1, release run ledger v1, and Phase 0 completion gate are implemented.
- V15 Phase 1 is complete: replay harness now has bar + factor-frame replay evidence v1, offline `ExecutionGate` / `RiskPolicyService` recompute metrics v1, order/position/supervisor lifecycle coverage plus order causality / broker slippage / supervisor counterfactual / risk subaction replay v1 through `/api/ops/replay/bar-run`, Web quick replay via `/api/ops/replay/bar-decisions` + `/api/ops/replay/bar-preview` with historical selection, K线、实际盈亏、平仓归因和学习样本状态, autonomy health persistence/trend + scope approval trail v1, tightening-only scope enforcement binding v1, release approval trail v1, incident playbook plan/event binding v1, and Web `/v15` cockpit without bypassing live control boundaries.
- V16 Phase 1 is complete: read-only `BrainStateService` writes `brain_state_snapshot`, read-only `BrainMemoryService` writes `brain_memory`, exposes `/api/ops/brain/state` and `/api/ops/brain/memory`, adds readiness `brain_state` / `v16.brain_state`, and Web `/v16` shows world model, memory retrieval, observe-only hypotheses, counter-evidence, Critic scope limit, evidence refs, and explicit `affects_trading=false` boundary.
- V16 Phase 2 shadow brain is complete at minimum loop: `BrainActionPlannerService` writes `brain_action_plan`, `BrainActionPlanEvaluatorService` writes `brain_action_plan_eval`, exposes `/api/ops/brain/action-plans` and `/api/ops/brain/action-plan-evals`, adds readiness `v16.action_plans` / `v16.action_plan_evals`, and Web `/v16` displays factor-weight, parameter-template, context-policy, supervisor-template shadow plans plus posterior comparison coverage. These plans/evals are record-only and do not execute, mutate runtime overlay/snapshot, change weights/templates, submit orders, or write learning samples.
- V16 Phase 3 low-impact minimum loop is complete: `BrainLowImpactExecutorService` writes `brain_low_impact_execution`, exposes `/api/ops/brain/low-impact-executions` and `/api/ops/brain/low-impact-executions/run`, adds readiness `v16.low_impact_executions`, and Web `/v16` can trigger/display whitelisted low-impact replay jobs. Execution requires `RiskPolicyService.evaluate("run_replay_job")`, records evidence score, Critic verdict, RiskPolicy verdict, rollback/downgrade plan, replay result, and posterior monitor; optional bad-posterior tighten goes through existing incident-control/RiskPolicy/overlay path only when explicitly allowed.
- V16 Phase 4 medium-impact governance minimum loop is complete: `BrainMediumImpactGovernanceService` writes `brain_medium_impact_governance`, exposes `/api/ops/brain/medium-impact-governance` and `/api/ops/brain/medium-impact-governance/materialize`, adds readiness `v16.medium_impact_governance`, and Web `/v16` can materialize/display medium-impact `policy_suggestion` candidates. P4 records P2/P3 evidence, RiskPolicy verdict, DecisionPolicy preview for weight actions, rollback/release requirements, and never directly applies factor weights, switches templates, submits orders, or writes learning samples.
- V16 Phase 5 live-ready guardrails minimum loop is complete: `BrainLiveReadyGuardrailService` writes `brain_live_ready_guardrail`, exposes `/api/ops/brain/live-ready-guardrails`, `/api/ops/brain/live-ready-guardrails/evaluate`, and `/api/ops/brain/live-ready-guardrails/tighten`, adds readiness `v16.live_ready_guardrails`, and Web `/v16` can evaluate/display live capability lock, broker/local divergence, incident memory, release rollback, P3/P4 evidence, and tightening-only incident-control actions. P5 never submits orders, applies suggestions, writes learning samples, or relaxes incident mode.
- Unified Proposal Registry first implementation is complete: `ProposalRegistryService` writes `proposal_registry`, normalizes `policy_suggestion`、`brain_governance_candidate`、`brain_action_plan`、`learning_application_log`、`evolution_decision`、shadow/advisory audit and LLM advisory audit, exposes `/api/ops/autonomy/proposals*`, detects same control-surface conflicts, and review is audit-only. Web `/v16` shows the proposal bus without recomputing risk or authorizing actions.
- Governed live-autonomy unlock first implementation is complete: `LiveAutonomyService` writes `live_autonomy_unlock_event`, exposes `/api/ops/autonomy/live-status` and `/api/ops/autonomy/live-unlock*`, evaluates readiness/cTrader/live loop/incident/release rollback/replay/broker alignment/proposal conflicts/RiskPolicy budget, and persists `live_autonomous` / revoke back to `live_candidate` only through `RuntimeConfigMutationService` and runtime overlay/snapshot.
- Live-autonomy budget hardening is complete at the code path level: when a live open is blocked by `RiskPolicyService` with `live_autonomy_budget_breach`, `live_service` automatically requests `runtime_incident_mode=no_new_risk` through `RuntimeIncidentControlService`, preserving stricter existing modes and overlay/snapshot audit.
- Models remain shadow/advisory unless a future explicit governance stage changes that boundary.
- Risk control convergence is complete: `RiskLimitSnapshot` / `RuntimeHealthSnapshot` provide the unified risk/runtime input vocabulary, live event filters feed `RiskPolicyService` instead of independently blocking, live daily drawdown quick-stop reads the same risk limit snapshot, model stage gates reuse model permissions, and legacy `PreTradeChecker` / `CircuitBreaker` / `ExecutionRouter` are documented as paper/backtest compatibility.
- Live open-trade execution is structured as a readable pipeline in `backend/services/live_service.py`: prepare candidate sizing, request `RiskPolicyService.evaluate("open_trade")`, apply market-session/order block, submit broker order, then write post-fill protection/recovery/ledger evidence.
- Live signal decision is structured as `backend/services/live_decision_pipeline.py`: factor refresh/append, normalization, composition, context policy, and ExecutionGate now produce a `LiveDecisionFrame` before risk/execution; it does not read account state, call RiskPolicy, or touch broker execution.
- Demo learning sampling uses `RuntimeConfig.demo_learning_max_daily_trades` through `RiskLimitSnapshot.source=runtime_config:demo_learning` to raise the effective daily trade cap only in `autonomy_mode=demo_autonomous`; all opens still pass `RiskPolicyService` and broker execution semantics.

Latest verified baseline:

- Full test suite previously passed: `1211 passed`.
- Backend and learning worker were verified active after runtime overlay cleanup.
- Demo live loop initialized, warmed bars, cTrader ready, and paused open-market work while market was closed.
- `/api/health` returned healthy during the latest production check.
- 2026-07-06 close-learning incident fixed: `close_source` is preserved for close ledger/review, backend restarted, 8 failed close facts were backfilled into `decision_ledger.close`, `position_lifecycle_event.closed`, `trade_outcome_review`, controlled `experience_memory`, and `autonomous_learning_sample` with missing-attribution samples kept out of strong training.

## 2. Current Main Line

Current main line:

```text
Proposal Registry + governed live-autonomy unlock first loop complete; continue evidence freshness/budget/readiness hardening before increasing live autonomy permissions
```

Reason:

- Documentation governance is clean enough to start V15 implementation.
- V15 Phase 0 now has complete readiness/replay/health/incident-control/release-ledger/phase0-gate code facts and release checklist v1 documentation.
- V15 Phase 1 closed with bar + factor-frame replay evidence v1, offline `ExecutionGate` / `RiskPolicyService` recompute metrics v1, order/position/supervisor lifecycle coverage plus causality/slippage/counterfactual/subaction replay metrics v1, autonomy health persistence/trend + scope approval trail v1, tightening-only scope enforcement binding v1, release approval trail v1, incident playbook plan/event binding v1, and Web `/v15` cockpit without bypassing `RiskPolicyService`, `DecisionPolicy`, or runtime overlay/snapshot.
- V16 Phase 1 loop is read-only: `BrainStateService`、`BrainMemoryService` and Web `/v16` consume V15 facts and emit/display world model / memory retrieval / observe-only hypotheses / counter-evidence / Critic boundaries without changing live, shadow, learning labels, risk, weights, or runtime overlay state.
- V16 Phase 2 loop is shadow-only: `BrainActionPlannerService` turns brain hypotheses into record-only action plans for factor weights, parameter templates, context policy, and supervisor templates; `BrainActionPlanEvaluatorService` compares those plans with replay reports, trade outcome reviews, learning application effects, and supervisor traces. Future execution still requires `RiskPolicyService`, `DecisionPolicy` where applicable, runtime overlay/snapshot, replay/release evidence, and rollback JSON.
- V16 Phase 3 first execution loop is low-impact only: `BrainLowImpactExecutorService` can run whitelisted read-only replay jobs from P2 evals after `RiskPolicyService` verdict, and can optionally tighten to `shadow_only` through incident-control if a bad posterior is observed and the caller explicitly allows tightening. It still cannot change factor weights/templates, submit orders, or write learning samples.
- V16 Phase 4 first governance loop materializes medium-impact `policy_suggestion` candidates only: `BrainMediumImpactGovernanceService` uses P2 evals, `RiskPolicyService`, and `DecisionPolicy` preview to create proposed governance candidates without applying runtime mutations. Future apply still requires the existing governed write/release paths.
- V16 Phase 5 first live-ready guardrail loop evaluates capability lock, broker/local divergence evidence, incident memory, release rollback evidence, and P3/P4 evidence; explicit tightening can only move incident mode stricter through `RuntimeIncidentControlService` and `RiskPolicyService`.
- Proposal Registry now gives the brain a unified meta-governance bus without becoming a new executor: it can refresh, show conflicts, recommend routes, and record review, but cannot approve/apply or mutate source rows.
- `live_autonomous` is now a gated runtime mode, not a direct trading bypass: one-time unlock requires backend evidence and manual confirmation, while `RiskPolicyService` blocks new-risk actions when unlock evidence or account budget fails and keeps risk-reducing actions available.

## 3. Immediate Tasks

### D1: Clean Old Root Documents

Status: `done`

Goal:

- Remove obsolete AI/context documents.
- Keep root `TODO.md` short and current.
- Remove references to deleted documents.

Done:

- Deleted obsolete root AI context document.
- Replaced old 2800-line `TODO.md` with this compact current board.

### D2: Update Architecture To Current V3

Status: `done`

Goal:

- Bring `docs/architecture.md` up to the current system state.
- Add factor roles, Factor Catalog, FactorGovernanceOrchestrator, runtime overlay, context policy, redundancy governance, and rollback facts.
- Remove statements that imply factor governance is still missing.

Validation:

- Architecture should agree with `docs/system-source-of-truth.md`.

### D3: Update Contracts For Autonomous Main Path

Status: `done`

Goal:

- Update parameter template, parameter tuning, and position supervisor contracts.
- Distinguish autonomous main path from manual override/audit entry.
- Keep `RiskPolicyService` as mandatory gate for high-impact actions.

Target docs:

- `docs/parameter-template-contract.md`
- `docs/parameter-tuning-boundary.md`
- `docs/position-supervisor-contract.md`

### D4: Fix Frontend Documentation Split

Status: `done`

Goal:

- Make Web frontend docs describe current console state, not only a future plan.
- Make startup and root README agree that Web is the full console and mini-program is lightweight status.

Target docs:

- `README.md`
- `docs/startup.md`
- `docs/web-frontend-upgrade-plan.md`
- `docs/development-workflow.md`

### D5: Normalize Metadata

Status: `done`

Goal:

- Add `Status`, `Last verified`, and `Scope` headers to maintained docs.
- Mark planning-only docs as draft or historical.

## 4. Known Active Gaps

| ID | Gap | Status | Source |
|---|---|---|---|
| G1 | Some maintained docs still need normalized `Status / Last verified / Scope` headers | active | docs metadata |
| G2 | Multi-symbol remains future work | future | `docs/planning/multi-symbol-pipeline.md` |
| G3 | V15 Phase 0 and Phase 1 are complete, including Web `/v15` cockpit | done | `docs/planning/v15-autonomous-runtime-platform.md` |
| G4 | Digital twin / replay layer has bar-window, factor-frame, ExecutionGate, RiskPolicy, order/position/supervisor lifecycle, order causality, broker slippage, supervisor counterfactual, and risk subaction evidence v1 | done | V15 |
| G5 | Long-term autonomy health has persistence/trend v1, scope approval trail v1, and tightening-only enforcement binding v1; real alert/approval automation quality remains to observe | observe | V15 |
| G6 | V15 release controls have release approval trail v1 and incident playbook plan/event binding v1; deeper real alert/freeze-thaw binding is future hardening | future-hardening | `docs/v15-release-checklist.md` |
| G7 | V16 Phase 1 read-only brain backend + Web `/v16` are complete | done | `docs/planning/v16-autonomous-intelligence-brain.md` |
| G8 | V16 Phase 2 shadow action plan ledger and posterior eval cover factor weights, parameter templates, context policy, and supervisor templates without execution authority | done | `docs/planning/v16-autonomous-intelligence-brain.md` |
| G9 | V16 Phase 3 low-impact replay execution ledger runs through RiskPolicy and records rollback/downgrade monitor without trading authority | done | `docs/planning/v16-autonomous-intelligence-brain.md` |
| G10 | V16 Phase 4 medium-impact governance materializes policy suggestions with RiskPolicy/DecisionPolicy evidence but no runtime mutation | done | `docs/planning/v16-autonomous-intelligence-brain.md` |
| G11 | V16 Phase 5 live-ready guardrails evaluate capability lock/divergence/release/incident evidence and allow tightening-only incident-control actions | done | `docs/planning/v16-autonomous-intelligence-brain.md` |
| G12 | Unified Proposal Registry normalizes proposal sources and exposes review-only Web/API surface | done | `docs/autonomous-governance-architecture.md` |
| G13 | `live_autonomous` unlock/revoke is available behind readiness, release, replay, broker alignment, proposal conflict and RiskPolicy budget gates | first-loop-done | `docs/autonomous-governance-architecture.md` |
| G14 | Post-unlock evidence freshness degradation and budget-breach automatic `no_new_risk` tightening should be observed in live runtime before increasing permissions | observe | live autonomy hardening |

## 5. Technical Debt Registry

Technical debt should live in [docs/legacy-debt-register.md](docs/legacy-debt-register.md), not in this file.

Current high-signal debt categories:

- Factor/autonomy legacy semantics.
- Runtime state and overlay recovery edge cases.
- Multi-symbol and symbol-specific price/volume assumptions.
- Old frontend route assumptions.
- Remaining large live-service gravity pockets.

## 6. Before Any Code Change

Use this order:

1. Read [docs/system-source-of-truth.md](docs/system-source-of-truth.md).
2. Read [docs/legacy-debt-register.md](docs/legacy-debt-register.md).
3. Use [docs/change-impact-checklist.md](docs/change-impact-checklist.md).
4. Update this file only if the current main line or immediate tasks change.
