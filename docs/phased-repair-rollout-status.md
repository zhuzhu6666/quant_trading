# 全项目分期修复发布状态

> Status: production rollout active; governance dual-record and Safety shadow healthy
> Snapshot: 2026-07-19 14:08 CST
> Scope: Phase 0-5 compatibility implementation, migrations, verification, and remaining live evidence gates

## 1. 当前结论

Phase 0-5 的兼容代码、additive schema、CI/test gates 和事实源文档已经实现并通过本地与隔离 PostgreSQL 验证。生产 PostgreSQL 已在线从 schema v7 升到 v9，`experiments.db` 也在哈希一致的备份后完成显式 additive repair。

目标事实已确认为 `demo_autonomous`：demo 仅表示模拟资金，不表示需要日常人工批准。历史无 mutation 绑定的 nursery overlay 已先备份，再用精确旧 hash 的 CAS 清空，由 `settings.yaml` 的 `demo_autonomous` 重新成为配置事实。清空后 learning worker 已自主提交 11 个 disabled SHADOW 因子 lifecycle 投影；它们都是 `risk_tightening`、`committed/current` 且 config/domain hash 完整，没有恢复旧因子权重或覆盖 autonomy mode。

生产 backend 与 learning worker 已完成受控重启并健康运行。当前 `governance_mutation_coordinator_v2_mode=dual_record`，Safety v2 已推进到 `shadow`，Generation、Execution outcome 与 PG job queue 仍保持 false；新进程启动恢复不再执行无权威 legacy supervisor restore。独立只读 cTrader 对账确认 demo 环境、fresh 空仓、fresh account、unknown execution=0，市场关闭期间 live loop 持续运行且系统健康恢复为 1.00。

历史 overlay/governance 与 release reconstruction cause 已在验证完成后按 cause 精确释放；Safety shadow 取得连续三轮 authoritative freshness 后，watchdog 又自主释放了自己的 cause，当前 latch 为 cleared。一次 cTrader account timeout 被正确处理为 safety 继续、alpha 阻断，后续连接自行恢复且未要求人工复位。

## 2. 已完成的工程门禁

### Python/backend

- 默认全量：`2226 passed, 10 deselected`；PostgreSQL integration 由独立门禁执行。
- PostgreSQL integration：`10 passed, 2226 deselected`，使用 PostgreSQL 临时 schema/事务回滚，不以 SQLite 替代。
- P0 执行/紧急/对账/stop-open/default-off safety 故障矩阵：`296 passed`。
- 从最小历史 baseline 到 v9 成功；同一迁移第二次执行 `applied_count=0`。
- `compileall`、`git diff --check`、OpenAPI snapshot、dependency lock check、`pip check` 通过。
- ASGI TestClient 与 async ASGI smoke 在允许线程调度的隔离环境中均小于 5 秒。

### Web / 小程序

- Web smoke、architecture、fact/auth、fact behavior tests 通过。
- Web TypeScript typecheck 通过。
- Web production build 通过。
- 小程序 live reducer test 通过。

FactBoundary/UI 实现遵循：缺失 `_fact` 按 unknown，stale 保留最后值和时间，unknown/stale/error 不允许 start/unlock，但 stop/emergency 始终保留。

## 3. 已完成的生产 additive migration

- PostgreSQL `state_v1`：v7 -> v9。
  - v8：runtime schema contract completion。
  - v9：runtime overlay authority manifest/index。
  - apply 后 `--check` 为 `ok=true`；重复 apply 为零变更。
- `data/experiments.db`：迁移前备份到
  `data/experiments.before-schema-v1-20260718T224646Z.db`，备份 SHA-256 为
  `893ef8c28359197bf7984284d31caf7af689ce5e81fbcd3fe602150e63f6c0d5`。
  apply/check 后 schema 与 SQLite integrity check 均通过。

迁移后：

- `quant-backend.service` active；
- `quant-learning-worker.service` active；
- `/api/health` 为 `status=ok, db=connected, ctrader=connected`；
- `broker_execution_intent` 当前无记录，因而 unresolved intent 为 0；
- live loop 仍运行，broker positions 当前为空；
- `/api/health` 已返回 `system.health.v2` 的 known `_fact`，证明生产进程已加载兼容代码。

## 4. 已实现的主要安全/治理边界

- broker reconcile/result contract、unknown execution intent、20 秒 emergency post-reconcile、append-only safety outbox；
- final-open PG/session/spot/reconcile gate，close/reduce/tighten/emergency 不依赖 PG/audit 成功；
- default-off 与 v2 都先执行 broker snapshot/safety，5 秒 watchdog、15 秒 fail-closed；
- generation ownership、draining start rejection、已准入 open RPC 与 post-fill 线性化；
- deals-first session restore，partial/final close 权威 deal 解析；
- `GovernanceMutationCoordinator`、V16 claim/finalize、committed-only live policy、stable factor identity/lifecycle/eligibility；
- runtime overlay committed/hash authority 与 legacy per-key quarantine；
- `_fact` envelope、Web FactBoundary、小程序 allSettled/per-source reducer；
- Auth v2 access/refresh/logout/ws ticket/step-up 兼容迁移；
- PostgreSQL persistent job queue、独立 worker、lease/claim/heartbeat/cancel recovery；
- legacy backtest diagnostic-only 与 parity replay evidence boundary；
- runtime backend/worker schema-writer retirement。

首轮 dual-record publish 发现 PostgreSQL schema guard 将换行 `ON CONFLICT ... DO UPDATE` 的 `DO` 误判为 DDL，导致 10 个 committed factor projection 暂时 degraded。classifier 已改为只识别 SQL 绝对起点或分号后的语句边界；受控重启后 backend recovery 报告 `attempted=10/current=10/degraded=0`，后续自治 mutation 也直接进入 current。

Phase 5 façade 收敛继续完成：`live_service.py` 当前为 11,452 行，已将
supervisor re-entry、risk-reduction safeguards、position path metrics、entry
protection latch、startup safety/bar warmup、factor initialization/warmup 和
generation-bound serial tick runner 迁入独立模块。v2 与 legacy safety-first
tick orchestration、safety candidate reduction dispatcher，以及 start/drain/stop
generation ownership 也已从 façade 抽离；dispatcher 模块没有任何 entry order
surface。`_run_loop_body` 仅拥有 generation 日志资源并通过 `try/finally` 关闭；
active generation body 已无内嵌 tick loop，stale generation 不能执行 factor hot
reload。账户刷新源码门禁现检查权威 tick runtime，并继续证明 kickoff 先于本地
K 线 warmup。

## 5. 尚未满足的 live rollout 门槛

以下内容不能由单测替代，当前保持未完成：

1. `live_safety_plane_v2_mode=shadow` 至少观察一个完整持仓生命周期；无持仓时完成 24 小时 shadow 与故障注入。
2. generation/execution/governance/job flags 逐项灰度；当前 governance 已在 `dual_record`，不得一次全开。
3. 真实 demo 环境持续验证 safety heartbeat <=15 秒、account/position reconcile age <=15 秒、unknown intent=0、无 duplicate mutation。
4. Job worker 开启后验证 global/per-kind lease、SIGTERM drain 与 kill-9 lease recovery。
5. 客户端迁移窗口结束后才能删除 URL JWT、legacy access token/hash 与其余兼容路径。
6. 一个稳定发布周期后才能删除旧 safety 尾部、旧 globals、V16 consume、direct overlay/registry mutation 和 recursive frontend compatibility。

## 6. 下一次发布的固定顺序

1. 持续记录 Safety shadow comparison、broker positions、SL/TP、session risk、circuit、unknown intent 与 backend/worker config hash。
2. 保持 governance dual-record，不从历史 overlay 恢复任何 expanding control。
3. 完成 24 小时无仓观察或一个完整持仓生命周期，并执行 shadow 故障注入矩阵。
4. shadow 零动作差异后再评估 Safety enforce；Generation、Execution outcome、PG job queue 继续逐项发布。

任一 duplicate broker mutation、双 generation、safety heartbeat 丢失、session unavailable 自动归零、emergency 假成功或 committed mutation 缺 hash，都必须立即停止阶段切换并保持 `no_new_risk`。

Safety shadow 的进程内 last-comparison 不再单独作为观察证据。部署后每个 full cycle 追加 `data/safety/safety_shadow_observations.jsonl`，并以 `scripts/safety_shadow_gate.py` 只读计算 24 小时 continuity 或完整 position lifecycle；ledger 缺失、间隔超限、reconcile 非 fresh、unknown execution、forced shadow、候选 mismatch/duplicate/conflict 任一出现都保持 `observing`，不能切 enforce。

2026-07-19 14:03 CST 最终 generation 启动前再次完成独立只读 cTrader 预检：有效环境为 demo、account/positions 均为 fresh、broker 确认空仓、unknown execution 为 0。首轮 startup-unknown 与启动期 K 线补充造成的一次 account RPC timeout 都被 ledger 保留为安全窗口重置点；系统按设计继续 safety、阻断 alpha，并在补充完成后自主恢复，不要求人工复位。后台 account reconcile 最小间隔已从 10 秒收紧为 5 秒，随后连续 4 个 full cycle 覆盖 121 秒，account age 为 11.0/11.0/11.0/11.9 秒、positions age 为 0、comparison 独立且零差异。最新 verifier 的唯一 blocker 是 `duration_or_lifecycle_incomplete`。连续窗口遇到 reconcile/unknown/freshness/comparison/duplicate/conflict/forced-shadow 异常或超过 75 秒的观测间隔会从异常后重新计时，不会删除历史故障，也不会让一次历史启动故障永久污染后续 24 小时合格窗口。
