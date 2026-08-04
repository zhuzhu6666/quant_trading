# Active Legacy Debt Register

> Status: active
> Last verified: 2026-08-03
> Scope: 只登记尚未退出的兼容、重复 authority、隔离数据和回归。

已完成旧债不在本文保留；Git 历史和测试是追溯依据。新增条目必须写清 canonical 路径、剩余旧路径、退出条件和验证。

## 1. 全局收敛

### 平行 authority、重复门控和无退出兼容层

- 状态：`active`
- canonical：一个事实只有一个生产计算者和一个写入者；Safety、Risk、Readiness、API、前端不得平行重算同一授权事实。
- 剩余：Safety/Generation/Execution Outcome/Governance/PG Job Queue 仍有发布期开关或旧兼容；客户端仍有少量旧 fact 字段迁移。
- 退出：新路径通过各自运行门后，同批删除旧 authority、fallback、同义 blocker 和 pass-through wrapper。
- 验证：调用链、静态入口扫描、合同测试、运行 snapshot 与 `git diff --stat`。

### shadow/discovered/live 生命周期兼容

- 状态：`migrating`
- canonical：`factor_lifecycle_state` + `factor_runtime_projection`；ACTIVE 必须经 typed Coordinator/V16、稳定 artifact、fresh health 和 loaded ack。
- 当前：Catalog 已以 lifecycle row 覆盖 Registry/RuntimeConfig stage/admission，审计和 canary 名称不再独立创建目录条目；coordinator projection 已改用稳定身份并在 backend 恢复时删除同 factor 历史 PID 行。invalid DSL 只保留 `shadow_register_invalid_dsl_skipped` 审计，不进入 Registry/lifecycle；缺真实 shadow performance 的候选留在当前 stage，不用 fallback 分数推进。旧版治理形成的 terminal builtin quarantine 已接入证据化 `generation+1` SHADOW 重入，旧 terminal 行和 mutation 保持不可变，不再保留 no-op 自动恢复钩子。
- 剩余：切入 typed lifecycle 前已存在的 native ACTIVE builtin 尚无 lifecycle row，Catalog 对这组代码内置因子保留 Registry/RuntimeConfig 兼容；领域服务的 coordinator-off 隔离兼容仍在，但生产状态库已禁止回退到直接 overlay 写入。2026-08-04 已删除自动晋升对 catalog `source=shadow`、runtime name 等于 canonical DSL ID 及 builtin 重入对旧 `governance_action=disable_factor_live` 的依赖，统一按 canonical lifecycle origin/stage/definition 判定；同时删除 preflight 300 秒、最终 lifecycle 180 秒的 freshness 错位（统一为 900 秒，与 15 分钟 health 刷新节拍对齐），evolution_hourly cron 由 `2 * * * *` 提升为 `23,53,58 * * * *`（唯一不与独立治理 15/30/45、nursery 7/22/37/52、supervisor 9/39、autonomous 12/42、offmarket :20 任一 single-flight 运行窗口重叠的 3 档排布）使完整治理链每小时 3 次且不互斥跳过，退役因子保留 DEAD 健康快照以推进恢复时间线，activate 健康门槛与晋升证据对齐（WATCH + score≥40）。真实 V16 链已创建 `vol_ma_ratio`、`obv_slope` generation 2 SHADOW，并把 prepared GP `dsl_auto_a3eeb...` 激活为 ACTIVE/admitted。
- 退出：现有 ACTIVE builtin 按 code-bound identity、V16、prepared、真实 loaded ack 和 fresh health 分批重入 lifecycle 后删除 builtin fallback；稳定 enforce 发布后删除领域服务的 generic restore 兼容。启动层的旧 template/supervisor/Registry restore 已删除，不得用直接数据库回填 ACTIVE 绕过晋升证据。

### 历史 runtime overlay 缺少 committed mutation 绑定

- 状态：`quarantined`
- canonical：非空 mutation 必须是 committed/current 且 config/domain hash 完整绑定；空 mutation 只允许经 hash-bound operator review 恢复明确 risk tightening。
- 禁止：用来源名、默认值或“看起来保守”恢复扩张/未知 overlay。
- 退出：历史行逐项复核、重建或清理；确认 committed projection 后按 cause 身份释放 latch。

## 2. 执行与运行时

### JobManager 本地重任务兼容

- 状态：`migrating`
- canonical：PG Job Queue 开启后，八类重任务由 PostgreSQL durable job + 独立 worker 执行。
- 当前：静态开关默认关闭，job worker inactive；flag-off 仍有本地 executor 兼容。
- 退出：受控开启、lease/recovery 稳定发布后删除本地重任务执行路径。

### emergency close 严格完成语义

- 状态：`migrating`
- canonical：先持久化 no-new-risk latch；只有 fresh post-reconcile 确认目标 position ID 消失才算 completed。
- 剩余：非 safety 调用仍可能使用 legacy `refresh_positions()` 值接口。
- 退出：所有安全/恢复调用只接受 immutable authoritative reconcile contract。

### broker unknown outcome 兼容

- 状态：`migrating`
- canonical：结果只允许 confirmed/rejected/unknown/simulated；unknown 立即锁存、禁止重发，必须由 broker recovery/reconcile 唯一消解。
- 当前：Execution Outcome v2 静态开关默认关闭，故障矩阵已建立，仍需受控 demo 观察。
- 退出：通过发布门后删除 position-ID 猜测和旧 result 兼容；unknown 语义永久保留。

### cTrader deal price 修复运行验收

- 状态：`migrating`
- canonical：executionPrice/entryPrice 保留 broker 原始价格；只有 money 字段按 moneyDigits 缩放。
- 已完成：1,150 条历史 deal 精确更正，污染学习、反事实和治理链已隔离或回滚。
- 剩余：新的 broker deal 与完整开仓—保护—平仓—同步—学习生命周期验收。
- 禁止：用固定金价阈值或猜测值补价格。

### live generation / Safety shadow 兼容

- 状态：`migrating`
- canonical：旧线程真实退出前保留 ownership；每 tick 先 reconcile/safety 后 alpha；Safety v2 与独立 legacy preview 比较。已通过门的同一 closed bar 仅在 watchdog 自有 freshness cause 短暂锁存时由现有 serial owner 保留一次内存 admission retry，下一轮 canonical safety/reconcile 后复用原 open pipeline；bar 推进或出现其他 cause 立即丢弃，不新增执行通道。
- 当前：Safety 为 shadow，尚未满足完整持仓生命周期或 24 小时无仓观察；Generation 开关不变。
- 退出：观察与故障矩阵通过、受控发布稳定后删除 loop globals、旧 safety 尾部执行和并发 refresh 兼容。

### live_service 领域重力

- 状态：`migrating`
- canonical 模块：reconciliation、serial loop、emergency、position protection、open submission/protection/processing、execution recovery 已分离；fresh position reconcile 是既有 `recovery_position_state.recovery_meta.position_path` 的唯一 live 累计写入边界，event/API 投影不写入。
- 剩余：`live_service` 仍保留 process wiring、兼容状态发布和少量 lifecycle wiring；仓位路径持久化失败必须显式降级为 unknown，不得把单次观测伪装成累计 MFE/MAE。
- 验证：月初当月月库为空时，暖机、DataStore 与 system_health 通过 `bars_monthly_read_paths()` 回读最近历史闭合 bar；未改变 bar freshness、风险或 readiness 门槛。
- 退出：只迁出真实决策/状态机；不为“拆文件”新增 wrapper。稳定发布后删除旧 globals 和 compatibility authority。

## 3. 治理、研究与客户端

### 因子扩张缺少后验防抖（已接入）

- 状态：`active`
- canonical：`FactorGovernanceOrchestrator._posterior_expansion_guard` + `posterior_expansion_verdict`，复用 `learning_application_effect`（scope_type=factor、最新非 rolled_back/superseded effect）作为唯一后验事实源。`delta_avg_reward < factor_governance_posterior_block_delta`（默认 -0.05）且 `observed_trade_count >= factor_governance_posterior_min_samples`（默认 10）→ `blocked_by_posterior` 剔除扩张候选；样本不足 → `posterior_degraded` 保留但标记；无记录/非负 → `posterior_ok`。四类扩张候选（builtin activation、active zero-weight restore、quarantined builtin restore、shadow promotion）统一在 preflight 过闸，preflight 输出 `posterior_blocked_ids`/`posterior_degraded_ids` 只读投影；V16 delegate 粒度不变（批轮次、specialist 选因子），生产查询不确定 fail-closed 阻断扩张。
- 剩余：`posterior_degraded` 的降级应用路径（受限权重/scope）尚未在 apply 侧实现，当前仅标记不阻断。
- 已关闭（2026-08-05 L4/L5 批次）：因子×regime 条件绩效缺口由批次 A（lightgbm v5.0 新增 `current_regime_fit_score`/`rolling_regime_fit_avg`/`rolling_regime_fit_min` 特征，消费 `trade_outcome_review.regime_fit_score`）+ 批次 B（`project_current_market_regime()` 从 `experience_memory.regime_id` 只读投影当前 regime）补链，**未改 ic_tracker 签名**（Q1 拍板）；factor_health 的 `regime_consistency` 保持 5 段分桶近似，真实 regime 条件绩效唯一由 lightgbm 承担。批次 C/D 在此基础上实现降权/恢复条件化。
- 退出：degraded 降级应用落地且连续真实周期验证后，将本条目转为 `migrating`。

### 治理 mutation 跨账本提交兼容

- 状态：`migrating`
- canonical：`GovernanceMutationCoordinator` 在同一 PG 事务内 reserve、重验 before、写 intent/领域事实、finalize；commit 后才发布 RuntimeConfig。
- 当前：mode 为 dual-record；旧 off 兼容与旧 ledger 投影仍在。只读 release preflight 已按 `error_stage=v16_claim` 与真实 transaction/recovery failure 分类 aborted intent，不改变状态机或应用证据要求。
- 退出：稳定 enforce 发布后删除旧 consume/direct overlay/Registry mutation 兼容。

### position supervisor 旧 advisory 冲突占位

- 状态：`migrating`
- canonical：持仓模板的自动切换只从 V16 candidate bridge 进入，`V16CommandGate.claim` 与 `PositionSupervisorGovernanceMutationService` 的 Coordinator transaction 共同完成单次授权和 finalize。
- 当前：历史/旧 worker 写入的 non-V16 `position_supervisor_template` advisory 仍可留作审计记录；它们已不再拥有 approve/apply 或 candidate conflict 权力，并将在既有 demo review/apply 路径中 terminalize。新生成候选只能针对一个 control 和一个 regime stratum，完整快照必须能由 evidence 中的单 scalar patch 证明。2026-08-01 已修正正常 V16 `posterior_not_selected` rotation 的 scorecard 统计、claim evidence 传递和 aborted mutation 的 command-bound retry；首个真实 V16 bridge 已完成 `claim -> Coordinator finalize -> application`，旧失败审计保留。
- 剩余：已应用 suggestion 仍需经过 effect observation 与既有 maturity counting，不能据此解锁自治或删除旧 advisory writer。`legacy_awe_trailing` 的非 Demo 兼容 planner/trace/close attribution 仍存在；Demo 已在 protection cycle 中标记 `observed/superseded`，不得与 canonical supervisor 同时 applied。Parity replay 仍是 diagnostic-only，不能替代 broker lifecycle 证据。
- 退出：历史 active advisory 全部 terminalize，连续真实 demo cycle 证明 V16 bridge、claim、Coordinator finalize 和 effect observation 连通后，删除旧 advisory 生成路径；另在 replay、trace、effect 证明 trailing 行为等价后，删除 legacy AWE trailing 执行分支、兼容配置和不再需要的耦合测试。不得通过 SQL 改写历史 review、补 command 或补成熟样本提前满足退出条件。

### parity replay 尚非 live-equivalent

- 状态：`migrating`
- 当前：复用 closed-bar、RiskPolicy 与保护纯原语，并绑定 config/data/code/factor artifact hash，但缺 broker/tick/safety/account/cost/projection-ack 的完整 PIT 事实。
- 权限：固定 `diagnostic_only`、治理数量为零；runner 永不自授权。
- 退出：只有独立 certification 重验完整 live lifecycle 后才能讨论 live-parity evidence。

### 历史参数扫描回测

- 状态：`retired`
- canonical：`ParityReplayRunner` 经 `/api/backtest/run`、CLI 和参数模板离线验证统一调用。
- 已删除：12 组参数扫描、旧文本报告解析、`strategy/backtest.py`、`alpha/backtest/vectorized.py` 和重复 `/api/ops/replay/parity-run` 执行入口。
- 边界：回测工件只提供隔离模拟训练样本，不写真实学习/后验/治理账本，也不计入模型晋级。

### API/frontend 旧事实字段

- 状态：`migrating`
- canonical：endpoint-specific `fact.v1`；unknown/stale/error 不得显示绿色或授权 start/unlock，最后 known 值可带时间保留。
- 剩余：Web/小程序 recursive compat 和旧字段窗口。
- 退出：客户端迁移完成且满足两个小程序版本或 30 天取更长者，删除旧回退。

### legacy auth 路径

- 状态：`migrating`
- canonical：Argon2id、短 access、旋转 refresh session、单次 WS ticket、扩张 step-up、durable revocation。
- 剩余：SHA-256、legacy access、URL JWT 三个显式兼容开关。
- 退出：全部客户端迁移后关闭并删除；stop/emergency 的本地可验证风险缩减能力不得受 PG 故障阻断。

## 4. 明确退役，禁止恢复

- SQLite `data/state.db` 运行态主库；
- 历史 tick 采集与 `ticks.duckdb`；
- L2 collector、depth 风控字段与历史 L2 库；
- MT5 并行执行路线；
- 旧 Web Console/H5 web-view；
- 旧 cloud deploy/docker-compose 打包路线；
- 临时前端 smoke/debug 脚本和仓库内历史回测输出。

## 5. 登记模板

```text
### 标题
- 状态: active | migrating | quarantined | regressed
- canonical:
- 剩余:
- 退出:
- 验证:
```
