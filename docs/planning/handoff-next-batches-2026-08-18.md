# Handoff / Next-Batches TODO（项目接续总览）

> Status: active handoff — 2026-08-18（**2026-08-22 刷新**：缺陷批 D1–D13 部署完成、cTrader token 已续期、全部改动已 commit+push；最新状态先读 docs/README.md「当前结论」，本文件待办项仍有效）
> 用途：给**新会话**的唯一"剩余待办"入口；动手前必须先以只读方式复核真实状态，再执行。
> 已验证基线（2026-08-18 收敛批交付后实测）：全量回归 **2813 passed / 12 skipped**；
> 剩余 3 项 `test_state_store_schema_guard` 失败为 S 阶段既有债务（见 §P4）。
> 最新基线：2026-08-22 缺陷批后全量回归 **2775 passed / 12 skipped**（数量差异来自 D7 测试隔离与净删，无回退）。

---

## 0. 纪律与约束（先读，勿违反）

- 用户硬约束：**不 commit / 不 push / 不切换生产开关 / 不清锁 / 不做生产 schema 迁移**（除非用户明确要求）。
- 回答任何"能否交易 / 当前可交易状态"前，必须重查：服务状态(systemctl/端口)、PostgreSQL、`runtime_kv`、日志、broker(cTrader)。查询状态一律用 `scripts/state_query.py --sql` / `get_state_pg_conn()`，禁止手写 `sqlite3 data/state.db`。
- 服务启停由**用户**执行；你不得重启生产服务。**代码验收 ≠ 运行验收**。
- 架构收敛规则（`AGENTS.md` 3.0.1）：单一 owner；一个事实一个生产者；删旧路径；不新增表/线程/调度器/threshold/wrapper。
- 系统级改动先按文档治理读影响面：`system-source-of-truth.md` → `legacy-debt-register.md` → `change-impact-checklist.md`。
- 每批先针对性测试（`.venv/bin/python -m pytest`）；全量回归只在阶段收口/发布门/影响面不可界定跑。
- SSH 易断：长任务逐项落盘 checkpoint；同时考用 tmux 兜底会话。

## 1. 已完成（不要再重做）

- S0–S5 ✅；S7.1–S7.3 冷启动+业务表重建 ✅；S7.4 代码修复 ✅
- S3 结构修复 A1–A6 / B1 / B2 / B5 ✅（含 trade_review 实时写入器、label 单一口径、posterior/positive memory、effect 归因链、supervisor trace 成熟、归因排除、责任域枚举）
- evolution_decision 8 列单轨收敛 ✅（PG 落库实测；canonical_v2 SAVEPOINT 镜像修复）
- **learning_application 域全代码收敛**（2026-08-18）✅：唯一中心 store `backend/services/learning_application_store.py::LearningApplicationStore`（PG==SQLite），backend ~22 文件 + research（governor/feature_provider）全部写者/读者收敛到 store；orchestrator 3 项 factor-governance 目标测试恢复绿；db-access-contract 修复；全量 2813 passed / 12 skipped
- 文档：以上批次的权威/债务/状态记录已同步（见 §5）

## 2. 待办 P1 — 运行验证 & 进化闭环（需真实运行 / 用户操作为主）

- [x] **受控重启双服务加载当前工作区代码（完成 2026-08-19 19:51，用户授权）**：quant-backend / quant-learning-worker 均已加载最新工作区代码（P2 清扫 / P4 schema-guard / learning_application 收敛 / 0019 索引全部随重启生效）。验：backend 启动 0 模块/导入错误、cTrader App→Account→fully authenticated、background connect OK、symbol schedule 加载；worker 数据库初始化、RuntimeConfig 加载、调度器启动、nursery 注册正常；首次 utility 周期 evolution_hourly `executed successfully`；**重启后 missing-index 报错 0 次**（0019 修复在运行态生效）；三服务均 active。
- [ ] **S7.5 观察**：重启后 60 分钟启动日志持续观察、live loop 安全态（当前手动停止、fail-closed）、cron 多轮完成。
- [ ] **S7.6 进化闭环首验（核心）**：等**第一笔真实平仓** → canonical `trade_review` 实时事件（A1 写入器）→ sample 进入 → posterior/effect 链（A5）→ **闭环第一次自然闭合**。README 首页更新必须以该真实证据为准。
  - 进展（2026-08-22 只读复核）：链路已首次走通——`284485647` 平仓样本于 8/21 worker 重启重算后 integrity=full / train_weight=1.0 / governance_eligible=1，证明"开仓→保护→平仓→同步→复盘→合格样本"全链条可产出干净样本；但该仓属 D11 修复部署前开仓，S7.6 终验标准 = 部署后新开仓位平仓时样本直接 full/1.0（无回放降级）。当前挂单 284602893 为旧仓不计入；周一开盘后第一笔新平仓为判定点。历史 8 笔回放降级样本（train_weight=0）按硬边界保留审计用途。
- 备注：旧卡死 run `evorun_552d20cc1bd84204`（position_supervisor_trace_maturation）会在下次 evolution run 启动时被 `expire_stale_evolution_runs` 自动 expire；下个周期 trace maturation 应能正常落库闭合。

## 3. 待办 P2 — S3 遗留清扫（代码批，可开工，默认下一批）

- [x] **遗留读取全切 canonical**（✅ 完成 2026-08-19）：`autonomous_learning.py`（原 ~10 处）**及** `entry_quality_governance`/`autonomy_health`/`autonomous_evolution_cycle`/`autonomous_demo_apply_stepper`/`learning_fact_views`/`evolution_ledger`/`state_payloads`/`replay_harness` 等全部直读直写 `autonomous_learning_sample` 收敛到 canonical reader / `record_sample_row`；`ensure_autonomous_learning_tables` 的 sample DDL 块移除。期间修复 canonical reader 4 个潜伏 bug（None params / `cols` 未定义 / Row 不能 dict / tuple-row 归一化）+ 新增 `system_contaminated`/`decision_id`/`trade_id` 过滤。
- [x] **R2 迁移脚本退役**（✅ 完成 2026-08-19）：删 `scripts/` 中 20 个 backfill/reconcile/equivalence 脚本 + 6 个配套测试；`_code_version()` 内联到保留脚本；**保留 4 个**：`canonical_v2_live_reconcile.py` / `canonical_v2_consistency.py` / `canonical_v2_projection_rebuild.py` / `canonical_v2_position_decision_index.py`（生产消费）。
- [x] **R4 删除余项**（✅ 完成 2026-08-19）：确认并删除 `execution/oms.py`、`execution/algos.py`、`alpha/factor_engine.py`（零引用）+ 关联 `tests/test_oms.py` + `api/paper.py` 及 `paper_service.py`（实为活跃路由，含 4 端点，一并删）`tests/test_backend_paper_service.py`；`cli/paper.py` 因子健康块、`main.py` 因子参数、`alpha/__init__.py` 导出、`ALL_ROUTERS` 净删。
- [x] **R1 legacy_mapping 路径净删**（✅ 完成 2026-08-19）：`canonical_v2.py` 的 `put_legacy_mapping` 定义 + 6 调用点 + `__all__` 净删；`canonical_v2_reader.py` 映射解析路径净删（表已 DROP，S5）——`_resolve` 改为按 live-id 约定（`live_decision_/live_ordevt_/live_posevt_/live_review_/...`）直接推导，恢复 canonical 直读；`scripts/canonical_v2_consistency.py` 的 legacy_mapping 审计净删。
- [x] **R3 双轨镜像/fallback 分支净删**（✅ 完成 2026-08-19）：reader `iter_training_sample_rows`/`get_training_sample_row` 无条件 canonical；`_upsert_sample`/repair 无条件 `record_sample_row`；materialize/entry_* 的 `except→legacy SELECT` 兜底净删；materialize DELETE legacy 分支净删；`_canonical_ready` 样本域门禁移除（决策/评审域双模保留，属批次外）。
- [x] **R5 测试夹具 canonical 化**（✅ 完成 2026-08-19）：9 样本域测试文件全部改 canonical 夹具（`tests/canonical_fixture.py` 共享裸表夹具 + `ensure_training_sample_row_sqlite`）；含大文件 `test_autonomous_learning.py`（39 passed）与 integration/`test_v15`/`test_learning_fact_views`/`test_autonomous_evolution_cycle` 等。
- 每项完成门：针对性测试绿（样本域 138 passed / 1 skipped）+ 旧路径/脚本净删 + 全量回归不回退（见 §7 基线）+ 文档状态列刷新（本文件 + phased-repair-rollout-status §3）。⚠ 注：documented `autonomous_learning_sample` scope_type/事件名等语义字符串保留（非表访问）。

## 4. 待办 P3 — S6 容量阀 P6（⏳ 观察期：用户决定先看增量再定方案，2026-08-19）

- [ ] **决策待定**：归档/分区**设计冻结**，先观察真实 DB 增量后由用户定案（清库重建后全库仅 6.2MB / 2652 行，无现成危机）。
- [ ] **观测已就绪**：`scripts/capacity_observe.py`（只读，逐表行数/大小追加到 `run_artifacts/capacity/observations.tsv`，基线 2026-08-18 已建）。
- [ ] **硬边界（用户定死）**：记忆/学习类数据**永不归档/删除**（学过就忘无意义）——脑记忆、经验/先验、learning_application、lesson、后验/仲裁、模型与评测证据等保留；后续任何清理方案不得触碰。
- [ ] event 按月 RANGE 分区评估（方案甲，目前挂起；如需做=一次生产表迁移，表空时最便宜）
- [ ] 保留窗口 + 归档（`brain_state_snapshot` 只留最新 / 历史归档）——设计待定
- [ ] 容量监控接入 `system_health`（看板：表大小/增速/磁盘告警）
- 完成门：活跃集恒定、增长看板可见。

## 5. 待办 P4 — 既有债务（✅ 完成 2026-08-19，见 phased-rollout §3 P4 批）

- [x] `test_state_store_schema_guard` 3 项：`test_legacy_create_ensure_is_catalog_validation_only` / `test_legacy_create_ensure_fails_closed_on_missing_column` / `test_index_catalog_validation_checks_table_and_key_definition`。根因在 `backend/core/state_store.py` 的 `validate_runtime_state_schema`（验证后误执行 DDL）与 `_validate_runtime_schema_statement`（`except...pass` 吞掉缺对象）漂移出契约。修复：纯目录校验 + fail-closed（迁移 CLI 唯一 writer）。验：schema_guard 21 passed + 相邻 98/1 + 全量回归 0 失败。已从 legacy-debt-register 标记 resolved。

## 5b. 补库批次撤回 + 待收敛清单（↩️ 已撤回 2026-08-19，勿按旧"完成"理解）

曾按"缺什么补什么"做了 4 个补库迁移（0019–0022）+ db.py 改动，**经用户质询后整体撤回**（详见 phased-rollout §3 撤回批记录）。方向纠正：**重构（S5 清库重建）标准 = runtime 75 表（按代码 DDL 提取）+ canonical_v2 9 事件表；旧迁移文件（0002–0016）的声明不是"必须存在"的标准**（台账 applied 是账本标记，S7.3 用代码 DDL 重建；旧迁移声明的部分表为重构淘汰死表）。

撤回后待收敛/待修复清单（**下一对话处理，勿直接改库**）：

- [x] **标准表索引未建全（已解决 2026-08-19，迁移 0019）**：S7.3 重建 75 张 runtime 表只建了表结构未建二级索引（PG 实测 76 索引中 72 个是主键）。已新增迁移 0019 `secondary_index_backfill`（128 个索引，覆盖 `brain_governance_candidate` 4 索引、`factor_catalog_snapshot`、`factor_governance_shadow_audit` 等），已 --apply 至生产（version 18→19，ok），运行路径复验不再报 missing index。详见 phased-rollout §3。
- [x] **17 个"表定义 vs 消费不一致"残留索引 / 极简表缺列（已解决 2026-08-19，迁移 0020）**：根因 = S7.3 重建用 `_PG_BUSINESS_TABLES_DDL` 极简版建表，而代码（store/reader）按 SQLite 完整版 `STATE_DB_DDL` 消费。审计确认 17 张"极简表"中 11 张已被后续 ensure/迁移补齐，**真缺列仅 6 张**（decision_ledger +14 / factor_health +4 / order_lifecycle_event +8 / position_lifecycle_event +8 / recovery_position_state +14 / trade_outcome_review +15，共 63 列），全部为活跃消费列（unreferenced=NONE）。迁移 0020 `backfill_minimal_table_columns`（11 语句：6 补列 + 5 补索引 idx_decision_ledger_pos_event/idx_order_lifecycle_trade/idx_position_lifecycle_pos/idx_trade_outcome_review_trade/idx_recovery_position_status）已 --apply 至生产（19→20，ok）。验证：6 表 5 处原报错消费路径全 OK、迁移相关测试 47/4 绿、全量回归 2782/12 0 失败、无 missing index 报错。**保守策略**：仅补缺列，不碰已有错列名（如 factor_health 的 factor_id vs factor 并存）。注：`idx_order_lifecycle_execution_intent`/`idx_trade_outcome_review_archive` 引用标准外列（execution_intent_id/review_archive_hash）不建（孤儿索引）。
- [x] **S7.3 漏建 7 张活跃表（已解决 2026-08-19，迁移 0021）**：只按 `_PG_BUSINESS_TABLES_DDL` 26 张表重建，漏建代码 ensure_* 预建的另一批表。迁移 0021 `create_missed_runtime_tables`（18 语句：7 建表 + 11 索引，完整列取自 STATE_DB_DDL）已 --apply（20→21，ok）。表：`factor_contribution_review` / `decision_factor_snapshot` / `calibrator` / `decision_log` / `lifecycle_events` / `shadow_trades` / `weight_history`。7 处消费路径复验全 OK；运行态 autonomous_learning/evolution 不再报缺表。死表不建：`strategy_perf` / `sync_health`（0 真实 SQL 引用，归清理）。**纠正旧误判**：`lifecycle_events` 实为活表（5 处真实 SQL 引用），非"淘汰死表"。
- [x] **`idx_offmarket_training_window_unique`（完成 2026-08-19，迁移 0025）**：澄清误解——契约是**部分唯一索引** `WHERE training_window_key <> ''`，24 行空 key 'skipped' 审计行不在覆盖范围内；非空 key 0 重复组。原"24 行重复数据无法建 UNIQUE"判断撤销，0025 补建成功（24→25，应用到生产）。
- [ ] **standard 表定义与消费不一致**（recovery_position_state 已在 0020 补列；旧条目修正为已解决）。
- [x] **死表声明清理（完成 2026-08-19）**：`strategy_perf` / `sync_health` 的 STATE_DB_DDL CREATE 声明已删除（0 真实 SQL 引用确认；sync_health 为 Python `SyncHealth` 对象名非表）。测试 28 passed 无破坏。注：`lifecycle_events` 前记为"死表改代码"系误判，实测为活表（0021 已补建）。
- [x] `scripts/validate_ctrader_token.py`（完成 2026-08-19）：Twisted 二次 run bug 修复——重写为单 reactor 单 client 只读探针，实测 app auth OK + token VALID exit 0。
- [ ] cTrader access token 过期时间 2026-08-22（**已完成续期 2026-08-22 凌晨随缺陷批部署**：refresh token 流程走通，.env 已更新，新有效期约 30 天；下次到期约 2026-09-21）。
- [x] **日志文件被测试/子进程污染（已解决 2026-08-19，logging.py v10）**：前端 `/api/logs/tail?source=backend` 面板显示大量历史错误（`no such table: payload_blob`、`current_version=0` boot failure）——根因是**测试子进程 import 后端模块时同样触发 `setup_logging`，把 pytest 堆栈和一次性 lifespan/boot 日志写进生产 backend.log/debug.log**（backlog 中 pytest 产物 29 万行/debug.log 26MB）。修复：`backend/core/logging.py` v10 在 pytest 环境（`PYTEST_CURRENT_TEST`/`PYTEST_VERSION` 检测）下跳过文件 sink，只写 stderr。验证：真实 pytest 跑后 backend.log 行数不变（13518→13518）；普通进程仍写文件。历史污染已归档（`logs/*.bak-20260819-2250`）+ 重启后端重建干净日志，当前面板 0 ERROR/WARN。test_logs_api 2 passed。
- [x] **factor_lifecycle_service 旧 schema 列名引用（已解决 2026-08-19，迁移 0028）**：根因 = `factor_lifecycle_state` 表被 S7.3 重建时用 db.py 极简 DDL 创成 7 列版（factor_id/stage/origin/artifact_hash/evidence_json/created_at/updated_at），但权威定义（0001 迁移 + 所有消费方 factor_lifecycle_service/factor_catalog/ledger service）是 **17 列完整版**（factor_name/definition_fingerprint/lifecycle_stage/generation/runtime_admission/mutation_id/config_version/config_hash/metadata_json/activated_at/retired_at）。启动报 `column s.mutation_id does not exist`（non-fatal）即此。修复：**0028 迁移 ADD 11 缺失列 + 建 idx_factor_lifecycle_unique_name 唯一索引**（apply 27→28，12 语句）。验证：重启后端后 `[lifespan] committed governance projection recovery attempted=0 current=0 degraded=0`（原 `recovery failed: s.mutation_id` 消失）；启动无 ERROR；cTrader auth OK；factor 相关测试 57 passed/1 skipped。同理已确认：0001 的 3 个 factor_lifecycle 索引契约（name_stage/admission/mutation）之前因缺列不可建，现列已齐（idx_factor_lifecycle_unique_name 已建，另 2 个 name_stage/admission 属于 C 类契约差异，可后置）。
- [x] **factor_runtime_projection 缺列（完成 2026-08-19，迁移 0026）**：启动 warning `column process_role does not exist` 根因 = S7.3 用 `_PG_BUSINESS_TABLES_DDL` 4 列极简版建表（projection_id 主键版被替换为 factor_id 主键 4 列），而 0001 迁移/代码消费 18 列完整版。0026 ADD 15 缺失列（projection_id/factor_name/process_role/process_id/boot_id/generation/artifact_hash/mutation_id/config_version/config_hash/loaded/status/error_message/heartbeat_at/created_at，保留现有 factor_id 主键 + projection_json）+ 建 0001 3 索引（identity/health/factor）。applied 25→26。重启后端后 `process_role` warning 消失 ✅。**新暴露同类**：recover_governance_projections 的 `s.mutation_id` 旧列名（见上条，改代码待办）。
- [x] **B 类活表缺索引（完成 2026-08-19，迁移 0027 建 3 个）**：`idx_brain_candidate_review_fingerprint` / `idx_proposal_registry_projection_key` / `idx_decision_factor_snapshot_lineage_status`（列经 0020-0026 补齐后现可建）applied 26→27。idx_factor_lifecycle_* 4 个因旧列名（factor_name/lifecycle_stage/runtime_admission）无标准列，归上条改代码待办。
- [x] `/api/ops/autonomy/proposals` 500（已解决 2026-08-19）：根因 = `proposal_registry` 缺 `proposal_action` 列（SQLite 标准有、PG 无）+ 索引契约不匹配。0022 补列 + 0023 重建索引后 `ProposalRegistryService.latest()` 实测跑通（ok:true）。401 为预期鉴权（RequireUser）非故障。
- [x] cTrader 账户号 47276606（ctidAccount）/ 5817896（traderLogin）为同一 Demo 账户两种编号，非配置错误（已实测确认）。
- [x] 撤回后迁移 `--check` = v18 clean；我建对象零残留；factor 两表已还原极简版（7/4 列）。全部改动未 commit / 未 push。

## 6. 待办 P5 — 文档收口 & 交付

- [x] README 首页更新（**2026-08-19 已刷第一版**：S5/S7 完成、0019–0024 schema 收敛、双服务重启验收、S7.6 明确标注待真实闭环后再写最终版）
- [ ] acceptance matrix / 验收矩阵（有 phased-repair-acceptance-matrix.md / frontend-refactor-acceptance-matrix.md 两文件，待复核覆盖度）
- [ ] 各阶段文档状态列持续刷新
- [ ] **最终拍板：是否 commit / push**（当前 75+ 文件 uncommitted diff，全按用户约束保留未提交）——交用户决定

## 7. 当前未提交改动

- ~~工作区 186 文件 uncommitted diff~~ **（2026-08-22 已过时）**：全部改动已随 2026-08-20/21/22 各批提交并推送至 origin/main（最新 ef989d7 缺陷批 D1–D13），本地与远端一致，无未提交内容。历史记录见 Git log。

## 8. 相关文档路由

- `docs/README.md` — 项目入口/阶段/主线
- `docs/phased-repair-rollout-status.md` — 当前状态索引 + 批次记录（learning_application 批与 **2026-08-19 P2·S3 遗留清扫批**均在此 §3）
- `docs/planning/final-execution-checklist.md` — 停机重建蓝本（S3 复核明细、S7.4b 收敛记录）
- `docs/legacy-debt-register.md` — 债务（learning_application 已 resolved；schema-guard 新增）
- `docs/system-source-of-truth.md` — 事实源（learning_application_log/effect 精简契约 + 唯一 store 已写）
- `docs/change-impact-checklist.md` — 影响面
- `docs/planning/handoff-next-batches-2026-08-18.md` — 项目接续总览（本文件即是该 handoff 的唯一待办入口）
- `docs/planning/audit-defects-2026-08-21.md` — **全项目缺陷审计（D1–D10，未修复待处理）**：幽灵 ACTIVE/applied 账本矛盾、测试写生产事件流、双头配置等，处理前先读此文件
