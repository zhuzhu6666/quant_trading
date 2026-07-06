# TODO - Current Status Board

> Status: active
> Last verified: 2026-07-06
> Scope: current work queue, blockers, and near-term execution only.

本文只保留当前状态和下一步。历史阶段流水、长测试日志、旧审批路线和已完成实现细节不再放在这里，避免后续开发被旧上下文带偏。

长期事实源见：

- [docs/system-source-of-truth.md](docs/system-source-of-truth.md)
- [docs/architecture.md](docs/architecture.md)
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
- Models remain shadow/advisory unless a future explicit governance stage changes that boundary.

Latest verified baseline:

- Full test suite previously passed: `1211 passed`.
- Backend and learning worker were verified active after runtime overlay cleanup.
- Demo live loop initialized, warmed bars, cTrader ready, and paused open-market work while market was closed.
- `/api/health` returned healthy during the latest production check.
- 2026-07-06 close-learning incident fixed: `close_source` is preserved for close ledger/review, backend restarted, 8 failed close facts were backfilled into `decision_ledger.close`, `position_lifecycle_event.closed`, `trade_outcome_review`, controlled `experience_memory`, and `autonomous_learning_sample` with missing-attribution samples kept out of strong training.

## 2. Current Main Line

Current main line:

```text
V15 autonomous runtime platform closed; observe replay, release, and autonomy health evidence before V16
```

Reason:

- Documentation governance is clean enough to start V15 implementation.
- V15 Phase 0 now has complete readiness/replay/health/incident-control/release-ledger/phase0-gate code facts and release checklist v1 documentation.
- V15 Phase 1 closed with bar + factor-frame replay evidence v1, offline `ExecutionGate` / `RiskPolicyService` recompute metrics v1, order/position/supervisor lifecycle coverage plus causality/slippage/counterfactual/subaction replay metrics v1, autonomy health persistence/trend + scope approval trail v1, tightening-only scope enforcement binding v1, release approval trail v1, incident playbook plan/event binding v1, and Web `/v15` cockpit without bypassing `RiskPolicyService`, `DecisionPolicy`, or runtime overlay/snapshot.

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
