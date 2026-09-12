# 全项目底层修复方案与核销记录

> Status: active
> Started: 2026-09-12
> Scope: 全仓代码级修复。所有结论以代码为准（file:line 证据在案）；文档和测试只作线索不作证据。每批完成后在本页核销并 git commit；全量测试只在 B7 运行一次；观察项全部沉底（§B8），不阻塞任何批次。
> 决策记录：2026-09-12 用户确认——每批一 commit；B7 测试冗余分析后直接裁剪。

## 0. 审计方法

- 导入图/分层/私有导入：AST + grep 全量扫描（生产代码排除 tests/）。
- live_service 巨石：模块级 globals 清单（105 项）、344 函数、外部消费者 grep。
- 死代码：零入度模块扫描 + 依赖声明 vs import 对照 + scripts 引用源核对（deployment/backend/SOP）。
- 双写者：runtime_config_overlay 全部写点枚举。
- 已知偏差：`postgres_integration` 标记测试需独立 PG，单独归类。

## 1. 问题清单（证据）

### P1 overlay 双写者竞态（资金风险面，最高优先级）
- `backend/services/governance_mutation_coordinator.py:1702-1738` `_persist_overlay`：`INSERT ... ON CONFLICT(overlay_id) DO UPDATE` **整行覆盖** overlay_json/overlay_hash/source/run_id/mutation_id，并把 `legacy_authority_json` 无条件清为 `'{}'`。
- `backend/services/runtime_config_overlay.py:862-906` `_mutate_overlay`：`_begin_serialized_write`（PG advisory lock / SQLite BEGIN IMMEDIATE）→ 事务内读当前 → `_deep_merge`（:887，replace_overlay=False 时）→ 写行 + snapshot → commit。自带 `expected_overlay_hash` 乐观校验（:879-886）。
- **B1 修正后的结论**：两条写路径实际使用**同一把** advisory lock（`hashtext('quant_runtime_config_overlay')`，coordinator `_lock_overlay` :1676 与 overlay service `_begin_serialized_write` :816），且 Coordinator 在事务内重读当前 overlay 后 deep merge——"不同锁竞态丢键"的初判不成立。代码层面可证明的真实缺陷是：①两个 `_read_overlay` 在**行存在但 JSON 不可读/非 dict 时静默返回 `{}`**（runtime_config_overlay `_read_overlay_in_transaction`、coordinator `_read_overlay`），随后 patch merge 到空 overlay 上整行写回，其余键全部丢失——这是唯一可复现的丢键机制（fail-open）；②两个 upsert 无条件清空 `legacy_authority_json`，销毁 operator 复核清单的审计证据（校验本身按 overlay_hash 绑定 fail-closed，清空不必要）；③overlay 表运行时 DDL 存在 evolution_ledger/runtime_config_overlay 两份副本。09-11 CVaR 消失的运行态根因仍待 B8 观察复核。
- 第三份 runtime_config_overlay DDL：`backend/services/evolution_ledger.py:192`（与 `runtime_config_overlay.py:213`、migrations/state_pg/0009 重复）。

### P2 分层违反
跨模块私有导入（生产代码，30 处，去重后关键边）：

| 边 | 位置 |
|---|---|
| monitor/system_health ← live_service 私有 | `monitor/system_health.py:138(_get_ctrader),183(loop_status),336(_market_session_snapshot)` |
| data 层 ← live_service 私有 | `data/live_sync/ctrader_puller.py:85(_get_ctrader,_wait_ctrader_ready)` |
| services ← api 私有 | `backend/services/backend_runtime_lifecycle.py:123,129(_on_startup/_on_shutdown)` |
| services ← services(live_service) 私有 | `backend_runtime_lifecycle.py:167(_stop_live_scheduler)`、`live_decision_pipeline.py:219(_loss_streak_ladder_facts)` |
| api/ws ← live_service 私有 | `backend/api/live.py:60,538(_live_state_get,_live_state)`、`backend/api/market.py:11(_get_live_bars)`、`backend/ws/endpoints.py:75(_live_state_snapshot)`、`live_service.py:5601,11253,11334,11518←ws.endpoints._position_to_dict` |
| runtime ↔ services 私有（环） | `backend/runtime/evolution_orchestrator.py:34(_autonomy_mode,模块级),228(_code_version)` ←→ `autonomous_learning.py:5388(_update_weights)`、`api/learning.py:1898(_update_weights)` |
| canonical_v2 写者私有被消费 | `canonical_v2_reader.py:29(_db_time,_payload_text_cache_clear,_sql)`、`factor_redundancy.py:10`、`learning_fact_views.py:23(_sql)`、`api/state.py:12←ws.endpoints._read_state_snapshot` |
| 其他 | `research/learning/experience_builder.py:22←live_position_lifecycle._compact_supervisor_mapping`、`alpha/streaming_factor_engine.py:594←alpha.registry._supertrend_strength_array`、`risk/circuit.py:177←risk.regime._atr`、`research/model_inference_contract.py:14←offline_trainer._factor_features,_predict_score`、`backend/services/parity_replay.py:406←factor_governance_lightgbm._current_row_label,_sample_from_row`、`scripts/db_doctor.py:36←alpha.attribution_engine._ensure_trades_duckdb_schema` |

顶层包 → backend 反向依赖（44 个生产文件，scripts 29 个除外）：research 21、alpha 8、data 6、execution 3、monitor 3、risk 2、config 1（`config/runtime_config.py:28-29` 模块级依赖 `backend.core.env`/`backend.runtime.runtime_state`，懒加载 `backend.core.db.STATE_DB`、`backend.services.runtime_config_startup/live_safety_state/runtime_config_overlay`）。

### P3 live_service 巨石
- 12,705 行、344 函数、105 个模块级赋值（`_RISK_POLICY`/`_LEDGER` 等单例、11 组 lock+cache：`_ENTRY_CLUSTER_POLICY_CACHE`、`_EVENT_WINDOW_POLICY_CACHE`、`_ENTRY_QUALITY_POLICY_CACHE`、`_local_positions`、`_prev_position_ids`、`_pos_*`、`_ACCOUNT_CACHE/_POSITIONS_CACHE/_CACHE_LOCK`(:5145-5149)、`_probe_ctrader_cache`(:5154)、`_live_state`+`_LIVE_STATE_LOCK`(:3127-3131)）。
- `_lifecycle_*` alias 兼容名：:746,747,764-767,1010-1013,1063-1064,1234-1235,2724-2726。
- 测试侧 ~958 处 patch / 131 个私有名（tests 对 live_service）。

### P4 DDL 重复
- `backend/core/db.py:325-1072` STATE_DB_DDL（748 行 / 53 表）；`migrations/state_pg/*.sql` 38 文件 / 89 表；50 表两边都定义。
- 第三份 overlay DDL：`evolution_ledger.py:192`、`runtime_config_overlay.py:213`。
- SQLite 运行路径已退役（AGENTS 硬边界），但 4 处服务内 `_ensure_schema` SQLite fallback 仍在：`backend/ledger/service.py:209`、`backend/services/factor_cards.py:548`、`backend/services/parameter_templates.py:196`、`backend/services/parameter_template_validation.py:71`。

### P5 死代码/死依赖
- `strategy/mab_router.py`（414 行，全仓零引用）；`strategy/registry.py` 唯一消费者 `backend/api/strategies.py:8` 且 :21 直取私有 `strategy_registry._strategies`。
- `core/state.py:398`、`core/event_bus.py:185` 懒加载不存在的 `core.app.AppContext`（潜伏 ImportError）；core/ 仅被 `risk/circuit.py:26-27` 消费；`risk/circuit.py` 按 SoT 仅 evolution `auto_tune_risk` 复用（以代码复核为准）。
- `research/report_generator.py:293` 懒加载不存在的 `research.factor_library.FactorLibrary`。
- scripts 无引用清单（待 B6 逐个核对 docs 后删除）：canonical_v2_trade_lineage_audit(482)、invariant_sweep(357)、baseline_comparison(335)、feature_ic_snapshot(325)、canonical_v2_projection_rebuild(287)、discover_factors(279)、load_gld_holdings_sec(254)、phase_b_risk_check(261)、load_cot_gold(163)、migrate_external_data(122)、migrate_bars_monthly(110)、open_quality_validation(104)、restore_em_20260912(72)、safety_shadow_gate(37)、check_openapi_snapshot(55)。
- 依赖声明零直接 import：`websockets`、`pydantic-settings`（transitive 应声明为准）；`APScheduler` 单点 `backend/runtime/scheduler.py:28-30`（try/except 回退 threading.Timer）；`backtrader` 待 B6 核对 requirements-dev。
- SOP 引用不存在的脚本：`record_windows_restore_drill`、`record_windows_pull_backup`（docs/server-backend-sop.md）。

### P6 重复 PnL 公式
- `execution/deal_sync.py:380` 内联 `gross_profit + swap + close_commission`；同文件 :571 已用 `backend/core/pnl.net_pnl`。其他消费方（realized_pnl/session_restore）已复用，无第二公式。

### P7 状态连接 wrapper
- `backend/api/risk.py:42-44`、`backend/services/learning_backfill.py:56-57`（+:60-61 二跳 `_connect_state`）、`backend/api/factor_v4.py:32-33`、`backend/runtime/evolution_orchestrator.py:41-42`、`backend/services/live_service.py:3237-3240,3243-3246`。

### P8 测试耦合
- ~638 个生产私有名被 tests 引用；live_service ~958 处 / 131 私有名 + 95 个 setattr 目标；199 个生产模块被测试 import；tests 97,069 行 ≈ 生产 193,900 行的一半。

### P9 文档-代码漂移
- reason code ~659 个（含 reason 后缀字面量近似统计）；SOP 幻影脚本 ×2。

## 2. 修复批次

### B0 基线（本批）
- 本文档落盘；git 基线 commit。
- 验收：`git log -1 --stat` 含本文档。

### B1 逻辑链修正（done 2026-09-12）
1. **overlay 单写者收敛 ✓**：①`runtime_config_overlay._read_overlay_in_transaction` 与 coordinator `_read_overlay` 改为 fail-closed——行存在但 payload 空/非字符串/不可解析/非 dict 时抛 `overlay_json_unreadable`，不再静默按空 overlay merge 写回；②两个 upsert（overlay service `_persist_overlay_row`、coordinator `_persist_overlay`）的 DO UPDATE 不再触碰 `legacy_authority_json`，保留 operator 清单审计证据；③`evolution_ledger.ensure_evolution_ledger_tables` 删除 runtime_config_overlay 建表副本，overlay 表运行时唯一 owner = `RuntimeConfigOverlayService.ensure_table`（coordinator `_prepare_storage` SQLite 分支补调用）。验收：`tests/test_runtime_overlay_authority.py`+`test_governance_mutation_coordinator.py`+`test_db_access_contract.py` 52 passed，含 2 个新增失败路径测试；`grep -c "legacy_authority_json='{}'" governance_mutation_coordinator.py` = 0。
2. **PnL 单公式 ✓（审计误报修正）**：`deal_sync.py:380` 经核实是**docstring**，真实代码路径 `_cd_to_real_pnl` 已使用 `net_pnl`；全仓扫描无第二公式。已修正模块与函数 docstring 指向唯一公式。验收：`grep -rn 'gross_profit +' --include='*.py'` 仅剩注释且已指向 net_pnl。
3. **死 import 与 core 链退役 ✓**：`core.app`（core/state.py:398、core/event_bus.py:185）与 `research.factor_library`（report_generator.py:293）引用已删；`core/`（state+event_bus, 623 行）→ 仅被 `risk/circuit.py` 消费 → circuit 仅被 evolution `auto_tune_risk` 调用且效果是写**学习 worker 进程内存假 state**（equity 恒为默认 1000，不触达生产风控）、`risk_tuned` story 事件零消费者 → **整链退役**：删除 `core/`、`risk/circuit.py`、`risk/regime.py`（circuit 是其唯一消费者）及 `tests/test_state.py`、`tests/test_circuit_breaker.py`，evolution `update_weights` 内 auto_tune 块删除。净删除 ≈ 2,000 行生产 + 2 个测试文件。验收：全仓 grep `from core.state|from core.event_bus|from risk.circuit|from risk.regime|auto_tune_risk` = 0。
4. **私有跨模块公共化 ✓（非 live_service 部分）**：`update_weights`（ex `_update_weights`）、`autonomy_mode`（ex `_autonomy_mode`）、`code_version`（ex `_code_version`）、canonical_v2 `sql`/`db_time`/`payload_text_cache_clear`、ws `position_to_dict`/`read_state_snapshot`、offline_trainer `factor_features`/`predict_score`、factor_governance_lightgbm `current_row_label`/`sample_from_row`、alpha `supertrend_strength_array`、attribution_engine `ensure_trades_duckdb_schema`、api/learning `require_governance_confirm` 全部去下划线成为显式 API；`compact_supervisor_mapping` 删除私有别名（真 owner = `supervisor_payload_contract`）。**附带修复一个潜伏 NameError**：`v16_posterior_arbitration.py:105` 使用 `_compact_supervisor_mapping` 但从未 import，该代码路径一执行即崩——已补 `from backend.services.live_position_lifecycle import compact_supervisor_mapping`（已确认无导入环、不引入 live_service）。验收：`import backend.services.v16_posterior_arbitration` 等导入冒烟通过；相关 90 测试绿。

### B2 反向依赖修复（done 2026-09-12）
1. **monitor/system_health ✓**：`_get_ctrader`→`get_ctrader`、`_market_session_snapshot`→`market_session_snapshot` 公共化，monitor 改用公共访问器（`loop_status` 本就公共）。system_health 保留为 live 进程内 60s 主动探针（带错误细节诊断），不改为消费 runtime_health_projection——投影是只读事实投影，探针是本地 watchdog，角色不同；此决定登记于 §3。
2. **services→api 倒置消除 ✓**：db-health 缓存机制（~410 行）从 `backend/api/db_health.py` 平移至新 owner `backend/services/db_health_service.py`（`start_background_refresh`/`stop_background_refresh`/`snapshot_or_compute` 公共入口）；api/db_health.py 只剩路由+注册；`backend_runtime_lifecycle` 改调 service。`tests/test_db_health_runtime_owner.py` 跟随新 owner。验收：`grep -rn 'from backend.api' backend/services backend/runtime` = 0。
3. **data→live_service ✓**：`ctrader_puller.connect()` 改导入公共 `get_ctrader/wait_ctrader_ready`（原有独立桥回退路径保留）。验收：`grep -rn 'live_service import _' monitor/ data/` = 0。
4. **live_decision_pipeline ✓**：改导入公共 `loss_streak_ladder_facts`。
5. **lifecycle→live_service 私有 ✓**：`_stop_live_scheduler`→公共 `stop_live_scheduler`，lifecycle 与 2 个测试文件跟随。
6. live_service 读投影私有消费（api/live、api/market、ws/endpoints 的 `_live_state*`/`_get_live_bars`）随 B3/B4 搬迁时改新 owner，不在本批重复碰。

### B3 live_service 拆分①读投影
- `get_status/get_account/get_positions/get_live_readiness/_get_live_bars` + `_ACCOUNT_CACHE/_POSITIONS_CACHE/_CACHE_LOCK/_probe_ctrader_cache`(:5145-5154) → 读投影 owner 模块；调用方（api/market 等）改新 owner；测试 patch 点同批迁移。
- 验收：live_service 行数下降且对应函数已删；`grep -n '_ACCOUNT_CACHE' backend/services/live_service.py` = 0。

### B4 live_service 拆分②状态与管道
- `_live_state_*/_runtime_kv_*`(:3134-3381，含 `_RUNTIME_KV_PENDING_PATH/LOCK` pending jsonl) 并入 runtime_kv_store / 独立 live_state owner；bar warmup/fetch(:6947-7316) 归 live_factor_bootstrap 既有 owner。
- 验收：对应 globals 与函数从 live_service 删除；调用方与测试更新。

### B5 live_service 拆分③收尾
- 剩余 11 组 lock+cache 随 owner 收敛；删除全部 `_lifecycle_*` alias 兼容名（调用方直呼 owner）；live_service 只留 loop wiring 与注入。
- 验收：`grep -c '^_.*=' backend/services/live_service.py` 显著下降并留清单；测试全绿（相关文件）。

### B6 死代码/死依赖删除
- 删 `strategy/mab_router.py`；`strategy/registry` 与 `api/strategies.py` 私有访问一并处理。
- `core/`+`risk/circuit.py` 按 B1-3 结论执行（整链退役或修复）。
- 无引用 scripts 删除（逐个先 grep docs/ 与 deployment/）；`backtrader`/`websockets`/`pydantic-settings`/`APScheduler` 依赖清理；SOP 两个幻影脚本引用修正。
- `backend/services` 18 个 <120 行单调用方壳层内联（逐个核实调用方后删除壳层）。
- STATE_DB_DDL（748 行）+ 4 处 SQLite `_ensure_schema` fallback 退役；schema 唯一 owner = `migrations/state_pg`；`core/db.py` 仅保留连接/只读辅助。
- 验收：`grep -n 'STATE_DB_DDL' backend/core/db.py` = 0 或仅注释；删除文件清单核销。

### B7 全量测试 + 冗余分析与裁剪（一次性）
- `.venv/bin/python -m pytest tests -q`；失败三类：①本批引入→必修；②历史既有→发现问题必须修；③`postgres_integration` 环境门→单独报告。
- 测试冗余分析（三维：被测对象已删 / 纯实现耦合无行为断言 / 同一生产路径重复覆盖）→ 直接裁剪，逐文件理由记录于 §4。
- 同步 docs：system-source-of-truth 受影响小节、legacy-debt-register 对应条目（live_service 领域重力、平行 authority、overlay 条目等）、server-backend-sop 幻影引用。

### B8 观察项（沉底，不阻塞、不排期）
- safety timing 5~122s 归因；学习 worker 内存 HWM；dsl_auto 积压排空；supervisor 证据积累；reason code 零发射统计；72 投影表并表评估；demo CVaR overlay 修复后下一次真实 autonomous 写入复核（B1 验收的运行态部分）。

## 3. 顶层→backend 保留登记（B2 决定）

**决定：接受包级依赖，登记豁免理由；逐条修复的只有私有名耦合与语义倒置（B2-1~5，已全部消除）。**

- research(21)/alpha(8)/data(6)/execution(3)/monitor(3)/risk(2) 对 `backend.core.db`（连接与路径辅助，叶子模块）的依赖：这些包是 backend 应用的组成库而非独立框架消费方，状态库连接层是它们唯一的 backend 依赖面；把 ~40 个模块的数据访问搬进 backend 或造 repository 层属于"为拆而拆"的大 churn，且不能证明减少 authority。接受。
- `config/runtime_config.py:28-29` 对 `backend.core.env`、`backend.runtime.runtime_state`（均验证为叶子，无环）的模块级依赖：接受；懒加载的 `backend.services.runtime_config_startup/live_safety_state/runtime_config_overlay` 是 overlay 恢复语义的既定 owner，方向为 config→services，属既定架构事实（system-source-of-truth §2）。
- `monitor/system_health` 保留为 live 进程内主动探针（60s、带错误细节）：与 `runtime_health_projection.v1`（跨进程只读事实投影）角色不同，不是第二计算者——探针结果只进本地健康报告与告警，不授权交易、不进 readiness 裁决。
- 其余零散边（scripts→backend 29 个）是入口脚本性质，天然依赖 backend，不登记为债务。

## 4. 测试裁剪记录（B7 填写）

| 测试文件 | 裁剪类别 | 理由 |
|---|---|---|

## 5. 批次核销

| 批次 | 状态 | commit | 核销时间 |
|---|---|---|---|
| B0 | done | 待填 | 2026-09-12 |
| B1 | pending | | |
| B2 | pending | | |
| B3 | pending | | |
| B4 | pending | | |
| B5 | pending | | |
| B6 | pending | | |
| B7 | pending | | |
| B8 | pending | | |
