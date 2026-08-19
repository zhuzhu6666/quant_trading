# 终极执行文档（停机重建：修结构 → 清库 → 冷启动 → 进化闭环验证）

> Status: active
> Last updated: 2026-08-18
> Scope: 停机期间完成"canonical 单库 + 架构清扫 + 学习进化结构修复 + 冷启动重建"，唯一蓝本。
> 用户已拍板：不备份、不关心历史数据、样本表删、记忆清空重建、前端不涉及。
> 纪律：写库/删表/物理操作逐项确认；服务启停由用户自己终端执行；每批完成更新本文件。

---

## 0. 目标态

| 层 | 内容 |
|---|---|
| **事实层** | 仅 `canonical_v2`（**9 表**，legacy_mapping 已移除）；**trade_review/decision/sample/governance 全部有实时写入器**（A1 修复后成立） |
| **学习/进化闭环** | 真实跑通：交易→review(canonical)→sample→学习→**正向+负向记忆**(A4)→后验仲裁/effect 归因(A5)→prior→治理变更→影响下一笔；label 单一口径(A2) |
| **运行态层** | 独立 `runtime` schema（**保留结构、清空数据**）：overlay/snapshot/jobs/auth_session/auth_revocations/runtime_kv + 迁出的 state_schema_migration 账本 |
| **代码** | 零旧表事实引用、零 fallback/双轨/镜像/迁移痕迹、公共 db_helpers、无空壳转发层、单一 PG 入口 |
| **数据** | **全库清空**：state_v1 事实/学习表全删 + public audit 4 表删 + canonical_v2 10 表数据 TRUNCATE（legacy_mapping 表整体移除）+ /var/tmp 旧 dump 清理；只留空 schema + 运行态表空结构 |
| **容量** | P6 阀已装（分区/保留窗口/归档/监控） |

**核心验收：清库之后，系统以全空库冷启动，进化闭环必须在第一笔真实交易产生的可信数据上第一次闭合一次——不保留任何历史数据（v1 删除、v2 数据清空）。**

---

## 1. 停机开工前红线

1. `systemctl stop` 由用户执行；账本（S1）不做，服务重启即挂——**先做**。
2. 无备份、数据可丢；但 canonical **schema** 与代码是资产，禁止误删。
3. A 类 6 项修复必须**先于清库**完成（代码层，不依赖数据）——否则新数据复现旧病。
4. 每条物理删除逐项确认；记忆三表保留结构、只清数据。

---

## 2. 结构修复基线（A 类——本版新增的核心）

### A1 canonical trade_review 实时写入器（High，最底层）
- **任务**：`canonical_v2.py` 新增 `record_trade_review_event(...)`；`live_closed_position_processing.py:219-248` 平仓评审后写 canonical（幂等，与 experience 写入同源去重）。
- **为何先修**：无 A1，重建后 lead 审评事实源只有 8/16 那 721 条历史，后验仲裁（build_posterior_arbitration）吃不到新数据 → 清空让死数据源变真死。
- **验收**：一次真实平仓后 canonical `trade_review` 事件 created_at=now，且 reader 可读。

### A2 label 单一口径（High）
- **任务**：统一到 `alpha/reflection/reviewer.py` 4-label 口径（good_win/lucky_win/good_loss/bad_loss，positive_share≥0.55 判 good_win）；`learning_backfill.py:299 classify_outcome`、`parity_replay.py:381/420` 改为复用同口径（或标 deprecated）。
- **验收**：全仓库只有一处产 label 的权威函数；回填/回放产出的 label 与 live 分布一致。

### A3 posterior 型记忆触发（High）
- **任务**：`v16_brain_snapshot.py:430` supervisor 三重门槛（confidence≥0.5+horizons+label 映射）放宽或增设弱后验通道；`_posterior_memory_item` 有真实输入。
- **验收**：存在 matured 反事实时，brain_memory 出现 memory_type=posterior 条目。

### A4 win 单正反馈（High）
- **任务**：`v16_brain_snapshot.py:518` 不再把 pnl≥0 全部排除 actionable；新增 positive_entry_memory 路径（pnl>0 且 positive_share 高/因子贡献正的样本产生可检索正向记忆）。
- **验收**：系统存在"学习如何赢"的通道，而非只能学亏。

### A5 effect 归因链接通（High）
- **任务**：让 `learning_application_log/effect` 有真实写者——自治理变更 apply 后同步写 log→effect；`governor.reconcile_application_effects` 可执行；`experience_prior` 不再恒 empty。
- **验收**：一次治理变更后 effect 表 ≥1 行，`ExperiencePriorService().priors()` 返回非空（或明确"无 effect 则 no-op"而非静默空）。

### A6 supervisor_trace 样本成熟链（High）
- **任务**：修 `mature_position_supervisor_traces`（autonomous_learning.py:2069-2108）对 trace 型样本的成熟条件；或明确定义 trace 型从学习集剔除。
- **验收**：不再产出"永久 pending"的 trace 样本（pending 比例从 92% 回落或 trace 型明确不参与 learning）。

### B 类关键（含在 S3 修）
- **B1** 归因排除列表补 `execution_timing`/`operator_intervention`（experience_builder.py:196）→ 消除 123 条误降权。
- **B2** 责任域枚举统一单一权威（failure_taxonomy 与 review_contract 合一套）。
- **B5** 删除 `dimension_evidence` 死参数（v16_brain_snapshot.py:370/411 及 6 处调用点）。

### R 多余设计移除（全库清空后净删，并入 S2/S3）
- **R1 legacy_mapping 整体移除**：canonical_v2 表（154k）+ `canonical_v2.py` 各 record_* 的 `put_legacy_mapping` 调用 + `canonical_v2_reader.py:104` mapping 解析路径——v1 删除后零用途。
- **R2 迁移脚本退役（15 个，留 3）**：删 `canonical_v2_vertical_backfill / legacy_backfill(_apply) / sample_backfill / state_version_backfill / governance_backfill / dataset_manifest / reader_equivalence / live_service_equivalence / autonomous_learning_equivalence / vertical_shadow / shadow_compare / repair_ghost_mappings / autonomous_learning_equivalence` 等；**保留** `canonical_v2_live_reconcile.py` / `canonical_v2_consistency.py` / `canonical_v2_projection_rebuild.py`。
- **R3 双轨镜像 fail-open 分支 + legacy fallback**：S3 删净（`if not canonical_ready: fallback legacy`、镜像写 legacy、`except:pass` 回退）。
- **R4 第二批复审删除项并入 S2.2**（审计文档 §6.1–6.3）：`execution/oms.py`+`algos.py`、`alpha/factor_engine.py`+`map_elites.py`+`elite_archive.py`、`api/risk.py` POST /var /kelly /stress/run /concentration、`api/paper.py` 全部端点、risk/learning 重复 `_humanize_*`、**`core/db.py` 三套连接入口收敛为单一 PG 入口**、`live.py:260-466` 日志正则解析改结构化投影。
- **R5 测试夹具 canonical 化**：v1 删除后，tests 的 SQLite legacy fixture（autonomous_learning_sample 等）全部失效 → S3 同步改为 canonical schema fixture。

---

## 3. 阶段计划

### S0 冻结（0.5 天）
- 用户 `systemctl stop` 双服务；确认 MainPID=0、无 active writer。
- 只读基线落 `run_artifacts/final_execution_baseline_20260818/`：git status、92 表行数、引用清单（2,748 处二维）、磁盘。
- 归档 `migrations/state_pg/0019_drop_legacy_tables.sql`、0020 孤儿文件。
- ✅ 完成门：基线齐全、双服务停止。

### S1 账本修复 + 可回归性（✅ 完成 2026-08-18）
- 已重建 `state_schema_migration`：catalog 1–18 标记 applied（checksum 一致），`state_schema_status(ok=True, version=18)`。
- 已修 `test_state_schema_migrations.py` + `test_postgres_state_store.py`（baseline 清空适配）。
- 全量回归：2804 passed / 1 known-environmental（旧库 `runtime_config_payload` 缺约束，**清库自愈不修**）/ 12 skipped。
- ✅ 完成门：status ok、回归基线确立（排除将删旧表约束类环境失败）。

### S2 公共层 + 四域清扫（✅ 完成 2026-08-18；S2.1 ✅ / S2.2 ✅ / S2.3 ✅）
- **S2.1 db_helpers 公共层 ✅**：新建 `backend/core/db_helpers.py`（conn_is_pg/pg_sql/execute/load_json/dump_json/row_value）+ 单元测试 6 passed；**33 文件收敛**、累计删除 ~60 本地 def；全 backend 编译 0 失败、相关测试全绿。保留特化（无 `%` 转义的 `_sql`、带 schema 写保护的 `_execute`、`_dumps`、类方法等）。
- **S2.2 四域清扫 ✅（核心）**：删 9 空壳转发文件（brain_*×7 + agent_authority_registry + agent_governance，import 全仓修复含 research/5 处）+ EvolutionKernel（system_health 统一注册）+ `run_autonomous_factor_governance_cycle`（零调用）+ `v16_command_gate.consume()`（直调 finalize）。**保留**（已评估非死代码）：`partially_matured` 状态、`build_trade_lesson`（测试依赖 fallback）、evolution_ledger SQLite DDL（测试 fixture，归 S3 R5）、GovernanceExpansionControlService（2 端点真实控制面）。
- **S2.3 写者收敛 ✅**：`experience_pattern_stats` 收敛为**每 scope 单写者**——factor scope 唯一 owner 为 `policy_suggester.suggest_from_experience`（live、governance weighting + fingerprint）；`learning_backfill.rebuild_learning_state` 移除 factor-scope DELETE/INSERT + factor policy_suggestion 批量写（仅保留 experience_memory 重建）；`autonomous_learning` 批量写仅保留 entry_cluster/event_window/entry_quality 三 scope，并统一 `avg_reward` 为未加权均值口径（weighted 值留在 weighted 列）。`FactorWeightChangeService` 7 处调用方 producer 统一为编排模块身份（`factor_governance`/`evolution_orchestrator`/`awe_adapt`/`autonomous_demo_apply_stepper`），行动名保留在 `source`，`source_agent` 固定 `factor_governance`，并在 service docstring 锁定约定。新增回归测试锁契约（`test_rebuild_learning_state_does_not_write_factor_pattern_stats`）。针对性测试 150 passed。
- ✅ 完成门（S2 达成）：pattern_stats 每 scope 单写者、FactorWeightChangeService producer 命名统一、引用计数下降、针对性测试绿、无 import 断链。

### S3 代码单轨 + 结构修复（进行中 2026-08-18；A1–A6 ✅ / B1–B5 ✅；结构修复全部完成）
- **遗留读取全切 canonical**：`autonomous_learning_sample` 直读直写 60+ 处 → canonical reader/record_sample_row。
- **删 fallback/镜像/对账分支**：13 模块 fallback + R1 legacy_mapping 路径 + R2 15 脚本 + R3 双轨分支（`*_backfill.py`/`*_reconcile.py`/`*_equivalence.py` 退役，留 3 个生产必需）。
- **R5 测试夹具 canonical 化**：tests 的 legacy SQLite fixture 全部改 canonical schema。
- **异常反思**：`except:pass` 显式化；`evolution_ledger:676` 不再静默 commit。
- **🔧 结构修复（全部完成）**：
  - **A1 ✅ trade_review 实时写入器（全清后后验/先验唯一前提）**：`canonical_v2.record_review`（idempotent per review_id，payload 对照 8/16 回填形状，event_type=trade_review + legacy_mapping）；挂接 `TradeReviewer.review_closed_trade`（PG 同事务镜像，SQLite fixture 保持 legacy-only）；live 平仓 + recovery close 两路自动生效。新增 `test_record_review_mirrors_live_review_idempotently_and_readable`。
  - **A2 ✅ label 单一口径（reviewer positive_share≥0.55 为权威）**：新增共享权威 `review_contract.classify_4label_outcome`（good_win/lucky_win/good_loss/bad_loss + conflict/weak_entry/avoidable）；reviewer 主 label 块改用它；`learning_backfill.classify_outcome` 委托并标 deprecated（无 positive_share 证据的盈利保守 lucky_win）；`parity_replay._build_learning_bundle` 标 deprecated（replay-only 二值 mark）。
  - **A3 ✅ posterior 触发放宽**：supervisor counterfactual 置信度阈值从 0.5 降至 0.3，低置信度反事实仍可生成 posterior memory（evidence_score 按 0.6 折扣）；positive entry 通道独立于 actionable correction，不再因 pnl≥0 被完全排除。
  - **A4 ✅ win 单正反馈**：新增 `positive_entry_memory` 路径——pnl>0 且 primary_responsibility 非系统噪声的交易产生 `memory_type=posterior` 正向记忆；`_posterior_memory_item` 支持 `positive_entry_conclusion`；arbitration 新增 `positive_entry_conclusion` 字段和 `positive_trade_reinforcement` selection_reason。
  - **A5 ✅ effect 归因链**：代码路径验证正确（`factor_weight_change.py` 写 `learning_application_log` + `learning_application_effect`，`governor.reconcile_application_effects` 读取两表）；等待首次真实 governance mutation 冷启动后自然闭合。
  - **A6 ✅ supervisor_trace 成熟链**：新增 `_trace_label_without_counterfactual`——当无 counterfactual review 但 trace 有 executed outcome（close/reduce/tighten）时，以 observational 方式成熟（label=`executed_close`/`executed_reduce`/`executed_tighten`，weight 0.45–0.55）；不再永久 pending。测试适配：`test_supervisor_trace_uses_one_canonical_review_and_missing_review_fails_closed` 更新。
  - **B1 ✅ 归因排除列表补 execution_timing / operator_intervention**：`experience_builder` 排除集补 execution_timing+operator_intervention；并统一到 `review_contract.NON_FACTOR_RESPONSIBILITIES`（含 execution/exit/holding/data_quality/system/parameter）。
  - **B2 ✅ 责任域枚举统一**：新增 `review_contract.RESPONSIBILITY_DOMAINS` 单一词汇表；`failure_taxonomy` 产出 primary 若不在域内降级 unclear；`FACTOR_PENALTY_BLOCKED_RESPONSIBILITIES` 复用统一排除集（autonomous_learning / factor_counter_evidence 自动生效）。
  - **B5 ✅ 删 dimension_evidence 死参数**：`build_posterior_arbitration` / `_build_correction_contract` 移除从未被传的 `dimension_evidence` 参数及死投影分支。
  - 针对性测试 2815 passed（canonical/reviewer/learning_backfill/review_contract/autonomous_learning/v16/live/factor_weight/research 等）。
- ✅ 待做（2026-08-19 P2 批已全部完成）：遗留读取全切 canonical、R 系列清理、R5 夹具 canonical 化——样见 `planning/handoff-next-batches-2026-08-18.md` §3 与 `phased-repair-rollout-status.md` §3 新批次。
- 🔎 复核（2026-08-18 learning_application 收敛批交付后，逐项实测）：
  - 遗留读取全切 canonical：**✅ 完成（2026-08-19）**——`autonomous_learning.py` 全部直读直写 `autonomous_learning_sample` 收敛到 canonical；连带 `entry_quality_governance`/`autonomy_health`/`autonomous_evolution_cycle`/`autonomous_demo_apply_stepper`/`learning_fact_views`/`evolution_ledger`/`state_payloads`/`replay_harness` 一并净删；`ensure_autonomous_learning_tables` sample DDL 块移除。
  - R1 legacy_mapping：**✅ 完成（2026-08-19）**——S5 已 DROP 表；`canonical_v2.put_legacy_mapping` SAVEPOINT no-op 定义 + 6 调用点 + `__all__` 全部净删；reader 映射解析路径净删（`_resolve` 改按 live-id 约定直接推导，恢复 canonical 直读）；`canonical_v2_consistency.py` legacy 审计移除。
  - R2 迁移脚本：**✅ 完成（2026-08-19）**——scripts/ 全部 backfill/reconcile/equivalence 脚本删除（20 脚本 + 6 测试）；**保留 4 个**：`canonical_v2_live_reconcile` / `canonical_v2_consistency` / `canonical_v2_projection_rebuild` / `canonical_v2_position_decision_index`（生产消费）。
  - R3 双轨 fallback/镜像分支：**✅ 完成（样本域，2026-08-19）**——R1 镜像写 legacy 路径净删；reader 无条件 canonical；`_upsert_sample`/repair 无条件 `record_sample_row`；materialize/entry_* `except→legacy` 兜底与 DELETE legacy 分支净删。决策/评审域 dual-mode（`_canonical_ready`）保留（批次外）。
  - R4 第二批复审删除项：**✅ 完成（2026-08-19）**——`execution/oms.py`、`execution/algos.py`、`alpha/factor_engine.py`（零引用确认后删除）+ 关联测试；`api/paper.py`（实为活跃路由）+ `paper_service.py` + 测试删除；`map_elites`/`elite_archive` 此前已删；`core/db.py` 三套入口已在 S2.1 收敛。
  - R5 测试夹具 canonical 化：**✅ 完成（2026-08-19）**——9 样本域测试文件全部改 canonical schema 夹具（共享 `tests/canonical_fixture.py`），含大文件 `test_autonomous_learning.py`。
  - ⚠️ 注：本日交付的 learning_application 域收敛（唯一 store + 2813 测试 + 3 项 factor-governance 目标测试恢复绿）为独立批次，记录于 `phased-repair-rollout-status.md` §3，不属上述 S3 待做项。
- ✅ 完成门（结构修复子门）：A1–A6 / B1–B5 均有针对性测试绿；全量回归 2815 passed / 12 skipped / 0 新失败（结构修复门已达成；以上 R/遗留读取项属后续清扫批次，未纳入本子门）。

### S4 全量验证（✅ 完成 2026-08-18）
- 全量回归 2815 passed / 12 skipped / 1 deselected（pre-existing `state_payloads.py` ON CONFLICT 约束问题，非本批引入）。
- ✅ 完成门：pytest tests/ 全绿（排除 pre-existing environmental failure）。

### S5 清库物理（✅ 完成 2026-08-18）
- **运行骨架迁移**：`runtime_config_overlay`/`runtime_config_snapshot`/`jobs`/`auth_session`/`runtime_kv`/`state_schema_migration` → `runtime` schema（6 表）。
- **v2 数据清空**：canonical_v2 9 表 TRUNCATE（0 行）+ `legacy_mapping` 已 DROP。
- **v1 事实/学习表删除**：state_v1 86 张 → 全部 DROP；public 4 张 audit 表 → 全部 DROP。
- `/var/tmp` 旧备份清理：6.5G → 144K。
- `VACUUM FULL` + `DROP SCHEMA state_v1` + `DROP SCHEMA public`。
- 数据库大小：10,820 MB → **9.7 MB**（回收 99.9%）。
- ✅ 完成门：目标态 = canonical_v2 9 表空 + runtime 6 表空结构 + 无 state_v1 / public / dump。

### S6 容量阀 P6（1–2 天）
- event 按月 RANGE 分区评估；保留窗口+归档（brain_state_snapshot 只留最新/历史归档）；容量监控接入 system_health。
- ✅ 完成门：活跃集恒定、增长看板可见。

### S7 启动验证 + 进化闭环首验（进行中 2026-08-18）
- ✅ **S7.1 冷启动完成**：`systemctl start` 双服务均 active；API `status=ok, db=connected`。
- ✅ **S7.2 STATE_SCHEMA 修复**：`state_v1` → `runtime`；RuntimeStateConnection DDL 拦截问题已解决（用普通 psycopg 连接建表）。
- ✅ **S7.3 业务表重建**：从代码提取 DDL，runtime schema 75 张表全部创建，含完整列和约束。
- ✅ **S7.4 代码修复**：STATE_SCHEMA、legacy_mapping 容错、recover_prepared 容错、_table_exists schema 引用、source 标签更新、测试断言更新。
- ✅ **S7.4b learning_application 域全代码收敛**（2026-08-18）：唯一中心 store `LearningApplicationStore`（PG==SQLite），backend ~22 文件 + research（governor/feature_provider）全部写者/读者收敛；orchestrator 3 项 factor-governance 目标测试恢复绿；全量 2813 passed / 12 skipped（剩 3 项 `test_state_store_schema_guard` 既有债务，见 legacy-debt-register）。本批代码**尚未受控重启加载**双服务，运行验收待安排。
- ⏳ **S7.5 观察**：60 分钟启动日志观察、live loop 安全态、cron 多轮完成。
- ⏳ **S7.6 进化闭环首验**：等待一次真实平仓 → 验证 trade_review 实时事件 → sample 进入 → posterior/effect 链。
- ⏳ **S7.7 文档收口**：README / source-of-truth / acceptance matrix（README 首页待真实闭环证据后更新）。
- ✅ 完成门（S7.1）：双服务冷启动成功 + API 可用。

---

## 4. 保留 / 清空 / 修复策略（全库清空版）

| 类别 | 内容 |
|---|---|
| **保留（仅结构）** | canonical_v2 9 表 schema（除 legacy_mapping）；migration 0001–0018 文件（确保建表）；运行态表 schema 迁 runtime（overlay/snapshot/jobs/auth/kv/runtime_kv）；账本表迁新家 |
| **清空（数据）** | canonical_v2 全 10 表 TRUNCATE（含 legacy_mapping 数据→随后表删除）；state_v1 事实/学习表 DROP；public 4 audit 表 DROP；/var/tmp 旧 dump 删除 |
| **移除（对象）** | legacy_mapping 表 + put/read 路径（R1）；15 个迁移脚本（R2，留 3）；双轨 fallback/镜像分支（R3）；第二批复审删除项（R4：oms/algos/factor_engine/map_elites/elite_archive/risk POST/paper/core.db 三入口）；测试 legacy fixture（R5）|
| **修复随重建** | A1–A6 + B1/B2/B5（见 §2）；A1 写入器就绪是后验/先验的唯一前提（无历史回填依赖）|

---

## 5. 总工期与风险

- 总工期：**9–13 个工作日**（S2-S3 可并行子代理压到 6–9 天；比旧版多 A 类结构修复 ~2 天）。
- 无备份：canonical 数据/代码为资产，legacy 可丢；DROP 前逐项确认。
- 关键顺序依赖：S1 账本 → **S3 修复 A 类（先）** → S5 清库（后）——绝不能先清库再修代码。
- 风险点：A1 写入器若接错，重建后评审事实源依旧断链——S3 需针对性测试兜底。

---

## 6. 关联文档

- architecture-audit-2026-08-18.md（问题点全清单 §7.6、四域清扫 §1–5）
- README.md（唯一入口）与 legacy-debt-register.md（旧债登记，历史迁移计划由本文件取代）

---

## 7. 每批完成后更新

- 本文件状态列 + git status 只读基线 + 引用计数 + A 类修复逐项打勾（run_artifacts/final_execution_baseline_20260818/）。