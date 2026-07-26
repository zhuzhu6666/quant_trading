# 分期修复故障与验收矩阵

> Status: active acceptance index
> Snapshot: 2026-07-26
> Scope: reproducible acceptance evidence and unresolved live evidence

本文只记录“如何证明”。架构事实见 `system-source-of-truth.md`，实施阶段见
`planning/production-autonomy-repair-optimization-plan.md`，当前状态见
`phased-repair-rollout-status.md`。

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
| Unknown semantics | unknown/warming_up/stale/error 未被默认值转换 |
| Remaining compatibility | 真实调用方、退出条件和最晚删除阶段 |
| Rollback | 代码、配置、schema、数据的可执行恢复方式 |

以下任一存在，批次不能标记 complete：

- 同一事实仍有第二个生产计算者或 writer；
- canonical 路径已通过但旧路径没有删除；
- readiness/API/frontend 仍自行推导；
- 兼容层没有真实调用方或退出条件；
- 只有单测，没有必要的运行态验证；
- unknown/stale/warming_up 被补零或假定安全。

## 2. Safety 与执行不变量

| 场景 | 必须保持 |
|---|---|
| bars/factor/session 失败 | safety 先执行，alpha 阻断 |
| PostgreSQL/audit 失败 | 新风险 fail-closed；close/reduce/tighten/emergency 继续 |
| account/positions reconcile 非 fresh | 不解释为空仓或零账户 |
| spot stale | final open admission 拒绝 |
| order timeout/延迟/未知 protobuf | outcome unknown，禁止重发并 latch |
| amend 无 fresh projection ack | 不报告 confirmed |
| emergency pre/post reconcile 失败 | 不报告 completed |
| stop 与 open 并发 | 单 generation ownership；已准入 RPC 完成保护/恢复 |
| session cache 缺失/损坏 | 不归零、不开放新风险 |
| partial close | 仍开放 position 不计 completed trade |
| safety heartbeat stale | 持久化 no-new-risk，保护继续 |

权威固定测试由以下 runner 管理：

```bash
.venv/bin/python scripts/safety_fault_matrix.py
.venv/bin/python scripts/execution_outcome_fault_matrix.py
```

源码或绑定测试变化后旧 attestation 自动失效，必须重跑；矩阵不能替代真实 broker
lifecycle。

## 3. P1 broker 成交事实

代码合同必须覆盖：

- `executionPrice`/`entryPrice` 不使用 `moneyDigits`；
- commission/gross/swap/balance 按各自 money contract；
- buy/sell、volume、timestamp 不因修复变化；
- deal/entry 数量级不一致时 quarantine；
- unknown close price 不进入 attribution、review、experience、counterfactual 或 governance；
- restart replay 不猜测价格或 position identity。

数据合同必须证明：

- correction manifest 行数和更新行数一致；
- realized PnL、commission、swap 修复前后不变量一致；
- 污染样本治理 effective weight 为零；
- 无污染 suggestion/effect/mutation 泄漏。

仍未满足的运行证据：

- post-repair 新 broker deal；
- restart replay；
- 完整 `open -> protection -> close -> deal sync -> review -> sample`。

三项未完成前 P1 状态保持 `runtime acceptance`。

## 4. P2 canonical risk

| 合同 | 固定验证 |
|---|---|
| clean review + position notional | `tests/test_live_risk_metrics_snapshot.py::test_risk_inputs_use_clean_reviews_and_position_notional` |
| stale broker facts 不续鲜 | `tests/test_live_risk_metrics_snapshot.py::test_stale_broker_facts_replace_previous_known_snapshot` |
| warm-up 不伪装零风险 | `tests/risk/test_backend_risk_metrics.py::test_var_warmup_is_not_reported_as_zero_risk` |
| closed-bar returns + final candidate | `tests/risk/test_backend_risk_metrics.py::test_forward_var_uses_closed_bar_returns_and_candidate_notional` |
| current/final signed notional | `tests/risk/test_backend_risk_metrics.py::test_forward_var_projects_current_and_final_candidate_notional` |
| unknown price 不变零敞口 | `tests/risk/test_backend_risk_metrics.py::test_snapshot_does_not_turn_unknown_position_price_into_zero_exposure` |
| Policy 不把 unknown/stale 当零 | `tests/risk/test_policy_service.py::test_open_trade_blocks_unknown_var_instead_of_treating_it_as_zero` |
| live/replay 同输入 | `tests/test_research_parity_boundaries.py::test_parity_replay_freezes_closed_bar_returns_for_candidate_var` |
| readiness 只读投影 | `tests/test_backend_readiness_contract.py::test_readiness_projects_canonical_forward_var_snapshot` |
| API 只读 canonical | `tests/test_risk_summary_inputs.py::test_risk_summary_uses_canonical_snapshot` |
| frontend 无旧字段 fallback | `web_frontend/src/tests/fact-behavior.test.mjs`、`architecture.test.mjs` |

最后结果：

- D16/risk/policy/live/parity/readiness/replay/API：`275 passed`；
- 补充模块：`236 passed`、`163 passed`；
- Web test/typecheck/build：通过；
- schema v12、OpenAPI：通过；
- P2 complete。

P2 complete 不授权清锁或切换静态 flag。

## 5. P3 证据/记忆/effect 准入矩阵

P3 首批在写代码前必须完成：

| 检查 | 通过条件 |
|---|---|
| writer inventory | review/counterfactual/memory/sample/application/effect 全部生产 writer 可追踪 |
| identity | account/position/deal/review/scope/version/source hash 稳定 |
| duplicate authority | 每类 current projection 选择一个现有 canonical writer |
| contamination | partial/missing/contaminated 治理权重为零 |
| effect | 同 scope 最多一个 active，terminal/bounded 才能形成 prior |
| deletion | 每新增或修改一个 canonical 入口，同批删除平行 writer/reader |
| schema | 只有现有 schema 无法表达必要 lineage/revision 时才允许 additive migration |

P3 禁止以“先搭平台”为理由新增：

- `ExperienceMemoryService/Writer`；
- 新 scheduler/worker；
- 第二套 evidence store；
- pgvector；
- compatibility shadow writer。

如确实无法复用，必须先在状态文档记录不可复用的代码和运行证据。

首批证据（2026-07-26）：

- writer inventory 已覆盖 review/counterfactual/memory/sample/application/effect；
- `learning_backfill.v1` 重复 memory writer 已删除，生产代码净删除；
- 260 条重复 projection 已删除，6 条 evidence 引用已迁移，0 残留、0 悬空；
- canonical source anchor 继续使用 `trade_outcome_review.review_id`，未修改 schema。
- live rich lesson 计算已复用现有 `upsert_trade_lesson_memory()` 单 writer，旧 SQL writer
  和所有 `live_review` reader 分支已删除；
- 576 条 `live_review` projection 已合并到 canonical lesson 后删除；189 条 suggestion
  evidence 的 2,373 个旧 ID 已迁移，重启后 0 残留、0 格式异常、0 悬空；
- 第二小批针对性验证为 `64 passed`，reader 删除补充验证为 `27 passed`。
- 170 条历史兼容 projection 均有更完整 canonical lesson；11 条 suggestion evidence 的
  35 个旧 ID 已迁移，旧 projection 与两个启动/脚本 writer 已删除，0 残留、0 悬空；
- application/effect 为 3,423 个唯一 application ID、3,368 个唯一 effect、0 orphan
  effect；55 个无 effect application 均为 blocked/failed 终态；
- 当前 16 个 active application/effect 对应 16 个唯一 scope，符合单 scope 单 active；
- 第三小批 memory、application/effect 与 domain writer 针对性验证为 `80 passed`，
  P3 writer/identity 准入完成。

## 6. P4 V16 闭环矩阵（complete）

必须验证：

- 多交易 fixture 不产生 cross-trade posterior；
- 同交易不同 causal scope 独立；
- readiness actionable 与 Gate 实际可 claim 完全一致；
- expired/superseded 队首不阻塞；
- claim/release/recovery 不延长授权；
- 一条命令最多一个 committed mutation；
- transaction failure 不增加 apply count；
- scope→agent→required gates 只有一个 authority；
- 三条 lane 均覆盖 success/noop/reject/retry/rollback/effect。

不得新增第二套 command queue、actionable predicate 或 readiness verdict。

当前证据：

- 多交易且复用同一 `position_id` 的 fixture 已证明 supervisor/entry 只在同一
  `review_id`/`trade_id` lineage 内组合；同交易 entry 与 supervisor scope 仍独立保留。
- `V16CommandGate.is_actionable()` 已替代 orchestrator status 和 stepper 的平行判断；
  stale head、claimed command、authorize、claim 的结果一致。
- 运行库过期队首已终态为 `cancelled/authority_expired`，`apply_count=0`；重启后 fresh
  command 在 orchestrator、stepper 和 Gate 三处分别为 1/1/allowed。
- claim 单次绑定、release/recovery 不续期、transaction failure 不增加 apply count 的既有
  contract 与本批回归共 `36 passed`；specialist/Coordinator 回归 `57 passed`。
- planner、command 与 specialist gate 已删除各自硬编码，统一只读 Agent Authority 的
  `execution_owner` 与 `required_gate`。
- authorize/claim 删除固定 200 行截断，并用既有 authority freshness 先缩小读取范围；
  205 条其他 scope 新命令不会阻塞目标命令。低负载合并验证 `23 passed`。
- `entry_quality` 已纳入 autonomous learning 的唯一 execution owner / RiskPolicy gate，
  专用 delegation 不再硬编码 target 或 gate。

三条 lane 终态证据：

| lane | success | noop/reject | retry | rollback | effect |
|---|---|---|---|---|---|
| autonomous learning | entry-quality/parameter-template atomic commit | manual/no eligible/eligibility reject | transaction abort 后无残留，可由原 suggestion 重试 | v1 invalidation 与 parameter effect rollback | observing/terminal effect + new-evidence retry |
| factor governance | atomic weight mutation | replay/admission/V16 block | mutation/risk failure 释放 reservation | domain fault/runtime target rollback | observing/ineffective effect |
| position supervisor governance | atomic template switch | missing evidence/illegal stop/rolled-back application ignored | domain fault 后 suggestion 保持 approved、reservation released | ineffective supervisor effect rollback | observing -> ineffective terminal effect |

bounded runtime trace：

```text
v16cmd_7be9876b49138e64e726
  -> autonomous_learning
  -> psg_entry_quality_92771bd6472259f1
  -> gmut_e7cba57522aa44fd8d36d4d370cd1f08
  -> lapp_a2b661abfcc25d2ee724 / learning_application_effect
  -> gmut_deddadacb3b849d2bd5da975c53530cd (committed rollback)
```

原 command `apply_count=1`，原 mutation 明确记录 `rolled_back` 与
`rollback_mutation_id`；rollback 不复用或增加原 command apply count。P4 最终分批低优先级
验证共执行 131 个测试，全部通过。

## 7. 删除验收

删除模块、字段或兼容层时至少执行：

1. 代码图谱 inbound/outbound trace。
2. `rg` 检查 import、字符串、动态入口、CLI、systemd、cron 和文档。
3. 检查 schema/JSON 是否持久化 fully-qualified name。
4. import/startup/相关 contract smoke。
5. 删除对应配置、导出、测试和文档。
6. `git diff --check` 和生产代码净变化核对。

静态零引用不足以证明可删；完成上述扫描后也不需要额外等待固定 30 天。

## 8. 发布和运行验收

阶段切换顺序固定：

```text
safety_enforce
  -> generation_enable
  -> execution_outcome_enable
  -> governance_enforce
  -> pg_job_queue_enable
  -> pg_job_queue_verify
```

每个 target 前运行：

```bash
.venv/bin/python scripts/phased_repair_release_gate.py --target <target>
```

必须同时证明：

- predecessor flags 精确；
- required systemd services active；
- process-loaded flags、PID、start time 和 fingerprint 新鲜；
- latch/unknown execution/reconcile 状态明确；
- readiness 和 worker config/overlay hash 一致；
- required fault matrix 当前代码绑定通过；
- governance/queue 专项 preflight 通过。

该命令只读，不修改 flag、不重启服务、不 claim job。

Safety enforce 之前还必须满足二选一：

1. 连续 24 小时 broker-confirmed 空仓 shadow；或
2. 一个完整 broker position lifecycle。

该二选一只授权静态 Safety v2 从 shadow 切到 enforce，不是有界 Demo
`runtime_incident_mode=normal` 或开仓的等待锁。

## 9. 全量测试策略

针对性测试是每批默认要求。全量测试只在：

- P1/P2/P3/P4 阶段收口；
- 静态发布门；
- 公共 authority 或大范围 dead-code 删除；
- 影响面无法可靠隔离；
- operator 明确要求。

运行全量测试时记录 commit/worktree fingerprint、命令、passed/skipped/deselected 和
PostgreSQL isolation。旧全量结果只能作为基线，不能证明后续源码。
