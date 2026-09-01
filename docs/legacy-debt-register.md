1:# Active Legacy Debt Register
2:
3:> Status: active
4:> Last verified: 2026-09-01 (代码层复核：live_service 12840行/9次catalog/posterior_degraded 仅标记；5项假债已关；代码基线 05636ac)
5:> Scope: 只登记尚未退出的兼容、重复 authority、隔离数据和回归。
6:
7:已完成旧债不在本文保留；Git 历史和测试是追溯依据。新增条目必须写清 canonical 路径、剩余旧路径、退出条件和验证。
8:
9:## 1. 全局收敛
10:
11:### 旧状态 schema 清理（历史记录）
12:
13:- 状态：`complete`；旧 PostgreSQL `state_v1` schema 与其数据已清空，生产不再读取、写入或重建该 schema。
14:- canonical：运行态使用 `runtime`，不可变事件、生命周期事实和学习样本使用 `canonical_v2`。
15:- 当前：本条只保留清库和命名迁移的审计背景；不得把历史 migration/backfill/legacy projection 叙述当作当前运行事实。
16:
17:### runtime 退役事实投影与 broker intent schema（2026-08-21 复核）
18:
19:- 状态：`resolved`；v30 已通过 service-backed migration/cleanup 完成运行态收敛，旧事实表已退役，`data/state.db` 空残留也已移入回收站。
20:- canonical：不可变决策、订单/持仓生命周期、review、监督 trace、counterfactual 和训练样本分别由 `canonical_v2` 事件/样本 writer 与 reader 唯一负责；生产 Python 已无旧事实表 SQL、旧样本表 DDL 或旧监督执行 writer。
21:- 已确认运行事实：`runtime.state_schema_migration` 已应用 v29/v30；`runtime.broker_execution_intent` 存在且当前无未决 intent；旧 `runtime` 事实表不存在。backend/learning worker 已重启并加载 `live_safety_plane_v2_mode=enforce`，cTrader fresh account/positions reconcile 成功，空仓且 `unknown_execution_count=0`。
22:- 处理边界：旧事实数据已按用户授权清理，不保留兼容查询/写入路径；canonical 事件与审计记录不删除。临时迁移 dump 不再保留。
23:- 剩余：首批真实 supervisor close 已完成 lifecycle、trace、counterfactual 和 maturity 的部分闭环，但仍需达到治理验收样本量并覆盖 `tighten/reduce`；这不影响旧路径退役状态。
24:
25:### position supervisor 失效确认链四断线（2026-08-26 修复）
26:
27:- 状态：`resolved`（代码修复、测试、重启加载和首批真实运行证据已完成；治理扩权仍未完成）。
28:- 问题事实：2026-08-26 复盘最近 10 笔仓位发现监督器"诊断正确但从未动手"。深挖确认四根结构性断线：① `signal_reversal` 只有读取方无生产者；② 开仓路径从不写 `entry_regime` → `regime_shift` 恒 none（29/29 笔实证）；③ 时间衰减证据需 timeout_ratio≥0.8 而实际持仓时长使其数学不可达；④ `thesis_broken_confirmations` 无递增者恒为 0。叠加 transition_confirming 姿态禁用主动动作，反事实链恒空，治理模板更新死循环。
29:- canonical：三个生产者全部落在既有模块——entry_regime 由 `_persist_pending_entry_protection_plan` 盖章（live_service.py）、signal_reversal 由监督器上下文构建处产生（live_position_lifecycle.py build_position_supervisor_context_payload）、thesis_broken_confirmations 由 path-metrics 状态机递增（position_metrics.py）。tighten 解锁在 evaluate_position_supervisor 动作仲裁内，仅限盈利单 + profit_protection_window_ready + giveback≥阈值。
30:- 已删除：无旧实现可删；删除的是"`signal_reversal`/`regime_shift`/`persistent_price_path` 是有效证据"的隐性假象。
31:- 验证：新增 tests/test_supervisor_confirmation_chain.py 11 项；监督域+治理域回归 229 passed 零退化。
32:- 运行证据：当前已有 15 条逻辑 supervisor trace，其中 8 条 `executed/applied`、5 条 deferred、2 条 failed；已有 5 条 full/matured supervisor trace，仍不能把失败或污染样本计入治理。
33:- 剩余：仍需 ≥10 笔合格真实仓位、更多 `tighten/reduce` 覆盖和连续效果观察，才能完成模板治理闭环。
34:
35:### 单仓 supervisor template 开仓绑定（代码与重启验收已完成，真实生命周期证据待收口）
36:
37:- 状态：`monitoring`（2026-09-01 代码层复核：live_service 已写 `position_supervisor_binding`/`recovery_meta_json` 并 `verify_position_supervisor_binding`，重启后 `NRestarts=0`；等一次真开/重启/平仓闭环后 resolved）
38:- canonical：`position_supervisor_binding.v1` 由 live open path 在成交前绑定，保存于现有
39:  `entry_protection_plan.supervisor_binding` 和 `recovery_position_state.recovery_meta_json`；监督计算仍由
40:  `PositionSupervisor` 唯一负责，风险裁决仍由 `RiskPolicyService` 负责。
41:- 当前：新仓位保存完整规范化 template snapshot、version、hash、source、selection key 和 evidence refs；
42:  重启/恢复会校验 hash。旧仓位只标记 `legacy_global_fallback`，损坏或未知 binding 进入
43:  `unknown/hold`，硬风险仍可收口。全局 `position_supervisor_template_id` 只作为新仓位基线，不改写已绑定仓位。
44:  2026-08-27 双服务受控重启后，backend/worker 均 `active/running` 且 `NRestarts=0`，既有 learning
45:  周期真实发布 `position_supervisor_selection.v1`；当前状态 `insufficient_evidence`、候选 `0`、自动模式
46:  `off`，本次选择链没有发生 broker mutation。
47:  2026-08-28 只读复核：最新持仓 `285427255` 的 `recovery_position_state.recovery_meta_json` 已验证 `entry_regime=trend=weak|vol=low`、`selection_key`、`supervisor_binding.template_hash=cdfe2bf...`、`thesis_broken_confirmations=136`、`signal_reversal`、`current_regime` 三生产者均有值；`live_position_lifecycle.build_position_supervisor_context_payload` 与 `position_metrics` 状态机打通。`recovery_position_state 61 行` 最新 3 笔均携带完整 binding。
48:- 退出：完成至少一次真实 open/restart/recovery/close lineage 验证，并证明所有新 supervisor trace 都能
49:  回溯 binding；不得新增第二个 supervisor writer、表或调度器。
50:
51:### supervisor 经验已进入记忆索引，但自动模板准入仍未达标
52:
53:- 状态：`monitoring`（2026-09-01 代码层复核：阈值已达 28 eligible / 36 matured / trace 100，0f1521f 漏 `supervisor_counterfactual_review` 已由 05636ac 补齐；等下轮 bridge）
54:- canonical：原始事实由 `canonical_v2.supervisor_trace/counterfactual_review` 承载，学习资格由 `canonical_v2.training_sample_row` 承载，经验检索使用 `experience_memory`，V16 检索/后验使用 `brain_memory` 和 `posterior_arbitration`。
55:- 当前（2026-08-28 只读）：`canonical_v2.training_sample_row` 10294 行，`supervisor_execution_trace 9369` 中 `governance_eligible=1 & matured=5`（阈值 ≥10，仅 5 笔：另 4 笔 `full/matured/not_eligible` 等待治理），`pending 2085 + excluded 6598` 占 93%；`canonical_v2.event supervisor_trace 15`（近 2 天 8 笔），`counterfactual_review 33`；`brain_memory 160`（counterfactual 3/posterior 1/semantic 116）、`experience_memory 62` 已入；`position_supervisor_selection.v1` 仍 `insufficient_evidence/candidate_count=0/fresh`，`brain_governance_candidate_review bridge_ready 8/517`，`learning_application_log 21 (observing 9/inconclusive 8/reinforced 3)` 无 supervisor application。自动开启代码已加载，记忆仍只能供检索和审查。
56:- 自动开启：`off` 仅是无证据时的安全基线；证据投影达到资格后，由 learning worker 自动经 V16、RiskPolicy 和 Coordinator 切入有界 Demo，不需要人工再改一个模式开关。单条 brain memory、提案或未成熟后验仍不能直接授权。
57:- 退出：`≥10 笔 governance_eligible matured supervisor_execution_trace` + `tighten/reduce` 覆盖 + 候选 review、V16/Coordinator application、effect observation 和 rollback 连续可追溯；selection projection 新鲜且可解释；任何单条记忆不得直接改模板或放大交易权限。
58:
59:
60:### learning_application_effect / learning_application_log 代码(宽) vs DB(精简) 双轨断开
61:
62:- 状态：`resolved`（2026-08-18 完成；走退出条件 1，全代码收敛到精简 schema）
63:- canonical：权威 DDL（`backend/core/db.py` PG 分支）为精简 schema：
64:  - `learning_application_log`：application_id/run_id/source/status/details_json/created_at/updated_at
65:  - `learning_application_effect`：effect_id/application_id/scope/effect_json/created_at
66:- 收敛方式：新建唯一中心 store `backend/services/learning_application_store.py`
67:  （class `LearningApplicationStore`，PG==SQLite；含 `prepare_application/transition_application/
68:  get_application/latest_application/iter_applications/write_effect/update_effect/latest_effect/
69:  iter_effects/store_for_conn`）。该域全部写者+读者经 store 读写，不再手写引用已删宽列的 SQL；
70:  旧宽列字段全部并进 `details_json`/`effect_json` 保留语义。
71:- 覆盖范围：backend + research 全部该域访问点（write/read），含 factor_lifecycle_service、
72:  learning_application_state、stepper、entry_quality、position_supervisor(_templates/governance)、
73:  parameter_templates、factor_weight_change、proposal_registry、factor_cards、backend_readiness、
74:  v16_brain(orchestrator/planning/read-only brain)、autonomous_learning、autonomy_health、
75:  api/learning、api/ops、autonomous_evolution_runner/cycle、learning_experiment_admission、
76:  learning_effect_quality、experience_prior、factor_governance_effect_tracker、agent_scorecard、
77:  research/learning/governor、research/features/feature_provider；orchestrator
78:  `_rollback_failed_actions/_latest_posterior_effect/_factor_has_pending_effect/_mark_application_rolled_back`。
79:- 验证：全量回归 2813 passed；三测试
80:  `test_expansion_preflight_finds_fresh_builtin_activation` /
81:  `test_quarantine_review_acquits_frozen_factor_without_health_ok` /
82:  `test_prepared_discovered_gp_candidate_reaches_preflight_and_activation` 恢复绿。
83:- 遗留（本批无关、既有）：`test_state_store_schema_guard` 3 项（见下方新条目）。
84:
85:### state_store_schema_guard 3 项失败（✅ resolved 2026-08-19）
86:
87:- 状态：`resolved`（2026-08-19 P4 批修复；贴合既定契约修实现，非放宽测试）
88:- 现象：`tests/test_state_store_schema_guard.py` 3 项失败：
89:  `test_legacy_create_ensure_is_catalog_validation_only`（期望 2 次 SQL 调用实为 3，多出一条
90:  `CREATE TABLE runtime_kv`）、`test_legacy_create_ensure_fails_closed_on_missing_column`、
91:  `test_index_catalog_validation_checks_table_and_key_definition`（experience_memory 索引定义
92:  不匹配时未抛 `RuntimeStateSchemaMissingError`）。
93:- 根因与修复：`backend/core/state_store.py` 的
94:  `validate_runtime_state_schema`（验证后误执行 DDL）与 `_validate_runtime_schema_statement`
95:  （用 `except RuntimeStateSchemaMissingError: pass` 吞掉缺表/缺列/索引不匹配）在 S 阶段漂移出
96:  契约（纯目录校验 + fail-closed）。修复：删除 DDL 执行循环（迁移 CLI 是唯一 schema writer），
97:  删除两处吞错 `except...pass`（缺对象一律 fail-closed 抛 `RuntimeStateSchemaMissingError`）。
98:- 影响：当批先恢复了校验层 fail-closed；2026-08-29 后续收敛又删除了普通 psycopg
99:  `_ensure_pg_business_tables` 旁路，所有 PG DDL 统一只走 `scripts/state_schema_migrate.py --apply`。
100:- 验证：`test_state_store_schema_guard.py` 21 passed（含原 3 失败）+ state_store 相邻集 98 passed/1 skipped + 全量回归见批次记录。
101:
102:### RuntimeStateConnection DDL 拦截导致 ensure_* 函数在 PG 模式下无法建表
103:
104:- 状态：`resolved`（2026-08-28 P1 批：PG 分支改为显式 fail-closed 校验）
105:- canonical：`RuntimeStateConnection` 与业务 `ensure_*` 只校验不建表；`StateMigrationConnection` + `migrations/state_pg` + `scripts/state_schema_migrate.py --apply` 是唯一 PG schema writer。
106:- 当前（2026-08-29）：运行期 `_ensure_pg_business_tables`、`_PG_BUSINESS_TABLES_DDL` 及启动调用已删除；`ensure_evolution_ledger_tables` 只读校验 migration ledger，其他已迁移 ensure 路径继续做 catalog validation。空库初始化已进入同一 migration runner 的 `bootstrap_legacy_baseline.sql`，CI 不再维护第二份 Python baseline。
107:- 退出：已收敛，`resolved`；后续新增/修改表只能新增 versioned migration，业务 ensure 不得写 PG schema。
108:
109:### 平行 authority、重复门控和无退出兼容层
110:
111:- 状态：`migrating`（2026-08-18；P4 单轨写入完成，P5 DROP 因代码引用未完成而回滚）
112:- canonical：一个事实只有一个生产计算者和一个写入者；Safety、Risk、Readiness、API、前端不得平行重算同一授权事实。
113:- 当前：2026-08-10 已将账户/持仓 freshness blocker 收敛到 `live_reconciliation.evaluate_reconciliation_snapshot`，最终开仓 admission 与 readiness 复用同一结果；loop/readiness 只投影一个失败 blocker，`loop_status()` 不再通过读状态写入诊断事实。持仓对账、unknown execution、no-new-risk latch、generation 和 authority 校验仍保持独立 fail-closed。
114:- 剩余：Safety/Generation/Execution Outcome/Governance/PG Job Queue 仍有发布期开关或旧兼容；客户端仍有少量旧 fact 字段迁移。
115:- 退出：新路径通过各自运行门后，同批删除旧 authority、fallback、同义 blocker 和 pass-through wrapper。
116:- 验证：调用链、静态入口扫描、合同测试、运行 snapshot 与 `git diff --stat`。
117:
118:### shadow/discovered/live 生命周期兼容
119:
120:- 状态：`migrating`
121:- canonical：`factor_lifecycle_state` + `factor_runtime_projection` + Factor Card `factor_admission_evidence.v1`；ACTIVE 必须经 typed Coordinator/V16、稳定 artifact、fresh health、loaded ack、至少 20 个独立成熟干净证据和受控 observing effect，成熟正向真实 effect 前不得扩权。
122:- 当前：代码已使 legacy ACTIVE 缺完整准入证据时以 `legacy_evidence_incomplete` 排除选择，并由治理 owner 使用同 generation `demote_to_shadow`；context/gate 不投方向票，alpha 以 signed IC 校验方向。Evolution 只由 learning worker 在 `23,53` 运行，使用 `evolution_cycle_watermark.v1` 幂等 GP，并按 `QUANT_CANARY_EVALUATION_LIMIT` 背压；Backend 重任务注册和启动补偿已删除。
123:- 剩余：运行态尚需应用代码/迁移并观察遗留 ACTIVE 的真实排除与退回；切入 typed lifecycle 前的 native builtin fallback、领域服务 coordinator-off 隔离兼容和静态开关关闭兼容仍在。不得通过数据库回填 ACTIVE 或伪造 PIT/walk-forward/cost/lineage/effect 证据。
124:- 退出：现有 ACTIVE builtin 按 code-bound identity、V16、prepared、真实 loaded ack 和 fresh health 分批重入 lifecycle 后删除 builtin fallback；稳定 enforce 发布后删除领域服务的 generic restore 兼容。启动层的旧 template/supervisor/Registry restore 已删除；除六因子有界 Demo 经典种入外，不得用直接数据库回填 ACTIVE 绕过晋升证据。
125:
126:### 历史 runtime overlay 缺少 committed mutation 绑定
127:
128:- 状态：`resolved`（2026-08-28 只读：`runtime_config_overlay` 已无空 `mutation_id` 行）
129:- canonical：非空 mutation 必须是 committed/current 且 config/domain hash 完整绑定；空 mutation 只允许经 hash-bound operator review 恢复明确 risk tightening。
130:- 当前（2026-08-29 清理后）：`runtime.runtime_config_overlay 1 行（autonomous_factor_governance / committed mutation）` 零空 `mutation_id`；`runtime_config_snapshot 2,076 行`（其中 1,284 条带 mutation、792 条空 mutation，版本 8~3,445）只保留每段 hash 的末行及所有被引用审计事实，均非 live overlay，不阻塞 `governance_authority`。清理后的停盘误触发 catch-up 又产生了 9 条有真实 mutation 的 shadow 因子快照，属于治理/生命周期事实而非垃圾，已保留。`RuntimeConfigOverlayService.review_legacy_quarantine` 仍为唯一空行修复入口。
131:- 禁止：用来源名、默认值或“看起来保守”恢复扩张/未知 overlay。
132:
133:### 配置镜像膨胀与停盘重复学习（2026-08-29）
134:
135:- 状态：`resolved`（代码提交 0f1e8f0；受控重启和最终回放证据已收口）。
136:- canonical：有效配置载荷唯一保留在 `runtime.runtime_config_payload`，快照由
137:  `runtime.runtime_config_snapshot` 负责版本/回滚；canonical 事件、状态、训练样本和治理账本继续保留原始事实。
138:- 已删除：`canonical_v2.payload_blob` 中无任何 `event/state_version` 引用、且代码无读取者的
139:  `runtime_config_version` 镜像 3,428 行（原始 1,558,384,837 bytes），以及 6 条已核实的孤立测试载荷
140:  （1 条 `evolution_decision`、5 条 `supervisor_trace`，原始 36,837 bytes）；另删
141:  `runtime_config_snapshot` 中 1,324 条连续同 hash、空 mutation、无业务引用的重复行。
142:  终验又删 37 条同 hash、空 mutation、无任何运行/训练/审计引用的旧重复行（快照共删 1,361 条）。
143:  每段重复只保留末行，带引用、非空 mutation 和最新版本均未删；没有删除训练样本或审计事件。
144:- 代码收敛：相同有效配置 hash 复用最新 snapshot，不再新增版本；移除无读取者的 canonical 配置镜像写入；
145:  evolution 只把行情输入指纹视为新输入，配置/代码指纹漂移不再制造新学习周期；在新输入未出现且市场已确认收盘时跳过
146:  GP/Canary/退休/IC/权重维护，supervisor 学习复用既有事实水位并跳过历史反事实扫描。存在已批准治理建议时仍保留治理 owner 的处理机会。
147:- 启动投影收敛：`FactorLifecycleService` 的 committed Registry 恢复和运行中已提交因子加载均以
148:  `log_event=False` 只重建进程内投影，不再把每次后端重启误写成新的 `factor_observation`；真实生命周期提交仍保留事件。
149:  已产生的历史恢复事件属于 canonical 不可变审计事实，未做无证据删除。
150:- 启动补偿收敛：`learning_worker` 的 factor-health catch-up 先复用确认收盘的市场投影和
151:  `LearningCycleWatermarkService` 事实水位；停盘且无新事实时直接记录 skip，不进入重型
152:  evolution/governance 计算。市场状态或事实水位不可用时仍保留原恢复路径，不用猜测值静默跳过。
153:- 验证：payload 孤儿查询为 0；当前 snapshot 2,076 行、最新版本 3,445；runtime 配置载荷 1,288 行且每条 hash 唯一；
154:  PostgreSQL 清理并 vacuum 后约 906MB，随后一次实际 shadow catch-up 追加事实后约 910MB。当前有 4 条 approved
155:  因子治理建议和 4 条 `PROMOTION_PREPARED`，故不能把治理 backlog 误报为空闲；停盘期间没有 broker/position/risk/trade
156:  新事实，误触发 catch-up 产生的 9 条生命周期/治理事实已按审计要求保留。
157:
158:## 2. 执行与运行时
159:
160:### JobManager 本地重任务兼容
161:
162:- 状态：`resolved`（2026-08-31 已完成受控开启、真实消费和兼容路径删除）
163:- canonical：八类重任务由 PostgreSQL durable job + `quant-job-worker.service` 独立 worker 执行；`JobManager` 只投递、查询和取消。
164:- 当前（2026-08-31 只读）：`pg_job_queue_v2_enabled=true`；`quant-job-worker.service` 为 `enabled/active`；`factor_health` smoke job `82d25fe9013e4d32b65d923b2c8ba29c` 已由 worker 消费完成（`attempt_count=1`、无 error）；lease/recovery 相关 18 项测试通过；旧本地 executor、JSONL fallback、API closure 和后台 daemon thread 已删除。
165:- 退出：已完成；后续只允许经 PG Job Queue + 独立 worker 执行，不恢复本地兼容双轨。
166:
167:### emergency close 严格完成语义
168:
169:- 状态：`migrating`
170:- canonical：先持久化 no-new-risk latch；只有 fresh post-reconcile 确认目标 position ID 消失才算 completed。
171:- 已收口：`refresh_positions()` / `refresh_account_info()` 兼容入口已删除；安全、恢复和显示读取统一使用显式 reconcile 结果或其只读投影。
172:- 退出：所有安全/恢复调用只接受 immutable authoritative reconcile contract。
173:
174:### broker unknown outcome fail-closed
175:
176:- 状态：`resolved`（2026-08-28 只读复核：fail-closed 语义在真实运行中持续生效，剩余仅 attestation 绑定）
177:- canonical：结果只允许 `confirmed/rejected/unknown`；unknown 立即锁存、禁止重发，必须由 broker recovery/reconcile 唯一消解。单一 execution intent writer 和同一 broker executor 不依赖发布开关。
178:- 当前（2026-08-28 只读）：`runtime.broker_execution_intent 8792（confirmed 132/rejected 8660/unknown 0）` 近 3 天 `84 confirmed` 零 unknown；`canonical_v2.event broker_execution 127` 与 `live.loop unknown_execution_count=0` 一致；`backend_readiness_snapshot.v1` `ready_for_live_execution true`。故障矩阵代码合同已覆盖持久化、超时、未知回执和恢复边界，`execution_outcome_fault_matrix` 需随当前源码重绑定 attestation。
179:- 退出：保留 unknown 事实语义；删除的 flag-off/空 intent/position-ID 猜测路径不得恢复。
180:
181:### cTrader deal price 修复运行验收
182:
183:- 状态：`resolved`（2026-08-28 只读复核：post-repair 完整生命周期已闭环）
184:- canonical：executionPrice/entryPrice 保留 broker 原始价格；只有 money 字段按 moneyDigits 缩放。
185:- 已完成：1,150 条历史 deal 精确更正，污染学习、反事实和治理链已隔离或回滚。
186:- 运行证据（2026-08-28 只读）：`canonical_v2.event broker_execution 127 / position_transition 125 / trade_review 99`，`runtime.broker_execution_intent confirmed 132（近 3 天 84：market_open 34 / close 16 / amend 34，unknown 0）`，`canonical_v2.training_sample_row trade_review_outcome 67` 其中 `full/1.0/governance_eligible=1/matured 46` 连续产出（`2026-08-21 → 2026-08-28`，近 4 天 8-11/天，`recovery_position_state 61` 最新 `285427255` `entry_price 4584.12` 原始价落库），`risk_metrics_snapshot.v2` `cvar 1.55% / var 1.11%` known，`unknown_execution_count=0`。D1–D13 缺陷批后新开仓位 `open→protection→close→deal sync→review→sample` 全链条在真实运行中稳定产出干净样本。
187:- 禁止：用固定金价阈值或猜测值补价格。
188:
189:### live generation / Safety shadow 兼容
190:
191:- 状态：`resolved`（2026-08-28 只读复核：enforce 持续生效，补充有仓位证据；off 路径已删）
192:- canonical：`LiveLoopController` 是唯一 live loop generation/heartbeat owner；Safety 以 `live_safety_plane_v2_mode=enforce` 运行，监督动作只有 `supervisor -> RiskPolicy -> cTrader -> lifecycle -> fresh reconcile` 一条执行链。独立 legacy preview、双 candidate compare/fallback 和 Demo observation gate 已删除。
193:- 当前（2026-08-28 14:14/14:53）：旧 loop globals、并发 refresh 入口、legacy startup history pull 和旧 safety 尾部执行已删除；active `legacy_awe_trailing` candidate 在实时执行边界拒绝，旧 trace/close attribution/parity replay 仅诊断读取。双服务 `quant-backend 891039 / quant-learning-worker 891040` `active` `NRestarts=0`，`Safety enforce / heartbeat 5.4s / fresh reconcile`，`market_session open_confirmed / can_open_positions true`，`risk_metrics known cvar 1.55%`，当前持仓 `285427255` 有仓且 `unknown_execution_count=0`。生产已加载 `governance enforce`，`pg_job_queue_v2_enabled=false` 保持。
194:- 剩余：首批真实 Demo 持仓已从空仓验证扩至有仓 `governed_execute` 闭环（含 `thesis_broken/close` 证据），仍需 `≥10 笔 matured` 治理样本与 `tighten/reduce` 覆盖完成模板 effect observation；这不构成兼容执行路径。
195:
196:### live_service 领域重力
197:
198:- 状态：`migrating`
199:- canonical 模块：reconciliation、serial loop、emergency、position protection、open submission/protection/processing、execution recovery 已分离；fresh position reconcile 是既有 `recovery_position_state.recovery_meta.position_path` 的唯一 live 累计写入边界，event/API 投影不写入。
200:- 剩余：`live_service` 仍保留 process wiring、兼容状态发布和少量 lifecycle wiring；仓位路径持久化失败必须显式降级为 unknown，不得把单次观测伪装成累计 MFE/MAE。
201:- 验证：启动暖机优先使用 cTrader 在线历史，月初当月月库为空或 broker history 不可用时再通过 `bars_monthly_read_paths()` 回读最近历史闭合 bar；live bar freshness、风险和 readiness 以 online trendbar frame 为准，月库只作低频副本与离线兜底。
202:- 退出：只迁出真实决策/状态机；不为“拆文件”新增 wrapper。稳定发布后删除旧 globals 和 compatibility authority。
203:
204:## 3. 治理、研究与客户端
205:
206:### 因子治理重复重算与固定候选错配（2026-08-30）
207:
208:- 状态：`migrating`（2026-09-01 代码层复核：`build_factor_catalog` 仍被 `factor_governance_orchestrator` 一轮调 9 次，未做缓存；文档前版写 monitoring 不实，改回 migrating）
209:- canonical：因子目录仍由 `build_factor_catalog` 唯一生成；Canonical 决策快照仍是冗余分析和学习证据的只读事实；V16 仍以单候选、固定 manifest 委派扩张 mutation。
210:- 已确认根因：`run_cycle` 用累计 action 触发后续目录重建；shadow 绩效、决策快照、review payload 和 admission evidence 存在 N+1/重复全量扫描；冗余 group 数被当作候选数但没有具体候选，导致 V16 固定候选合同无法执行。
211:- 已修复：按阶段 action 只在真实 mutation 后刷新目录；shadow 绩效和冗余快照改为批量读取；学习/卡片复用已解码的 Canonical payload；review freshness 一次聚合；参数模板在同一卡片快照内复用；冗余报告生成一个具体配置 mutation candidate，并限制执行只能消费该 V16 candidate；无变化时不生成 mutation 或重复目录重建。
212:- 验证：历史实测治理轮次约 `697.3s`；本次只读复测目录约 `0.5–3.4s`、冗余约 `2.6s`、因子卡片约 `5.8s`、参数推荐约 `7.0s`；受影响测试 `179 passed`，全量 `2966 passed, 11 skipped`。
213:- 真实复测：手动写入型 run `manual_perf_dcb5122c200b` 总耗时 `142.091s`，治理 run `132.040s`，因无当前 V16 command 正常 `blocked_by_v16_command` 且无 mutation；随后正式 `evolution_hourly` 日志 `138.6s`、治理 run `130.787s`。这相对历史 `697.3s` 已显著下降，但仍高于只读探针，剩余主要是逐 action 审计/投影写入耗时，不能宣称已完成性能收口。
214:- 测试垃圾清理：仅删除上述唯一 run 的 20 条 suggestion、1 条 catalog snapshot、40 条 runtime decision、20 个独占 API mutation payload 和 1 条 run；Canonical V2 40 条不可变事件保留，未删除任何有用事实或仍被引用的 payload。
215:- 剩余：发布后仍需以正式版本 PID/日志和真实 V16 claim/finalize、Coordinator projection/effect 证据完成治理验收。不因性能修复删除 Canonical 事实、治理账本或仍有引用的历史记录。
216:
217:### PostgreSQL 重复写入与写放大（2026-08-30）
218:
219:- 状态：`resolved`（2026-09-01 代码层复核：`factor_runtime_projection_pkey` + `idx_identity` 双唯一已落地 1566行，`RuntimeKVStore` deterministic 已收敛；monitoring 完成）
220:- 清理结果：权限审计旧重复累计删除 `97889+3+1` 条，保留 `2` 条当前语义结果；policy `1824` 组、因子目录 `50` 组、runtime config payload 孤儿 `0`、projection 失效 coordinator `0`；canonical_v2 未触碰。
221:- 运行证据：停盘且 watermark 无新事实时当前仅有 pending governance，四类重型任务均在约 `0.10s` 内返回；readiness/selection 两个大 JSON 的相同语义写入只更新行级时间，不重写 value body。
222:- 后续：保留至少 24 小时的表大小、WAL、TOAST、写入次数和调度周期证据；若 pending governance 清空，再确认状态稳定落到 `skip_closed_no_new_facts`。
223:
224:### 因子扩张后验降级应用未完成
225:
226:- 状态：`active`（2026-09-01 代码层复核：`posterior_degraded_ids` 仅 `append` 未 `apply`，确认真债）
227:- canonical：`FactorGovernanceOrchestrator._posterior_expansion_guard` + `posterior_expansion_verdict`，复用 `learning_application_effect` 的最新有效 factor effect；V16 delegate 粒度和既有后验阈值不变。
228:- 当前：因子扩张候选已统一经过 posterior preflight；`blocked_by_posterior` 会阻断，样本不足只标记 `posterior_degraded`，查询不确定时 fail-closed。
229:- 剩余：`posterior_degraded` 的受限权重/scope 应用路径尚未落地，不能把标记解释为已执行降级治理。
230:- 退出：降级应用经过现有 RiskPolicy、V16、Coordinator 和 effect observation 连续真实周期验证后，从本登记册删除。
231:
232:### 治理 mutation 跨账本提交兼容
233:
234:- 状态：`resolved`（2026-08-28 14:53 f2eb9c9 已删除生产 `off` 直连路径）
235:- canonical：`GovernanceMutationCoordinator` 在同一 PG 事务内 reserve、重验 before、写 intent/领域事实、finalize；commit 后才发布 RuntimeConfig。
236:- 当前（2026-08-28 14:53）：静态开关 `governance_mutation_coordinator_v2_mode=enforce` 已双服务加载（`backend 891039 / worker 891040`）；生产 `off` 分支已删——`parameter_templates` 的 off→fail-closed / `model_influence` 的 off 直连 `RuntimeConfigMutationService` 路径在 f2eb9c9 移除，仅保留隔离测试 `*_off_compat` 覆盖。`runtime_config_mutation:213` / `factor_governance:2548` 的剩余 `off` 检查仅为测试隔离路径，不再触达生产 overlay/Registry。
237:- 补充收敛：Coordinator 应用会在同一事务内物化生成模板的 registry target；offline release candidate 复用已审核 candidate 作为审批事实，不再创建缺少 eligibility 的伪 approved suggestion。
238:- 验证：`git show f2eb9c9 --stat` 2 files 8+/30-；`parameter_templates:845` / `model_influence:329` 已 fail-closed，`grep -rn governance_mutation_coordinator_v2_mode.*off backend/` 仅测试隔离。
239:
240:### position supervisor 旧 advisory 冲突占位
241:
242:- 状态：`resolved`（2026-08-28 只读：`submitted` 残留已清，仅留 `audit`）
243:- canonical：持仓模板的自动切换只从 V16 candidate bridge 进入，`V16CommandGate.claim` 与 `PositionSupervisorGovernanceMutationService` 的 Coordinator transaction 共同完成单次授权和 finalize。
244:- 当前（2026-08-28 只读）：`BrainGovernanceCandidateService.reconcile_submitted_bridges` 执行 `reconciled 0/missing 0`（`status superseded 40/active 13/applied 6/rejected 2`，`submitted/bridge_pending/awaiting_execution 0`）；历史 non-V16 `position_supervisor_template` advisory 已全部 terminalize 为审计记录，无 `approve/apply` 或冲突占位；`supervisor_learning_scheduler` 仅跑证据，显式 `materialize` 保留为 legacy audit 入口。新候选仍限 `单 control + 单 regime` + 单 scalar patch 可证；V16 桥接 `active→bridge_pending→awaiting_execution→applied/superseded/rejected` 已统一。`position_supervisor_selection.v1` 仅消费 learning 投影。
245:- 退出：已完成，`migrating` 关闭；后续新候选仍走 V16 桥接，禁止 SQL 恢复历史。
246:
247:### parity replay 尚非 live-equivalent
248:
249:- 状态：`migrating`
250:- 当前：复用 closed-bar、RiskPolicy 与保护纯原语，并绑定 config/data/code/factor artifact hash，但缺 broker/tick/safety/account/cost/projection-ack 的完整 PIT 事实。
251:- 权限：固定 `diagnostic_only`、治理数量为零；runner 永不自授权。
252:- 退出：只有独立 certification 重验完整 live lifecycle 后才能讨论 live-parity evidence。
253:
254:### Tauri/React 前端替换
255:
256:- 状态：`migrating`（2026-09-01 代码层复核：服务器为 `sparse checkout` 无 `web_frontend` 目录，属本地 Tauri 2 个人自用桌面债，非服务器运行债；保留 migrating 但不计入服务器未退出数）
257:- canonical：web_frontend 内的 Tauri 2 + React 19 renderer；服务器端 API、fact.v1、
258:  /ws/state、认证和 mutation contract 继续作为唯一权威。
259:- 当前：新五工作区和直接废弃路由已经落在 React 19 + Vite/Tauri renderer；Workbench Shell、Safety
260:  rail、唯一 `/ws/state`、强类型 endpoint decoder、IndexedDB 研究缓存、Tauri 2 壳和
261:  signed NSIS updater artifact 均已落地。旧页面、旧 AppShell、`src/lib/compat.ts`、旧 route alias、
262:  旧页面绑定 accessibility 样式和关键 endpoint 的宽泛 decoder 已删除。`/api/market/bars`
263:  已补齐 `market.bars.v1`，缺数据明确返回 unknown；默认月库路径保持兼容，交易页的
264:  `source=live` 只读 cTrader trendbar 内存 feed，不把月库作为实时替代；本次 live 投影尚未随远程服务重载，当前远端仍可能返回 `bars_monthly`，需下一次后端发布后完成运行验收。2026-08-13 的 static artifact
265:  切换到公网 Caddy 根目录属于历史验证；当前迁移要求撤下浏览器静态入口、服务器只保留
266:  API/WSS 与后端工作树；2026-08-14 已完成 sparse checkout、blob 过滤、Caddy API/WSS-only
267:  和前端产物清理。API 合同补丁已部署并重启验证，旧 dist 和 API pre-change
268:  文件已留存仓库外 rollback archive；factor cards 已改为优先复用最新持久 catalog
269:  snapshot，远程 44 个 factor-card 测试通过。
270:- 替代：Workbench Shell、Trade Ops、Risk Desk、Research Lab、Governance、Ops
271:  五个工作区、全局 Safety rail、强类型 endpoint decoder、唯一 live store 和
272:  IndexedDB 研究只读缓存。
273:- 剩余：代码层离线读取缓存、动作禁用、Credential Manager bridge 和 updater wiring
274:  已实现，但真实 Tauri 断网恢复、缓存 hash/schema、Windows 安装卸载、WebView2
275:  缺失路径、GitHub Actions Secret/manifest（workflow 已准备，Secret 未配置）、签名成功/失败回退、Linux API/WS/auth 全量
276:  运行验证和完整验收仍未完成。
277:- 退出：五个工作区通过 frontend-refactor-acceptance-matrix.md，生产入口一次性
278:  切换，新旧 route 不再并存，旧页面/fallback/import/宽泛类型删除，签名包和
279:  updater 回退通过；回滚使用 commit/artifact，不恢复长期旧地址别名。
280:
281:### API/frontend 旧事实字段
282:
283:- 状态：`migrating`
284:- canonical：endpoint-specific `fact.v1`；unknown/stale/error 不得显示绿色或授权 start/unlock，最后 known 值可带时间保留。
285:- 剩余：Web/小程序 recursive compat 和旧字段窗口。
286:- 退出：客户端迁移完成且满足两个小程序版本或 30 天取更长者，删除旧回退。
287:
288:### legacy auth 路径
289:
290:- 状态：`migrating`
291:- canonical：Argon2id、24 小时 access、旋转 refresh session、单次 WS ticket、扩张 step-up、durable revocation。
292:- 剩余：SHA-256、legacy access、URL JWT 三个显式兼容开关。
293:- 退出：全部客户端迁移后关闭并删除；stop/emergency 的本地可验证风险缩减能力不得受 PG 故障阻断。
294:
295:## 4. 明确退役，禁止恢复
296:
297:- 旧 PostgreSQL `state_v1` schema 及其数据；生产只使用 `runtime` 与 `canonical_v2`；
298:- SQLite `data/state.db` 运行态主库；
299:- 历史 tick 采集与 `ticks.duckdb`；
300:- L2 collector、depth 风控字段与历史 L2 库；
301:- MT5 并行执行路线；
302:- 旧 Web Console/H5 web-view；
303:- 旧 cloud deploy/docker-compose 打包路线；
304:- 临时前端 smoke/debug 脚本和仓库内历史回测输出。
305:
306:## 5. 登记模板
307:
308:```text
309:### 标题
310:- 状态: active | migrating | quarantined | regressed
311:- canonical:
312:- 剩余:
313:- 退出:
314:- 验证:
315:```

[You have received this identical output 3 times. Re-reading 'ssh://quant-server/home/ubuntu/quant_trading/docs/legacy-debt-register.md' will not change it — use a narrower selector (path:A-B), or proceed with the edit.]