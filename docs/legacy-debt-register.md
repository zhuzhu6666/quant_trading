# Active Legacy Debt Register

> Status: active
> Last verified: 2026-08-18
> Scope: 只登记尚未退出的兼容、重复 authority、隔离数据和回归。

已完成旧债不在本文保留；Git 历史和测试是追溯依据。新增条目必须写清 canonical 路径、剩余旧路径、退出条件和验证。

## 1. 全局收敛

### state_v1 多表 payload 重复与 canonical_v2 重建

- 状态：`complete`（2026-08-18；**全库清空已完成**：state_v1 86 表已 DROP、public 4 表已 DROP、canonical_v2 数据已 TRUNCATE + legacy_mapping 已 DROP；运行骨架迁入 runtime schema）
- canonical：`canonical_v2` 9 表空结构；`runtime` schema 6 表（overlay/snapshot/jobs/auth/kv/migration）。
- 当前：migration 16 和隔离 writer 初版已建立；尚未进行历史 backfill、生产切换或旧 projection 退役。现有 `mutation_payload`、`evolution_events`、`evolution_run`、`governance_mutation_intent`、`brain_memory` 和 factor catalog 仍由旧路径解释。只读审计确认旧 `position_supervisor_trace` 存在递归 `latest_supervisor` payload；新写入边界已修复并由 `tests/test_supervisor_trace_writer_bounds.py` 覆盖，历史行已在本批 archive。2026-08-15 supervisor/review repair manifest 已生成；2026-08-16 已将当前 31 个 supervisor/review 直接读取路径切换到 verified archive loader，静态 coverage 为 `31 migrated / 0 pending`。同日 vertical backfill planner 扫描 16,791 decision、1,357 order、2,956 position、721 review、58,632 sample，并以数据库 `EXISTS` 校验 sample source 引用，quarantine 为 0，mapping digest 为 `ffe1194e39e2a1b80355c700df42d884ce5fd1c0dd15a8ffb62bcc8d94161257`；fresh full compact dry-run 又确认 `brain_action_plan_eval` 为 141,514 行、138,312 条重复 payload 引用行，三域逻辑重复下界约 17.68GB。2026-08-16（当日）用户确认后已完成并验证：payload 三域 apply（14597/141514/44734 行全量 payload_hash + inline 置空，去重 3582/3202/35787 与 dry-run 一致）与 supervisor/review apply（47768/721 行全量 archive，无行删除/合并），`audit_double_write` 5,927 conflicts / 8,489 unmatched 已分类（5926 条为空 config_hash 写边界问题、8489 条未关联），lineage 收敛 88 行（api_canonical→/api→api_unmatched），两批 verify 均 `ok`（payload ref/review archive 缺失、SHA-256、archive metadata、semantic 差异全 0）。`scripts/state_payload_compact.py` PG lineage 回写因运行态 schema 守卫已改为纯 DML executemany（不建临时表）。仍未执行：物理 `--rewrite`、历史 backfill apply、canonical 垂直写入、投影重建、切换退役。
- 剩余：Phase 6 shadow read 已完成；批次 2/3a/4 已完成。**2026-08-17 C1 单轨完成**：13 模块全部删 legacy 兜底分支，测试适配 + 全量回归 2806 passed / 0 failed。**同日 P2 边界表事件化**：supervisor_counterfactual_review（576）/ position_supervisor_trace（48,319）/ ctrader_deals（1,446）→ canonical 事件 + 映射（migration 0018 扩展 event_type CHECK 约束）；backfill 脚本 `scripts/canonical_v2_boundary_backfill.py`。**P1 样本域数据对齐**：canonical training_sample_row 58,640/58,640 全覆盖（8 条 mirror 缺口已补），reader 函数已创建（`iter_training_sample_rows` / `get_training_sample_row`），SQL 切换待做。**⏳ 待做**：decision_factor_snapshot（4M 行 1.17GB）退役改推导、P4 写者切换、P5 旧库退役、P6 容量治理。详见 [`planning/architecture-audit-2026-08-18.md`](planning/architecture-audit-2026-08-18.md) 与 [`planning/final-execution-checklist.md`](planning/final-execution-checklist.md)。：governance/evolution 事件域 canonical 化（`scripts/canonical_v2_governance_backfill.py`：mutation 2,814→8,177 governance_effect 按 lifecycle stage、evolution 44,796→44,796 governance_command + 722 caused_by 边、config payload 池 14,597；plan_digest `63c36769…` dry-run/apply/幂等重跑三一致；115 条无 run 链 mutation quarantine）、state_version 回填（`scripts/canonical_v2_state_version_backfill.py`：2,556 行 + 12,041 无治理生产者事件 quarantine，digest `b932c6f1…`，幂等重跑零新增）、`decision_factor_snapshot` PIT 决策=不纳入 canonical（对账 13,543/13,543 + 7 模块 98 项消费端回归）、dataset manifest（`scripts/canonical_v2_dataset_manifest.py`：`ds_entry_supervisor_feedback_reference_20260816` + 620 member，verify 0 mismatch、重跑可复现）。修复 `canonical_v2._same_value` 时区比较 bug（UTC 归一化，回归测试覆盖）。`evolution_events`(9,700) 按 occurrence/run lineage 保留 legacy；position-quality 106 样本 manifest 另立项。**同日 A1（`autonomous_learning.py` 17 处读取迁移）完成**：review/decision/order 读取窗全部切 canonical reader（索引预载 + 拆 join + 有界时间窗反向 keyset），新增只读探针 `scripts/canonical_v2_autonomous_learning_equivalence.py`（9 窗口 ok），依赖模块 180 passed；`position_supervisor_trace`/`supervisor_counterfactual_review`/`ctrader_deals`/`decision_factor_snapshot` 保持直读，无新增写入。**同日 A2/A3（live 热路径 + ledger 决策查询）完成**：用户拍板投影物化=文件（`scripts/canonical_v2_position_decision_index.py` → `run_artifacts/canonical_v2_position_decision_index.json`，679 条，幂等 digest `781cd144…`，可删除重建）；`live_service` 4 处（entry_decision_id/open_decision_context 走索引、supervisor 窗口有界扫描、same-bar dedup ±5s 窗口）与 `ledger.get_latest_entry_decision`（索引→decision_row）迁移，探针 `scripts/canonical_v2_live_service_equivalence.py` 5 窗口 2100 例 ok；`test_live_service_bar_dedup` 更新 canonical 契约；未命中/异常回退 legacy（批次 5 前保留），投影重建时机列入批次 5 部署事项。下一步为 E1 服务运行验证（✅ 2026-08-16 完成：双服务运行 >1h、API/WSS/live loop/worker 周期正常、无 canonical 错误、consistency 全量 ok；观察项 `awe_adapt ... v16_command_required` 为设计内治理防护）、E2 全量测试、批次 5 单轨（C1 删 fallback → C2 红蓝观察 → C3–C5）与 D 组物理操作（逐项确认）。服务已按授权启动并保持运行（C2 红蓝观察沿用当前窗口）。下一次接手顺序以 [`planning/final-execution-checklist.md`](planning/final-execution-checklist.md) 为准。**2026-08-17（P0 写侧迁移 + 样本域 + 单轨开始）**：① 增量写入管道上线并生效——`canonical_v2` 新增 `record_decision_event / record_order_event / record_position_event / record_governance_mutation_event / record_governance_command_event / record_sample_row`，`DecisionLedger`（决策/订单/仓位）、`governance_mutation_coordinator`（reserved/committed/aborted/rolled_back/superseded 5 阶段）、`evolution_ledger`（进化命令+配置载荷池）、`autonomous_learning._upsert_sample`（样本行）同事务幂等镜像（fail-open，legacy 单写入者不变，非双写）；② 增量回填管道（`--since-epoch`）与 `scripts/canonical_v2_live_reconcile.py` 对账；③ 样本域 P1：迁移 0017（`canonical_v2.training_sample_row` 30 列）apply，`scripts/canonical_v2_sample_backfill.py` 全量 58,632/58,632 对账 0 缺失，学习任务样本写入实时镜像；④ C1 单轨开始：删除 5 模块 fallback（factor_cards / factor_counter_evidence / trade_lesson_memory / live_reentry_guard / memory_integrity），测试适配，全量回归 0 失败（agent_scorecard 守卫保留双条件，随旧表删除收敛）；⑤ 体检（运行 13h43m）发现并修复：live.increment.v1 运行记录 source_watermark 冲突（142 次镜像失败，fail-open 保护交易）→ 统一参数 + live.increment.v2；71 幽灵事件补映射（`scripts/canonical_v2_repair_ghost_mappings.py`）；15+42 双写清理（回填与实时镜像幂等键不同 → 回填加跳过 live 已镜像守卫，producer IN 三 live 生产者）；worker 随重启加载新代码。验证：六域对账全绿、90,923 payload 一致性 0 失败、全量回归 2803/0。剩余：P3 余 8 模块约 35 处、P2 边界 4 域、P4 写者切换、P5 旧库退役（G1–G7 逐组确认）、P6 容量治理（分区/保留/归档/监控）。
- 退出：已满足。全库清空重建已完成，旧 v1 表已删除，系统以全空库冷启动。S7 冷启动完成：STATE_SCHEMA 已改为 runtime，75 张业务表已重建，双服务 active。
- 验证：`docs/planning/final-execution-checklist.md` S7.1 完成门。

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
- 剩余：非 safety 调用仍可能使用 legacy `refresh_positions()` 值接口。
- 退出：所有安全/恢复调用只接受 immutable authoritative reconcile contract。

### broker unknown outcome 兼容

- 状态：`migrating`
- canonical：结果只允许 confirmed/rejected/unknown/simulated；unknown 立即锁存、禁止重发，必须由 broker recovery/reconcile 唯一消解。
- 当前：Execution Outcome v2 静态开关默认关闭；关闭分支现在显式返回 `execution_intent_status=compat_missing_intent`，不再用空 ID 冒充完整追踪。故障矩阵代码合同已覆盖 intent prepare/submitting/complete/unknown 和恢复边界，仍需当前源码绑定 attestation 与受控 Demo 真实生命周期。
- 退出：通过发布门后删除 position-ID 猜测和旧 result 兼容；unknown 语义永久保留。

### cTrader deal price 修复运行验收

- 状态：`migrating`
- canonical：executionPrice/entryPrice 保留 broker 原始价格；只有 money 字段按 moneyDigits 缩放。
- 已完成：1,150 条历史 deal 精确更正，污染学习、反事实和治理链已隔离或回滚。
- 剩余：新的 broker deal 与完整开仓—保护—平仓—同步—学习生命周期验收。
- 禁止：用固定金价阈值或猜测值补价格。

### live generation / Safety shadow 兼容

- 状态：`migrating`
- canonical：旧线程真实退出前保留 ownership；每 tick 由现有 serial owner 按 positions reconcile -> account reconcile/publish -> Safety -> alpha 排序，Safety v2 与独立 legacy preview 比较。broker fresh snapshot 与 active recovery row 不一致时，先复用既有 close-deal retirement/recovery 投影做一次有界确认，证据不足仍保持冲突 latch。已通过门的同一 closed bar 仅在 watchdog 自有 freshness cause 短暂锁存时由现有 serial owner 保留一次内存 admission retry，下一轮 canonical safety/reconcile 后复用原 open pipeline；bar 推进或出现其他 cause 立即丢弃，不新增执行通道。
- 当前：2026-08-13 已完成代码、针对性测试并完成受控重启前的验证；`safety_freshness` 仍按既有 shadow 观察，尚未满足完整持仓生命周期或 24 小时无仓观察；Generation 开关不变。live-loop 的并发 account/positions refresh 已删除，开仓 admission 不再执行同 tick 二次 reconcile；同一 tick 的账户对账现在复用持仓对账的已知 PnL，不再发重复 PnL RPC；loop 完成时间不再被 tick 开始阶段冒充。`make_initial_ctrader_data_pull` 并行历史 K 线 writer 已删除；cTrader live trendbar 内存 feed 仍是 live bar authority，月库为低频 durable replica。
- 退出：重启后确认无 legacy startup history pull，完成共享 deadline/阶段耗时验证，观察与故障矩阵通过、受控发布稳定后删除 loop globals 和旧 safety 尾部执行。

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
- 剩余：已应用 suggestion 仍需经过 effect observation 与既有 maturity counting，不能据此解锁自治或删除旧 advisory writer。`legacy_awe_trailing` 的非 Demo 兼容 planner/trace/close attribution 仍存在；Demo 已在 protection cycle 中标记 `observed/superseded`，不得与 canonical supervisor 同时 applied。Parity replay 仍是 diagnostic-only，不能替代 broker lifecycle 证据。遗留 `submitted` 行需要通过 service-backed reconciliation 迁移到显式 pending/terminal 状态，禁止 SQL 直接恢复 active。
- 退出：历史 active advisory 全部 terminalize，连续真实 demo cycle 证明 V16 bridge、claim、Coordinator finalize 和 effect observation 连通后，删除旧 advisory 生成路径；另在 replay、trace、effect 证明 trailing 行为等价后，删除 legacy AWE trailing 执行分支、兼容配置和不再需要的耦合测试。不得通过 SQL 改写历史 review、补 command 或补成熟样本提前满足退出条件。

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
  已补齐 `market.bars.v1`，缺数据明确返回 unknown。2026-08-13 的 static artifact
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
