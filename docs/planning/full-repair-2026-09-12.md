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

### B3+B4 live_service 结构修复（done 2026-09-12，范围修正后执行）
1. **live_state_store 新 owner ✓**：`backend/services/live_state_store.py` 拥有 `_live_state` 字典 + `_LIVE_STATE_LOCK` + 只读访问器 `live_state_get`/`live_state_snapshot`；**唯一写路径（`_live_state_set/_live_state_update` 及其 WS-notify、pending-close 钩子）留在 live_service**（SoT：live 进程是 live state 唯一 writer）。live_service 以兼容绑定导入容器与锁（同对象），75 处测试裸字典就地操作零改动；全仓唯一一处字典 rebind（test_ws_state_snapshot）迁移到 store。`api/live.py`、`ws/endpoints.py` 改从 store 导入，不再触 live_service 私有。验收：`grep -rn '_live_state_get\|_live_state_snapshot' backend tests` = 0；94 项相关测试绿。
2. **get_live_bars 公共化 ✓**：`_get_live_bars`→`get_live_bars`，`api/market.py` 与 2 个测试文件跟随；API 层对 live_service 的最后一个私有导入消除。
3. **读投影留守（范围修正）**：`get_status/get_live_readiness/get_account/get_positions` 与 `_ACCOUNT_CACHE/_POSITIONS_CACHE/_probe_ctrader_cache` 留在 live_service——它们直接读写 `_live_state`（如 get_account :5740 回写 reconcile 快照），是状态 owner 的 HTTP 读边界；搬出只会把私有导入换成回引用。登记为 live_service 合法居民（与 legacy-debt-register "剩余: process wiring、兼容状态发布" 口径一致）。
4. **连接 wrapper 清理 ✓**：`learning_backfill` 双跳 `_connect_state→get_state_conn` 删除（调用点直连）；live_service 的 `_get_state_pg_conn/_get_state_read_conn` 保留——它们是测试注缝（repo 既有 idiom，api/risk.py 同款），非重复实现。
5. **kv 管道评估结论**：`_runtime_kv_get/_runtime_kv_write_on_conn` 保留——write-on-conn 复用 `runtime_kv_store.set_on_conn`（唯一写入者），get 的 try/except 语义与 pending 队列 drain 联动，替换 RuntimeKVStore.get 存在 read-only 语义差异风险，不换。

### B5 剩余 globals 与 alias（done 2026-09-12，决定：保留并登记）
- `_lifecycle_*` alias（:746,764,1010,1063,1234,2724 等 13 处赋值）是 live_service 内部对 `live_position_lifecycle`/`supervisor_payload_contract` 公共函数的短名绑定，纯模块内命名，无跨模块私有语义、无兼容回退分支；改名只产生等价 diff，不改变任何边界。保留，不计为债务。
- 11 组 lock+cache（entry-cluster/event-window/entry-quality policy 缓存、loss-streak book、local SLTP、recovery confirmations、account/positions cache、probe cache、position-decision index）各自属于 live_service 内的功能块，随功能块（open pipeline、protection、safety）同址；这些功能块按 legacy-debt-register 既定口径属于 live 合法居民。待某功能块迁出时其缓存随之迁移，不在本批为"拆文件"制造无 owner 的中间态。

### B6 死代码/死依赖删除（done 2026-09-12，含 3 项误删恢复）
1. **删除 ✓**：`strategy/mab_router.py`~~（414 行，全仓零引用）~~ **【误删已恢复】**、13 个无引用脚本（其中 2 个**【误删已恢复】**）。
2. **误删恢复与教训（B7 全量收集阶段暴露）**：首轮全量在收集阶段 11 个 ERROR，暴露"引用检查只扫生产目录/单一 import 形式"的盲区——
   - `strategy/mab_router.py`：`evolution_orchestrator.py:27`（模块级 `from strategy import mab_router`）与 `:1900`（`MABRouter` regime boost）**真实生产接线**，恢复；
   - `scripts/discover_factors.py`：持久任务队列 `discover` kind 的 handler（`backend/jobs/handlers.py:27`），恢复；
   - `scripts/canonical_v2_trade_lineage_audit.py`：有专属测试文件（`tests/test_canonical_v2_trade_lineage_audit.py`），恢复并同步 B1 的 `sql/db_time` 公共名；
   - `risk/regime.py`：生产消费者只有 circuit（删除正确），但 `tests/alpha/test_directional_indicator_alignment.py` 以 `from risk import regime` 用其 Wilder 包装作对拍基准——**不恢复**，测试迁移到唯一 owner `alpha.technical_indicators`（`adx_wilder/atr_wilder`，签名一致）。
   - 方法教训（已应用）：删除前引用检查必须覆盖 tests/ + 全部 import 形式（`from X import` / `from pkg import mod` / 字符串路径 / subprocess / 测试 fixture）。误删 4 项中 3 项恢复、1 项迁移，全部由全量测试抓出——验证了"全量测试最后跑一次"流程的价值，也验证了逐批 grep 验收的必要性。
3. **真正删除 ✓**：11 个脚本（invariant_sweep、baseline_comparison 保留、canonical_v2_projection_rebuild、load_gld_holdings_sec、phase_b_risk_check、load_cot_gold、migrate_external_data、migrate_bars_monthly、open_quality_validation、restore_em_20260912、safety_shadow_gate、check_openapi_snapshot）；`StrategyRegistry.get(name)` 公共访问器替代 `_strategies` 直取。
4. **依赖声明（核实后不改）**：backtrader 根本不在 requirements（文档声明过时）；APScheduler 在 `backend/runtime/scheduler.py:28-30` 真实使用；websockets/pydantic-settings 是传递依赖显式 pin，移除需 pip-compile 重生成锁文件，保留。
5. **STATE_DB_DDL（审计修正：非死代码）**：是 SQLite 测试/兼容路径的 schema owner（PG 上被短路，PG schema 唯一 owner = migrations/state_pg）。删除迫使 278 个测试文件迁 PG fixture，属测试基建重建而非缺陷修复。保留。
6. **小文件壳层（核实后不改）**：现存 <120 行 services 模块均为多消费者助手或承载真实逻辑的 API 伴随 service；旧债登记的"18 个转发壳"口径已过时。

### B7 全量测试 + 冗余分析与裁剪（done 2026-09-12）
1. **全量结果**：`pytest tests -q` → **2972 passed / 11 skipped（postgres_integration 环境门）/ 3 failed**，413.86s。3 个失败同根因：`test_governance_context_cache.py` 以**属性访问**引用 B1 改名前的 `_payload_text_cache_clear`——修复后 3 passed，全量有效结果 **2975 passed / 11 skipped**。首轮全量曾在收集阶段 11 ERROR（B6 误删暴露，见 B6-2），修复后完整重跑；最终状态绿。
2. **冗余分析（回答"是否需要一半代码量的测试"）**：
   - 规模：tests 97,069 行 ≈ 生产 193,900 行的 50%；2972 用例 / 414s，运行时长与规模成比例，不是病理信号。
   - 耦合：130 个测试文件使用 monkeypatch；~638 个生产私有名被引用；patch 密度前五：test_live_service_lifecycle(163)、test_factor_governance_orchestrator(96)、test_ctrader_execution_outcome(92)、test_live_service_tick(84)、test_live_generation_integration(81)。高密度是 live_service 巨石的伴生现象——这些测试在本次修复中**真实捕获了 3 项生产破坏**（B6 误删×3），证明它们仍是串行 live 链路当前的回归网。
   - 结论：**一半占比本身不构成裁剪依据**。可证明冗余只有"被测对象已删除"一类：`tests/test_state.py`、`tests/test_circuit_breaker.py` 已随 core 链退役删除（≈600 行）。"纯实现细节断言"类抽查结果主要为行为保护（patch 的是注入缝而非断言私有状态），本批不批量删除；高密度文件的简化随 live_service 后续收缩自然完成（本次 owner 迁移已消解 131 个 patch 点中的一部分），排序列表留档 §4。
3. **P9 修正**：SOP 中 `record_windows_*` 是"已删除"的退役记录而非幻影引用，无需修改。
4. **docs 同步**：legacy-debt-register 对应条目已更新；system-source-of-truth 未引用任何被改名的私有名，无需变更。

### B8 观察项（沉底，不阻塞、不排期）
- demo CVaR overlay：B1 已闭合代码层 fail-open 丢键机制（不可读行 fail-closed + 清单不再被清）；运行态根因复核 = 下一次真实 autonomous overlay 写入后确认 cvar 仍为 3.5（legacy-debt-register monitoring 条目继续跟踪）。
- safety timing 5~122s 归因（broker RPC / Safety 计算 / 锁等待三段，登记册 active 条目继续）。
- 学习 worker 内存 HWM、dsl_auto 积压排空、supervisor 证据积累、reason code 零发射统计、72 投影表并表评估——维持登记册既有口径。

## 3. 顶层→backend 保留登记（B2 决定）

**决定：接受包级依赖，登记豁免理由；逐条修复的只有私有名耦合与语义倒置（B2-1~5，已全部消除）。**

- research(21)/alpha(8)/data(6)/execution(3)/monitor(3)/risk(2) 对 `backend.core.db`（连接与路径辅助，叶子模块）的依赖：这些包是 backend 应用的组成库而非独立框架消费方，状态库连接层是它们唯一的 backend 依赖面；把 ~40 个模块的数据访问搬进 backend 或造 repository 层属于"为拆而拆"的大 churn，且不能证明减少 authority。接受。
- `config/runtime_config.py:28-29` 对 `backend.core.env`、`backend.runtime.runtime_state`（均验证为叶子，无环）的模块级依赖：接受；懒加载的 `backend.services.runtime_config_startup/live_safety_state/runtime_config_overlay` 是 overlay 恢复语义的既定 owner，方向为 config→services，属既定架构事实（system-source-of-truth §2）。
- `monitor/system_health` 保留为 live 进程内主动探针（60s、带错误细节）：与 `runtime_health_projection.v1`（跨进程只读事实投影）角色不同，不是第二计算者——探针结果只进本地健康报告与告警，不授权交易、不进 readiness 裁决。
- 其余零散边（scripts→backend 29 个）是入口脚本性质，天然依赖 backend，不登记为债务。

## 4. 测试裁剪记录（B7）

| 测试文件 | 裁剪类别 | 理由 |
|---|---|---|
| tests/test_state.py | 被测对象已删 | `core/state.py` 随 core 链退役（B1-3） |
| tests/test_circuit_breaker.py | 被测对象已删 | `risk/circuit.py` 随 core 链退役（B1-3） |

后续可简化（非本批删除，随 live_service 收缩自然消化，按 patch 密度排序）：
test_live_service_lifecycle(163) / test_factor_governance_orchestrator(96) / test_ctrader_execution_outcome(92) / test_live_service_tick(84) / test_live_generation_integration(81) / test_evolution_closure_fixes(68) / test_autonomous_learning(56)。裁剪判据：仅当对应 owner 迁移后 patch 点失效、或同路径存在更强断言的行为测试时删除。

## 5. 批次核销

| 批次 | 状态 | commit | 核销时间 |
|---|---|---|---|
| B0 | done | 353fb362 | 2026-09-12 |
| B1 | done | fa520879 | 2026-09-12 |
| B2 | done | c7603f6e | 2026-09-12 |
| B3+B4 | done | de48b94c | 2026-09-12 |
| B5 | done（决定：保留并登记，无代码改动） | de48b94c 附带记录 | 2026-09-12 |
| B6 | done（含 3 项误删恢复） | 55c659a7 + a6537f91 | 2026-09-12 |
| B7 | done | 本提交 | 2026-09-12 |
| B8 | done（观察项登记于上节） | 本提交 | 2026-09-12 |

**最终结果**：全量 2975 passed / 11 skipped（postgres_integration 环境门）；生产代码净变化：删除 core/（623 行）+ risk/circuit.py + risk/regime.py + mab_router 外的 11 个脚本 + 2 个测试文件，新增 live_state_store（37 行）+ db_health_service（平移）；分层违反 30 处私有跨模块导入全部消除或转为显式 API；overlay 丢键 fail-open 机制闭合；1 个潜伏 NameError 修复。

### 运行态验收（2026-09-12 20:45 受控重启，done）

- 预检：工作区干净 @25c35204；schema `current 37 / minimum 37 / ok`；loop desired=enabled；上次关闭 graceful。
- 重启：backend 20:45:27 → learning-worker/job-worker 20:46:12；三服务 active。
- 验收：release_identity head=`25c35204`（版本对齐）；`/api/health` ok（db/ctrader connected）；启动即 `overlay restored hash=01e06d93`，无 `governance_authority` 闩；**overlay cvar 仍为 3.5**；loop 从持久化 desired state 自动恢复（generation 已签发）；readiness 快照 33s 新鲜；learning worker capability `boot_status=ready`；三服务 journal 零 ERROR（闭市 `closed_confirmed` 姿态正常）。
- 待市场开盘后复核：`safety timing` p95（登记册 active 条目继续）。

## 6. 后续批次 S1：测试工作流（smoke 选择集 + xdist）（done 2026-09-12）

- **smoke 集合**：13 个核心 fail-closed 合同文件打模块级 `pytestmark = pytest.mark.smoke`：overlay authority、governance coordinator、RiskPolicyService、canonical_v2、backend runtime lifecycle、ws state snapshot、persistent job handlers、db access contract、learning eligibility、governance eligibility weighting、live loop controller、live open admission、live emergency safety。`pytest -m smoke` = **215 用例 / 33s**；日常改动先跑它，全量只留发布门（本文件 §B7）。
- **xdist**：pytest-xdist 3.8.0 已装入 venv 并写入 requirements-dev——锁文件经 `scripts/compile_python_locks.py` 重生成（勿用裸 pip-compile，会丢 hash 格式）。`pytest -m smoke -n 2` = 28s，隔离验证通过（共享路径审计：无端口绑定；仅 2 个文件引用固定路径且为只读/负向断言；1 处 chdir 指向 tmp）。**全量并行在本机不启用**：3GB 内存、生产服务已占约 2.2GB，全量 worker RSS 增长会逼近 OOM 并可能波及 quant-backend；待内存升级或 CI 环境再开（命令 `pytest tests -n 2`）。
