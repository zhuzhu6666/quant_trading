# 全项目分期修复发布状态

> Status: active current-state index
> Snapshot: 2026-07-26
> Scope: current phase, last verified evidence, next batch, and unresolved runtime acceptance
> Source of truth: 运行状态必须在每次实施前重新读取服务、PostgreSQL、`runtime_kv`、日志和 broker

本文不保存逐时操作流水，也不重复架构合同。历史细节通过 Git 历史和 repair ledger
追溯。当前权力边界见 `docs/system-source-of-truth.md`，执行流程见
`docs/change-impact-checklist.md`。

## 1. 当前阶段

| 阶段 | 状态 | 剩余工作 |
|---|---|---|
| P0 保护现场 | complete | 无 |
| P1 broker 成交事实 | runtime acceptance | 新 broker deal、restart replay、完整持仓生命周期 |
| P2 风险指标平面 | complete | 继续观察，不新增平行风险路径 |
| P3 证据/记忆/effect | complete | canonical memory 与 active application/effect identity 已收敛 |
| P4 V16 因果调度 | complete | causal grouping、单一 actionable/authority、单次 mutation 与三条 lane 已收口 |
| P5 架构收敛 | continuous | 每个 P3/P4 小批同步删除，不再单独堆大重构 |
| P6 Demo 观察/毕业 | blocked | 等前置正确性和真实样本 |

当前运行姿态：

- 有界 Demo incident 已由 operator 显式恢复 `normal`，governance pause 已解除，
  no-new-risk latch 为 cleared；
- Safety 保持 `shadow`；
- Generation、Execution Outcome、PG Job Queue 保持 disabled；
- Governance 保持 `dual_record`；
- 不自动切 flag、不进入 `live_autonomous`；开仓继续服从市场时段、canonical RiskPolicy、
  fresh reconcile 和 cause-specific safety latch。

上述值必须在下一批开始前从 process-loaded flags 和运行事实重新验证，不能只相信本文。

## 2. 已完成事实

### P0

- incident：`AUTONOMY-REPAIR-20260724-01`。
- 数据备份、污染 cohort、repair ledger 和修复不变量已建立。
- close/reduce/tighten/rollback 与只读观察保持可用。

### P1 代码和历史修复

- 1,150/1,150 broker deals 已按原始价格合同更正。
- 金额字段与价格字段解析已拆分。
- 10,800 条直接关联学习样本已隔离。
- 12 条无法权威恢复的 close quote 保留审计原值并 quarantine。
- 48 条双重污染 counterfactual 终态失效，13 条干净记录重建。
- 唯一污染扩张 mutation 已原子回滚。
- Execution Outcome price-integrity 已进入故障矩阵。

P1 尚未取得 post-repair 新成交和完整生命周期，因此仍是 runtime acceptance，不得因
单测或历史数据修复标记 complete。

### P2 canonical risk

唯一生产链：

```text
closed-bar forward_var_input.v1
  + fresh account/positions
  + current/final candidate signed notional
  -> backend.risk canonical calculators
  -> risk_metrics_snapshot.v2
  -> RiskPolicy / readiness / API / Web read-only consumers
```

已完成：

- 删除重复 root risk 模块、live 内联统计和 API 平行重算。
- live/replay 共用 frozen input、projection 和 lifecycle payload builder。
- 95% 使用既有硬闸；99% 只 shadow，没有新增阈值。
- readiness 只读 `runtime_kv[risk_metrics_snapshot.v2]`。
- Web 四个页面统一使用 `decodeCanonicalRiskSnapshot()`。
- 旧 VaR/stress/concentration 字段和别名 fallback 已删除。
- known 空仓零敞口可见；unknown/warming_up/error 不补零。
- schema v12 已应用，OpenAPI 未发生非预期变化。

最后一轮 P2 证据：

- D16/risk/policy/live/parity/readiness/replay/API 针对性批次：`275 passed`；
- 补充模块批次：`236 passed`、`163 passed`；
- 前端 `npm test`、`typecheck`、`build`：通过；
- 上一轮全量基线：`2452 passed, 9 skipped`；
- 本批按 operator 要求未重新运行全量。

## 3. P3 第一批：writer/identity 收敛

已完成 writer 盘点：

| 事实 | 当前生产 writer | authority / identity |
|---|---|---|
| trade review | `TradeReviewer.review_closed_trade()`；历史 backfill `insert_review()` | `trade_outcome_review.review_id` |
| supervisor counterfactual | `evaluate_counterfactuals()` | `supervisor_counterfactual_review.counterfactual_id` + review/position/close_ts |
| experience memory | `ExperienceBuilder.build_from_review()` 计算 rich lesson，并统一调用 `upsert_trade_lesson_memory()` 写入；受控历史脚本复用同一 upsert | `trade_lesson:{review_id}`，source anchor 为 `trade_outcome_review.review_id` |
| learning sample | `autonomous_learning._upsert_sample()` | sample type + source table/source ID + contract fingerprint |
| application/effect | `LearningApplicationStateService` / `RuleEvolutionGovernor`；typed domain transaction writers | application ID + scope/action + committed mutation |

运行证据与本批删除：

- 576 条 review 均同时存在 `live_review` 与 `trade_lesson_memory.v1`；
- 260 条又被 `learning_backfill.v1` 重复写入，部分历史 source anchor 达 4–6 份；
- 删除 `rebuild_learning_state()` 内 `learning_backfill.v1` INSERT 和专用 ID 生成器；
- 6 条 suggestion evidence 已迁移到 canonical `trade_lesson_memory.v1` ID；
- PostgreSQL 已删除 260 条重复 projection，残留与悬空引用均为 0；
- 未新增 service、表、scheduler、worker、阈值、schema 或兼容字段。

第二小批已完成：

- `ExperienceBuilder` 保留原有 rich lesson 计算，但删除自身 SQL writer，统一调用现有
  `upsert_trade_lesson_memory()`；
- feature provider、agent briefing、factor counter-evidence、scorecard 和 V16 memory
  均只读 `trade_lesson_memory.v1`，旧 `live_review` reader 分支已删除；
- 576 条 canonical lesson 已合并原 live rich context，并保留 lesson/agent attribution；
- 189 条 suggestion evidence 完成 2,373 个旧 experience ID 精确替换；
- PostgreSQL 已删除 576 条 `live_review` projection，重启后残留、格式异常和悬空引用均为 0；
- 针对性测试：`64 passed`；删除最后两个 reader 分支后补充 `27 passed`。

第三小批已完成：

- 157 条 `legacy_experience_migrated.v1`、8 条
  `controlled_close_learning_backfill.v1`、5 条 `experience_builder.legacy_repaired` 均已证明
  存在 canonical lesson，且 canonical context 全部更完整；
- 删除 `ExperienceBuilder` 启动时 legacy source/timestamp 修复 writer；
- controlled-close 历史工具删除专用 ID 和 SQL writer，改为复用
  `upsert_trade_lesson_memory()`；
- 11 条 suggestion evidence 完成 35 个旧 ID 精确替换，170 条历史兼容 projection 已删除，
  残留与悬空引用均为 0；
- application/effect 审计为 3,423 个唯一 application ID、3,368 个唯一 effect、0 orphan
  effect；55 个无 effect application 全部是 `blocked_by_evidence` 或 `failed` 终态；
- 当前 16 个 active application/effect 对应 16 个不同 scope，无重复 active scope；
- memory、application/effect 与三类 domain writer 针对性测试：`80 passed`。

P3 完成。

### P3 技术底座补充（2026-07-29，Demo 未发布）

- `MemoryIntegrityReport` 已作为唯一只读比较器进入 brain memory API 和 learning readiness，覆盖原始 review、canonical lesson projection 与有界检索索引；降级只暴露证据问题，不成为交易或治理权限写入口。
- Windows 主动拉取、脱敏 `runtime_kv` 健康投影与隔离 restore verifier 已进入代码库；服务器不保存备份文件、未启用 S3、WAL archive、pgBackRest repository 或 timer。收到 Windows 成功拉取回执前，灾备状态仍是 `missing`；收到回执但尚未完成隔离演练时为 `degraded`，不是 completed。
- 未新增业务事实表、迁移、PG Job Queue 发布、外部缓存/向量库或任何实盘静态开关。

## 4. P4：V16 因果调度与专员闭环

- `V16CommandGate.is_actionable()` 是 readiness、stepper、authorize 和 claim 共用的唯一
  actionable predicate；只允许授权未过期、仍为 `available` 且尚未达到 apply 上限的
  delegate command。
- stepper 从候选窗口按该 predicate 选择，过期队首不再阻塞后续有效命令；claimed
  command 不再被重复视为可授权。
- authority freshness 只读不可变 `authority_issued_at`；claim/release/recovery 不续期。
- orchestrator 使用同一既有最大授权年龄终态取消过期 available command，不新增队列、
  readiness verdict、阈值或恢复 authority；同一实质委派需要重试时保留旧的
  `authority_expired` 终态并生成带新不可变授权时间的新 command，重复运行仍复用有效重试记录。
- posterior 仲裁先选择 supervisor 证据，再只在相同 `review_id`，缺失时依次按
  `trade_id`、`position_id` 匹配 entry review；复用 position 的另一笔交易不会混入。
- 运行库原唯一过期 available command 已取消为 `authority_expired`，`apply_count=0`；
  重启后新生成的 fresh command 在 orchestrator、stepper 和 Gate 三处一致为 actionable。
- P4 闭环针对性测试 `36 passed`，三类 specialist/Coordinator 补充批次 `57 passed`。

第二小批已完成：

- 删除 planner 与 orchestrator 内两套 `scope_type -> target_agent` 硬编码；
- `AgentAuthorityRegistryService.execution_owner()` 现在从既有 agent contract 的
  `execution_owner` 字段返回唯一执行 owner；
- V16 command 的 `specialist_must_use` 直接复用
  `AgentAuthorityRegistryService.required_gate()`，不再把三种 lane 一律标成同一组 gate；
- supervisor contract 补齐既有 `position_supervisor_template` execution owner 声明；
- V16 contract 内重复的静态 `required_gates` 描述已删除，未新增表、服务、队列、阈值或
  readiness；
- Gate 的 authorize/claim 删除固定 200 行候选截断，并在 SQL 层只读取既有授权时间窗；
  大量同 agent 其他 scope 的新命令不再遮挡目标 scope；
- 服务器恢复后仅运行低优先级针对性测试，最终合并验证 `23 passed`。

最终收口：

- `entry_quality` 已补入 autonomous learning 的 Agent Authority control surface、
  execution owner 和 RiskPolicy gate；专用 V16 delegation 删除最后一套硬编码 target/gate。
- autonomous learning、factor governance、position supervisor governance 三条 lane 均已验证
  success、noop/reject、失败释放后 retry、rollback 和 effect 终态。
- bounded runtime trace 已核对：
  `v16cmd_7be9876b49138e64e726 -> autonomous_learning ->
  psg_entry_quality_92771bd6472259f1 ->
  gmut_e7cba57522aa44fd8d36d4d370cd1f08 ->
  lapp_a2b661abfcc25d2ee724/effect`；原 mutation 为 `rolled_back`，并明确指向 committed
  rollback mutation `gmut_deddadacb3b849d2bd5da975c53530cd`。
- P4 分批低优先级最终验证：lane `20 + 28 + 3 + 5 passed`，V16/Authority/Coordinator
  `52 passed`，最后 authority 补充回归 `23 passed`；合计 131 个测试执行，全部通过。
- 未新增 service、queue、table、scheduler、worker、阈值或 readiness verdict。

P4 完成。后续只保留 P1 真实成交/完整生命周期验收、Safety shadow 运行证据和 P6 Demo
观察，不再继续扩展 V16 调度层。

2026-07-27 CVaR 最小仓位死锁修复：

- canonical authority 仍是 `RiskPolicyService`，阈值输入仍只来自
  `RiskLimitSnapshot <- RuntimeConfig`；
- 当日启用 candidate-forward CVaR 证据后的三笔最小仓位候选全部被旧 `2.0%` 上限
  拦截，精确值为 `2.007110%`、`2.009619%`、`2.092075%`，证明当前权益和 broker
  最小 volume 组合下存在交易停摆；
- 经 existing Governance Mutation Coordinator 提交
  `gmut_fcf9e7cc545e41ca9ba7b90e568a764e`，以 committed/current runtime overlay
  将 CVaR 开仓硬上限校准为 `2.5%`；该有界 Demo operator 入口不适用于 system actor
  或 live 账户。新上限足以覆盖近期最高候选，同时
  `2.5001%` 仍被硬拦截；未新增 service、gate、table、thread、计算者或旁路；
- reason 展示改为四位小数，消除 `2.0% > 2.0%` 的舍入歧义；
- policy/runtime/config/governance/live readiness 针对性验证 `209 passed`；
- backend 与 learning worker 受控重启后均 active，overlay hash
  `7bd980479adfd9f0e2c16390e6a1472a8ded5dc10d73ecf212b34b65dd62a619`
  在两进程恢复成功，effective CVaR limit 为 `2.5%`；启动期 safety freshness latch
  在 fresh reconcile 确认 intent 表为空、broker 无持仓后自动释放，未手工旁路。

2026-07-27 因子治理闭环收敛：

- canonical authority 保持为 `factor_lifecycle_state`、`governance_mutation_intent`、
  `V16CommandGate` 和现有 `FactorGovernanceOrchestrator`，未新增 service、table、
  scheduler、thread、阈值或 readiness verdict；
- hourly evolution handoff 改为同一 coordinator 内
  `factor_health -> V16 decision -> factor governance`，消除 300 秒 health freshness
  与错位 cron 无交集的问题；V16 失败仍只阻断扩张，不阻断收紧；
- Factor Governance 在领取 V16 前先生成 expansion preflight，没有 builtin
  activation/restore、shadow promotion 或 redundancy mutation 时返回
  `idle_no_expansion_action`，不再把 idle 周期写成 `blocked_by_v16_command`；
- 同周期 V16 对 concrete preflight 签发 evidence-bound
  `factor_governance_cycle` 单次委托；builtin 首次 `register_shadow_factor` 复用同一动作族，
  DSL promotion 直接读取 canonical lifecycle expression。每周期最多提交一个 lifecycle
  mutation；有提交时命令 `finalized/apply_count=1`，无提交时取消，不能跨周期复用；
- Catalog 删除 audit/canary 名称作为独立因子来源，运行只读目录由 5,380 条收敛为
  首次 clean snapshot 729 条；后续 health/lifecycle 产生的 canonical 条目可正常增长，
  2026-07-27 最终运行快照为 758 条。存在 lifecycle row 时其 stage/admission 覆盖进程
  Registry 和 RuntimeConfig，committed mutation 覆盖旧 suggestion 展示状态；
- `governance_coordinator` projection 改用稳定
  `factor_lifecycle_service/canonical` identity；backend committed-registry 恢复按 factor
  删除 5,542 条旧 PID 投影；最终仅保留稳定 identity 的 131 条当前投影，旧 projection
  是可重建运行事实，canonical lifecycle/mutation 审计未删除；
- 运行闭环实测：DSL `dxy` 候选进入 `PROMOTION_PREPARED`；下一周期仅
  `wick_rejection` 完成 `shadow_registered`，对应 V16 command
  `finalized/apply_count=1/max_apply_count=1`，同周期未执行第二个扩张 mutation；
- 因子治理、lifecycle、catalog、recovery 和 cards 针对性验证 `115 passed`；另有
  V16/Coordinator 交接回归集 `46 passed`。

2026-07-28 最小仓位监督动作收敛：

- canonical authority 保持为 `PositionSupervisor`；同一执行链新增的是既有 supervisor
  action 的 broker 可执行性预检，不新增 service、table、thread、配置或阈值；
- broker 最小/步进 volume 在 RiskPolicy 前把 reduce 归一为可成交 reduce、强证据
  close 或去重 no-op。弱证据最小仓位不再每轮产生“政策允许减仓”记录，也不触达 broker；
- 删除 reduce executor 内第二次 risk evaluation 和 reduce-to-close 改判。close 升级复用
  supervisor template 现有 `near_stop_loss_progress`（默认 0.85），不再使用独立硬编码
  0.8；
- Safety V2 planner 与 legacy preview 消费同一动作归一结果，避免 shadow 比较因最小仓位
  产生伪 mismatch；
- `/api/risk/policy/verdicts` 只读关联 `position_supervisor_trace`，Web 风险页分别显示
  “政策允许/政策拦截”和“真实执行/未执行”，历史 allowed-but-skipped 不再被显示成成交；
- supervisor sizing/action/lifecycle/API 针对性验证通过，Web production build 通过；
  仍需发布后观察新的最小仓位 MFE 回吐周期，确认运行日志不再连续产生不可交易 reduce。

2026-07-28 因子实时信号恢复与展示最小修复：

- recovery 的 canonical authority 保持为 `live_execution_recovery` + 既有
  `execution.deal_sync`；复用现有 `replay_lookback_seconds` bounded replay window，
  不新增 service、table、thread、调度器、轮询或阈值。`last_seen_at` 不再作为固定 5 秒
  的 close-deal 硬边界，完整 volume/cursor 与 projection commit 仍是关闭确认条件；
- `TradingPage` 复用 `/api/v4/recent-ticks` 返回的 signal rows，只有缺少显式
  `ts/time` 的记录才不展示；缺失 tactical/macro 等字段显示“未知”，不补零、不丢弃最新
  signal，也没有新增 WS/polling 路径；
- 针对性验证：recovery/deal-sync 21 passed，生命周期回归 5 passed，Web smoke、
  architecture、fact/auth、fact behavior、typecheck、production build 全部通过；
- 受控重启后 `recovery_position_state.position_id=279452614` 已落为
  `closed_replayed`，日志确认 recovery reconciled；随后 canonical `decision_log` 已
  产生新信号行 43705、43706，证明原先的 14:45 停摆已解除；
- 重启后出现的 cTrader `get_positions/fetch_bars/account_info` RPC timeout 属于独立
  的 broker transport/freshness 问题，当前仍按既有 fail-closed 规则保持新增风险受限，
  不通过增加重试并发或绕过 readiness 来掩盖。

2026-07-28 Web 实时状态来源收敛：

- `web_frontend/src/hooks/useLiveState.ts` 将 WebSocket 保持为 live snapshot 唯一实时来源；
  WS 建连前、close、error 和 ticket 失败均不再启动 HTTP snapshot 轮询，状态显示为
  `offline/WS 重连中`，只保留单 socket、有界 backoff 重连；
- loop/account/positions 的 HTTP endpoint 在 WS 已连接时仅保留低频 fact verification，
  WS 断开后 `refetchInterval=false`，删除 3 秒 HTTP fallback，不再出现 WS/轮询来源来回切换；
- 保留用户显式“刷新”触发的一次性 snapshot 请求和页面独立的 health/risk/session 查询，
  不新增后台 timer、WS 连接或并发重试；
- 前端 smoke、architecture、fact/auth、fact behavior、typecheck 和 production build
  均通过，构建产物中已无旧 `startPolling`/`setSource("polling")` fallback 路径。

2026-07-29 因子信号未知分与重复写入最小修复：

- `build_signal_decision_log_payload` 是 factor_v4 signal 的唯一 canonical writer；保留
  `direction=0`/gate blocked 记录中的真实 tactical、macro、active、abstain 字段；
- 删除旧 `_write_live_trade_log_factor` 及两个调用点，重复 decision bar 不再伪造
  `decision_type=signal`，减少无意义的 PostgreSQL 写入，不新增线程、定时器、轮询或重试；
- 针对性验证：`tests/test_live_decision_pipeline.py` 与
  `tests/test_live_service_tick.py` 共 33 passed，`live_service.py` 定向编译和 diff check 通过；
  发布后需确认新 `state_v1.decision_log` signal 行不再出现无因子字段的交替记录。

## 5. 仍需真实运行证明

以下不能由测试替代：

- post-repair 新 broker deal 的价格/金额合同；
- restart 后 deal replay；
- `open -> protection -> close -> deal sync -> review -> sample` 完整生命周期；
- Safety shadow 连续 24 小时空仓或一个完整 broker position lifecycle；
- 当前源码绑定的 fault matrix；
- 每次发布阶段的 process-loaded flags 和 release preflight。

真实证据未满足时：

- P1 保持 runtime acceptance；
- Safety 不从 shadow 切 enforce；
- 后续静态开关不推进；
- 不阻止有界 Demo 在现有 legacy-authoritative 开仓链产生验收交易；不得用 Safety
  enforce 的观察时长重新制造 operator incident 锁。

## 6. 每批状态更新格式

以后本文件只追加或替换以下当前信息，不保留逐时流水：

```text
Batch:
Canonical authority:
Deleted paths:
Targeted verification:
Migration/OpenAPI/build:
Runtime verification:
Remaining compatibility:
Unresolved live evidence:
Next batch:
```

阶段完成后删除已失效的中间描述，只保留最终结论和指向验收矩阵/repair ledger 的引用。
