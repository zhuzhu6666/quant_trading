# 全项目分期修复发布状态

> Status: active current-state index
> Snapshot: 2026-08-28 (HEAD f2eb9c9; 治理 enforce 已加载, off 直连已删)
> Scope: current phase, last verified evidence, next batch, and unresolved runtime acceptance
> Source of truth: 运行状态必须在每次实施前重新读取服务、PostgreSQL、`runtime_kv`、日志和 broker

本文只保留当前状态和可复核的未完成证据，不保存逐时操作流水，也不重复架构合同。历史批次通过 Git 历史追溯；权力边界见 `docs/system-source-of-truth.md`，执行流程见 `docs/change-impact-checklist.md`。

## 1. 当前阶段

| 阶段 | 状态 | 剩余工作 |
|---|---|---|
| S0 冻结 | complete | 无 |
| S1 账本修复 | complete | 无 |
| S2 公共层+四域清扫 | complete | 无 |
| S3 代码单轨+结构修复 | complete | A1–A6 / B1–B5 全部完成 |
| S4 全量验证 | complete | 2926 passed / 12 skipped（2026-08-27 最终回归） |
| S5 清库物理 | complete | 10.8GB → 9.7MB，canonical_v2 9 表空 + runtime 6 表空结构 |
| S6 容量阀 P6 | pending | event 分区、保留窗口、归档、容量监控（evolution_story.jsonl 50605 行/140M 待轮转方案） |
| S7 启动验证+进化闭环首验 | in_progress | 双服务冷启动 ✅；**evolution_decision 8 列单轨 & learning_application 精简 schema 已收敛**；**S7.6 核心闭环已达成**（`trade_review_outcome full/1.0 46` `2026-08-21→08-28` 8-11/天, `broker 127/position 125/review 99`）；**cTrader/unknown 已通过**；持仓监督 `governed_execute` 单轨+单仓 binding 已打通（`285427255`），但 `supervisor_execution_trace 5/10` 未达 `off→Demo` 阈值，`selection insufficient_evidence` 待积累；**治理已切 `enforce`（2026-08-28 14:14 空窗 891039/891040 loaded），`off` 直连路径已于 f2eb9c9 删除** |

## 2. 最近一次运行核对

### 2026-08-28 只读复核结果（f2eb9c9 已加载，HEAD 即生产）

- 双服务 `quant-backend.service PID 891039` / `quant-learning-worker.service PID 891040` 均 `active/running`，`NRestarts=0`；`system_health overall=healthy score=1.0`，`market_session open_confirmed can_open_positions true`，`risk_metrics known cvar 1.55%`，`live.loop generation 172b7fd3... running` 接受新风险，当前持仓 `285427255` 有仓且 `unknown_execution_count=0`；`governance_mutation_coordinator_v2_mode=enforce` 已加载，`off` 直连已删。
- `canonical_v2.training_sample_row 10294`：`trade_review_outcome 67（full/1.0 46 连续 2026-08-21→08-28，近 4 天 8-11/天）`，`supervisor_execution_trace 9369（eligible matured 5/10）`，`shadow_open_decision 468`；`canonical_v2.event supervisor_trace 15 / counterfactual 33 / broker_execution 127`；`runtime.broker_execution_intent confirmed 132（近 3 天 84，unknown 0）`。
- `runtime_kv` 新鲜：`backend_readiness_snapshot.v1 05:14` `ready_for_live_execution/alpha true, autonomous_mutation false`，`runtime_factor_selection.v1 6 voters/4 groups/138 HEALTHY`，`position_supervisor_selection.v1 insufficient_evidence/0候选/fresh`，`risk_metrics_snapshot.v2 499 samples`，`evolution_cycle_watermark.v1 2026-08-28T04:25 M5`。
- `recovery_position_state 61` 最新 `285427255` 已验证 `entry_regime trend=weak|vol=low / signal_reversal / thesis_broken_confirmations 136 / selection_key` 三生产者打通，`entry_protection_plan.supervisor_binding hash cdfe2bf...` 完整；`learning_application_log 21 (observing 9)` / `brain_memory 160 / experience 62`。
- `governance_mutation_intent 1305 (committed 1142 / aborted 163 v16_claim 155)`，`policy_suggestion 947 (applied 39 / superseded 625)`，`factor_lifecycle SHADOW 1113 / PROMOTION_PREPARED 4 / ACTIVE 0`，`canary_state ACTIVE 0`。

### 2026-08-27 受控重启核对（历史）

- 本次受控重启后 `quant-backend.service` PID `901316`、`quant-learning-worker.service` PID `901526` 均为 `active/running`，两者 `NRestarts=0`；backend 启动时间为 `19:01:01`，worker 为 `19:01:10`（Asia/Shanghai）。
- `/api/health` 核对为 `status=ok`，DB 与 cTrader connected，release identity 与 backend PID 一致；health 只证明连通和进程存活，不授予开仓权限。
- learning worker 成功恢复既有 RuntimeConfig overlay；新增选择字段保持安全默认值，`position_supervisor_auto_selection_mode=off`。自动开启代码已加载，但投影没有合格候选，因此本次选择链未发生 broker mutation。
- 重启前已发布的 `runtime_kv[position_supervisor_selection.v1]` 当前仍为 `insufficient_evidence`、候选数 `0`、source watermark `0.0`；这证明投影能持久化，但不证明已有合格模板。worker 下一次 autonomous-learning 周期会按新逻辑自动重试。
- cTrader 在启动后完成认证，live loop 恢复并在 `19:03:00` 的 system health 周期回到 `healthy score=1.00`；恢复日志确认重新挂接 1 笔真实 Demo 持仓，未发现 902/903 这类三位数 cTrader 仓位，也未产生新的 broker mutation。
- 全量回归最终为 `2926 passed, 12 skipped`；针对性 binding/selection/overlay 测试和编译、diff 检查均通过。
- readiness 仍按 `no_new_risk`/事实可用性 fail-closed；任何后续判断必须继续区分 health、readiness、Safety 和 RiskPolicy。

### 2026-08-27 单仓监督治理升级（代码实现，已完成重启与投影验收）

- `position_supervisor_binding.v1` 已接入开仓成交、recovery 持久化、重启恢复和监督上下文；完整模板快照使用规范化 JSON hash。旧仓位明确为 `legacy_global_fallback`，binding 缺失/篡改/来源未知不会伪造历史信息，软策略保持 `unknown/hold`，硬风险继续收口。
- 既有 learning worker 周期新增 `runtime_kv[position_supervisor_selection.v1]` 投影；选择只接受当前 Coordinator mutation、完整干净成熟的 `causal_scope=supervisor` 证据、模板 hash 和有效 application/effect。普通 tick 不重新查询记忆。
- 已实现 `off | shadow | demo_execute | live_execute` typed 配置边界；`off` 只是证据不足时的安全启动基线。learning worker 在选择投影达到资格后自动通过既有 V16、RiskPolicy 和 Coordinator 进入有界 Demo，不需要人工改模式；`live_execute` 当前不准入。已有仓位只有在连续收盘 bar、known facts、无未决 intent/reconcile、无优先硬风险、投影新鲜、冷却期和次数限制均满足时才可单仓切换；切换写 trace 并保留旧 binding，不修改全局 RuntimeConfig。
- trace、counterfactual、training sample、experience memory、brain memory 和 effect 已补齐 binding 引用；历史样本不回填，也不参与新模板自动准入。
- 运行验收已确认双服务实际进程、overlay 重启恢复、projection 新鲜度和 `off` 模式下无 broker mutation；当前仍因证据不足保持基线，证据达到资格后将由 worker 自动切入 Demo，不需要人工开启；真实 open/close 全链路和治理扩权继续按单独退出条件观察。

### 2026-08-27 自动开启语义补充

- “全自动”指合格证据出现后由现有 learning worker 自动发起治理切换；不是让单条记忆直接改模板，也不是绕过 V16、RiskPolicy、Coordinator、Safety 或 broker reconcile。
- 当前实时投影仍为 `insufficient_evidence`、候选 `0`、模式 `off`，所以本次不会自动开启，也不会产生 broker mutation。
- 自动开启的目标只允许有界 Demo；`live_execute` 仍被代码拒绝，硬风险边界不因监督模板改变。

### 历史核对（2026-08-11）

- 2026-08-11 智能自主进化代码批次已部署：`quant-backend.service`（PID 3236037→重启后新 PID）、`quant-learning-worker.service` 均 active；`/api/health` 为 `db=connected`；learning worker 启动日志确认 `RuntimeConfig autonomous overlay restored`，overlay authority 恢复。
- 风险默认 profile 按 operator 指示放宽并已生效（`RuntimeConfig` 有效值）：单笔 Kelly 止损风险 `5.0%`、日亏损 `10%`、最大回撤 `16%`、普通及 Demo 每日开仓 `30`；`risk_cvar_threshold_pct` 保持 overlay `2.5`。settings.yaml/runtime_config.py 静态基值同步，`docs/system-source-of-truth.md` 风险 profile 描述已更新。
- 部署重绑定（2026-08-11 两次）：静态基值放宽使 effective config hash 变化，原 committed mutation 失配，learning worker 按设计 fail-closed。均通过 `RuntimeConfigMutationService` + `GovernanceMutationCoordinator`（operator:zhu，dual_record，risk_tightening/no_change 免 V16）重新提交 cvar 2.5 确认 mutation（`gmut_a8a90e...`、`gmut_ebf5cd...`），绑定新 effective hash，overlay 恢复 committed/current；审计与备份在 `logs/rebind_20260811/`。
- live loop 保持 operator 手动停止（`live.loop.desired_state.enabled=false, reason=manual`），当前无持仓（operator 已手动平仓）；readiness 因 loop 停止处于 `no_new_risk` 姿态：blockers 为 ctrader warming_up / incident_control no_new_risk / risk_metrics stale，均为 loop 未运行的预期结果，待 operator 处理完成后按 SOP 恢复 loop。
- learning worker capability 为 `boot_status=ready`、`recovery_status=complete`；overlay 权威恢复后无 quarantine 记录。
- canonical Safety/supervisor 路径已切到 `live_safety_plane_v2_mode=enforce`；generation controller 与 broker execution intent/reconcile 没有独立开关，始终使用 canonical 单轨。当前进程是否已加载该姿态，仍必须以重启后的 process flag projection、PID/start timestamp 和日志复核。
- 源配置静态 flags 为 `live_safety_plane_v2_mode=enforce`、`governance_mutation_coordinator_v2_mode=dual_record`、`pg_job_queue_v2_enabled=false`。

readiness 的 ready 只表示当前事实和能力可用，不是开关提交权，也不替代 V16、Candidate Review、RiskPolicy、Coordinator 或真实 broker 证据。

## 3. 已完成并仍有效的事实

### P0 保护现场

- incident、污染 cohort、repair ledger 和修复不变量已建立。
- close/reduce/tighten/rollback 与只读观察保持可用。

### P1 代码与历史修复

- broker deal 的价格字段与金额字段解析已按各自合同拆分，历史错误价格已更正。
- 直接关联的污染学习/反事实记录已隔离；无法权威恢复的 close quote 保留审计原值并 quarantine。
- unknown execution outcome 不进入价格归因、review、experience、counterfactual 或治理。
- 代码修复完成不等于 P1 runtime acceptance 完成；运行验收仍以真实 post-repair lifecycle 为准。

### 2026-08-12 Safety freshness / live admission repair（已受控部署，运行验收未通过）

- Canonical authority（本批之前）：现有 `live_loop_tick_runtime` 串行 owner 负责 positions/account/Safety；现有 `data_sync` scheduler 是决策 K 线唯一 durable writer；不新增线程、服务、状态表或 Safety/Readiness/Risk sizing 平行裁决。
- Code change（本批之前）：每 tick 在 Safety 前完成 positions/account fresh fact；live tick 不再执行 20 秒 history RPC 或写 K 线，stale 本地快照以 `stale_waiting_for_data_sync` 阻断 alpha/开仓；删除开仓 admission 同 tick 二次 reconcile 和旧 account/positions 并发兼容刷新器；broker 缺失 active recovery row 时仍复用既有 close-deal retirement/recovery 投影做一次有界确认，无法取得权威 close deal 继续 fail-closed。
- Targeted verification：本批 live data-sync、lifecycle、open-admission、loop-runtime、Safety/账户事实组合 `116 passed`；全量测试 `2706 passed, 9 skipped`；`git diff --check` 和 Python compile 通过。
- Runtime verification：2026-08-12 16:59:28 受控重启 `quant-backend.service`，PID `74540 -> 222088`；新进程加载当前工作区代码，cTrader 认证完成，local/public `/api/health` 在启动恢复后均为 `db=connected, ctrader=connected`。启动阶段保留 fail-closed，未放行新增风险。
- Runtime result：新进程仍出现 `safety_freshness` activate/release 抖动；17:01:05 release 后 17:01:15 再次 activate，后续继续重复，当前 latch 仍为 `active`。日志同时显示 legacy `initial_ctrader_data_pull` 在 17:01:32、17:02:50、17:03:53 依次完成 M15/M30/H1 的 `n_bars=5000` 拉取；live tick 间隔仍约 20 秒，说明串行 Safety freshness 仍被启动/下游 broker 阶段拉穿。
- Remaining：本批已删除 live decision-bar 热路径 writer、admission 二次 reconcile 和 legacy startup history pull；但当前 PID `222088` 尚未重启加载本批删除，运行态仍需确认无 `init:fast`/`init:deferred` 线程及其拉取日志。此前 `safety_freshness` 抖动的运行结论不因代码删除自动改写，Safety v2 保持 shadow，不推进 enforce；重启后再补共享 deadline/阶段耗时证据。
- Residual deletion verification：本批针对性测试 `119 passed`，Python compile、`git diff --check` 通过，生产代码和测试中已无 legacy startup pull 符号；尚未重启，因此不把代码验收当作运行验收。

### 2026-08-12 cTrader live trendbar 主路径（本批）

- Canonical authority：`CTraderBridge` 的 cTrader live trendbar 内存 feed 是 live 决策和 live 风险历史窗口的唯一热路径 bar source；`data_sync` 保留为唯一 durable monthly writer，不新增第二个 bar 写入者。
- Code change：接入 `ProtoOASubscribeLiveTrendbarReq`，由 `ProtoOASpotEvent.trendbar` 解码并缓存；live tick 和 forward VaR/CVaR 改读 bridge 内存 frame；启动历史改为 cTrader 在线优先、本地月库兜底；本地 `data_sync` 调整为每 30 分钟低频回补，并将 system health/market session 的实时判断优先切到 online feed。
- Fail-closed：实时 trendbar 缺失、断流、乱序或未闭合时保持 `stale_waiting_for_live_trendbar`，不调用 history RPC、不读月库、不生成 alpha；cTrader 重连时重新订阅。
- Targeted verification：bridge、startup fallback、live tick、freshness、scheduler、health/risk 相关测试 `137 passed`；Python compile 与 `git diff --check` 通过；全量测试 `2707 passed, 9 skipped`。
- Runtime acceptance：受控重启后已观察到 `source=broker`、`seeded online trendbar feed`、`subscribe_live_trendbars OK` 和新 M5 bar；跨过 18:35 闭合边界后 live tick 继续正常输出，未再出现 `waits for data_sync`。启动 catch-up 的 `data_sync` 仅在 bridge warming 时跳过，正式调度仍为 `2,32 * * * *`；若 cTrader 账户/市场实际不推送 trendbar，必须继续保持 fail-closed，不得回退为在线 spot 伪造 OHLC。

### P2 canonical risk

```text
closed-bar forward_var_input.v1
  + fresh account/positions
  + current/final candidate signed notional
  -> backend.risk canonical calculators
  -> risk_metrics_snapshot.v2
  -> RiskPolicy / readiness / API / Web read-only consumers
```

P2 已删除重复 root risk、live 内联统计、API 平行重算和前端旧字段 fallback；known 空仓零敞口保持真实零，unknown/warming_up/error 不补零。

### P3 证据、记忆与 effect

- review 的 canonical identity 仍是 `trade_outcome_review.review_id`。
- rich lesson 统一由现有 `upsert_trade_lesson_memory()` 写入 `trade_lesson_memory.v1`；旧 `live_review`、`learning_backfill.v1` 重复 projection/writer 已删除。
- application/effect 继续按 scope 和 committed mutation 归因；污染、partial、missing evidence 不得形成可执行治理事实。

### P4 V16 因果调度

- `V16CommandGate.is_actionable()` 是 readiness、stepper、authorize 和 claim 共用的唯一 actionable predicate。
- Agent Authority 提供唯一 execution owner/required gate；同一命令最多一个 committed mutation。
- autonomous learning、factor governance、position supervisor governance 三条 lane 继续复用现有 RiskPolicy、V16、Candidate Review 和 Coordinator；不新增第二套 command queue 或 mutation writer。
- 本批新增 candidate lifecycle binding 后，V16 command gate 还会校验已存在 candidate 的 pending bridge；synthetic factor batch command 仍不受 brain candidate 表误约束。Factor batch command 现在携带固定 preflight fingerprint/candidate count，Factor Governance 在 apply 前重验 manifest。

### 2026-08-10 智能自主进化代码批次

- Canonical authority：风险只经 `RiskLimitSnapshot/risk_kelly_sizing`；Readiness 只读已有投影；learning worker 单写 evolution watermark；Factor Cards/lifecycle/effect 负责因子准入；broker intent 复用既有状态机。
- Deleted paths：无条件最小量、readiness 重型 V16/因子重算、Web readiness 详情 fallback、`:58` evolution 档、Backend evolution 注册/启动 catch-up 和同 hash snapshot 放大语义已删除。
- Targeted verification：风险/Readiness/执行组合 170 passed；生命周期/Candidate Card/evolution/config/lineage 组合 125 passed；Web 4 组合同测试与 production build 通过。
- Migration：`decision_factor_snapshot_lineage` migration 13 已由正式迁移器应用并复核 6 个 lineage 列；历史默认 `lineage_missing`。
- Runtime posture：2026-08-11 已受控重启部署本批代码；overlay authority 经 operator re-bind mutation 恢复（见第 2 节）；五项静态 flags 未推进。
- Unresolved live evidence：30 次 readiness p95/无重叠、有效 RuntimeConfig `5%/10%/16%/30`、watermark/backpressure 实际排空、legacy ACTIVE 退回、execution intent 100% 和完整 broker lifecycle。

### 2026-08-11 V16 bridge/lifecycle repair batch

- Canonical authority：`BrainGovernanceCandidateService` 负责 candidate lifecycle projection；`V16CommandGate` 负责 candidate/suggestion/command binding；Governor 和领域 Coordinator 复用同一 projection。
- Code changes：`keep/no_change` 或当前 supervisor template 目标不再 materialize candidate；bridge 使用 `bridge_pending -> awaiting_execution`；bridge transaction 原子写 suggestion/candidate；cleanup、claim、reissue 统一 pending predicate；冲突解析先看 V16 lineage/evidence；应用后同步 candidate 为 `applied`。
- Legacy repair：V16 run 会通过 service-backed reconciliation 将已 superseded/missing 的 legacy submitted bridge terminalize，不直接 SQL 恢复 active。
- Targeted verification：V16/candidate/governor/coordinator/领域 mutation/factor batch 组合 `178 passed`；`git diff --check`、Python compile 和 code graph re-index 通过。
- Runtime posture：2026-08-12 受控重启 `quant-backend.service`（PID `2333271`）和 `quant-learning-worker.service`（PID `2342495`），Caddy active；本地/公网 `/api/health` 均为 `db=connected, ctrader=connected`。learning worker 重启时旧进程优雅停止超过 90 秒并被 systemd 按超时 SIGKILL，随后新 PID 正常启动并恢复 overlay hash；本批部署和 reconciliation 未直接写 runtime overlay。重启后的既有 evolution hourly 正常执行一次 `factor_lifecycle.register_shadow`，写入 mutation `da29931c-5049-5e75-b4d1-a70bc4b70fc2` 的 shadow factor overlay，未由 V16 command 执行。
- Legacy repair：通过 `BrainGovernanceCandidateService.reconcile_submitted_bridges()` 完成 `108` 条 bridge reconciliation；`brain_candidate_544c1b8f18691c68` 按 superseded suggestion terminalize 为 `superseded/superseded_by_governance`，未恢复 active。随后通过 `expire_stale_evolution_runs()` 将因 worker 重启中断且超过 900 秒的 `evorun_2922ae8d33dd438b` 标记 `expired`，不修改样本、decision、runtime config 或交易状态。
- Runtime evidence：重启后 nursery 正常完成，后续 factor governance run `evorun_cc957412ff7544a0` 完成；新 V16 factor command 携带 `factor_governance_batch_manifest.v1`，无 committed action 时按 `factor_governance_cycle_no_committed_action` 取消且 `apply_count=0`。当前 readiness 保持 `ready_for_autonomous_mutation=true`，但 `incident_control.effective_mode=no_new_risk`、`ready_for_live_execution=false`，未扩大交易风险权限。
- Full-suite verification：全量 `2712 passed, 9 skipped`。本轮关闭的失败包括：discovered factor admission fixture、PostgreSQL migration baseline、RiskLimitSnapshot 显式测试输入、quarantined factor 弱证据 veto、prepared DSL candidate admission evidence、model `retired` tightening classification、live facade wiring、以及 supervisor scheduler 的 advisory-only contract；无失败项。

### 2026-08-18 evolution_decision 决策写入/读取收敛批次（8 列单轨）

Batch: `evolution_decision` 从 19 列宽表（SQLite 夹具）/未对齐 INSERT（PG 8 列表）收敛为单一 8 列合同，PG==SQLite 一致；修复 PG 上决策落库必挂的断链与镜像事务污染。

Canonical authority: 写入 `record_evolution_decision` 收敛为实际存在的 8 列（`decision_id/run_id/decision_type/decision_json/payload_hash/canonical_event_id/projection_type/created_at`）；语义字段 `scope_type/scope_key/action/status/config_version/config_hash` 进 `decision_json`；`evidence/risk_verdict/before/after/result/rollback` 经 `payload_hash -> mutation_payload` 驻留，读取端 JOIN 重读。canonical_v2 增量镜像仍写 canonical_v2 event/payload。

Deleted paths: evolution_decision 的 11 个宽语义列（scope_type/scope_key/action/status/evidence_json/risk_verdict_json/before/after/result/rollback_json/config_version/config_hash）从 SQLite DDL（evolution_ledger、db.py 两处）与 PG 表口径统一移除；所有读取方（`get_evolution_run`、`proposal_registry._from_evolution_decisions`、`autonomy_health`、`factor_governance_orchestrator._rollback_payload_for_decision`、`scripts/state_payload_compact` 的 `_mutation_rows/_audit_rows/_mutation_metadata_rows/_pg_payload_definition/_hash/_apply_payload_refs/_rollback`）改为解析 `decision_json`+JOIN `mutation_payload`，不再引用宽列。对应实现耦合测试断言同步更新（`test_state_payload_dedupe`、`test_autonomous_learning`）。

Root-cause 修复（伴随发现，属本批必改）：`canonical_v2.put_legacy_mapping` 的"优雅 no-op"在 `legacy_mapping` 表 S5 已 DROP 时，失败 SQL 会把 PG 事务置为 aborted，外层 `conn.commit()` 静默回滚——连带丢弃主 decision INSERT（表现为 record_evolution_decision 无异常却 0 行落库）。已在 SELECT/INSERT 外包 SAVEPOINT，失败时 ROLLBACK TO SAVEPOINT 恢复有效事务再 no-op。

Targeted verification: `test_state_payload_dedupe`(12)/`test_canonical_v2_governance_backfill`/`test_autonomous_learning`(39)/`test_proposal_registry`/`test_evolution_*`/`test_v15_runtime_platform_phase0`/`test_agent_coordination_fixes` 等 120+ 项针对性通过；evolution/autonomy 批次再 60 项通过；全改动文件 `py_compile` 通过。

Migration/OpenAPI/build: 生产 PG 表本就为 8 列，**零迁移**；无 OpenAPI/前端接口变化。

Runtime verification: PG 端到端实测 `record_evolution_decision`（此前 exact 报 `UndefinedColumn scope_type` 的调用）→ `get_evolution_run` 读回 → 新连接持久化 1 行，payload 经 `mutation_payload` 正确驻留重读。瞬态验证写已清理，生产库无残留测试数据。

Remaining compatibility: 原卡死 run `evorun_552d20cc1bd84204`（position_supervisor_trace_maturation）保持 running，会在下一次 evolution run 启动时被 `expire_stale_evolution_runs`(>900s) 自动标 expired；下一次 trace maturation 现可正常落库闭合。canonical 镜像对 S5 已 DROP 的 `legacy_mapping` 走 SAVEPOINT no-op。

Unresolved live evidence: 进化闭环首次**自然**闭合仍需下一次 learning worker 周期真实运行证明（trace maturation 写全 decision/event/sample/effect）。另 3 项 factor governance 存量失败（`test_expansion_preflight_finds_fresh_builtin_activation`/`test_quarantine_review_acquits_frozen_factor_without_health_ok`/`test_prepared_discovered_gp_candidate_reaches_preflight_and_activation`）经本日跟进确认**相关于 `learning_application_effect/log` 域代码(宽) vs DB(精简) 双轨断开**（S5 只收 DB 没收代码，后验守卫按宽列查询在生产精简表抛错 → fail-closed 阻断扩张），与本批 evolution_decision 收敛本身无关；详情与退出条件记入 legacy-debt-register，待用户拍板域级收敛范围（code 收敛 / DB 宽化）。

Next batch: 观察下一次 learning 周期 trace maturation 自然闭合；对 `learning_application_effect/log` 域双轨断开，等用户拍板收敛范围（code 收敛到精简 schema / DB 两表宽化）后执行并让 3 项 factor governance 测试恢复绿；同步到最终执行清单/验收矩阵。

### 2026-08-18 learning_application 域架构收敛批次（代码→精简 schema 单轨）

Batch: S5 只收敛了 `learning_application_log/effect` 的 DB（精简），本批把该域全部代码（写者+读者，backend + research）收敛到精简 schema，消除宽 SQL/宽列引用，恢复 3 项 factor-governance 目标测试并以全量回归验证。

Canonical authority: 唯一中心 store `backend/services/learning_application_store.py`（class `LearningApplicationStore`，PG==SQLite；`prepare_application/transition_application/get_application/latest_application/iter_applications/write_effect/update_effect/latest_effect/iter_effects/store_for_conn`）。域内全部读写经该 store；旧宽列（scope_type/scope_key/action/old_weight/new_weight/suggestion_ids_json/cycle_ts/mutation_id/governance_eligibility_version/decision_json/status/observed_trade_count/delta_avg_reward 等）并进 `details_json`/`effect_json` 保留语义；`scope` 列=scope_key（跨库因子过滤，不为 JSON 操作留门）。

Deleted paths: `learning_application_*` 域所有手写宽 SQL 移除（~22 文件 backend + research/learning/governor + research/features/feature_provider），统一在读改经 store；4 处宽 DDL 统一为精简（db.py ×2 + factor_lifecycle_service ×2）；orchestrator `_rollback_failed_actions/_latest_posterior_effect/_factor_has_pending_effect/_mark_application_rolled_back` 均走 store（`_rollback_failed_actions` 宽查询改 `iter_effects(scope_type='factor')` + `get_application` 内存过滤 + effect updated_at 排序取 5）。业务相关辅助：测试改 `_init_state_db(STATE_DB_DDL)` 初始化运行时 schema；evolution_ledger PG 分支去掉裸 psycopg，改调中心 `db._ensure_pg_business_tables()`（并把 runtime_config_snapshot/runtime_config_overlay/factor_catalog_snapshot 三表 DDL 并入 `_PG_BUSINESS_TABLES_DDL`，满足 db-access-contract）。

Targeted verification: 全量回归 `2812 passed, 12 skipped` + research 1 项随后修复（实 2813）；三目标测试 `test_expansion_preflight_finds_fresh_builtin_activation` / `test_quarantine_review_acquits_frozen_factor_without_health_ok` / `test_prepared_discovered_gp_candidate_reaches_preflight_and_activation` 恢复绿；`test_factor_governance_orchestrator.py` 38 全绿；`test_db_access_contract` 绿；`py_compile` 全通过。

Migration/OpenAPI/build: 生产 PG 两表本就精简，**零迁移**；无 OpenAPI/前端接口变化。

Runtime verification: 全量回归在 SQLite 精简夹具 + 生产 PG 同 schema 下通过；收敛后该域写者/读者对精简 schema 读写正确（store 单测通过）。**本批代码尚未受控重启加载到 `quant-backend` / `quant-learning-worker`**——按纪律"代码验收完成 ≠ 运行验收"，运行验证待下一次受控重启 + 下一次 learning worker 周期真实证据（如 trace maturation 自然落库）。本批改动与既有 S 阶段工作保持**未 commit / 未 push**（用户约束）。

Remaining compatibility: `test_state_store_schema_guard` 3 项失败为 S 阶段既有债务（experience_memory/runtime_kv 目录校验，非本批引入，已记入 legacy-debt-register）。

Unresolved live evidence: 进化闭环首次自然闭合仍待下一次 learning worker 周期真实运行证明（与上批相同）。

Next batch: 观察何时收敛批次交付后的下次 learning worker 周期；单独批次处理 `test_state_store_schema_guard` 3 项既有债务；README 首页待真实闭环证据后再更新。

### 2026-08-19 P2·S3 遗留清扫批次（样本域 canonical 全切 + R1/R2/R3/R4/R5 净删）

Batch: 执行 handoff P2（默认下一批）——把学习样本域遗留读取全切 canonical、退役 R2 迁移脚本、确认删除 R4 死代码、净删 R1 legacy_mapping 写读路径与 R3 双轨 fallback、R5 测试夹具 canonical 化。样本域收尾时**修复了 canonical reader 4 个潜伏 bug**（此前 PG 生产 materialize 样本读取实为空）。

Canonical authority: 样本域唯一事实源 = `canonical_v2.training_sample_row`（写经 `record_sample_row`/`put_training_sample`，读经 `canonical_v2_reader.iter_training_sample_rows/get_training_sample_row`，无条件单轨）。`canonical_v2._sql` 在 SQLite 下剥 `canonical_v2.` 前缀 → canonical 表以裸表建于测试主库（无 ATTACH），PG 保持限定名。reader 新增 `system_contaminated`/`decision_id`/`trade_id` 过滤；`_resolve` 改为按 live-id 约定（`live_decision_/live_ordevt_/live_posevt_/live_review_/live_evol_/live_gov_`）直接推导（legacy_mapping 表 S5 已 DROP），修复 canonical 直读。决策/评审域 dual-mode（`_canonical_ready` 驱动的 legacy 兜底）按批次边界保留（进下一批）。

Deleted paths: **R3** 样本域全部 legacy 路径——reader iter/get 的 `autonomous_learning_sample` fallback 分支、`_upsert_sample`/repair 的 legacy 写、materialize/entry_* 的 `except→legacy SELECT` 兜底、materialize DELETE legacy 分支、`ensure_autonomous_learning_tables` 的 sample DDL 块（CREATE/ALTER/索引）、`canonical_ready` 样本域门禁。**生产直读清理（9 文件）**：`autonomous_learning.py`（~10 处）+ `entry_quality_governance`/`autonomy_health`/`autonomous_evolution_cycle`/`autonomous_demo_apply_stepper`/`learning_fact_views`/`evolution_ledger`/`state_payloads`/`replay_harness` 全部直读直写 sample 收敛到 canonical reader；`research/open_quality_lightgbm` + `research/features/feature_provider` 同样净删。**R1**：`put_legacy_mapping` 定义 + 6 调用点 + `__all__` 净删；reader `_resolve` 映射解析路径净删；`scripts/canonical_v2_consistency.py` legacy_mapping 审计净删；`LEGACY_MAPPING_CONFIDENCE` 死常量删除。**R2**：scripts/ 20 个 backfill/reconcile/equivalence 脚本 + 6 个配套测试删除（`_code_version()` 内联到保留脚本）；保留 4 个运营工具（live_reconcile/consistency/projection_rebuild/position_decision_index）。**R4**：`execution/oms.py`/`execution/algos.py`/`alpha/factor_engine.py`（零引用确认）+ 关联 `tests/test_oms.py` + `api/paper.py`（实为活跃路由 4 端点）`paper_service.py` `tests/test_backend_paper_service.py` 删除；`cli/paper.py` 因子健康块、`main.py` 因子参数、`alpha/__init__.py` 导出、`ALL_ROUTERS` 净删。**R5**：9 样本域测试文件 canonical 化（`tests/canonical_fixture.py` 共享裸表夹具，含大文件 `test_autonomous_learning.py` 39 passed）。

Targeted verification: 样本域全集 138 passed / 1 skipped；`test_canonical_v2` 11 passed；research 域 57 passed；`test_open_quality_lightgbm` 3 passed（真实走 canonical reader，此前为空转）。修复的 4 个 reader 潜伏 bug：`None` params → sqlite3 ProgrammingError 被吞（空无过滤读取恒 []）；`cols` 未定义（canonical 分支恒 []，**PG 生产 materialize 样本读取实为空**）；`dict(sqlite3.Row)` ValueError（get 返 None → noop 失效）；tuple-row 连接（`connect_sqlite` 未设 row_factory）归一化。

Migration/OpenAPI/build: 生产 schema 零迁移（canonical_v2 表本就存在；legacy_mapping 已在 S5 物理 DROP，本次只净删代码路径，不改表）；无 OpenAPI/前端变化；`py_compile` 全通过。

Runtime verification: 全量回归 **2779 passed / 12 skipped / 3 failed**（3 failed = S 阶段既有 `test_state_store_schema_guard` 债务，与基线 3 项逐一相同；较基线 2813 passed 减 34 = 本批删除的 34 个测试函数，无回归）。期间自检发现并修复 1 项自引回归：新裸表 canonical 夹具使 `test_runtime_state_ddl_objects_have_a_schema_contract` 短暂失败 → 契约正则改为识别 schema 限定名（`canonical_v2.x` 规范化取裸名），断言语义不变。生产行为变化 = 样本域读写改走 canonical（与既有 PG 生产单轨一致，且修复 materialize 样本读取空 bug）。按纪律「代码验收 ≠ 运行验收」，双服务尚未受控重启加载本批，运行验证待安排。本批全部改动保持未 commit / 未 push（用户约束）。

Remaining compatibility: `test_state_store_schema_guard` 3 项失败为 S 阶段既有债务（未涉及，记于 legacy-debt-register）。

Unresolved live evidence: 进化闭环首次自然闭合仍待真实运行（同前批）。决策/评审域 legacy 兜底与 `position_supervisor_trace`/`supervisor_counterfactual_review`/`decision_factor_snapshot` 等直读保持为下一批边界。

Next batch: P3（S6 容量阀 P6）或 P4（schema-guard 债务）——听用户指定；`execution/analytics.py` docstring paper_service 提及等叙述性清理可在任一后续批一并做。

### 2026-08-19 P4 schema-guard 债务批次（全量回归归零）

Batch: 按用户指示处理既有债务（P4），并在过程中一并处理发现的其他债务。目标 = 全量完全通过。本批修复 `test_state_store_schema_guard` 3 项既有失败（唯一剩余失败面），全量回归归零。

Canonical authority: 运行时 schema 的迁移 CLI（`scripts/state_schema_migrate.py --apply`）是唯一 schema writer；`backend/core/state_store.py` 的 `RuntimeStateConnection`/`RuntimeStateCursor`/`validate_runtime_state_schema` 只做**目录校验**且 **fail-closed**（缺表/缺列/索引不匹配 → `RuntimeStateSchemaMissingError`；非法写 → `RuntimeStateSchemaWriteError`），绝不执行 DDL。

Root cause: `validate_runtime_state_schema`（验证后误执行一段 DDL 以"post-S5 恢复建表"）与 `_validate_runtime_schema_statement`（用 `except RuntimeStateSchemaMissingError: pass` 把缺表/缺列/索引不匹配全部吞掉）在 S 阶段漂移出既定契约（纯目录校验 + fail-closed），与 `tests/test_state_store_schema_guard.py` 3 项断言冲突。

Fix: ① 删除 `validate_runtime_state_schema` 的 DDL 执行循环（仅校验，不建/不改表）；② 删除 `_validate_runtime_schema_statement` 两处吞错 `except...pass`——缺表/缺列/索引定义不匹配一律抛 `RuntimeStateSchemaMissingError`；③ 同步两函数 docstring 为"catalog validation only / never mutates"。生产建表路径不受影响（本就经 migration `--apply` 与 `_ensure_pg_business_tables` 普通 psycopg）。

Targeted verification: `tests/test_state_store_schema_guard.py` **21 passed**（含原 3 失败）；state_store 相邻集（schema_migrations / backend_runtime_lifecycle / learning_worker_capability / live_open_admission）98 passed / 1 skipped；`state_store.py` py_compile 通过。

Full regression: **2782 passed / 12 skipped / 0 failed**（7:14）—— 全仓归零，较 P2 批 2779+3 failed 恰好回收 3 项，无回归。

Other debt triage during pass: 扫描 `legacy-debt-register.md` 其余条目——`RuntimeStateConnection DDL 拦截导致 ensure_* PG 模式无法建表`（`migrating`，S7 已用 `_ensure_pg_business_tables` 普通 psycopg 重建、其余 ensure_* PG 分支直接 return 属运行期/重启验证面）、大量 `migrating` 运行验收项（safety/effect/closed-loop）均需真实运行证据且受"不得切生产开关"约束，本批不触碰，留待对应运行门。

Docs: `legacy-debt-register.md` schema_guard 条目 → `resolved`；`handoff-next-batches` P4 → ✅ 完成 + §7 计数刷新。全部改动未 commit / 未 push（用户约束）；双服务未重启。

### 2026-08-19 补库批次（已整体撤回，勿作为完成事实）

Batch: 用户此前指示"重启后端 + 确认 cTrader"。重启后观察到一系列报错（索引缺失 / 表缺失 / 列缺失），按"缺什么补什么"做了 4 个补库迁移（0019–0022）+ db.py DDL 改动。**经用户质询后认识到方向错误**：

- 重构（S5 全库清空重建）的本质是**做减法**：state_v1 86 表 → runtime 75 表（按代码 DDL 提取）+ canonical_v2 9 事件表；**标准以"代码 DDL 提取的 75 张"为准，不是以旧迁移文件（0002–0016）的声明为准**。
- 旧迁移文件声明的不少表（如 `meta_model_shadow_audit` / `meta_shadow_report_snapshot` / `data_repair_run` / `data_repair_item` / `risk_daily_equity`）**业务代码零引用 = 重构已淘汰的死表**，补建是倒行。
- 迁移台账标记"applied"不代表对象必须存在（S1 账本层面标记，S7.3 用代码 DDL 重建）。

**撤回内容（已全部执行）**：
- 新增的 8 张表（meta_model_shadow_audit / meta_shadow_report_snapshot / data_repair_run / data_repair_item / risk_daily_equity / state_payload_archive / broker_execution_intent / decision_factor_snapshot）全部 DROP；
- 新增的 47 个索引全部 DROP；
- 14 个 ALTER 补列全部 DROP COLUMN；
- `factor_lifecycle_state` / `factor_runtime_projection` 恢复为 db.py `_PG_BUSINESS_TABLES_DDL` 极简版（7 列 / 4 列）；
- db.py 两表 DDL 还原为极简版；
- 迁移文件 0019/0020/0021/0022 删除；`state_schema_migrations.py` 移除 v19–22 注册；台账删除 v19–22 记录。

**当前验证（撤回后）**：迁移 `--check` = **current_version 18 / latest 18 / ok True / mismatches 0**；我建的对象零残留；factor 两表为 7/4 列极简版。**全部改动未 commit / 未 push**。

**保留的未做处理的真实观察（供下一对话收敛，勿直接改库）**：
1. `/api/ops/brain/governance-candidates` / `governance-candidate-reviews`：`brain_governance_candidate` 是**标准表**（source-of-truth 权威），但 `idx_brain_governance_candidate_created/stage/scope/source` 4 个索引在 PG 缺失 → 报 500。属"标准表索引未建全"，可用迁移补索引（方向对）。
2. `/api/live/realized-pnl-series`：`recovery_position_state` 是标准表，但实际是极简版（4 列），代码 `realized_pnl` / `live_recovery_position_store` 按完整列读写 → 报 `symbol` 列缺失。属"db.py `_PG_BUSINESS_TABLES_DDL` 定义与 store 消费不一致"，应对齐 store 的完整列定义。
3. `lifecycle_events` 表：**不在任何迁移/标准文档**，代码（learning/summary、alpha registry）仍在引用 → 属"重构淘汰后引用未清理"，正确修法是**改代码**，不是补表。
4. `validate_ctrader_token.py` 有 Twisted reactor 二次 run bug，不可作连接验证工具。
5. cTrader token 过期时间 **2026-08-22**，届时需刷新。
6. 重启后曾观察 `/api/ops/autonomy/proposals` 500（`ProposalRegistryService`），根因未定位完（撤回时一并放弃）。

Docs: 本文件本批次记录（撤回事实）+ handoff 5b 同步。全部改动未 commit / 未 push（用户约束）。

### 2026-08-19 0019 二级索引回填批次（✅ S7.3 索引欠账补齐）

Batch: 用户拍板"按建议补索引"。实测确认 S7.3 清库重建只建了 75 张 runtime 表的表结构、二级索引为 0（PG 76 索引中 72 个是主键），S5 前迁移/必须存在的供给在重建时被统一跳过。本批新增正式迁移 0019 `secondary_index_backfill`（128 个索引）并应用至生产。

Canonical authority: 索引定义以生产代码 DDL + 既有迁移文件 0001–0018 声明为准（迁移 CLI `state_schema_migrate.py --apply` 是唯一 schema writer，符合 P4 修复后的"只校验必建"纪律）。只补"表+列在 runtime schema 实际存在"的索引，**不新增表/列/线程/调度器**，不属于 S5 做减法的反向动作。

Root cause / 发现: S7.3 从代码 DDL 提取建表时，`_PG_BUSINESS_TABLES_DDL` 26 张表 0 索引，其余表由 ensure_* 预建；迁移链只做 ALTER/索引，而迁移文件 0001–0018 的索引声明未被重建执行。P4 把 schema 校验改为 fail-closed 后，缺索引从"被吞掉的静默欠账"变成每半小时 evolution_hourly 的真实报错（`missing PostgreSQL state index`）。

Targeted verification: 预检在事务内执行全部 128 条（ROLLBACK 未提交）0 失败；`--apply` 成功 current 18→19、ok:true、mismatches 0（412ms）；3 张目标表（factor_catalog_snapshot / brain_governance_candidate / factor_governance_shadow_audit）7 个索引全部落库；此前报错的 4 条运行路径（factor_catalog.ensure / FactorGovernanceLightGBMService._ensure_tables / brain_governance_candidates.ensure）PG 校验复验不再抛 missing index。迁移相关测试 `test_postgres_state_store::test_versioned_state_migration_executes_in_disposable_pg_temp_schema` 因夹具缺"生产预建列/表"而失败 → 按生产真实列补齐夹具（18 表补列 + 28 表新建 TEMP，序列/函数默认值剔除）后通过；schema-guard 全绿。

Remaining / 未处理: 17 个"表定义 vs 消费不一致"索引（缺列在 S5 极简表不存在，需对齐列或改消费）与 `idx_offmarket_training_window_unique`（24 行重复数据无法建 UNIQUE）未纳入 0019，单独列为 handoff §5b 待收敛项。全量回归待跑（见 §7 计数）。

Docs: 本批次记录（本文件）+ handoff §5b 状态刷新。全部改动未 commit / 未 push（用户约束）；双服务未重启（0019 为 DDL 索引，运行进程自动可见，无需重启）。

### 2026-08-19 0021 补建 S7.3 漏表批次（✅ 7 张活跃表物归原主）

Batch: 0020 复验时暴露 `factor_contribution_review` 不存在（autonomous_learning 每半小时报错）。全库只读审计（declared-vs-PG-actual + 真实 SQL 引用数）确认根因：**S7.3 重建按 `_PG_BUSINESS_TABLES_DDL` 只建 26 张表，代码 ensure_* 预建的另一批表全部漏建**。用户拍板方案 A（一并补建）。

Canonical authority: 建表 DDL 取自 `STATE_DB_DDL`（SQLite 完整标准，db.py 363 起）——7 张表均为活跃读写（逐表审计真实 SQL 引用：factor_contribution_review 8 / decision_factor_snapshot 11 / calibrator 1 / decision_log 2 / lifecycle_events 5 / shadow_trades 1 / weight_history 3），死表不建（strategy_perf / sync_health 0 真实引用，归清理）。建表列以 STATE_DB_DDL 为准（含 0013 迁移要求的 lineage 6 列），附 db.py 声明的 11 个相关索引。

Root cause: S7.3 清库重建只遍历 `_PG_BUSINESS_TABLES_DDL` 名单建表，因此在 STATE_DB_DDL/ensure_* 里定义、但不在该名单的表在 PG 从未建。之前 handoff §5b 将 `lifecycle_events` 记为"重构淘汰死表→改代码"系误判（实测 5 处真实 SQL 引用，是活表），本批纠正。

Targeted verification: dry-run 事务内 18/18 语句通过（ROLLBACK 零残留）；`--apply` 成功 20→21、ok:true、mismatches 0（18 语句：7 建表 + 11 索引）；7 张表列数正确落库；7 处消费路径复验全 OK；迁移相关测试 47/4 绿；针对性 64 passed。全量回归见 §7（0021 批后）。

Remaining: 死表声明清理（strategy_perf / sync_health）纳入遗留清单；`idx_offmarket_training_window_unique` 仍挂起。

Docs: 本批次记录 + handoff §5b 更新（漏表项 → 已解决，lifecycle_events 误判纠正）。全部改动未 commit / 未 push；双服务未重启（0021 纯建表，进程自动可见）。

### 2026-08-19 收口批：死表清理 + token 工具 + proposals 修复 + 容量看板（✅ 6 项代码落地）

Batch: 用户在"剩余账目清单"拍板全部按推荐方向做，把代码类落地一次做完。

- **死表声明清理**（任务 1）：db.py `STATE_DB_DDL` 删除 `strategy_perf` / `sync_health` 两张 0 真实 SQL 引用死表的 CREATE 声明（前者 0、后者为 Python `SyncHealth` 对象名非 SQL 表——测试 28 passed 确认无破坏）。从 handoff §5b "死表归清理" 落实。
- **validate_ctrader_token.py 修复**（任务 2）：根因 = 一次进程对全局 Twisted reactor 多次 `run()`（二次 run 即炸）。重写为单 reactor + 单 client 的只读探针（TokenProbe），去掉调试噪音（故意错 secret / 重复测试 / raw dump）与跨版本字段名假设。实测连 demo 成功：app auth OK → token VALID（5 账户）exit 0，无 reactor bug。
- **/api/ops/autonomy/proposals 500 根因修复**（任务 3）：复核发现根因 = `proposal_registry` 缺 `proposal_action` 列（SQLite 标准有、PG 无）+ 索引契约不匹配。修 = 迁移 0022（补 proposal_action + brain_governance_candidate_review.evidence_fingerprint + learning_experiment_reservation.mutation_id，3 列）+ 0023（重建 idx_proposal_registry_source_ref_updated_v2 为 DESC 契约）+ 0024（重建 idx_jobs_claim_ready 加 priority DESC）。`ProposalRegistryService.latest()` 实测 ok:true。401 为预期鉴权。**期间全量索引契约审计发现 46 项存量 mismatch**（7 死表豁免 / ~16 活表缺索引 / ~20 存在但契约差异，全量对齐 = 独立下批，见 run_artifacts/index_mismatch_46_note.md + handoff §5b）。
- **README 首页刷新**（任务 4）：2026-08-12 旧快照 → 2026-08-19（S5/S7 完成、0019–0024 schema 收敛、双服务重启验收、S7.6 明确标注待真实闭环、P1 runtime acceptance、Safety shadow）。
- **idx_offmarket_training_window_unique 恢复**（任务 5）：澄清误解——契约是部分唯一索引 `WHERE training_window_key <> ''`，24 行空 key 'skipped' 审计行不在覆盖范围内；非空 key 0 重复组。迁移 0025 补建成功（24→25）。
- **容量观测接入 system_health**（任务 6）：`capacity_observe.py` 增 `--json`（全量快照+增速）与 `--trend`（紧凑增长摘要，供看板）；`risk.py` `_system_health_summary` 增 `capacity` 字段（subprocess 调 `--trend`，只读独立进程、失败降级 None 不阻塞 health）。`/api/risk/summary` system_health.capacity 可见。遵守架构纪律：不新增线程/调度器，读时实时计算。

验证：相关测试 28+6+33 绿；迁移台账 v25 / ok / mismatches 0；全量回归见 §7（本批后）。全部改动未 commit / 未 push；双服务未重启（代码改动 + 迁移 0022-0025 均进程可见；下次重启自然加载）。

### 2026-08-19 0026-0027 批：factor_runtime_projection 对齐 + B 类缺索引补建

Batch: 用户把 `process_role` 缺列加入队列并处理。

- **0026 align_factor_runtime_projection**：根因 = 启动 warning `column process_role does not exist`。S7.3 重建用 `_PG_BUSINESS_TABLES_DDL` 4 列极简版建 `factor_runtime_projection`（factor_id 主键），而 0001 迁移声明 18 列完整版（projection_id 主键）且代码（factor_lifecycle_service/factor_catalog）按完整列消费。0026 ADD 15 缺失列（保留现有 factor_id 主键 + projection_json 不动）+ 建 0001 声明的 3 索引（identity/health/factor）。applied 25→26。
- **0027 build_backfilled_live_indexes**：B 类活表缺索引、列经 0020-0026 补齐后现可建的 3 个（idx_brain_candidate_review_fingerprint / idx_proposal_registry_projection_key / idx_decision_factor_snapshot_lineage_status）。applied 26→27。idx_factor_lifecycle_* 4 个因引用旧列名（factor_name/lifecycle_stage/runtime_admission，无标准列）未建，归"改代码"待办。
- **新暴露债务（记 handoff，非本批处理）**：重启后 `process_role` 消失，但 `recover_governance_projections`（factor_lifecycle_service.py）106 处旧列名引用（s.mutation_id/factor_name/lifecycle_stage/runtime_admission）报 `column s.mutation_id does not exist`（non-fatal）——消费方代码未跟上 `factor_lifecycle_state` 重构（现 7 列无 mutation_id），修法 = 改代码对齐新列名 + mutation 关联迁 governance_mutation_intent，独立精细子任务。

验证：重启后端 process_role warning 消失、cTrader auth OK；迁移相关测试 47/4 绿；全量回归 2782/12 0 失败；迁移台账 v27 / ok / mismatches 0。全部改动未 commit / 未 push。

### 2026-08-19 0028 批：factor_lifecycle_state 补列（消除最后启动 warning）

Batch: 用户确认"修"最后一项运行态技术欠账。

- **0028 align_factor_lifecycle_state**：`factor_lifecycle_state` 被 S7.3 重建创成 7 列极简版，权威定义（0001 + 全部消费方）是 17 列完整版 → 启动报 `column s.mutation_id does not exist`（recover_governance_projections JOIN）。0028 ADD 11 缺失列（factor_name/definition_fingerprint/lifecycle_stage/generation/runtime_admission/mutation_id/config_version/config_hash/metadata_json/activated_at/retired_at）+ 建 idx_factor_lifecycle_unique_name 唯一索引。applied 27→28。
- 验证：重启后端 `governance projection recovery attempted=0 current=0 degraded=0`（原 failed warning 消失）；启动无 ERROR；cTrader auth OK；factor 测试 57/1 绿；全量回归 2782/12 0 失败。
- **代码消费方无需改动**（factor_lifecycle_service/factor_catalog/ledger 本来就按 17 列写，之前是表缺列）。

### 2026-08-20 持仓监督单轨执行与学习闭环修复

Batch: 按“结果可学、动作不可归因”原则，移除 Demo observation gate 和 legacy AWE active fallback；Demo 与未来实盘共用同一 governed supervisor executor。

Canonical authority: active `position_supervisor_template.v1` 只接受 `governed_execute`；普通 `tighten/reduce/close` 唯一路径为 `supervisor verdict -> RiskPolicy -> cTrader broker -> lifecycle -> fresh reconcile -> position_supervisor_trace`。`autonomy_demo_auto_apply` 只控制已通过治理证据的 mutation，不控制普通持仓监督 broker 执行。

Deleted paths: live observation gate、`_observed_adaptive_verdict`、Safety 双 candidate compare/fallback、active `legacy_awe_trailing` planner/candidate writer/executor。历史 AWE/observation trace 只读审计；generic executor 对新的 legacy candidate 明确拒绝。

Contract: `evidence_contract_json.consumer_eligibility` 按 `outcome_learning`、`supervisor_counterfactual`、`governance_mutation` 隔离用途；非真实 observation/superseded trace 幂等 terminalize 为 `excluded/train_weight=0`，真实 applied trace 才进入 supervisor maturity。`restart_replay/manual_close` 仍阻断 supervisor counterfactual，但不抹掉权威 broker PnL 的低权重 outcome learning。

Lineage/timing: review repair 优先读取 canonical position-decision index 和 canonical close/order events，统一 epoch seconds；单位混用、逆序或缺 timeframe 标记 `timing_invalid`，不制造 execution delay。对 `284253609` 的只读验证得到 canonical decision `dec_e58a4e1f3a7d478b`、M5、signal-to-fill 312.784435 秒、timing valid；线上 backfill 尚未执行。

Targeted verification: 监督/风险/生命周期/学习/readiness 针对性回归持续通过；本批新增严格 broker component truth、loop ownership 和旧 preview 净删测试。未用测试伪造 broker applied 事实；运行态重启、readiness、日志和 cTrader fresh reconcile 已验证，真实 supervisor broker lifecycle 仍待有仓位时证明。

Remaining: 已有真实 supervisor close 的正向 lifecycle/反事实/成熟证据，但仍需 ≥10 笔合格仓位、`tighten/reduce` 覆盖和连续 effect observation；单测和 readiness 不替代该证据。历史 `284253609` 可在 service-backed backfill 后用于低权重 outcome learning，但不得作为 supervisor counterfactual 或治理证据。

### 2026-08-21 canonical-v2 follow-up：运行态迁移与旧事实清理完成

本批代码继续收敛为 canonical 单轨：生产 `backend/alpha/execution/risk/research/scripts/db` 已无退役事实表的直接 SQL；`autonomous_learning_sample` 已从进程启动 DDL 删除，canonical 样本唯一写入 `canonical_v2.training_sample_row`；readiness 现在同时校验 active `governed_execute` 模板、broker orders 开关、完整 schema 版本和 `runtime.broker_execution_intent`。

运行态复核确认 PostgreSQL migration ledger 已到 v30，`runtime.broker_execution_intent` 存在；旧 `runtime` 事实表已由 service-backed cleanup 清空并由 v30 退役，不再提供兼容查询或写入路径。canonical event 当前承载 decision/order/position/review 事实，历史 AWE/observation 仅可经 replay/audit 读取，不能进入实时 broker executor。

重启验收（2026-08-21）：backend PID `24950`、learning worker PID `7828` 均运行；进程 capability 已加载 `live_safety_plane_v2_mode=enforce`，旧 generation flag 不再出现。cTrader 账户认证、positions/account fresh reconcile 成功，当前空仓、`unknown_execution_count=0`、`reconciliation_state=fresh`。一次完整 `system_health` 周期已输出 `market_session_status=closed_confirmed`，与 `runtime_health_projection.v1` 的 cTrader schedule 事实一致；修复前的静态时段 `open_confirmed` 误报已消失。当前 live execution 仍由 market closed/no-new-risk 事实 fail-closed，没有触发人工或测试下单。

本批额外净删：无调用方的 `live_legacy_safety_preview.py`、死的 `_trailing_state` 清理投影和 lifecycle PnL/account/price 合成回退；缺 broker 明确 `pnl_state=known` 的持仓现在保持 unknown，不再伪造监督指标。新增监控回归后，目标集 `169 passed`，compileall 与 scoped diff check 通过。

### 2026-08-22 审计缺陷批 D1–D13（修复 + 部署 + 周末运行验收）

Batch: 审计缺陷 D1/D3/D6/D7/D11/D12/D13 六项修复端到端落地（原 planning/audit-defects-2026-08-21.md 已归档 Git 历史）；D11 为学习链总断点真凶。迁移 0031_align_factor_health（factor_health 删死列、主键→factor，db.py 极简 DDL 对齐）+ 0032_restore_jobs_primary_key。

Canonical authority: factor_health 唯一写合同 = `ON CONFLICT(factor)` 标准列集；canary PROMOTION→ACTIVE 需 `_has_committed_active_backing` 台账背书；evolution_story 生产路径 pytest 全量改道（core 默认路径 + conftest 单例沙箱双保险）；关键持久化失败经 monitor/persistence_alerts.py 升级告警（1h 同键抑制）。

Deleted paths: weight_history old==new 零信息行停写；生产 evolution_story.jsonl 176 条污染行原子清洗（备份 .bak_20260822）；4 条幽灵 ACTIVE canary 降回 PROBATION。

Targeted verification: 全量回归 2775 passed / 12 skipped；gate 测试与 73 项相关测试绿；EXPLAIN ON CONFLICT(id) 通过 + 临时 schema 重放测试绿。

Runtime verification: 双服务受控重启（backend 04:08、worker 03:06），NRestarts=0；重启后 60 分钟 0 ERROR；cTrader fully authenticated；factor_health 健康报告首次落库成功（64 行 persisted=True，40 余天来首次）；v16_brain_command 新增 specialist_no_action（fail-closed 正常）。周末休市复核（08-22 13:43）：system_health degraded 系 market closed_pending_positions 设计态 fail-closed（挂单仓位 284602893 无新报价），非故障；readiness ok:true，仅 live_execution 因 market_session_blocks_open + broker_position_price_unknown 阻断，符合预期。

Migration incidents: 迁移 SQL 注释含分号类标点导致 psycopg 解析失败（已按规范重写注释）；0032 apply 后改文件致校验和失配 → 服务拒启一次 → 按先例 restamp ledger v31/v32（run_artifacts/restamp_ledger_31_32.py）后干净重启。

Unresolved live evidence: 部署后新开仓位的完整 lifecycle 样本（S7.6 终验）未取得——284485647 于 8/21 重算转正 full/1.0 证明链路通，但属部署前开仓；挂单 284602893 同为旧仓不计入。周一开盘后第一笔新平仓为判定点。

Remaining compatibility: policy_suggestion 仍有 27 条无 mutation 背书的 applied 幽灵历史行待清（修复后新增 applied 行均带 gmut_ 背书，合法）；根目录 backtest settings.yaml 已证实无加载路径，删除即可（D9 死代码债）。

### 2026-08-22 因子模块批（B 换血 / D 定性 / A 解锁晋升 / C 宏观观察态接入）

Batch: 因子模块四向推进，全程治理 mutation 通道、零手改生产状态。

B 权重换血：factor_health 2000-bar 证据显示 supertrend_str 方向反了（rolling_ic=-0.0022）、engulfing 无信号（0.0013）；经 `gmut_ad05e2f066c0404881acfa2712d48f6a` 双双降权至 0（risk_tightening 免审批）。运行态验证：runtime_factor_selection 投影两因子权重已为 0，backend 未重启自动感知。

D 评分定性：HEALTHY 高分但负 IC 系设计使然——mean_abs_ic 占 40% 奖励预测强度不问方向；direction 字段已预留反向使用契约，留待管道打通后评估。

A 晋升死锁修复（commit e91a053）：准入预检要求 canary==ACTIVE 与 D1 背书门构成循环依赖（lifecycle 激活本身是背书的生产者），13 个全条件达标 GP 因子（oos 967–1408 bars、正 PnL、1599 valid）卡死 CANARY_5。修复：orchestrator `_promotion_evidence` 与 factor_cards preflight 两处接受 PROBATION 为完成态证据；中间阶段仍拦；激活侧全部门禁不变。回归 5 新增测试绿 + 相关面 91 passed（1 失败为基线既有）。

C 宏观观察态接入（gmut_7e1195d43a6c4b6f8571c1751cefffdd）：real_yield_chg / cot_mm_net_chg_4w / cot_mm_net_zscore_52w / cb_total_chg_3m / gld_tonnes_chg_20d / slv_gld_ratio 六个 PIT 安全宏观因子以 role=observe 进入信号配置（no_change 分类）。沿用 dxy_corr_20 先例破"alpha 要证据、证据要被计算"死圈；horizon_ics 在 UNKNOWN 态照常测量，alpha 升格留待数据说话（独立风险分类 mutation）。

Runtime verification: 双 mutation 均 committed/current，后端零重启热加载；health ok；journal 仅周末休市心跳。

Remaining: 周一开盘后观察 observe 因子 n_obs 增长与 horizon_ic 质量；GP 因子走完 PROBATION→治理激活→canary ACTIVE 全链路需下个 evolution cycle 验证。

### 2026-08-26 S7.6 达成确认 + 治理合同收紧批 B（工作区待提交）

Batch: 只读复核确认 S7.6 终验标准达成；治理链路 fail-closed 合同收紧批（16 文件 +733/-88）与业务告警边沿触发批在工作区完成收敛、针对性回归全绿，**未提交、未重启加载**。

S7.6 runtime verification（只读，2026-08-26）: D 批部署后新开仓位已产出 **26 笔** `integrity=full / train_weight=1.0 / governance_eligible=1` 平仓样本（首笔 284662038 2026-08-24，最新 284962512 2026-08-25）；双服务 active、journalctl 24h 无 ERROR；当前空仓。skip/rejected 决策样本按合同双轨落库（risk_policy full/1.0 eligible、posterior full/0.35 非 eligible）；supervisor trace 样本 partial/0.0（excluded_never_executed）排除训练、保留审计。历史结论"周一开盘后第一笔新平仓为判定点"已由真实数据满足。

批次 B canonical authority: 候选创建/提交唯一门 = AgentAuthorityRegistry scope 写权限（advisory 源 fail-closed 不落库）；V16 delegate 认领唯一门 = bridge_ready 且新于候选 updated_at 的评审 + 命令证据携带匹配 review fingerprint；因子预检批 = 恰好一个冻结 execution_ready 候选引用（单控制候选合同）；context_policy = unsupported governance surface（无 runtime writer 时只观察）；demo 参数模板自动应用必须先过 V16CommandGate.authorize 并转发 v16 身份四元组；specialist no-action 后同指纹命令不重发。

Deleted paths: `research/factor_governance_lightgbm.py::_supersede_inactive_demo_suggestions` 净删，逻辑迁入 `FactorPruningGovernanceService.supersede_inactive_demo_suggestions`（唯一 owner 归位）。测试夹具回归修复：`test_autonomous_evolution_cycle` 补齐合格评审行以满足新合同（非放松生产代码）。

Targeted verification: 治理+告警+因子+参数模板领域针对性回归 **156 passed**（2026-08-26 实测：test_governance_contract_convergence / test_live_service_business_alerts / test_v16_brain_orchestrator / test_v16_read_only_brain / test_factor_governance_orchestrator / test_factor_governance_recovery / test_autonomous_evolution_cycle / test_factor_pruning_governance / test_agent_coordination_fixes / test_governance_control_plans）。

Unresolved live evidence: 批次 B/A 已 commit/push（5a4e4db 告警批、cfc126e 治理批、b69ec07 文档批，2026-08-26），但用户决定**暂不重启**——当日新开仓位 285005705 带仓运行中，运行态仍执行 8/22 D 批代码；重启加载顺延至下一空仓窗口，加载后需观察 V16 delegate 链在真实评审流下的放行率（fail-closed 收紧后 candidate_review_required 取消属设计态）。

Next batch: ~~下一空仓窗口受控重启加载批次 A/B~~ → 已于 2026-08-26 14:27 空仓窗口重启加载（含批次 A/B 与监督确认链修复），观察 V16 命令链、告警边沿与监督 tighten 行为。

### 2026-08-26 持仓监督失效确认链修复（四断线补齐）

Batch: 复盘发现监督器“诊断正确但从未动手”，深挖确认四根结构性断线并补齐生产者。影响面清单原见 planning/supervisor-confirmation-chain-fix-plan.md（已归档 Git 历史，HEAD f2eb9c9 前版本）。

Canonical authority: entry_regime 开仓盖章 = `_persist_pending_entry_protection_plan` + `_current_regime_hint()`；signal_reversal = `build_position_supervisor_context_payload` 比较 last_composite.direction 与持仓方向；thesis_broken_confirmations = path-metrics 状态机逐观测递增/intact 归零；transition_confirming 姿态盈利单 tighten 解锁 = evaluate_position_supervisor 动作仲裁内新增分支（profit_protection_window_ready + giveback≥阈值，summary_reason=transition_profit_protection_tighten）。

Deleted paths: 无旧实现删除（纯补齐缺失生产者）；不新增表/线程/调度器/阈值/RuntimeConfig 键。

Targeted verification: 新增 tests/test_supervisor_confirmation_chain.py 11 passed；监督域+治理域回归 229 passed 零退化（test_trade_reviewer / test_live_supervision_path_metrics / test_live_position_lifecycle / test_learning_backfill / test_position_supervisor_templates / test_governance_contract_convergence / test_autonomous_evolution_cycle / test_v16_brain_orchestrator）。

Runtime verification: 2026-08-27 已核验重启加载；首批新持仓已产生非空 `recovery_meta.entry_regime`、`executed/applied` supervisor trace 和 counterfactual/maturity 记录。

Unresolved live evidence: 目前仍只有少量 `close` 样本，需 ≥10 笔合格真实仓位并覆盖 `tighten/reduce`，再完成 supervisor template candidate/application/effect/rollback 的连续闭环；记忆索引本身不能替代该证据。

## 4. 仍需真实运行证明

以下证据不能由单测、历史快照或 readiness 替代：

- ~~`open -> protection -> close -> deal sync -> review -> sample` 完整生命周期~~ ✅ **已满足（2026-08-26）**：部署后新仓位 26 笔 full/1.0 样本，转入常态观察；

- post-repair 新 broker deal 的价格/金额合同；
- restart 后 deal replay 与 position identity 恢复；
- `open -> protection -> close -> deal sync -> review -> sample` 完整生命周期；
- Safety enforce 连续 24 小时空仓或一个完整 broker position lifecycle；
- 当前源码绑定的 execution/safety fault matrix；
- 每次发布阶段的 process-loaded flags、PID、fingerprint 和 release preflight。

在证据未满足前：

- P1 保持 `runtime acceptance`；
- Safety 不从 `shadow` 切 `enforce`；
- 后续静态开关不推进；
- 不把 readiness ready、单次 bridge、单次 effect 或测试通过解释为自治毕业。

## 5. 下一批处理顺序

1. 只读复核新 broker lifecycle、deal replay、review/sample lineage 和 Safety shadow continuity。
2. 对 `legacy-debt-register.md` 中仍在迁移的路径逐项收集退出证据，并在 canonical 验证后同批删除旧路径。
3. 仅在真实证据满足后运行对应 release gate；不通过 SQL 改写历史 review、command、sample 或 maturity。
4. 保持 active supervisor 使用 `governed_execute` 单轨；不把历史 observation 记录升级为 live 执行、supervisor counterfactual 或治理证据。

## 6. 每批状态更新格式

以后本文件只保留或替换以下当前信息：

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
