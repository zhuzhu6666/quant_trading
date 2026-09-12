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
- 两条写路径串行化原语不同；Coordinator 若在事务外/更早时点读 overlay 再整行写回，与 apply_patch 交错即丢键——2026-09-11 `risk_cvar_threshold_pct: 3.5` 消失事件与此机制吻合（legacy-debt-register §1 monitoring 条目）。
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

### B1 逻辑链修正
1. **overlay 单写者收敛**：`GovernanceMutationCoordinator._persist_overlay` 不再整行覆盖——在 Coordinator 提交事务内以与 `_mutate_overlay` 相同的 serialized 写锁重读当前 overlay 行，对 plan patch 做 `_deep_merge` 后写回；`legacy_authority_json` 不再无条件清空（无显式迁移则保留原值）；删除 `evolution_ledger.py:192` 第三份 DDL，`ensure_evolution_ledger_tables` 对 overlay 表改为复用 `RuntimeConfigOverlayService.ensure_table`。
   - 验收：`grep -c "legacy_authority_json='{}'" backend/services/governance_mutation_coordinator.py` = 0；新增/改造针对性测试模拟 apply_patch 与 coordinator mutation 交错，键不丢；`tests/test_db_access_contract.py` 等相关测试绿。
2. **PnL 单公式**：`execution/deal_sync.py:380` 改用 `net_pnl(...)`。验收：`grep -n 'gross_profit + swap' execution/deal_sync.py` = 0。
3. **死 import**：删 `core/state.py:398`、`core/event_bus.py:185` 的 `core.app` 死分支；删 `research/report_generator.py:293` 的 `factor_library` 死分支；`core/`+`risk/circuit.py` 链条以代码核实 `auto_tune_risk` 调用后决定整链退役或就地修复（结论记录于此）。
4. **私有跨模块公共化（非 live_service 部分）**：`_update_weights`、`_autonomy_mode`、`_code_version`、`_position_to_dict`、`_read_state_snapshot`、canonical_v2 三私有、`_compact_supervisor_mapping`、`_supertrend_strength_array`、`_atr`、`_factor_features/_predict_score`、`_current_row_label/_sample_from_row`、`_ensure_trades_duckdb_schema`——能搬家的搬到唯一 owner，其余去下划线成为显式 API；同批删除旧私有引用。
   - 验收：§1 P2 表中非 live_service 边全部消失（grep 断言逐条记录）。

### B2 反向依赖修复
1. `monitor/system_health.py` 改消费 `runtime_health_projection.v1` / 公共状态入口（字段缺失先在投影 owner 补投影，不在 monitor 重算）。
2. `backend_runtime_lifecycle` ↔ `api.db_health` 钩子方向反转（owner 移入 lifecycle/core，api 只注册）。
3. `data/live_sync/ctrader_puller.py` 不再 import live_service：bridge 访问器归位（execution 侧或注入）。
4. `live_decision_pipeline.py:219` `_loss_streak_ladder_facts` 归位。
5. `api/live.py`、`api/market.py`、`ws/endpoints.py` 改用 live_service 唯一公共只读快照入口；`live_service` 对 `ws.endpoints._position_to_dict` 的 4 处反向私有引用改为公共函数或本地化。
6. 顶层 44 文件逐个清点：数据访问收敛进 backend 域 store；纯函数下沉；确需保留的登记于 §3 并给理由。
   - 验收：`grep -rn 'live_service' monitor/ data/ --include='*.py'` = 0；`grep -rn 'from backend.api' backend/services backend/runtime` = 0；顶层→backend 边数与登记清单一致。

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

## 3. 顶层→backend 保留登记（B2 填写）

| 文件 | 依赖 | 决定 | 理由 |
|---|---|---|---|

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
