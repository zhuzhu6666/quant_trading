# 全项目分期修复发布状态

> Status: production rollout active; governance dual-record and Safety shadow healthy
> Snapshot: 2026-07-19 15:31 CST
> Scope: Phase 0-5 compatibility implementation, migrations, verification, and remaining live evidence gates

## 1. 当前结论

Phase 0-5 的兼容代码、additive schema、CI/test gates 和事实源文档已经实现并通过本地与隔离 PostgreSQL 验证。生产 PostgreSQL 已在线从 schema v7 升到 v10，`experiments.db` 也在哈希一致的备份后完成显式 additive repair。

目标事实已确认为 `demo_autonomous`：demo 仅表示模拟资金，不表示需要日常人工批准。历史无 mutation 绑定的 nursery overlay 已先备份，再用精确旧 hash 的 CAS 清空，由 `settings.yaml` 的 `demo_autonomous` 重新成为配置事实。清空后 learning worker 已自主提交 11 个 disabled SHADOW 因子 lifecycle 投影；它们都是 `risk_tightening`、`committed/current` 且 config/domain hash 完整，没有恢复旧因子权重或覆盖 autonomy mode。

生产 backend 与 learning worker 已完成受控重启并健康运行。当前 `governance_mutation_coordinator_v2_mode=dual_record`，Safety v2 已推进到 `shadow`，Generation、Execution outcome 与 PG job queue 仍保持 false；新进程启动恢复不再执行无权威 legacy supervisor restore。独立只读 cTrader 对账确认 demo 环境、fresh 空仓、fresh account、unknown execution=0，市场关闭期间 live loop 持续运行且系统健康恢复为 1.00。

历史 overlay/governance 与 release reconstruction cause 已在验证完成后按 cause 精确释放；Safety shadow 取得连续三轮 authoritative freshness 后，watchdog 又自主释放了自己的 cause，当前 latch 为 cleared。一次 cTrader account timeout 被正确处理为 safety 继续、alpha 阻断，后续连接自行恢复且未要求人工复位。

为消除无仓 shadow 的周期性 freshness 抖动，串行 tick 现在只在刚取得 fresh、
immutable、明确空仓且不超过 15 秒的 broker position reconcile 时，为紧随其后的
account reconcile 复用 unrealized PnL=0 事实；有仓、stale、failed、cache/event 或
兼容投影仍调用 broker PnL 并 fail-closed。部署后连续多个空仓周期未再出现
`account_info failed` / `fresh account unavailable`。

同时修复了 commit 后 projection publish 前的瞬态治理窗口：失败的 overlay hash 不再
被标记为已完成，后续轮询在 mutation 成为 committed/current 且 hash 绑定完整后，只
自动释放 `governance_authority/runtime_config_overlay_refresh` cause。生产 mutation
`74f1529d-0be8-59d3-8567-72c066b0a9ea` 已用该路径自行恢复；当前 latch cleared、
cause_count=0，无人工 clear。

无人值守运行层已核实：`quant-backend.service` 与
`quant-learning-worker.service` 均为 enabled、`Restart=always`、
`RestartSec=10`，当前 active/running。backend readiness 中 worker boot=ready、
recovery=complete、observation/research/mutation capability 均 available、mutation
circuit closed、backend/worker config 与 overlay hash 一致，
`ready_for_autonomous_mutation=true`。休市期间 `ready_for_live_execution/alpha=false`
只由 `market_session_blocks_open` 产生，不会误报为 broker 或 safety 故障。

proposal registry 的历史 source-ref 索引存在升降序同名漂移；本轮新增
`0010_proposal_registry_source_ref_contract.sql`，用显式 `DESC` 的 v2 索引名恢复
运行时契约。旧索引保持不变，迁移后 agent feedback 与 proposal bus 可自主恢复。

## 2. 已完成的工程门禁

### Python/backend

- 默认全量：`2257 passed, 10 deselected`；PostgreSQL integration 由独立门禁执行。
- PostgreSQL integration：`10 passed`，使用 PostgreSQL 临时 schema/事务回滚，不以 SQLite 替代。
- P0 执行/紧急/对账/stop-open/default-off safety 故障矩阵：`296 passed`。
- 从最小历史 baseline 到 v10 成功；同一迁移第二次执行 `applied_count=0`。
- `compileall`、`git diff --check`、OpenAPI snapshot、dependency lock check、`pip check` 通过。
- ASGI TestClient 与 async ASGI smoke 在允许线程调度的隔离环境中均小于 5 秒。
- 分期故障矩阵的逐项测试映射与本轮结果见
  `docs/phased-repair-acceptance-matrix.md`；当前显式非 PG 矩阵
  `269 + 81 passed`，PG job queue 隔离 schema `6 passed`。

### Web / 小程序

- Web smoke、architecture、fact/auth、fact behavior tests 通过。
- Web TypeScript typecheck 通过。
- Web production build 通过。
- 小程序 live reducer test 通过。

FactBoundary/UI 实现遵循：缺失 `_fact` 按 unknown，stale 保留最后值和时间，unknown/stale/error 不允许 start/unlock，但 stop/emergency 始终保留。

## 3. 已完成的生产 additive migration

- PostgreSQL `state_v1`：v7 -> v10。
  - v8：runtime schema contract completion。
  - v9：runtime overlay authority manifest/index。
  - v10：proposal source-ref 显式 `DESC` 的 additive v2 索引契约。
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

Phase 5 façade 收敛继续完成：`live_service.py` 当前为 10,447 行，已将
supervisor re-entry、risk-reduction safeguards、position path metrics、entry
protection latch、startup safety/bar warmup、factor initialization/warmup 和
generation-bound serial tick runner 迁入独立模块。v2 与 legacy safety-first
tick orchestration、safety candidate reduction dispatcher，以及 start/drain/stop
generation ownership 也已从 façade 抽离；dispatcher 模块没有任何 entry order
surface。`_run_loop_body` 仅拥有 generation 日志资源并通过 `try/finally` 关闭；
active generation body 已无内嵌 tick loop，stale generation 不能执行 factor hot
reload。账户刷新源码门禁现检查权威 tick runtime，并继续证明 kickoff 先于本地
K 线 warmup。recovery position store、recovered-close replay/retirement、emergency
execution fallback、post-close fail-closed cycle 及 attribution/audit/learning/cleanup
以及 open submission/draining/post-fill fail-closed、broker-confirmed SL/TP attach/
fresh projection ack、filled/amended context/recovery/audit 也已迁入独立领域模块。

Factor lifecycle 的 builtin 活跃路径也已收敛：native callable 以代码 artifact
SHA-256 建立 durable identity，SHADOW enrollment、prepare、loaded ack/fresh health、
ACTIVE+显式正权重分阶段通过 `FactorLifecycleService`；弱 builtin 进入 typed
QUARANTINED 终态并将 admission/权重收紧，但不删除代码 Registry。dual-record/enforce
不再用 generic overlay 复活终态，零权重 context 也不再冒充 ACTIVE。

## 5. 尚未满足的 live rollout 门槛

以下内容不能由单测替代，当前保持未完成：

1. `live_safety_plane_v2_mode=shadow` 至少观察一个完整持仓生命周期；无持仓时完成 24 小时 shadow 与故障注入。
2. generation/execution/governance/job flags 逐项灰度；当前 governance 已在 `dual_record`，不得一次全开。`governance_enforce` 的只读账本门禁当前已证明 31 个 committed/current mutation 的 config/domain hash 完整、无 in-flight mutation；未来 risk-expanding mutation 还必须通过 finalized V16 三重绑定检查。
3. 真实 demo 环境持续验证 safety heartbeat <=15 秒、account/position reconcile age <=15 秒、unknown intent=0、无 duplicate mutation。
4. Job worker 开启前的只读 YAML/schema/handler/runnable-kind/active-lease 门禁已接入 `pg_job_queue_enable`；开启后的 `pg_job_queue_verify` 已要求 service active、durable capability <=30 秒、worker identity、八类 handler 与 final loaded flags 完整。真实环境仍需验证 global/per-kind lease、SIGTERM drain 与 kill-9 lease recovery。
5. 客户端迁移窗口结束后才能删除 URL JWT、legacy access token/hash 与其余兼容路径。
6. 一个稳定发布周期后才能删除旧 safety 尾部、旧 globals、V16 consume、direct overlay/registry mutation 和 recursive frontend compatibility。

15:31 CST 的连续安全后缀已从 15:21 左右自主起算：17 条 full-cycle observation、
持续约 580 秒，latch cleared、unknown execution=0；release preflight 的唯一 blocker
为 `safety_shadow_gate_incomplete`。历史 unsafe/reset 记录保留用于审计，不会被删除或
改写；门禁只以最后一个连续安全后缀判定。

16:04 CST 已运行 code-bound Safety fault matrix：12 类场景展开为 28 个用例，
全部通过并以 `live_safety_fault_matrix.v1` append-only/fsync attestation 记录；当前
binding hash 为 `133e586e90fa50b244c3fb6285cb0c49073cf0ab46e279761b3aa8a93e688d55`。
release preflight 已验证该记录，当前 Safety target 仍只剩 24 小时/完整 lifecycle
观察时长 blocker。后续 Safety source 或对应测试变化会自动使 binding stale 并要求重跑。

17:50 CST 将 session restore 状态选择迁入独立纯函数后，旧 Safety binding 按设计
变为 stale；同一 12 类、28 个用例已重新运行并全部通过，最新 binding hash 为
`c97665262e54b3f3fa32cd643012789e81fe8afd00aa18afe6e4e59963d74b9d`。旧 record
继续保留为历史审计，release gate 只接受 append-only ledger 的最新记录。

18:11 CST 将 legacy protection cycle 从 `live_service` 迁入独立领域模块后，Safety
binding 再次按设计失效；12 类、28 个固定用例重新运行全部通过，最新 binding hash
为 `fb5f1dabfa4f68b6e02ed7b9808245899c4a600e3176bfbf74f6d29f26961927`。
全量 pytest 同时证明 Learning Worker capability 仍由 systemd PID `3069652` 发布，
未再出现测试进程覆盖生产 runtime fact。

18:27 CST 将 `recovery_position_state` CRUD 迁入独立 store 后，Safety binding 再次
按设计失效并重跑；12 类、28 个固定用例全部通过，最新 binding hash 为
`099b6f74d0b445153c54c4406094b32b0735cdcc09daf79f5ecbbb81c0b1c098`。
该迁移不改变 recovery schema、broker mutation、静态 flag 或当前 shadow authority。

18:42 CST 将 emergency execution-intent contract/fallback 决策迁入
`live_execution_recovery` 后，全量 pytest 为 `2319 passed, 9 skipped`；Safety
12 类/28 用例与 Execution Outcome 7 类/11 用例均以当前源码重跑通过。
最新 binding hash 分别为
`a556e39a0c43f6c85c9d8256cfbc0febb132503fbdf9524fb78b1bdef3348f49` 和
`f7fc296090503c760054fe1976ac618226114d70add4d57125fb52352f1c8f7f`。
该迁移不依赖 PostgreSQL，不修改当前静态 flag 或 broker mutation authority。

18:57 CST 将 recovered-close replay 与 broker-missing retirement 迁入
`live_recovery_close` 后，全量 pytest 为 `2325 passed, 9 skipped`。Safety
12 类/28 用例重跑通过，最新 binding hash 为
`5883ec6c5278c02d2ecda2cceaa5c4d2b7e8711ebff898ce48811ac430e317cc`；
Execution Outcome 7 类/11 用例仍以当前 binding
`f7fc296090503c760054fe1976ac618226114d70add4d57125fb52352f1c8f7f` 通过。
该迁移不触发 broker RPC，不重启服务，不改变 shadow authority。

19:14 CST 将 close 后 attribution/recovery/session fail-closed 编排迁入
`live_closed_position_cycle` 后，全量 pytest 为 `2329 passed, 9 skipped`；
Safety 12 类/28 用例以当前源码重跑通过，最新 binding hash 为
`a8de9e6f110e4e2aa205fcc7749bda8e3c00bbf3c6366ef3a37ae9cca6269016`。
Execution Outcome 7 类/11 用例仍以
`f7fc296090503c760054fe1976ac618226114d70add4d57125fb52352f1c8f7f` 通过。
新模块没有 broker order/close surface，本批未部署、未重启、未切换 flag。

19:29 CST 将 post-close attribution、decision audit、ledger repair/write、
review/experience/suggestion 和 local cleanup 迁入
`live_closed_position_processing` 后，全量 pytest 为 `2334 passed, 9 skipped`。
Safety 12 类/28 用例以当前源码重跑通过，最新 binding hash 为
`7eeef99cb2b17db55cc467b3a4af15e0c94621ffe04de61d0c85af3b63f09532`；
Execution Outcome 7 类/11 用例继续通过。新 processing 模块无 broker
mutation surface，本批同样未部署、未重启、未切换 flag。

后续阶段不再仅凭 CLI 新进程解析配置判断 predecessor 已运行。backend readiness
将持久化实际 process-loaded static flags、fingerprint、PID 与启动时间；从
`generation_enable` 起若配置已改但 backend 尚未重启，preflight 会以
`backend_process_static_flags_unconfirmed` 阻断。当前 Safety 首阶段不要求为该新增
投影重启，连续 shadow ledger 仍是当前 backend 已加载 shadow 的直接证据。

Learning worker capability heartbeat 同步新增同构 process-loaded flags 投影；
`governance_enforce`、`pg_job_queue_enable` 与后置 `pg_job_queue_verify` 必须同时证明 backend/learning worker
都已加载 predecessor flags。只重启 backend 不再足以推进治理阶段。

Execution outcome 切换现已增加独立代码绑定故障矩阵：8 类场景、15 个固定用例于
2026-07-19 全部通过，并以 `execution_outcome_fault_matrix.v1` append-only/fsync
attestation 记录。`execution_outcome_enable` 预检现在强制读取该证明并重算
execution source/test binding；记录缺失、最近失败、内容篡改或相关代码变化都会
fail-closed。当前 binding hash 为
`a883c05fb30925cd7141458347cade991b6cdc316e058ec6b2693671adc2b18d`；这不改变
当前 flag，也不越过 Safety/Generation 的阶段顺序。

19:46 CST 将最终 admission、draining 拒绝、broker RPC 线性化和 confirmed-open
post-fill fail-closed 编排迁入 `live_open_submission`；`live_service` 只保留依赖
wiring。全量 pytest 在迁移后为 `2339 passed, 9 skipped`，Safety 12 类/28 用例
以 binding `793c70883df6e8701b50c3fa3786f57bb5addbc9f0b7b198f564f55a02855400`
通过。审计同时发现 Execution Outcome 旧 binding 未覆盖新 authority，现已将新模块、
façade wiring 和 confirmed-open 后 post-fill/reconcile 双故障纳入证明；扩展后的 8 类/
13 用例全部通过，最新 binding 为
`ca13333190e85fea53c0e3e9c998be0ecfd010f8f4210b7fe0fc509b447c7142`。
该批不重启服务、不切换 flag、不改变当前 Safety shadow authority。

20:01 CST 将 broker-confirmed open 的 SL/TP amend、fresh projection verification、
matching latch release 与 unverified failure dispatch 迁入 `live_open_protection`；
`live_service._attach_open_trade_protection` 仅构造 immutable request 并注入 callbacks。
Execution Outcome 的 amend-projection 场景同步增加 façade 级固定用例，当前矩阵为
8 类/14 用例，且 binding 显式覆盖新 authority。实现过程中定向回归之外又逐字段
核对真实 success callback，发现并修复测试替身未暴露的 `bridge` wiring 缺失；该错误
若保留只会触发 fail-closed，不会假释放风险。Safety 12 类/28 用例与 Execution
Outcome 8 类/14 用例随后全部通过，最新 binding 分别为
`27c96cb0b4658b7934d9ab8d9d2886fce120a8733e66a18b50758aacacd0d472`、
`202d438d7422655b7e64394fa5ac67df59eadf4aeaf7c99a297954bc39f24871`。
全量 pytest 为 `2344 passed, 9 skipped`。本批仍不重启服务、不切换 flag。

20:19 CST 将 filled-open attribution/ledger/recovery、amended-success
local-state/execution-quality/learning/recovery/decision-log，以及 amend-failure
latch/context/status/audit 三段状态机迁入 `live_open_processing`。直接测试证明 ledger
辅助失败不阻断 recovery 尝试、amend failure 必须先 latch，109 项 lifecycle/open
回归通过。Execution Outcome 新增 latch-before-recovery 固定 nodeid 后为 8 类/15
用例；Safety 12 类/28 用例与 Execution Outcome 均以当前源码通过，最新 binding
分别为 `dcdfee66a2f0abb8c860c7ce500322f8412ad32e2b63d501d4253a63968ac6bc`、
`a883c05fb30925cd7141458347cade991b6cdc316e058ec6b2693671adc2b18d`。
全量 pytest 为 `2350 passed, 9 skipped`。本批未部署、未重启、未切换 flag。

20:24:59–20:26:40 CST 生产 cTrader 出现一轮连续 10 秒 account/positions RPC
timeout。live loop 每轮仍完成 Safety、以 fresh account/positions unavailable 阻断
alpha，并将 `positions_reconciliation_failed`/account freshness invalid 写入 shadow
ledger；未把缓存或空列表当作空仓。20:26:48 起 fresh reconcile 自主恢复，unknown
execution 仍为 0、latch cleared、服务未重启。该故障按 gate 规则使 24 小时连续窗口
从恢复后的首个安全 observation 重新计时，历史 unsafe 记录继续保留。

## 6. 下一次发布的固定顺序

1. 持续记录 Safety shadow comparison、broker positions、SL/TP、session risk、circuit、unknown intent 与 backend/worker config hash。
2. 保持 governance dual-record，不从历史 overlay 恢复任何 expanding control。
3. 完成 24 小时无仓观察或一个完整持仓生命周期，并执行 shadow 故障注入矩阵。
4. 运行 `scripts/phased_repair_release_gate.py --target safety_enforce`；只有所有权威检查为 green 且进程以 0 退出时才评估 Safety enforce。
5. 其后只允许依次运行 `generation_enable`、`execution_outcome_enable`、`governance_enforce`、`pg_job_queue_enable` 对应预检并逐项发布；queue flag 切换并启动 unit 后必须再运行不改变开关的 `pg_job_queue_verify`。每一步必须在前一阶段受控重启、运行事实恢复且新 target 预检为 green 后才可推进。

任一 duplicate broker mutation、双 generation、safety heartbeat 丢失、session unavailable 自动归零、emergency 假成功或 committed mutation 缺 hash，都必须立即停止阶段切换并保持 `no_new_risk`。

Safety shadow 的进程内 last-comparison 不再单独作为观察证据。部署后每个 full cycle 追加 `data/safety/safety_shadow_observations.jsonl`，并以 `scripts/safety_shadow_gate.py` 只读计算 24 小时 continuity 或完整 position lifecycle；ledger 缺失、间隔超限、reconcile 非 fresh、unknown execution、forced shadow、候选 mismatch/duplicate/conflict 任一出现都保持 `observing`，不能切 enforce。

`loop-status` 已加字段投影同一 `safety_shadow_gate` 结果，使 operator/Web 无需登录服务器执行 CLI 也能持续看到 observation count、连续时长和 blocker。该投影只读且按 ledger stat 缓存，不拥有发布开关提交权，gate 通过也不会自行把 shadow 切成 enforce。

Backend readiness 持久化快照不再依赖 operator/API 访问触发续期。既有
InProcessScheduler 每两分钟调用 single-flight refresh owner，max-age 为 90 秒；
构建线程仍由 BackendRuntimeLifecycle 在 shutdown 时停止接单并 join，不新增孤儿
event-loop/native worker。该周期刷新只维护事实投影，不授权任何控制或发布动作。

2026-07-19 15:19 CST 最后一次代码部署前再次完成独立只读 cTrader 预检：有效环境为 demo、account/positions 均为 fresh、broker 确认空仓、unknown execution 为 0。首轮 startup-unknown 被 ledger 保留为安全窗口重置点；系统按设计继续 safety、阻断 alpha，并自主恢复。无仓 account reconcile 复用同 tick 的 fresh empty-position 事实后，连续周期没有再触发 PnL RPC timeout；`loop-status`/release gate 随后报告 freshness ok、safety heartbeat current、comparison 独立且零差异，唯一 gate blocker 是 `duration_or_lifecycle_incomplete`。loop phase 的 degraded 只来自 `market_session_blocks_open`，不是 safety 故障。连续窗口遇到 reconcile/unknown/freshness/comparison/duplicate/conflict/forced-shadow 异常或超过 75 秒的观测间隔会从异常后重新计时，不会删除历史故障，也不会让一次历史启动故障永久污染后续 24 小时合格窗口。
