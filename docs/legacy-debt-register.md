# Active Legacy Debt Register

> Status: active
> Last verified: 2026-07-26
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
- 剩余：coordinator-off generic rollback/restore 兼容。
- 退出：稳定发布后删除 generic restore，Registry/RuntimeConfig 只保留 committed projection。

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
- canonical：旧线程真实退出前保留 ownership；每 tick 先 reconcile/safety 后 alpha；Safety v2 与独立 legacy preview 比较。
- 当前：Safety 为 shadow，尚未满足完整持仓生命周期或 24 小时无仓观察；Generation 开关不变。
- 退出：观察与故障矩阵通过、受控发布稳定后删除 loop globals、旧 safety 尾部执行和并发 refresh 兼容。

### live_service 领域重力

- 状态：`migrating`
- canonical 模块：reconciliation、serial loop、emergency、position protection、open submission/protection/processing、execution recovery 已分离。
- 剩余：`live_service` 仍保留 process wiring、兼容状态发布和少量 lifecycle wiring。
- 退出：只迁出真实决策/状态机；不为“拆文件”新增 wrapper。稳定发布后删除旧 globals 和 compatibility authority。

## 3. 治理、研究与客户端

### 治理 mutation 跨账本提交兼容

- 状态：`migrating`
- canonical：`GovernanceMutationCoordinator` 在同一 PG 事务内 reserve、重验 before、写 intent/领域事实、finalize；commit 后才发布 RuntimeConfig。
- 当前：mode 为 dual-record；旧 off 兼容与旧 ledger 投影仍在。
- 退出：稳定 enforce 发布后删除旧 consume/direct overlay/Registry mutation 兼容。

### parity replay 尚非 live-equivalent

- 状态：`migrating`
- 当前：复用 closed-bar、RiskPolicy 与保护纯原语，并绑定 config/data/code/factor artifact hash，但缺 broker/tick/safety/account/cost/projection-ack 的完整 PIT 事实。
- 权限：固定 `diagnostic_only`、治理数量为零；runner 永不自授权。
- 退出：只有独立 certification 重验完整 live lifecycle 后才能讨论 live-parity evidence。

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
