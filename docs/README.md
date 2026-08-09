# 项目总览与当前状态

> Status: canonical
> Last verified: 2026-08-09
> Scope: 新对话、实施、排障和发布的唯一文档入口。

读完本页即可知道项目当前处于什么阶段、系统怎样运行、哪些事情禁止做。只有准备修改某个领域时，才继续读后面的对应合同。

## 1. 当前结论

- 当前分支：`main`；工作区当前有未提交的 Demo-only 模型治理与历史证据收敛修复：
  模型阶段仅允许 `shadow -> demo_canary -> demo_active -> quarantined`，模型不得接入
  Live 阶段；真实 Demo `live_service`/live loop 执行链保留。
- P0 已完成。
- P1 代码和历史污染修复已完成；本批复核后 `trade_outcome_review=679`、
  `autonomous_learning_sample=34,328`。污染样本保留审计但不得训练/治理；
  仍继续等待更多真实 broker deal 与完整持仓生命周期运行验收。
- P2 代码和合同已完成，schema 已到 v12；运行验收仍继续，静态发布顺序不推进。
- P3 writer/identity 收敛已完成：current 与历史 review memory 均只保留
  `trade_lesson_memory.v1` canonical projection；application/effect 当前 16 个 active scope
  一一对应，无 scope 冲突或 orphan effect。此结果不扩大运行权限。
- P4 V16 因果调度已完成：同交易 causal grouping、唯一 actionable predicate、唯一 Agent
  Authority owner/gate、单命令单 mutation 和三条 deterministic lane 均已收口。
- 有界 Demo 已由 operator 显式恢复 `runtime_incident_mode=normal`，治理扩张暂停已解除，本地
  no-new-risk latch 已清空；Demo 开仓不再等待 P1/P2 观察时长，但仍服从 market session、
  canonical RiskPolicy、fresh reconcile 和真实 safety cause。
- Safety、Generation、Execution Outcome、Governance、PG Job Queue 的静态发布开关不得随普通修复切换。
- 学习记忆完整性已由只读 `MemoryIntegrityReport` 覆盖原始复盘、经验投影和检索索引；它只暴露证据问题，不改变 Demo 或实盘权限。
- 学习 worker 的完整周期已收口为 watermark-gated `:12/:42 UTC` 唯一自动 owner；常规 nursery 只执行 orchestration/review/bridge/reconcile 和最多一个 recommended apply step。周期投影统一为紧凑 `autonomous_learning_cycle.v2`，完整 evidence 继续落现有 canonical 表，不新增表、服务或调度器。
- 灾备当前采用 Windows 电脑在线时的主动拉取：服务器只流式输出 `quant_audit` 的逻辑快照，不保存备份文件、不启用 WAL archive、S3、pgBackRest repository 或 timer。尚未收到 Windows 成功回执或隔离恢复演练前，灾备必须显示 `missing/degraded`，不得误报为可恢复。
- 最近已知全量基线：`2452 passed, 9 skipped`。日常小批默认只跑针对性测试；阶段/发布验收才跑全量。
- 账户已切换：cTrader demo 账户 47276606（login 5817896）现为 USD 计价，
  当前权益约 `$344.76`（2026-08-01 实测 balance/equity=344.76，
  非旧文档记录的 EUR €10,982）；风控与 Kelly sizing 均以当前权益为基准。

2026-08-09 Demo-only 模型治理批次（全部为本次实查）：

- runtime 当前 `autonomy_mode=demo_autonomous`、`demo_model_influence_enabled=false`，
  三个模型均为 `shadow`；代码/配置不存在任何模型 Live 阶段标记。
- `close_without_open=0`；修复脚本默认不允许 close-only 推断，当前无剩余候选。
- 679 条 review 均带 `execution_quality_evidence.v2`；full 405、partial 272、unknown 2，
  不再用固定 `0.60` 作为有效执行质量证据。
- 34,328 条样本中污染 3,406 条，污染样本允许训练/治理均为 0；636 条开仓后验全部带
  `open_target.v2`。开仓消费者资格已按其实际职责单独计算：289 条可进入
  `open_quality_lightgbm`，其余因 flat、执行证据不足、未成熟或字段质量问题隔离；全局严格
  contract 仍保留 272 条，避免把因子归因要求误加到开仓模型。
- 当前 `open_quality_lightgbm` loader 候选 291 条、最终选中 289 条，另外 2 条因缺
  `composer_version`/`active_alpha_universe` 被质量门排除；历史污染样本不进入训练或治理。
- 新开仓在 broker 提交前必须通过 `open_learning_context.v2`：缺少 entry cluster、行情微观、
  完整 bar、执行、决策质量、事件、数据质量或 market session 任一项都会在 broker mutation
  前 fail-closed；成交后再要求真实 fill price 并写入 `open_context_quality.ready=true`。

2026-08-01 运行核对（全部为本次实查）：

- `quant-backend.service`、`quant-learning-worker.service`、`caddy.service` active；
- `quant-job-worker.service` inactive，与 PG Job Queue 静态开关默认关闭一致；
- `/api/health` 为 `db=connected`、`ctrader=connected`；
- PostgreSQL `state_v1` migration `current=minimum=latest=12`，无 mismatch；
- `risk_metrics_snapshot.v2` 与 `backend_readiness_snapshot.v1` 正常刷新
  （snapshot updated_at 均在分钟级内）；
- readiness：当前 generic `ready_for_autonomous_mutation=true`、
  `ready_for_release=true`，但 `ready_for_live_execution=false`、
  `ready_for_live_alpha=false`；当前 supervisor `learning_repair.ok=false`，
  `canary.broker_mutation_allowed=false`，成熟度门仍按 50 笔 clean mature
  positions 和既有 session/regime 条件执行；
- live loop 运行但处于 degraded/not accepting new risk，当前市场关闭；
  不把 cTrader 连接正常解释为可开仓；
- broker reconcile fresh（account/positions reconcile id 均存在），
  recovery_position_state 当前全部 `closed_replayed`（587 条），当前空仓；
- risk snapshot 为 closed-bar M5 500 样本，空仓时 VaR/CVaR=0 是计算结果，
  不是缺失值兜底；
- 月切换读取已统一复用月库新到旧路径：当月库为空但上月存在闭合 bar 时，暖机、
  DataStore 与 `system_health` 不再把数据误判为缺失；本批未修改风险、readiness 或成熟度阈值；
- AWE 权重自适应每 30 分钟计算，但持续 `blocked_by_admission`：
  rsi_14 / engulfing / wick_rejection / fib_rejection_confirmation /
  candle_body_pressure 等因子的 active application/effect 处于
  `mixed`/`observing`，reason 为 `existing_effect_window_must_terminalize`，
  权重计算未落地；
- 02:40:12 LONG 信号 gate=passed 但被 `learning_weak_signal_threshold`
  SKIP，未下单；
- readiness 快照内嵌 `system_health` 显示 unknown/score=0，但
  monitor.system_health 日志每 60s 报 healthy score=1.00，投影口径待核对；
- 02:18 前后出现过约 2 分钟 broker-missing 窗口（280452088 平仓前后），
  session_restore 连续 WARNING "broker-missing positions lack close deals"，
  随后 close deal 327654818 到达自动恢复，符合 position_reconcile_conflict
  安全闩设计；
- Safety shadow 仍只处于 observing，尚未满足完整持仓生命周期或 24 小时无仓观察门槛。
- 历史已修复项（保留为事实）：candidate-forward CVaR 开仓上限校准为 `2.5%`
  （原失真 2.0%）；factor governance hourly 闭环
  `factor_health -> V16 -> Factor Governance` 同周期执行；因子治理 watchfreshness
  时序缺口已修复（仅 watchdog 自有 freshness cause 可在同一 closed bar 内重试）。

本批 2026-08-01 V16 supervisor 首桥接修复刷新：

- `quant-backend`、`quant-learning-worker` 受控重启后 active；
  `/api/health` 为 `db=connected / ctrader=connected`。
- `v16_brain` 运行 scorecard `quality_score=0.5488`；正常
  `posterior_not_selected` rotation 不再作为失败生命周期扣分，内部统计未进入
  `agent_scorecard.v1`。
- 最新 review 为 `bridge_ready`；旧 aborted command/mutation 保留审计，恢复后的
  supervisor command 已 `finalized/apply_count=1`，Coordinator intent 为
  `committed/projection_status=current`，suggestion/application 已 applied；effect
  observation、maturity counting 和自治解锁仍未宣称完成。
- `runtime_kv[backend_readiness_snapshot.v1]` 当前 generic
  `ready_for_autonomous_mutation=true`，但当前 supervisor 的
  `learning_repair.ok=false`、`canary.broker_mutation_allowed=false`，所以不代表
  supervisor 自治扩张已解锁；市场关闭/no-new-risk 等 blocker 继续按真实状态投影，未通过
  SQL 改写历史 review、补 command 或补成熟样本。

这些是带时间的运行快照，不是永久事实。每次实施或回答“现在能否交易/发布”前必须重新查询。

## 2. 当前生产结构

```text
cTrader spot/account/positions/execution
  -> serial live loop
     -> closed-bar factors and signal
     -> canonical RiskPolicy / RiskGovernor
     -> broker execution intent and reconcile
     -> position protection / emergency reduction
  -> PostgreSQL state_v1
     -> canonical runtime snapshots
     -> read-only readiness and fact.v1 APIs
     -> Web full console / mini-program status surface

learning worker
  -> observation, learning, factor and governance evidence
  -> typed governance mutation path
  -> committed runtime projection
```

唯一权力边界：

| 领域 | 唯一事实或执行权 |
|---|---|
| broker 账户、仓位、成交 | cTrader 权威响应 + fresh reconcile |
| 运行态、恢复、学习审计 | PostgreSQL `state_v1` |
| 开仓/改仓/治理风险裁决 | `RiskPolicyService` / canonical risk calculator |
| Safety | serial safety plane；旧链只在发布观察期做只读比较 |
| RuntimeConfig 变更 | typed governance mutation + committed projection |
| readiness/API/frontend | 只读 canonical snapshot，不重新计算授权事实 |
| K 线 | `data/bars_monthly/bars_YYYY_MM.duckdb` |
| 外部 PIT 数据 | `data/external_data.duckdb` |
| 经济事件 | `data/events.duckdb` |

历史 tick、L2、SQLite `data/state.db`、旧 Web Console/H5、MT5 并行执行路线均已退役，不得恢复。

## 3. 当前主线

当前只做 P1/P2 运行验收与兼容删除；有界 Demo 可直接产生验收交易：

1. 继续核对 post-repair 新的真实 broker deal、开仓、保护、平仓、同步、复盘与学习
   生命周期（当前已复核 review 679 条、sample 34,328 条，证据持续积累）；
2. 完成 Safety shadow 的完整生命周期或 24 小时无仓观察与故障矩阵；
3. 对每条仍在迁移的兼容路径收集退出证据，同批删除旧 authority、旧重算、旧字段回退或无意义 wrapper；
4. P4 已完成，不再扩展 V16 调度层；下一步只处理真实运行验收与 P6 Demo 观察。

实施细节见 [planning/production-autonomy-repair-optimization-plan.md](planning/production-autonomy-repair-optimization-plan.md)，实际进度见 [phased-repair-rollout-status.md](phased-repair-rollout-status.md)，门槛见 [phased-repair-acceptance-matrix.md](phased-repair-acceptance-matrix.md)。

## 4. 最小工作流

```text
读本页
  -> 查 system-source-of-truth
  -> 查 active legacy debt
  -> 按 change-impact-checklist 确认调用链和影响面
  -> 最小修改，并同步删除被替代路径
  -> 针对性测试 + migration check + OpenAPI check
  -> 必要时受控重启
  -> 服务 / PostgreSQL / runtime_kv / 日志只读验收
  -> 更新本页状态、rollout status、acceptance matrix
```

硬规则：

- 一个事实一个计算者，一个状态一个写入者；
- 不新增 `RiskMetricsService`、线程、调度器、数据库表或阈值，除非现有合同无法表达且证据充分；
- unknown/warming_up/stale/error 保持真实语义，禁止默认零、兼容值或猜测值；
- readiness、API、Web、小程序不得复制 Safety、风险和授权计算；
- 新路径若未删除被替代路径，阶段不得标为完成；
- 不提交、不推送、不切换生产开关、不清锁，除非用户明确要求。

## 5. 文档地图

### 每次系统级修改必读

1. 本页；
2. [system-source-of-truth.md](system-source-of-truth.md)；
3. [legacy-debt-register.md](legacy-debt-register.md)；
4. [change-impact-checklist.md](change-impact-checklist.md)。

### 当前工程收口

- [planning/production-autonomy-repair-optimization-plan.md](planning/production-autonomy-repair-optimization-plan.md)：唯一活动实施计划；
- [phased-repair-rollout-status.md](phased-repair-rollout-status.md)：已完成、观察中、阻塞项；
- [phased-repair-acceptance-matrix.md](phased-repair-acceptance-matrix.md)：阶段门和发布证据。

### 领域合同，按需读取

- [api-fact-contract.md](api-fact-contract.md)：`fact.v1`、freshness、unknown 和前端展示语义；
- [learning-evidence-contract.md](learning-evidence-contract.md)：学习样本、污染、资格和权重；
- [position-supervisor-contract.md](position-supervisor-contract.md)：持仓监督器输入、候选和执行边界；
- [factor-card-schema.md](factor-card-schema.md)：因子卡片/目录展示合同；
- [parameter-template-contract.md](parameter-template-contract.md)：参数模板及 online/offline 变更边界；
- [server-backend-sop.md](server-backend-sop.md)：启动、日志、数据库、cTrader、重启和运行验收。

### 文档维护

- [documentation-governance.md](documentation-governance.md)：文档职责、更新和删除规则。

未列出的历史设计、版本计划和完成流水已删除；需要追溯时使用 Git 历史，不恢复为活动文档。
