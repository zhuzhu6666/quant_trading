# 分期修复故障与验收矩阵

> Status: active acceptance index
> Snapshot: 2026-08-28 (HEAD f2eb9c9; P1 complete, Safety/governance enforce active, pg_job pending)
> Scope: reproducible acceptance evidence and unresolved live evidence

本文只记录“如何证明”和当前未满足的运行证据。架构事实见 `system-source-of-truth.md`，实施阶段见 `planning/production-autonomy-repair-optimization-plan.md`，当前状态见 `phased-repair-rollout-status.md`。已完成批次的详细流水通过 Git 历史追溯，不在本矩阵重复保存。

## 1. 通用批次验收

每批必须同时给出：

| 维度 | 证据 |
|---|---|
| Problem fact | 日志、API、PostgreSQL、`runtime_kv`、broker 或失败测试 |
| Call chain | `search_graph -> trace_path -> get_code_snippet`，不足时补动态入口扫描 |
| Canonical authority | 唯一计算者、writer 和公开 contract |
| Deletion | 被替代代码、fallback、配置、测试和文档已删除 |
| Targeted tests | 覆盖真实行为 seam，不只验证 wrapper |
| Contract | 必要的 migration、OpenAPI、frontend decoder/build |
| Runtime | 受控重启后的日志、API、PG、`runtime_kv` 和 broker 只读事实 |
| Unknown semantics | `unknown/warming_up/stale/error` 未被默认值转换 |
| Remaining compatibility | 真实调用方、退出条件和最晚删除阶段 |
| Rollback | 代码、配置、schema、数据的可执行恢复方式 |

以下任一存在，批次不能标记 `complete`：

- 同一事实仍有第二个生产计算者或 writer；
- canonical 路径已通过但旧路径没有删除；
- readiness/API/frontend 仍自行推导；
- 兼容层没有真实调用方或退出条件；
- 只有单测，没有必要的运行态验证；
- unknown/stale/warming_up 被补零或假定安全。

## 2. Safety 与执行不变量

| 场景 | 必须保持 |
|---|---|
| bars/factor/session 失败 | Safety 先执行，alpha 阻断 |
| PostgreSQL/audit 失败 | 新风险 fail-closed；close/reduce/tighten/emergency 继续 |
| account/positions reconcile 非 fresh | 不解释为空仓或零账户 |
| broker fresh snapshot 缺 active recovery row | 先做一次既有 close-deal/recovery retirement 确认；证据不完整时保留 recovery conflict latch |
| live decision bar 缺最新已闭合 bar | cTrader live trendbar feed 是 live 热路径 source；live tick 只读 bridge 内存 frame，并以 `stale_waiting_for_live_trendbar` 阻断 alpha/开仓，不执行 history RPC、不读月库；启动历史在线优先、本地兜底；`data_sync` 每 30 分钟低频维护月库副本且仍是唯一 durable writer；代码验收确认 legacy startup history pull 已删除，运行验收需确认 live trendbar 订阅/重连、无 `init:fast`/`init:deferred` 拉取及本地副本延时不影响 live loop |
| open admission reconcile 非 fresh | 在 canonical serial Safety/reconcile 之后直接阻断；不在同 tick 再发 account/positions broker RPC |
| spot stale | final open admission 拒绝 |
| order timeout/延迟/未知 protobuf | outcome unknown，禁止重发并 latch |
| amend 无 fresh projection ack | 不报告 confirmed |
| emergency pre/post reconcile 失败 | 不报告 completed |
| stop 与 open 并发 | 单 generation ownership；已准入 RPC 完成保护/恢复 |
| partial close | 仍开放 position 不计 completed trade |
| safety heartbeat stale | 持久化 no-new-risk，保护继续 |

固定 fault-matrix runner：

```bash
.venv/bin/python scripts/safety_fault_matrix.py
.venv/bin/python scripts/execution_outcome_fault_matrix.py
```

源码或绑定测试变化后旧 attestation 自动失效，必须重跑；矩阵不能替代真实 broker lifecycle。

## 3. P1 broker 成交事实

代码合同必须覆盖：

- `executionPrice`/`entryPrice` 不使用 `moneyDigits`；commission/gross/swap/balance 按各自 money contract；
- deal/entry 数量级不一致时 quarantine；
- unknown close price 不进入 attribution、review、experience、counterfactual 或 governance；
- restart replay 不猜测价格或 position identity；
- `open_intent -> broker execution intent -> order/position -> review -> learning sample` 能通过稳定 lineage 回溯。

当前未满足的运行证据（2026-08-28 只读复核已满足，P1 runtime acceptance 完成）：

- post-repair 新 broker deal 的价格/金额合同 ✅（`broker_execution 127`，`executionPrice` 保留原始价，近 3 天 84 confirmed 零 unknown）；
- restart 后 deal replay ✅（`recovery_position_state 61` 最新 `285427255` 绑定完整，`health known`）；
- 完整 `open -> protection -> close -> deal sync -> review -> sample` 生命周期 ✅（`trade_review 99 / trade_review_outcome 67 / full 46` 连续 2026-08-21→08-28，`position_transition 125`）。

P1 runtime acceptance 已完成，转入常态观察；剩余仅 supervisor 治理样本量与 `tighten/reduce` 覆盖。

## 4. P2 canonical risk

固定合同：

| 合同 | 验证 |
|---|---|
| clean review + position notional | `tests/test_live_risk_metrics_snapshot.py::test_risk_inputs_use_clean_reviews_and_position_notional` |
| stale broker facts 不续鲜 | `tests/test_live_risk_metrics_snapshot.py::test_stale_broker_facts_replace_previous_known_snapshot` |
| warm-up 不伪装零风险 | `tests/risk/test_backend_risk_metrics.py::test_var_warmup_is_not_reported_as_zero_risk` |
| closed-bar returns + final candidate | `tests/risk/test_backend_risk_metrics.py::test_forward_var_uses_closed_bar_returns_and_candidate_notional` |
| current/final signed notional | `tests/risk/test_backend_risk_metrics.py::test_forward_var_projects_current_and_final_candidate_notional` |
| unknown price 不变零敞口 | `tests/risk/test_backend_risk_metrics.py::test_snapshot_does_not_turn_unknown_position_price_into_zero_exposure` |
| Policy 不把 unknown/stale 当零 | `tests/risk/test_policy_service.py::test_open_trade_blocks_unknown_var_instead_of_treating_it_as_zero` |
| 风险 API/前端区分开仓前置拦截、政策许可与真实执行 | `tests/risk/test_risk_api_policy.py::test_recent_policy_verdicts_summarizes_decision_ledger`、`web_frontend/src/tests/architecture.test.mjs` |
| live/replay 同输入 | `tests/test_research_parity_boundaries.py::test_parity_replay_freezes_closed_bar_returns_for_candidate_var` |
| readiness 只读投影 | `tests/test_backend_readiness_contract.py::test_readiness_projects_canonical_forward_var_snapshot` |
| API 只读 canonical | `tests/test_risk_summary_inputs.py::test_risk_summary_uses_canonical_snapshot` |
| frontend 无旧字段 fallback | `web_frontend/src/tests/fact-behavior.test.mjs`、`architecture.test.mjs` |

P2 complete 不授权清锁或切换静态 flag。

## 5. P3 证据、记忆与 effect

必须证明：

- review、counterfactual、memory、sample、application、effect 的生产 writer 可追踪；
- account/position/deal/review/scope/version/source hash identity 稳定；
- current lesson 只有 `trade_lesson_memory.v1` canonical projection；
- partial/missing/contaminated evidence 治理权重为零；
- terminal、bounded、可比较 effect 才能形成 prior；同一 scope 同时最多一个 active effect；
- 新 canonical 入口同批删除平行 writer/reader，不新增 ExperienceMemoryService、第二 evidence store、pgvector 或 shadow writer。

## 6. P4 V16 与因果治理

必须证明：

- 多交易不产生 cross-trade posterior；同交易不同 causal scope 保持独立；
- readiness actionable 与 `V16CommandGate` 可 claim 结果一致；过期/claimed 队首不阻塞；claim/release/recovery 不延长授权；
- 一条命令最多一个 committed mutation；transaction failure 不增加 apply count；
- scope -> agent -> required gate 只有一个 authority；
- autonomous learning、factor governance、position supervisor governance 三条 lane 均覆盖 success、noop/reject、retry、rollback、effect；
- V16、模型和候选层不能直接写 runtime、factor weight、order 或 broker；生产 mutation 只能经过现有 Candidate Review、RiskPolicy、V16CommandGate 和 Coordinator。
- `keep/no_change` 不生成 candidate/suggestion/command；有效 bridge 必须通过 `active -> bridge_pending -> awaiting_execution -> applied/superseded/rejected`，且 bridge、command cleanup、claim、reissue 共用同一 lifecycle predicate。

因子后验必须同时区分 evidence coverage 与 causal certainty；单笔交易只能形成 lead/inconclusive，不产生 executable patch；没有足够证据的维度显式 `no_change`。

本批针对性证据：

- `tests/test_v16_read_only_brain.py::test_v16_supervisor_keep_is_observation_only`
- `tests/test_v16_read_only_brain.py::test_v16_supervisor_target_equal_to_runtime_is_observation_only`
- `tests/test_v16_brain_orchestrator.py::test_bridge_pending_candidate_keeps_command_until_governor_review`
- `tests/test_v16_brain_orchestrator.py::test_cancelled_submitted_bridge_reissues_only_pending_approved_suggestion`
- `tests/research/test_rule_evolution_governor.py::test_conflict_resolver_prefers_current_v16_lineage_over_legacy_supervisor_priority`

## 7. 发布和运行验收

阶段切换顺序固定：

```text
supervisor_enforce
  -> governance_enforce
  -> pg_job_queue_enable
  -> pg_job_queue_verify
```

每个 target 前运行：

```bash
.venv/bin/python scripts/phased_repair_release_gate.py --target <target>
```

该命令只读，不修改 flag、不重启服务、不 claim job。必须同时证明 predecessor flags、systemd services、process-loaded flags/PID/fingerprint、latch/unknown execution/reconcile、readiness、worker config/overlay hash 和当前代码绑定 fault matrix 均一致。

Safety enforce 之前必须满足二选一：连续 24 小时 broker-confirmed 空仓 shadow，或一个完整 broker position lifecycle。该条件只授权 Safety v2 发布门，不是有界 Demo `runtime_incident_mode=normal` 或普通开仓的等待锁。

## 8. 当前未完成证据（2026-08-28 14:53 复核）

2026-08-28 只读：`quant-backend 891039` / `quant-learning-worker 891040` 均 `active` `NRestarts=0`，`live_safety_plane_v2_mode=enforce` 已加载（`f2eb9c9` off 路径已删），`governance_mutation_coordinator_v2_mode=enforce` 已加载；`broker_execution 127 / position_transition 125 / trade_review 99 / full/1.0 46 (2026-08-21→28)` 全链条稳定；`risk_metrics known cvar1.55%`，持仓 `285427255` 有仓，`unknown_execution_count=0`，`Safety heartbeat 5.4s`。

已满足：
- P1 runtime acceptance ✅（价格合同 + lifecycle 闭环）
- Safety v2 enforce ✅（有仓 `governed_execute` 验证）
- governance enforce ✅（14:14 flat 已切 `enforce`，`off` 直连路径 f2eb9c9 已删）

仍未完成：
- `pg_job_queue_enable → verify`（静态 `pg_job_queue_v2_enabled=false`，`reconciled 0/missing 0` 待发布门）
- supervisor 治理闭环 `governance_eligible matured 5/10`（需 10）与 `tighten/reduce` 覆盖
- P6 Demo 自治毕业（需 100 笔/30 天/2 regimes/PF 等）

## 9. 全量测试策略

针对性测试是每批默认要求。全量测试只在 P1/P2/P3/P4 阶段收口、静态发布门、公共 authority 或大范围 dead-code 删除、影响面无法可靠隔离，或 operator 明确要求时运行。

运行全量测试时记录 commit/worktree fingerprint、命令、passed/skipped/deselected 和 PostgreSQL isolation。旧全量结果只能作为基线，不能证明后续源码。

## 10. 智能自主进化合同批次

本批新增的固定验收面：

| 合同 | 针对性证据 |
|---|---|
| Demo 最小量仍受实际止损风险预算 | `tests/test_live_risk_sizing.py`、`tests/test_risk_runtime_policy.py`、`tests/test_runtime_config.py` |
| Readiness 不调用重型构建器、投影 stale/missing fail-closed | `tests/test_backend_readiness_contract.py`、`tests/test_readiness_dimensions_v2.py`、`tests/test_v16_read_only_brain.py` |
| Web V16 无 `readiness.v16.*` 详情 fallback | `web_frontend` production build 与架构测试 |
| evolution watermark、积压背压、单 owner/cron | `tests/test_evolution_cycle_watermark_v1.py`、`tests/test_live_scheduler_jobs.py`、`tests/test_factor_autonomy_hardening.py` |
| blocked/no-change 不制造 snapshot | `tests/test_evolution_config_snapshot_idempotency.py` |
| Candidate Card 方向、lineage、成熟证据和 effect 门 | `tests/test_factor_cards_api.py`、`tests/alpha/test_factor_score_evaluator.py` |
| legacy ACTIVE 排除与同 generation 退回 | `tests/alpha/test_runtime_factor_selection.py`、`tests/test_factor_lifecycle_service.py` |
| effect 成熟前不可扩权 | `tests/test_factor_weight_change_service.py` |
| 新 decision factor lineage 绑定或显式 missing | `tests/test_decision_factor_lineage.py`；schema migration `0013` |
| execution intent 兼容标记与故障恢复 | `tests/test_ctrader_execution_outcome.py`、`tests/test_broker_execution_intent.py`、`scripts/execution_outcome_fault_matrix.py` |

代码测试通过仍不能替代以下运行验收：

- 连续 30 次完整 readiness 构建的 p95<15 秒，且无 refresh 重叠、单核长期占满或 Safety freshness 抖动；
- PostgreSQL schema 13 的受控应用与新 lineage 写入；
- backlog 达预算后真实新 GP 注册数为 0，并持续排空；
- legacy ACTIVE 在 live selection 中排除并完成 `demote_to_shadow`；
- `supervisor_enforce` 后新开仓与持仓监督 execution intent 覆盖率 100%，并完成一次 `open -> close -> learning/effect` 全链生命周期；
- 四个静态发布目标逐项通过 release gate、受控重启和观察窗口。

这些证据完成前，P6 和后续静态开关保持关闭；历史缺失 intent/lineage 保持显式缺失，不回填猜测值。
