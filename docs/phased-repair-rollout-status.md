# 全项目分期修复发布状态

> Status: code gates passed; production rollout paused before restart
> Snapshot: 2026-07-19 07:26 CST
> Scope: Phase 0-5 compatibility implementation, migrations, verification, and remaining live evidence gates

## 1. 当前结论

Phase 0-5 的兼容代码、additive schema、CI/test gates 和事实源文档已经实现并通过本地与隔离 PostgreSQL 验证。生产 PostgreSQL 已在线从 schema v7 升到 v9，`experiments.db` 也在哈希一致的备份后完成显式 additive repair。

当前生产 backend 和 learning worker 仍运行迁移前启动的旧进程。没有重启、没有切 feature flag、没有修改 broker 订单、没有改写历史 RuntimeConfig overlay。这样可以保持现有 demo loop 连续运行，但也意味着新 Safety/Generation/Governance/Fact/Auth/Job 代码尚未接管生产进程。

禁止当前直接重启。生产 overlay 的 `mutation_id` 为空，且没有 legacy authority manifest。中央 before/after 分类证明其中混合了：

- `autonomy_mode: demo_autonomous -> demo_nursery`：risk tightening；
- 大量 factor weights、factor lifecycle/config 与 supervisor template：risk expanding；
- 若干与 base 相同的 no-change 字段。

因此不能把整行伪标为 `legacy_quarantined`。新代码若现在启动，会按设计持久化 `governance_authority` no-new-risk cause，并拒绝新增风险。

## 2. 已完成的工程门禁

### Python/backend

- 非 PostgreSQL 全量：`2163 passed, 10 deselected`。
- PostgreSQL integration：`10 passed, 2163 deselected`，使用临时 PG16 集群，不连接生产测试数据。
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
- 当前旧 API 尚无 `_fact`，证明生产进程仍未加载新代码。

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

## 5. 尚未满足的 live rollout 门槛

以下内容不能由单测替代，当前保持未完成：

1. 处理历史 overlay：所有 expanding/no-change 字段必须通过 typed committed mutation 重建，或由 operator 明确清理；只允许 tightening key 做 legacy quarantine。
2. 确认目标 autonomy mode。`settings.yaml` 是 `demo_autonomous`，当前 committed runtime snapshot/overlay 是 `demo_nursery`。
3. 部署新代码后受控重启，并验证前后 position IDs、SL/TP、session PnL、circuit 完全一致。
4. `live_safety_plane_v2_mode=shadow` 至少观察一个完整持仓生命周期；无持仓时完成 24 小时 shadow 与故障注入。
5. generation/execution/governance/job flags 逐项灰度；不得一次全开。
6. 实盘/真实 demo 环境验证 safety heartbeat <=15 秒、account/position reconcile age <=15 秒、unknown intent=0、无 duplicate mutation。
7. Job worker 开启后验证 global/per-kind lease、SIGTERM drain 与 kill-9 lease recovery。
8. 客户端迁移窗口结束后才能删除 URL JWT、legacy access token/hash 与其余兼容路径。
9. 一个稳定发布周期后才能删除旧 safety 尾部、旧 globals、V16 consume、direct overlay/registry mutation 和 recursive frontend compatibility。

## 6. 下一次发布的固定顺序

1. 冻结 overlay 写入，记录精确 overlay/config/domain hash。
2. 选择 `demo_autonomous` 或 `demo_nursery` 作为目标事实。
3. 对 expanding controls 生成 typed plan，并满足 V16/evidence/factor lifecycle/projection health；不合格项生成 rollback candidate。
4. Coordinator committed/current 后，确认 overlay mutation/config/domain hash 完整绑定。
5. 记录 broker positions、SL/TP、session risk、circuit、unknown intent。
6. 部署默认 off/shadow 的代码并受控重启。
7. 验证 startup barrier、fresh account/positions、execution recovery、session restore、safety heartbeat。
8. 按单一 flag 切 shadow；完成观察门槛后才切 enforce。

任一 duplicate broker mutation、双 generation、safety heartbeat 丢失、session unavailable 自动归零、emergency 假成功或 committed mutation 缺 hash，都必须立即停止阶段切换并保持 `no_new_risk`。
