# Change Impact Checklist

> Status: active
> Last verified: 2026-07-19
> Scope: pre-change and post-change checklist for backend, factor, governance, risk, data, and frontend contract work.

本文是每次动代码前后的检查清单。目标是先扩大影响面，再把改动收口，避免只修眼前一个点却破坏其他链路。

## 1. 改动前先判断类型

先把任务归类：

| 类型 | 例子 | 风险 |
|---|---|---|
| 因子语义 | role、归一化、组合、权重 | 可能影响方向评分、AWE、readiness、前端 |
| 自治治理 | 晋升、降权、禁用、回滚、模板切换 | 可能影响配置持久化和实盘安全 |
| 风控执行 | gate、sizing、position supervisor、event sizing | 可能影响下单、改仓、平仓 |
| 数据链路 | bars/cTrader spot/external/events/state | 可能影响 PIT、freshness、回测/live 一致性 |
| 学习链路 | 样本、evidence、模型、policy suggestion | 可能影响训练准入和治理证据 |
| API/前端契约 | 新字段、旧字段兼容、展示含义 | 可能造成小程序/Web 误读 |
| 运维启动 | systemd、startup、overlay restore、readiness | 可能造成重启后状态漂移 |

## 2. 必扫影响面

每次改动至少扫这些问题：

1. 这个改动改变了哪个事实源？
2. 是否有旧路径仍会写入同一状态？
3. 是否改变了 live、backtest、shadow、learning 任一链路的行为？
4. 是否改变了前端/API 字段含义？
5. 是否改变了 readiness 或健康检查口径？
6. 是否需要配置持久化、快照或回滚点？
7. 是否有历史文档或测试仍在表达旧理解？
8. 是否需要把旧债登记为 fixed/migrating/deprecated？

## 3. 因子改动检查

改因子系统时必须检查：

| 检查项 | 目标 |
|---|---|
| `StreamingFactorEngine` | 因子是否被计算，生命周期是否正确 |
| `SignalNormalizer` | 高频/低频采样是否合理 |
| `PortfolioCompositor` | 是否只有 alpha 进入方向评分 |
| `DecisionPolicy` | 权重写入是否唯一、是否过滤 lifecycle/role |
| `AdaptiveWeightEngine` | 是否只调整 alpha |
| `Factor Catalog` | role、enabled、used_in_score、lifecycle 是否一致 |
| 生命周期事实源 | generated factor 是否只由 `FactorLifecycleService` 在 Coordinator 事务内写 `factor_lifecycle_state`；Registry/RuntimeConfig 是否只作 committed 后投影 |
| ACTIVE admission | 是否同时验证 canonical DSL SHA-256、稳定 artifact hash、fresh loaded projection ack、显式 enabled、显式正权重和 fresh HEALTHY；时间戳缺失是否按 stale/unknown 阻断 |
| 运行投影恢复 | Registry publish 失败是否写 `factor_runtime_projection=degraded` 并保留 committed mutation 供 replay，是否避免重做 lifecycle mutation |
| 权重显式性 | discovered/generated factor 缺权重时是否保持 0、`explicit_weight=false`、`used_in_score=false`，是否不存在隐式默认值 |
| `readiness` | 是否误报 context/gate/sizing 缺权重 |
| 启动恢复预算 | 是否只恢复配置命中和 discovered budget 内工作集；冷因子证据是否仍完整保留 |
| `risk/concentration` | 是否只统计 alpha 共识 |
| 前端展示 | context 不显示为多空投票 |
| 测试 | live tick、alpha、AWE、readiness、frontend contract |

## 4. 自治治理改动检查

改 Orchestrator、overlay、policy suggestion、模板或回滚时必须检查：

| 检查项 | 目标 |
|---|---|
| `RiskPolicyService` | 动作是否有明确风控入口 |
| `RuntimeConfigMutationService` | 配置写入是否持久化 overlay 和 snapshot |
| `GovernanceMutationCoordinator` | 是否由 before/after 自动判定风险方向；intent、overlay/snapshot、领域事实和 V16 finalize 是否在同一 PG 事务；commit 后 publish 失败是否只降级 projection 并可重放 |
| typed control plan | ParameterTemplate、position supervisor、model/policy、shadow lifecycle、incident、autonomy freeze/unlock/revoke 与 operator pause 的执行入口是否只提交 typed plan；手工与自动入口是否复用同一 transaction writer；`off` 兼容与 `dual_record/enforce` 是否都忽略调用方 `risk_reduction` |
| 模板原子提交 | registry/active、application/effect、reservation、`policy_suggestion.applied_mutation_id` 是否与同一 committed mutation 绑定；writer 故障是否连同 overlay/snapshot 全部回滚；effect rollback 失败是否保留原控制并形成 pending 而非先改 registry |
| V16 单次凭证 | 是否只在 Coordinator 事务 finalize 时增加 apply_count，并绑定 mutation/config/domain hash；`authority_issued_at` 是否不可变，claim/release/recovery 是否拒绝通过 `updated_at` 续期；过期 claim/停滞 intent 启动恢复是否不会产生新授权；旧 consume 是否仅处于明确兼容期 |
| 治理证据资格 | executable governance 是否只使用 matured、full/verified recovered、非污染、model-ready、lineage 唯一完整的样本；partial/missing/contaminated 是否权重为 0；sample → stats → suggestion 的 eligibility version/fingerprint 是否一致且 Governor 只使用 effective sample count/weighted 指标 |
| 研究证据信任边界 | legacy indicator sweep 是否被 CLI、runner、服务、job/list/report 和所有 executable governance 入口强制标记为 `diagnostic_only`；parity replay 是否绑定 config/data/code/factor-artifact manifest，要求显式匹配四类 expected hash，并把月库部分读取、代码绑定缺失、factor identity/artifact/lifecycle/显式权重缺失、非原生 bid/ask 或任一 modeled lifecycle 输入保持 diagnostic-only；parameter-template review/deploy 是否对缺失 metadata 也无条件 fail-closed；历史/手工候选是否标 `legacy_quarantined/require_revalidation` 且只能用新 parity artifact 重验；调用方自报 verdict/`live_parity/governance_eligible/deployable_candidate` 是否无法绕过中央拒绝策略 |
| 因子稳定身份 | generated DSL factor ID 是否来自规范化 AST 的完整 SHA-256，禁止 Python `hash()`/截断摘要 |
| 因子生命周期状态机 | promote 是否只到 `PROMOTION_PREPARED`；ACTIVE 是否必须 V16；quarantine/retire 是否由 before/after 判定为 risk tightening 并免 V16；终态是否拒绝重新扩张 |
| 因子投影绑定 | loaded ack 是否绑定 factor ID、artifact、factor generation、prepared mutation、live loop generation/process/boot 且 freshness 有界；是否先在 warm buffer 真实执行 callable 且仍不进入 voting set；governance coordinator 自身投影是否不能冒充 live ack |
| `evolution_decision` | 是否记录判断和 rollback_json |
| `learning_application_log` | 是否记录应用状态 |
| `learning_application_effect` | 是否能支撑后验回滚 |
| 应用原子性 | `dual_record/enforce` 下权重 application/effect/reservation 是否与 overlay/snapshot、intent、V16 finalize 在同一 Coordinator 事务并绑定同一 `mutation_id`；故障是否全部回滚且不留下 `prepared`；`off` legacy prepared 是否仍能用 snapshot 幂等恢复 |
| `runtime_config_overlay` | 重启是否可恢复 |
| overlay authority | 非空 `mutation_id` 是否只恢复 `committed/current` 且 config/domain hash 完整绑定的 intent；空 `mutation_id` 是否要求精确 overlay hash + 全 key 的 `legacy_authority_json` operator review；悬空/缺 manifest 时是否 latch no-new-risk 并只保留只读收紧保护，绝不授权新增风险 |
| 原子性/并发 | overlay 与 snapshot 是否同事务；失败前是否禁止发布内存；producer 是否只写局部 patch |
| 跨进程刷新 | 是否由 YAML base + 完整 overlay 重建；空 overlay/删除 key 是否能传播 |
| `factor_catalog_snapshot` | 每轮治理是否留痕 |
| 生命周期单写者 | Evolution 是否只产候选；实际 promote/rollback/retire 是否只由 FactorGovernance 执行 |
| Canary 证据 | evidence/dataset hash 是否变化；stage 是否累计足够 fresh bars，是否拒绝重复窗口 |
| 效果证据质量 | 是否过滤污染/regime mismatch；并发 application 是否保持 observing 而非伪归因 |
| 效果闭环 SLO | bounded window 是否归档终态；inconclusive 重试是否要求终态后的新证据且无更新 application |
| 实验准入 | 同一 scope 是否只有一个 active effect；AWE/Factor Governance 是否共用门；微小 delta 是否被拒绝 |
| 全局实验预算 | active application/effect 是否按 application_id 去重计数；预算触顶是否仅阻止新增扩张实验且不阻止风险回滚 |
| 学习事实水位 | 无新 decision/order/position/review/supervisor 事实时是否跳过重建；失败轮次是否禁止推进 watermark |
| mutation 覆盖 | 每个已生效 AWE 权重 patch 是否存在对应 `learning_application_log/effect`，历史缺口是否单独标 legacy |
| incident/autonomy safety independence | no_new_risk/revoke 是否在 PG 前持久化本地 latch；PG、Coordinator、mutation audit 或 event ledger 失败时是否继续阻断新增风险并写 safety outbox；close/reduce/tighten/emergency 是否完全不调用治理 Coordinator |
| latch projection recovery | local latch 生效而 configured incident 仍为 normal 时，API 是否报告 effective no_new_risk；incident cause 是否先补交 no_new_risk committed projection、再以 step-up + V16 thaw；thaw 是否只释放 incident cause，且 broker unknown、heartbeat、emergency-resume、forced-shadow 等残余 cause 继续 fail-closed |
| watchdog autonomous recovery | watchdog 是否只有在 safety/account/positions freshness 与 unknown execution 连续三轮均 authoritative/current 后才释放精确 `safety_freshness/safety_watchdog` cause；中途一次 unsafe/idle/startup_unknown 是否重置计数；是否绝不释放 incident、emergency、broker unknown、governance 或 release cause |
| Safety shadow evidence | 是否只在 full cycle 追加 fsync observation；记录是否不含账户凭据/金额等无关敏感事实；遥测失败是否不改写 broker action；24 小时/完整 lifecycle gate 是否验证 continuity、fresh reconcile、unknown=0、independent exact match、duplicate/conflict/forced-shadow 为零；CLI 与 `loop-status.safety_shadow_gate` 是否复用同一 fail-closed evaluator，ledger 缺失/损坏时是否绝不报告通过 |
| unknown outcome resolution | broker unknown 是否按 intent/action/position 独立锁存；只有 fresh recovery/reconcile 得到 confirmed/rejected 且附 evidence 才能追加 resolution；通用 clear、incident thaw、进程重启或 PG failure 是否都不能删除 unresolved 证据 |
| thaw/unlock/unfreeze | before/target 是否被判为扩张并要求最近 step-up + Coordinator + V16；caller `risk_reduction`、action 命名和 startup/restore 字样是否都不能绕过；收紧是否不依赖 PG session authority；mutation 未 committed 时原 freeze/latch 是否保持 |
| operator expansion pause | `governance_expansion_paused` 是否对所有 mode 生效、自治服务不可修改；pause 是否免 V16、resume 是否要求 step-up + confirm + V16，且 rollback/retire/quarantine/downweight/tighten 仍可执行 |
| worker 能力隔离 | DB/schema/YAML/overlay/recovery 启动失败是否非零退出；三次 mutation 依赖失败是否只打开 mutation circuit 而 observation/research 继续 |
| readiness 自主刷新 | backend readiness persistent snapshot 是否由受管理 scheduler 每两分钟触发；是否复用 single-flight owner、90 秒 max-age 与 lifecycle drain/join；无人访问 API 时是否仍保持 180 秒 freshness，失败时是否保留旧值但 readiness/release preflight fail-closed |
| worker 配置一致性 | readiness 是否校验 75 秒 heartbeat、boot/config/overlay hash，分歧时 autonomous mutation 是否 fail-closed |
| live policy authority | live 风险策略、持仓监督与 Evolution 权重 bias 是否都拒绝 approved/auto-approved；supervisor 候选 shadow 是否只在 learning worker closed-position observation 路径生成并绑定 suggestion ID，旧 live `canary_shadow` 是否不能授权 readiness/auto-unfreeze；enforce 是否只接受 applied + committed mutation；legacy applied 是否仅在 off/dual 标记 quarantined 且只允许显式 tightening 子集 |
| 单一权重用例 | AWE、Factor Governance、Evolution/manual govern 是否都调用 `FactorWeightChangeService`，没有 DecisionPolicy/mutation 之间的旁路 |
| 经验先验 | 是否只来自 terminal bounded effects；生产 DecisionPolicy 调用是否传入且保持 0.85~1.15 有界 |
| 冷却/限频 | 单周期动作数量是否受限 |
| 测试污染 | pytest/test overlay 是否被生产拒绝 |

## 5. 风控执行改动检查

改 gate、sizing、仓位监督、事件窗口时必须检查：

| 检查项 | 目标 |
|---|---|
| `ExecutionGate` | 信号阈值、冷却、NFP/GVZ 等硬门槛是否保留 |
| `RiskPolicyService` | 新动作是否统一裁决 |
| `ContextPolicyService` | context 只影响阈值/仓位，不改方向 |
| `live_tick_pipeline` | gate 前后的顺序是否正确 |
| explicit reconcile | safety/startup/emergency/order recovery 是否只接受 `PositionReconcileResult` / `AccountReconcileResult` 的 fresh 全量快照；cache/event/failed 是否不会被当成空仓、零账户或零未实现 PnL |
| reconcile component truth | account/position push event 是否只更新 event projection 而不刷新 reconcile 年龄；有仓时 identity/protection/price/PnL 是否分别可追到 broker reconcile、fresh spot 和 PnL RPC；未知 price/PnL 是否阻断 open 但不阻断 timeout/entry repair/close/reduce/tighten；前端是否不把未知组件归零或染绿 |
| execution intent | market RPC 前是否 committed prepared/submitting；timeout、延迟回执、未知 protobuf、差分不唯一或 finalize 失败是否进入 unknown + durable no-new-risk、禁止重发；是否已删除同方向最大 PID / `positions_before[0]` 猜测 |
| emergency reconcile | 是否先落盘 no-new-risk latch、等待 open admission、只接受 fresh pre/post reconcile，并仅按 position ID 消失确认成功 |
| generation ownership | stop 后是否保持 generation/thread/scheduler/pipeline 所有权直到线程真实退出；draining 是否拒绝 start；总入口是否由 `try/finally` 收口 stopped/failed |
| startup barrier | broker ready、fresh account/positions、unknown intent recovery、session restore、position recovery attach、initial safety、factor warmup 是否依序完成；任一步失败是否 fail-closed |
| safety-first 顺序 | 每轮是否严格 broker snapshot → safety → session/circuit → closed bar/factor/open；bars、factor、PG、market session 和 circuit 失败是否仍保留 close/reduce/tighten |
| safety shadow 独立性 | V2 planner 是否纯只读、无 broker mutation；是否与 legacy 实际选中/覆盖动作做规范化 fingerprint 比较；`independent=false`、mismatch、planner exception 是否持续阻断新增风险；是否禁止把测试 match 冒充 24 小时/完整持仓生命周期观察完成 |
| session deals-first | runtime_kv 缺失时是否仍查询 `ctrader_deals`；是否要求 broker positions 不超过 15 秒；partial close 后仍开放的 position 是否从 completed trade 排除；`session_observed_at` 是否与 account/positions 独立；unavailable/degraded_cache 是否保留最后值、阻断开仓且不归零 |
| heartbeat freshness | safety heartbeat 缺失是否按 unknown、超过 15 秒是否自动阻断新增风险；有仓/unknown execution 是否保持 5 秒 cadence |
| 降风险可用性 | PostgreSQL、因子或审计失败时 close/reduce/tighten 是否继续，失败证据是否进入本地 append-only safety outbox |
| live façade 边界 | reconcile contract、safety/startup orchestration、emergency 状态机是否留在独立无 PG 模块；`live_service` 对应入口是否仍只是无循环/无异常编排的 callback wiring |
| `sizing trace` | 最终仓位变化是否可解释 |
| ledger | skip/open/close/amend 是否可追溯 |
| tests | live tick、risk、position lifecycle |

## 6. 数据改动检查

改数据源、PIT、外部因子或库路径时必须检查：

| 检查项 | 目标 |
|---|---|
| `FactorFrameBuilder` | live/evolution/health 是否共用入口 |
| runtime health projection | `/api/health`、readiness、learning worker 是否消费同一 cTrader/session 投影，且投影不参与交易授权 |
| `release_at` | 外部数据是否 point-in-time |
| monthly DuckDB | 当前月链接是否正确 |
| parity hash binding | replay 是否绑定 committed config、实际选中 PIT rows、逐代码文件和逐 factor artifact 的 SHA-256；是否必须显式提供并匹配 config/data/code/artifact 四类 expected hash 才能标 verified；缺失或不一致是否 fail-closed |
| replay 时间因果 | 是否只消费已闭合 bar、决策窗口严格为 `history[:i+1]`、成交只使用 next-bar 原生 bid/ask 并计入 spread/slippage/commission |
| replay 生命周期 | factor frame/selector/normalizer/compositor、RiskPolicy、position metrics、safety arbitration、supervisor/trailing/protection plan 是否分别记录“共享原语 exact”和“历史输入 verified”；selector 输出是否进入 artifact 且历史 factor projection ack/health/Registry generation 是否单独验证；broker receipt/reconcile/partial fill、tick 内路径、5 秒 cadence/AWE、account/session/runtime、真实 deal cost/swap、projection ack 或原生 bid/ask 任一缺失时是否只能输出 `diagnostic_only` |
| PostgreSQL state | 是否避免新增 SQLite state 写入 |
| schema writer | 新 state_v1 DDL 是否只进入版本化 migration；当前 backend/learning/job worker 是否只启动校验最低 schema version 9；高频 worker/model/readiness ensure 是否显式调用 catalog validation；普通 connection/cursor 是否继续把其余旧 ensure 降为 assertion 并阻断其他 schema write；canonical `experiments.db` 是否只读校验且只允许 `db_doctor --repair` 写 schema；外部 SQLite restore 是否只导入已迁移表；migration connection 是否在 advisory lock 下可重复执行且 checksum ledger 一致 |
| 静态 rollout flags | safety/generation/execution/governance/job queue 是否与当前分期发布值一致（当前 Safety 为 `shadow`、governance 为 `dual_record`，其余仍为 false）；是否每次只推进一个开关、只由发布配置+重启切换且不进入 RuntimeConfig overlay/自治 mutation；观察门未达成时 `scripts/phased_repair_release_gate.py --target safety_enforce` 是否非零并拒绝 enforce |
| 持久化任务队列 | heavy job 是否只由独立 worker claim；claim token/heartbeat/lease/retry/cancel 是否可恢复；全局与 kind 并发限额是否跨进程生效；flag 默认 off 是否保持当前 demo |
| worker 线程所有权 | JobManager 是否不创建 daemon event-loop thread；SIGTERM 是否续租排空当前 handler，硬退出是否只在 lease 过期后恢复；backend readiness refresh 是否 process-owned/单飞/非 daemon，并由 `BackendRuntimeLifecycle.stop()` 拒绝新任务且 join 当前 DuckDB/native worker |
| freshness | readiness 是否暴露数据时效 |
| fallback | 外部 enrichment 失败是否可降级 |

## 7. API/前端改动检查

改接口字段或展示语义时必须检查：

| 检查项 | 目标 |
|---|---|
| 旧字段兼容 | 小程序和旧调用不崩 |
| 新字段含义 | 前端不需要自行推断 |
| auth | 未授权返回是否符合预期 |
| Auth v2 | Argon2id/legacy flag、15 分钟 access、旋转 refresh/logout、一次性 WS ticket、5 分钟 step-up 是否闭合；step-up 是否只绑定当前 active `sid/fid`、先事务持久 `auth_time` 再发 token、refresh 是否只继承不续鲜、PG 失败是否阻断 start/unlock；logout 是否在 PG 前 fsync 持久撤销整个 session family 且重启不复活；普通 access 是否绑定 PG active session 并在 PG/撤销投影读失败时 fail-closed；stop/emergency 是否仍可仅靠本地签名 scope + durable revocation projection 执行，且撤销投影读失败不会夺走风险缩减能力 |
| `_fact` | 是否按 `docs/api-fact-contract.md` 维护 endpoint contract/source/observed_at/stale_after；Learning 是否只用显式持久化时间且缓存不续鲜；Ops read/write/mutation 是否分别验证账本时间、durable ID+commit timestamp、committed mutation；缺失、unknown、stale、error 是否永不显示绿色；stop/emergency 是否不被 freshness gate 禁用 |
| 401 并发 | 多个请求同时 401 是否只清理/跳转一次；非 401 是否保留 token 和最后事实 |
| 小程序合并 | `Promise.allSettled` 是否按来源更新；全部失败是否只推进 attempt；WS partial payload 是否不写默认零值 |
| readiness | 运维页能看到 frontend/live execution/live alpha/autonomous mutation/release 五维阻断；frontend readiness 不得授权 control/release |
| parity replay | additive replay API 是否保留 hash、因果、成本和 component blockers，且 runner/API 自身永不授予 governance/deploy 权限 |
| Factor Cards | 与 Catalog 语义一致 |
| 文档 | schema/contract 是否更新 |

## 8. 改动后验证顺序

默认顺序：

```text
最小单测
  -> 相关模块测试
  -> 关键 contract/readiness 测试
  -> 必要时全量测试
  -> 服务日志
  -> 接口健康
```

服务器运行问题优先：

```text
先看日志
  -> 再看接口
  -> 再改代码
  -> 最后重启验证
```

## 9. 文档收口

改完后检查：

1. `docs/README.md` 是否需要新增入口。
2. `docs/system-source-of-truth.md` 是否改变事实源。
3. `docs/legacy-debt-register.md` 是否有旧债状态变化。
4. 对应 contract/schema/SOP 是否需要更新。
5. planning 文档是否仍只是历史计划，不能变成隐性事实源。
6. 新增或调整 `_fact` 时，`docs/api-fact-contract.md` 的路由、contract、source、timestamp 与兼容状态是否同步。
