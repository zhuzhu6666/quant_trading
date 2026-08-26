# Active Legacy Debt Register

> Status: active
> Last verified: 2026-08-21
> Scope: 只登记尚未退出的兼容、重复 authority、隔离数据和回归。

已完成旧债不在本文保留；Git 历史和测试是追溯依据。新增条目必须写清 canonical 路径、剩余旧路径、退出条件和验证。

## 1. 全局收敛

### 旧状态 schema 清理（历史记录）

- 状态：`complete`；旧 PostgreSQL `state_v1` schema 与其数据已清空，生产不再读取、写入或重建该 schema。
- canonical：运行态使用 `runtime`，不可变事件、生命周期事实和学习样本使用 `canonical_v2`。
- 当前：本条只保留清库和命名迁移的审计背景；不得把历史 migration/backfill/legacy projection 叙述当作当前运行事实。

### runtime 退役事实投影与 broker intent schema（2026-08-21 复核）

- 状态：`resolved`；v30 已通过 service-backed migration/cleanup 完成运行态收敛，旧事实表已退役，`data/state.db` 空残留也已移入回收站。
- canonical：不可变决策、订单/持仓生命周期、review、监督 trace、counterfactual 和训练样本分别由 `canonical_v2` 事件/样本 writer 与 reader 唯一负责；生产 Python 已无旧事实表 SQL、旧样本表 DDL 或旧监督执行 writer。
- 已确认运行事实：`runtime.state_schema_migration` 已应用 v29/v30；`runtime.broker_execution_intent` 存在且当前无未决 intent；旧 `runtime` 事实表不存在。backend/learning worker 已重启并加载 `live_safety_plane_v2_mode=enforce`，cTrader fresh account/positions reconcile 成功，空仓且 `unknown_execution_count=0`。
- 处理边界：旧事实数据已按用户授权清理，不保留兼容查询/写入路径；canonical 事件与审计记录不删除。临时迁移 dump 不再保留。
- 剩余：仍需一次真实 Demo `tighten/reduce/close -> broker lifecycle -> fresh reconcile -> trace -> counterfactual -> maturity` 证明 supervisor 动作闭环；这不影响旧路径退役状态。

### position supervisor 失效确认链四断线（2026-08-26 修复）

- 状态：`resolved`（代码修复完成、测试绿，待重启加载与真实运行证据）。
- 问题事实：2026-08-26 复盘最近 10 笔仓位发现监督器"诊断正确但从未动手"。深挖确认四根结构性断线：① `signal_reversal` 只有读取方无生产者；② 开仓路径从不写 `entry_regime` → `regime_shift` 恒 none（29/29 笔实证）；③ 时间衰减证据需 timeout_ratio≥0.8 而实际持仓时长使其数学不可达；④ `thesis_broken_confirmations` 无递增者恒为 0。叠加 transition_confirming 姿态禁用主动动作，反事实链恒空，治理模板更新死循环。
- canonical：三个生产者全部落在既有模块——entry_regime 由 `_persist_pending_entry_protection_plan` 盖章（live_service.py）、signal_reversal 由监督器上下文构建处产生（live_position_lifecycle.py build_position_supervisor_context_payload）、thesis_broken_confirmations 由 path-metrics 状态机递增（position_metrics.py）。tighten 解锁在 evaluate_position_supervisor 动作仲裁内，仅限盈利单 + profit_protection_window_ready + giveback≥阈值。
- 已删除：无旧实现可删；删除的是"`signal_reversal`/`regime_shift`/`persistent_price_path` 是有效证据"的隐性假象。
- 验证：新增 tests/test_supervisor_confirmation_chain.py 11 项；监督域+治理域回归 229 passed 零退化。
- 剩余：重启加载后需 ≥10 笔带监督动作的真实仓位才能产出首批 counterfactual，届时本条 §23 的闭环证明才完整。


### learning_application_effect / learning_application_log 代码(宽) vs DB(精简) 双轨断开

- 状态：`resolved`（2026-08-18 完成；走退出条件 1，全代码收敛到精简 schema）
- canonical：权威 DDL（`backend/core/db.py` PG 分支）为精简 schema：
  - `learning_application_log`：application_id/run_id/source/status/details_json/created_at/updated_at
  - `learning_application_effect`：effect_id/application_id/scope/effect_json/created_at
- 收敛方式：新建唯一中心 store `backend/services/learning_application_store.py`
  （class `LearningApplicationStore`，PG==SQLite；含 `prepare_application/transition_application/
  get_application/latest_application/iter_applications/write_effect/update_effect/latest_effect/
  iter_effects/store_for_conn`）。该域全部写者+读者经 store 读写，不再手写引用已删宽列的 SQL；
  旧宽列字段全部并进 `details_json`/`effect_json` 保留语义。
- 覆盖范围：backend + research 全部该域访问点（write/read），含 factor_lifecycle_service、
  learning_application_state、stepper、entry_quality、position_supervisor(_templates/governance)、
  parameter_templates、factor_weight_change、proposal_registry、factor_cards、backend_readiness、
  v16_brain(orchestrator/planning/read-only brain)、autonomous_learning、autonomy_health、
  api/learning、api/ops、autonomous_evolution_runner/cycle、learning_experiment_admission、
  learning_effect_quality、experience_prior、factor_governance_effect_tracker、agent_scorecard、
  research/learning/governor、research/features/feature_provider；orchestrator
  `_rollback_failed_actions/_latest_posterior_effect/_factor_has_pending_effect/_mark_application_rolled_back`。
- 验证：全量回归 2813 passed；三测试
  `test_expansion_preflight_finds_fresh_builtin_activation` /
  `test_quarantine_review_acquits_frozen_factor_without_health_ok` /
  `test_prepared_discovered_gp_candidate_reaches_preflight_and_activation` 恢复绿。
- 遗留（本批无关、既有）：`test_state_store_schema_guard` 3 项（见下方新条目）。

### state_store_schema_guard 3 项失败（✅ resolved 2026-08-19）

- 状态：`resolved`（2026-08-19 P4 批修复；贴合既定契约修实现，非放宽测试）
- 现象：`tests/test_state_store_schema_guard.py` 3 项失败：
  `test_legacy_create_ensure_is_catalog_validation_only`（期望 2 次 SQL 调用实为 3，多出一条
  `CREATE TABLE runtime_kv`）、`test_legacy_create_ensure_fails_closed_on_missing_column`、
  `test_index_catalog_validation_checks_table_and_key_definition`（experience_memory 索引定义
  不匹配时未抛 `RuntimeStateSchemaMissingError`）。
- 根因与修复：`backend/core/state_store.py` 的
  `validate_runtime_state_schema`（验证后误执行 DDL）与 `_validate_runtime_schema_statement`
  （用 `except RuntimeStateSchemaMissingError: pass` 吞掉缺表/缺列/索引不匹配）在 S 阶段漂移出
  契约（纯目录校验 + fail-closed）。修复：删除 DDL 执行循环（迁移 CLI 是唯一 schema writer），
  删除两处吞错 `except...pass`（缺对象一律 fail-closed 抛 `RuntimeStateSchemaMissingError`）。
- 影响：仅为校验层对齐契约（生产建表本就走 `scripts/state_schema_migrate.py --apply` /
  `_ensure_pg_business_tables` 普通 psycopg，不受影响；PG 运行期若缺对象将显式报错而非静默）。
- 验证：`test_state_store_schema_guard.py` 21 passed（含原 3 失败）+ state_store 相邻集 98 passed/1 skipped + 全量回归见批次记录。

### RuntimeStateConnection DDL 拦截导致 ensure_* 函数在 PG 模式下无法建表

- 状态：`migrating`（2026-08-18；S7 冷启动修复）
- canonical：`RuntimeStateConnection.execute()` 拦截所有 DDL 语句（CREATE TABLE/INDEX/ALTER TABLE），调用 `validate_runtime_state_schema` 验证后返回假查询 `SELECT 1 WHERE FALSE`，DDL 不会真正到达 PostgreSQL。`_base_execute` 调用 `psycopg.Connection.execute(conn, query)` 时 psycopg3 内部通过 `conn.cursor()` 创建 `RuntimeStateCursor`，再次拦截 DDL → 无限递归/静默吞掉。
- 当前：S7 冷启动通过 `_ensure_pg_business_tables` 使用独立的普通 psycopg 连接（非 RuntimeStateConnection）绕过拦截层，成功创建 75 张业务表。`ensure_evolution_ledger_tables` 的 PG 分支也已改为普通 psycopg。但其他 `ensure_*` 函数（`ensure_autonomous_learning_tables` 等）在 PG 模式下仍直接 `return`，不创建表。
- 剩余：将所有 `ensure_*` 函数的 PG 分支改为使用普通 psycopg 连接建表，或在 `init_all` 中统一处理。`validate_runtime_state_schema` 的 DDL 执行功能（通过 `_base_execute`）实际上不生效，因为 `_base_execute` 本身也经过 RuntimeStateCursor 拦截。
- 退出：所有 `ensure_*` 函数在 PG 模式下能正确建表，或统一由 `init_all` 处理。
- 验证：冷启动后所有业务表存在且列/约束完整。

### 平行 authority、重复门控和无退出兼容层

- 状态：`migrating`（2026-08-18；P4 单轨写入完成，P5 DROP 因代码引用未完成而回滚）
- canonical：一个事实只有一个生产计算者和一个写入者；Safety、Risk、Readiness、API、前端不得平行重算同一授权事实。
- 当前：2026-08-10 已将账户/持仓 freshness blocker 收敛到 `live_reconciliation.evaluate_reconciliation_snapshot`，最终开仓 admission 与 readiness 复用同一结果；loop/readiness 只投影一个失败 blocker，`loop_status()` 不再通过读状态写入诊断事实。持仓对账、unknown execution、no-new-risk latch、generation 和 authority 校验仍保持独立 fail-closed。
- 剩余：Safety/Generation/Execution Outcome/Governance/PG Job Queue 仍有发布期开关或旧兼容；客户端仍有少量旧 fact 字段迁移。
- 退出：新路径通过各自运行门后，同批删除旧 authority、fallback、同义 blocker 和 pass-through wrapper。
- 验证：调用链、静态入口扫描、合同测试、运行 snapshot 与 `git diff --stat`。

### shadow/discovered/live 生命周期兼容

- 状态：`migrating`
- canonical：`factor_lifecycle_state` + `factor_runtime_projection` + Factor Card `factor_admission_evidence.v1`；ACTIVE 必须经 typed Coordinator/V16、稳定 artifact、fresh health、loaded ack、至少 20 个独立成熟干净证据和受控 observing effect，成熟正向真实 effect 前不得扩权。
- 当前：代码已使 legacy ACTIVE 缺完整准入证据时以 `legacy_evidence_incomplete` 排除选择，并由治理 owner 使用同 generation `demote_to_shadow`；context/gate 不投方向票，alpha 以 signed IC 校验方向。Evolution 只由 learning worker 在 `23,53` 运行，使用 `evolution_cycle_watermark.v1` 幂等 GP，并按 `QUANT_CANARY_EVALUATION_LIMIT` 背压；Backend 重任务注册和启动补偿已删除。
- 剩余：运行态尚需应用代码/迁移并观察遗留 ACTIVE 的真实排除与退回；切入 typed lifecycle 前的 native builtin fallback、领域服务 coordinator-off 隔离兼容和静态开关关闭兼容仍在。不得通过数据库回填 ACTIVE 或伪造 PIT/walk-forward/cost/lineage/effect 证据。
- 退出：现有 ACTIVE builtin 按 code-bound identity、V16、prepared、真实 loaded ack 和 fresh health 分批重入 lifecycle 后删除 builtin fallback；稳定 enforce 发布后删除领域服务的 generic restore 兼容。启动层的旧 template/supervisor/Registry restore 已删除；除六因子有界 Demo 经典种入外，不得用直接数据库回填 ACTIVE 绕过晋升证据。

### 历史 runtime overlay 缺少 committed mutation 绑定

- 状态：`quarantined`
- canonical：非空 mutation 必须是 committed/current 且 config/domain hash 完整绑定；空 mutation 只允许经 hash-bound operator review 恢复明确 risk tightening。
- 禁止：用来源名、默认值或“看起来保守”恢复扩张/未知 overlay。
- 退出：历史行逐项复核、重建或清理；确认 committed projection 后按 cause 身份释放 latch。

## 2. 执行与运行时

### JobManager 本地重任务兼容

- 状态：`migrating`
- canonical：PG Job Queue 开启后，八类重任务由 PostgreSQL durable job + 独立 worker 执行。
- 当前：静态开关默认关闭，job worker inactive；learning worker 是 evolution 重任务唯一生产 owner，Backend 的 evolution 注册和启动 catch-up 已删除。其他 flag-off 本地 executor 兼容仍待 PG queue 分期发布后退出。
- 退出：受控开启、lease/recovery 稳定发布后删除本地重任务执行路径。

### emergency close 严格完成语义

- 状态：`migrating`
- canonical：先持久化 no-new-risk latch；只有 fresh post-reconcile 确认目标 position ID 消失才算 completed。
- 已收口：`refresh_positions()` / `refresh_account_info()` 兼容入口已删除；安全、恢复和显示读取统一使用显式 reconcile 结果或其只读投影。
- 退出：所有安全/恢复调用只接受 immutable authoritative reconcile contract。

### broker unknown outcome fail-closed

- 状态：`migrating`
- canonical：结果只允许 `confirmed/rejected/unknown`；unknown 立即锁存、禁止重发，必须由 broker recovery/reconcile 唯一消解。单一 execution intent writer 和同一 broker executor 不依赖发布开关。
- 当前：intent prepare/submitting/confirmed/rejected/unknown 的故障矩阵代码合同已覆盖持久化、超时、未知回执和恢复边界，仍需当前源码绑定 attestation 与受控 Demo 真实生命周期。
- 退出：保留 unknown 事实语义；删除的 flag-off/空 intent/position-ID 猜测路径不得恢复。

### cTrader deal price 修复运行验收

- 状态：`migrating`
- canonical：executionPrice/entryPrice 保留 broker 原始价格；只有 money 字段按 moneyDigits 缩放。
- 已完成：1,150 条历史 deal 精确更正，污染学习、反事实和治理链已隔离或回滚。
- 剩余：新的 broker deal 与完整开仓—保护—平仓—同步—学习生命周期验收。
- 禁止：用固定金价阈值或猜测值补价格。

### live generation / Safety shadow 兼容

- 状态：`resolved`（2026-08-21 收口）
- canonical：`LiveLoopController` 是唯一 live loop generation/heartbeat owner；Safety 以 `live_safety_plane_v2_mode=enforce` 运行，监督动作只有 `supervisor -> RiskPolicy -> cTrader -> lifecycle -> fresh reconcile` 一条执行链。独立 legacy preview、双 candidate compare/fallback 和 Demo observation gate 已删除。
- 当前：旧 loop globals、并发 refresh 入口、legacy startup history pull 和旧 safety 尾部执行已删除；active `legacy_awe_trailing` candidate 在实时执行边界拒绝，旧 trace/close attribution/parity replay 仅诊断读取。重启后 PID 15764 的 backend 与 PID 7828 的 learning worker（本次 backend 二次重载后 worker PID 保持不变）均正常，cTrader 认证、空仓 fresh reconcile 和 `unknown_execution_count=0` 已验证。健康监控也已改为复用带 broker schedule 的同一 live market-session authority。
- 剩余：只剩一次真实 Demo 持仓生命周期，用于证明 broker lifecycle、trace、counterfactual 和 maturity 的正向证据；这不构成兼容执行路径。

### live_service 领域重力

- 状态：`migrating`
- canonical 模块：reconciliation、serial loop、emergency、position protection、open submission/protection/processing、execution recovery 已分离；fresh position reconcile 是既有 `recovery_position_state.recovery_meta.position_path` 的唯一 live 累计写入边界，event/API 投影不写入。
- 剩余：`live_service` 仍保留 process wiring、兼容状态发布和少量 lifecycle wiring；仓位路径持久化失败必须显式降级为 unknown，不得把单次观测伪装成累计 MFE/MAE。
- 验证：启动暖机优先使用 cTrader 在线历史，月初当月月库为空或 broker history 不可用时再通过 `bars_monthly_read_paths()` 回读最近历史闭合 bar；live bar freshness、风险和 readiness 以 online trendbar frame 为准，月库只作低频副本与离线兜底。
- 退出：只迁出真实决策/状态机；不为“拆文件”新增 wrapper。稳定发布后删除旧 globals 和 compatibility authority。

## 3. 治理、研究与客户端

### 因子扩张后验降级应用未完成

- 状态：`active`
- canonical：`FactorGovernanceOrchestrator._posterior_expansion_guard` + `posterior_expansion_verdict`，复用 `learning_application_effect` 的最新有效 factor effect；V16 delegate 粒度和既有后验阈值不变。
- 当前：因子扩张候选已统一经过 posterior preflight；`blocked_by_posterior` 会阻断，样本不足只标记 `posterior_degraded`，查询不确定时 fail-closed。
- 剩余：`posterior_degraded` 的受限权重/scope 应用路径尚未落地，不能把标记解释为已执行降级治理。
- 退出：降级应用经过现有 RiskPolicy、V16、Coordinator 和 effect observation 连续真实周期验证后，从本登记册删除。

### 治理 mutation 跨账本提交兼容

- 状态：`migrating`
- canonical：`GovernanceMutationCoordinator` 在同一 PG 事务内 reserve、重验 before、写 intent/领域事实、finalize；commit 后才发布 RuntimeConfig。
- 当前：mode 为 dual-record；旧 off 兼容与旧 ledger 投影仍在。只读 release preflight 已按 `error_stage=v16_claim` 与真实 transaction/recovery failure 分类 aborted intent，不改变状态机或应用证据要求。
- 退出：稳定 enforce 发布后删除旧 consume/direct overlay/Registry mutation 兼容。

### position supervisor 旧 advisory 冲突占位

- 状态：`migrating`
- canonical：持仓模板的自动切换只从 V16 candidate bridge 进入，`V16CommandGate.claim` 与 `PositionSupervisorGovernanceMutationService` 的 Coordinator transaction 共同完成单次授权和 finalize。
- 当前：历史/显式旧 worker 写入的 non-V16 `position_supervisor_template` advisory 仍可留作审计记录；它们已不再拥有 approve/apply 或 candidate conflict 权力，并将在既有 demo review/apply 路径中 terminalize。`supervisor_learning_scheduler` 只运行反事实证据，不自动 materialize 旧 advisory；显式 materialize API 仅作为 legacy audit 入口保留。新生成候选只能针对一个 control 和一个 regime stratum，完整快照必须能由 evidence 中的单 scalar patch 证明。V16 candidate bridge 已统一为 `active -> bridge_pending -> awaiting_execution -> applied/superseded/rejected`，bridge 事务同时绑定 candidate、suggestion 和 command predicate；`keep/no_change` 或当前模板目标不会进入候选 lane。
- 剩余：已应用 suggestion 仍需经过 effect observation 与既有 maturity counting，不能据此解锁自治；历史/显式旧 advisory 仍需按既有 review/apply 路径 terminalize，遗留 `submitted` 行需要通过 service-backed reconciliation 迁移到显式 pending/terminal 状态，禁止 SQL 直接恢复 active。`legacy_awe_trailing` 的 active planner、candidate writer、executor 和 live fallback 已删除/退役；旧 trace、close attribution 与 parity replay 仅保留诊断读取，不能替代 broker lifecycle 证据。
- 退出：历史 active advisory 全部 terminalize，连续真实 demo cycle 证明 V16 bridge、claim、Coordinator finalize 和 effect observation 连通后，删除旧 advisory 生成路径。AWE 执行分支的退出条件已满足；不得通过 SQL 改写历史 review、补 command 或补成熟样本提前满足其他治理退出条件。

### parity replay 尚非 live-equivalent

- 状态：`migrating`
- 当前：复用 closed-bar、RiskPolicy 与保护纯原语，并绑定 config/data/code/factor artifact hash，但缺 broker/tick/safety/account/cost/projection-ack 的完整 PIT 事实。
- 权限：固定 `diagnostic_only`、治理数量为零；runner 永不自授权。
- 退出：只有独立 certification 重验完整 live lifecycle 后才能讨论 live-parity evidence。

### Tauri/React 前端替换

- 状态：migrating
- canonical：web_frontend 内的 Tauri 2 + React 19 renderer；服务器端 API、fact.v1、
  /ws/state、认证和 mutation contract 继续作为唯一权威。
- 当前：新五工作区和直接废弃路由已经落在 React 19 + Vite/Tauri renderer；Workbench Shell、Safety
  rail、唯一 `/ws/state`、强类型 endpoint decoder、IndexedDB 研究缓存、Tauri 2 壳和
  signed NSIS updater artifact 均已落地。旧页面、旧 AppShell、`src/lib/compat.ts`、旧 route alias、
  旧页面绑定 accessibility 样式和关键 endpoint 的宽泛 decoder 已删除。`/api/market/bars`
  已补齐 `market.bars.v1`，缺数据明确返回 unknown；默认月库路径保持兼容，交易页的
  `source=live` 只读 cTrader trendbar 内存 feed，不把月库作为实时替代；本次 live 投影尚未随远程服务重载，当前远端仍可能返回 `bars_monthly`，需下一次后端发布后完成运行验收。2026-08-13 的 static artifact
  切换到公网 Caddy 根目录属于历史验证；当前迁移要求撤下浏览器静态入口、服务器只保留
  API/WSS 与后端工作树；2026-08-14 已完成 sparse checkout、blob 过滤、Caddy API/WSS-only
  和前端产物清理。API 合同补丁已部署并重启验证，旧 dist 和 API pre-change
  文件已留存仓库外 rollback archive；factor cards 已改为优先复用最新持久 catalog
  snapshot，远程 44 个 factor-card 测试通过。
- 替代：Workbench Shell、Trade Ops、Risk Desk、Research Lab、Governance、Ops
  五个工作区、全局 Safety rail、强类型 endpoint decoder、唯一 live store 和
  IndexedDB 研究只读缓存。
- 剩余：代码层离线读取缓存、动作禁用、Credential Manager bridge 和 updater wiring
  已实现，但真实 Tauri 断网恢复、缓存 hash/schema、Windows 安装卸载、WebView2
  缺失路径、GitHub Actions Secret/manifest（workflow 已准备，Secret 未配置）、签名成功/失败回退、Linux API/WS/auth 全量
  运行验证和完整验收仍未完成。
- 退出：五个工作区通过 frontend-refactor-acceptance-matrix.md，生产入口一次性
  切换，新旧 route 不再并存，旧页面/fallback/import/宽泛类型删除，签名包和
  updater 回退通过；回滚使用 commit/artifact，不恢复长期旧地址别名。

### API/frontend 旧事实字段

- 状态：`migrating`
- canonical：endpoint-specific `fact.v1`；unknown/stale/error 不得显示绿色或授权 start/unlock，最后 known 值可带时间保留。
- 剩余：Web/小程序 recursive compat 和旧字段窗口。
- 退出：客户端迁移完成且满足两个小程序版本或 30 天取更长者，删除旧回退。

### legacy auth 路径

- 状态：`migrating`
- canonical：Argon2id、24 小时 access、旋转 refresh session、单次 WS ticket、扩张 step-up、durable revocation。
- 剩余：SHA-256、legacy access、URL JWT 三个显式兼容开关。
- 退出：全部客户端迁移后关闭并删除；stop/emergency 的本地可验证风险缩减能力不得受 PG 故障阻断。

## 4. 明确退役，禁止恢复

- 旧 PostgreSQL `state_v1` schema 及其数据；生产只使用 `runtime` 与 `canonical_v2`；
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
