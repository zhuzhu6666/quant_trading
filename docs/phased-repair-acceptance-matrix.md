# 分期修复故障与验收矩阵

> Status: active acceptance index
> Snapshot: 2026-08-03
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
| 最小仓位不被失真 CVaR 上限永久锁死 | `tests/risk/test_policy_service.py::test_open_trade_allows_recent_min_volume_cvar_below_adjusted_limit` |
| 调整后仍保留 CVaR 硬上限 | `tests/risk/test_policy_service.py::test_open_trade_keeps_cvar_hard_limit_above_adjusted_limit` |
| 最小仓位不可交易 reduce 在 Policy 前成为去重 no-op | `tests/test_live_service_lifecycle.py::test_supervisor_minimum_position_reduce_is_deduplicated_before_policy` |
| reduce-to-close 复用 supervisor 临近止损阈值 | `tests/test_live_supervision_actions.py::test_minimum_reduce_upgrades_only_at_supervisor_near_stop_threshold` |
| 风险 API/前端区分政策许可与真实执行 | `tests/risk/test_risk_api_policy.py::test_recent_policy_verdicts_summarizes_decision_ledger`、`web_frontend/src/tests/architecture.test.mjs` |
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

## 4.1 Demo 持仓监督器自适应重构

| 合同 | 必须证明 | 当前代码/测试证据 |
|---|---|---|
| canonical market context | live/replay 复用 compositor context state 与 `resolve_market_regime()`；缺失 ATR/range/structure 保持 unknown，不补零 | `tests/test_live_position_lifecycle.py`、`tests/test_position_supervisor.py`、`tests/test_market_regime.py` |
| posture priority | 强趋势 near-TP/giveback/time-decay 不提前 close/tighten；unknown/transition 只 observe；确认退出和硬风险仍 close | `tests/test_position_supervisor.py` |
| Demo execution boundary | adaptive `recommended/requested` 保留，`effective_action=hold`、`observed/observation_only`，不调用 RiskPolicy/broker；hard protection 仍 applied | `tests/test_live_service_lifecycle.py::test_demo_adaptive_supervisor_action_is_observed_without_risk_or_broker_mutation` |
| persistent dedupe | 同一 episode/bar/fingerprint 最多一次；posture 改变、trigger 清除重入、fingerprint 改变可重新建议 | `tests/test_live_service_lifecycle.py::test_adaptive_duplicate_requires_same_bar_episode_posture_and_fingerprint`、`tests/test_live_position_lifecycle.py::test_build_supervisor_recovery_meta_resets_adaptive_episode_on_posture_change_and_clear` |
| reduce safety | 不可交易 reduce 只产生一次 no-op/hold，不静默升级 full close；最小仓位无非法 broker RPC | `tests/test_live_service_lifecycle.py::test_supervisor_minimum_position_reduce_is_deduplicated_before_policy` |
| legacy AWE boundary | Demo AWE 只 observed/superseded，不与 canonical supervisor 同时 applied；非 Demo 删除受 replay/trace/effect gate 约束 | `tests/test_live_position_protection_cycle.py` |
| governance candidate | 每个生成候选一个 control/一个 regime，evidence 具备 base template、single patch、generation context、replay/counterfactual；approved 不等于 applied | `tests/test_position_supervisor_governance.py`、`tests/risk/test_policy_service.py` |

本批只把自适应动作部署为 observation-only；不解除 freeze、不切静态开关、不清理 active effect、不回滚既有 mutation，
也不把 parity replay 当作 live authority。只有现有 V16/Admission/RiskPolicy/Coordinator 链完整提交并形成有效 authority，
才可单项申请 `governed_execute`。

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

### 2026-07-31 今日治理后验断点收口

| 检查 | 通过条件与本批证据 |
|---|---|
| evidence contract normalization | materialize 与 `repair_evidence_contracts()` 共用 canonical normalization；全量 `18521` 行末次 repair 为 `42`、重复 repair 为 `0`，JSON contract 与资格列一致；repair 不从 `sample_type` 推断 executable 权限 |
| evidence fail-closed | matured/pending、full/recovered/partial、污染/非污染和 lineage 场景保持既有 eligibility 语义；污染、缺 lineage、未验证 recovered、pending 不得进入强训练/强治理；部署后 `bad_total=0` 且无污染质量放行 |
| candidate review expression | 新生成 review 的 `bridge_reason` 与最终 `review_status` 一致；`needs_evidence` 带具体 gap，原始 preview reason 仍在 `bridge_preview`；历史 review 仅保留审计事实；review/approved 不等于 applied |
| mutation/effect expression | active `mixed/observing` effect 继续阻断新实验且不新增 application/mutation；read-only preflight 将 `v16_claim` abort 与真实 transaction/recovery failure 分开统计，Coordinator 状态机不变 |
| attribution boundary | `largest_contribution_factor` 保持 observational；责任域为 `exit/holding/data_quality/parameter` 时不生成因子惩罚写入，既有 counter-evidence 仍可作为只读刹车 |
| shadow lane | malformed DSL 在 Registry/lifecycle 前跳过并写既有审计；缺真实 shadow perf 的候选保持当前 stage；valid shadow promotion 仍可通过 |
| regression | 计划中的 8 个测试文件合计 `119 passed`，补充治理/运行回归 `54 passed`；本批未新增 service、table、migration、thread、scheduler、threshold 或 public API |

### P3 记忆完整性与灾备底座（Demo，未发布）

| 检查 | 通过条件 |
|---|---|
| 三层记忆 | `MemoryIntegrityReport` 对原始 review、`trade_lesson_memory.v1` 与 `brain_memory` 返回来源覆盖、孤儿/重复/时间错位、污染隔离和索引引用；只读且不改变 readiness/trading authority |
| API/readiness 投影 | `/api/ops/brain/memory.memory.integrity` 与 `learning_repair.memory_integrity` 使用同一报告，不新增 endpoint、业务事实表、应用 worker 或静态开关；Windows 成功拉取回执只投影到既有 `postgres_backup_health.v1`，不改变交易/治理权限 |
| Windows 主动拉取合同 | forced-command SSH 仅允许流式 `pg_dump` 与格式受限的回执；服务器不保存备份文件、不启用 WAL/S3/pgBackRest repository/timer。没有 Windows 成功回执时必须显示 `missing/unavailable`；有回执但尚无成功恢复演练必须显示 `degraded`，不得宣称已可恢复 |
| 恢复演练 | 只允许独立 DSN，`verify_state_restore.py --confirm-isolated` 必须核对 schema 和 MemoryIntegrity；任何异常非零退出，不伪造离线快照与在线源的逐行一致性，禁止自动 promote；演练结果只可显式写入脱敏 health 投影 |

本批不引入 pgvector、外部向量库、Redis/Kafka、PG Job Queue 发布、状态 schema migration 或实盘静态开关切换。

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

### 2026-08-01 V16 Supervisor 首桥接死锁修复

| 检查 | 验收证据与当前状态 |
|---|---|
| 正常后验轮换不惩罚 | `posterior_not_selected` + `superseded` 不进入失败生命周期扣分；rotation-only、普通失败、混合样本单测通过；`agent_scorecard.v1` 不暴露内部计数 |
| review/bridge 门 | 当前真实 review 为 `bridge_ready=1`；风险、replay/trace/source、冲突、负效果、合同和当前 V16 delegate 门未放宽；负效果、合同违规、缺 evidence 或缺 delegate 的既有阻断测试保留 |
| suggestion 写入 | nursery/bridge 仍是唯一调用路径，`submit_candidate_to_policy_suggestion()` 幂等；重复提交回归通过，未新增 source-agent 直接 writer |
| V16 evidence binding | `PositionSupervisorTemplatePlan` 使用已 claim command 的权威 `evidence_fingerprint`；Coordinator 仍在同一 transaction 内严格重验并 finalize。production-like 回归验证 `apply_count=1` 且 intent fingerprint 与 command 一致 |
| 真实运行 | 重启后 backend/worker active；受控 nursery runner 完成真实 bridge。旧 aborted command/mutation 保留审计，新 command `..._r1794f53ecacc` 已 `finalized/apply_count=1`，新 intent 为 `committed/projection_status=current`，suggestion/application 已 applied；后续 effect observation 与 maturity 仍未宣称完成 |
| readiness/maturity | 未修改 50 个 clean mature positions、2 个 session/regime、replay/counterfactual/learning_shadow/governance 条件；generic `ready_for_autonomous_mutation=true` 不等于 supervisor `learning_repair.ok=true`，当前 `canary.broker_mutation_allowed=false`，首次 bridge 不等于自治解锁 |
| 回归 | 指定批次 `98 passed`；V16 orchestrator、V16/read-only 与 autonomous-learning 针对性回归通过；幂等键绑定当前 V16 command 的重试路径通过 |

本批已完成首个真实 command claim、Coordinator finalize、application 写入和 effect 记录；仍为
`migrating/observing`，不得把一次 applied 解释为自治解锁，也不得删除旧 advisory writer，直到
effect observation 与既有 maturity counting 连续贯通。

### 2026-08-01 月度 K 线边界最小修复

| 检查 | 验收证据与当前状态 |
|---|---|
| 月库读取 authority | `bars_monthly_read_paths()` 按月新到旧返回路径；暖机、`DuckDBDataStore` 与 `system_health` 共用；当前月空库时回读上月闭合 bar，当前月兼容链接不变 |
| 冷启动兼容 | 月库目录无可用文件时仍回退既有 legacy 单库路径；不新增表、服务、线程、队列或 flag |
| 运行事实 | 后端日志 `warmed up: 200 bars (source=local_db)`；risk snapshot `known / 500 / var known`；健康调度 `overall=healthy`、`bar_m1/bar_m5=ok` |
| 门槛不变 | 不修改 RiskPolicy、bar freshness、readiness、50 笔 mature positions、session/regime、replay、counterfactual 或 learning_shadow 条件 |
| 回归 | 月库边界、live warmup、启动、数据同步、风险与健康相关测试共 `108 passed`；py_compile 与 `git diff --check` 通过 |

三条 lane 终态证据：

| lane | success | noop/reject | retry | rollback | effect |
|---|---|---|---|---|---|
| autonomous learning | entry-quality/parameter-template atomic commit | manual/no eligible/eligibility reject | transaction abort 后无残留，可由原 suggestion 重试 | v1 invalidation 与 parameter effect rollback | observing/terminal effect + new-evidence retry |
| factor governance | atomic weight mutation | replay/admission/V16 block | mutation/risk failure 释放 reservation | domain fault/runtime target rollback | observing/ineffective effect |
| position supervisor governance | atomic template switch | missing evidence/illegal stop/rolled-back application ignored | domain fault 后 suggestion 保持 approved、reservation released | ineffective supervisor effect rollback | observing -> ineffective terminal effect |

因子治理运行闭环补充验收：

| 合同 | 验证 |
|---|---|
| health 后同周期先 V16 再 governance | `tests/test_evolution_governance_handoff.py::test_health_commit_immediately_hands_off_to_factor_governance` |
| 无扩张动作不领取 V16、不误报 blocker | `tests/backend/runtime/test_factor_governance_orchestrator.py::test_run_cycle_does_not_claim_v16_without_expansion_work` |
| fresh builtin 扩张候选进入 preflight | `tests/backend/runtime/test_factor_governance_orchestrator.py::test_expansion_preflight_finds_fresh_builtin_activation` |
| concrete preflight 生成 evidence-bound 单次 V16 delegate | `tests/backend/runtime/test_factor_governance_orchestrator.py::test_v16_delegates_only_concrete_factor_expansion_preflight` |
| 周期 delegate 覆盖 builtin 首次 SHADOW 登记 | `tests/backend/runtime/test_factor_governance_orchestrator.py::test_factor_governance_cycle_authorizes_shadow_enrollment_step` |
| lifecycle 覆盖 Registry/RuntimeConfig 推断 | `tests/test_factor_catalog_governance.py::test_factor_catalog_prefers_canonical_lifecycle_state` |
| 审计/canary 名称不制造目录条目 | `tests/test_factor_catalog_governance.py::test_factor_catalog_includes_factor_governance_shadow_audit` |
| committed mutation 覆盖旧 suggestion 展示 | `tests/test_factor_catalog_governance.py::test_factor_catalog_prefers_committed_mutation_over_older_suggestion` |
| coordinator projection 稳定身份并清理 PID 行 | `tests/test_factor_lifecycle_service.py::test_coordinator_projection_uses_stable_identity_and_prunes_pid_rows` |

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

## 10. 学习 worker 内存收敛验收（2026-08-03）

| 合同 | 验证 |
|---|---|
| 唯一自动完整周期 | 常规 nursery 四个运行窗口的 actions 只包含 orchestration/review/bridge/recommended step，不包含 `run_autonomous_learning_cycle`；完整周期只在 watermark-gated `:12/:42 UTC` 执行 |
| 显式能力 | `full_learning_cycle=true` 行为回归通过；运维 `--run-once` 直接完整周期后的 nursery pass 不再重复运行第二个完整周期 |
| 紧凑合同 | 连续两个 event 均为 `autonomous_learning_cycle.v2/completed`，包含 17 个阶段内存观测，payload 分别为 9,440 / 9,580 bytes；周期 event 不携带 sample/counterfactual/candidate 行集合 |
| canonical evidence | 两轮 `samples.total_changed=2303/2262`，第二轮 effect reconcile 正常推进 `observed=4/inconclusive=1`；watermark fingerprint 从 `e7f616...` 推进到 `fbb1af...`，完整证据仍落现有表 |
| 峰值 RSS | 阶段观测分别最高 449,740 / 476,668 KiB；包含相邻 nursery、supervisor 和 factor governance 的 2 秒外部观测全局峰值 635,876 KiB，低于 1.5 GiB |
| 10 分钟残留 | 两轮结束 10 分钟后 RSS 分别为 440,400 / 594,784 KiB，均低于 700 MiB |
| swap / 整机安全 | 修复后进程观察内 swap 最高 25,964 KiB，第二轮相对周期前增量约 13.6 MiB；host swap delta 无正增量，`MemAvailable` 最低 1,212,184 KiB，无 OOM 或 worker restart |
| 相邻高负载 | open market 下 `offmarket_position_quality_lightgbm` 在 0.1s 内以 `market_session_not_offmarket:open_confirmed` 跳过，不构造四个 LightGBM service，无 RSS/swap 跃升 |

验证命令/结果：

- learning/runner/watermark/capability/scheduler/offmarket 相关回归：`136 passed`；最后 run-once 去重后子集：`39 passed`。
- `scripts/check_openapi_snapshot.py`：当前；`state_schema_migrate.py --check`：`current=latest=minimum=12`、无 mismatch。
- `py_compile` 与 `git diff --check` 通过；无 migration、endpoint、静态开关、服务、线程、表或调度器变化。

连续两轮全部达标，因此条件性的一次性子进程隔离未启用。
