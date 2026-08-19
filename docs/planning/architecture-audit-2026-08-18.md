# 架构审计报告（2026-08-18）

> Status: active
> Last verified: 2026-08-18（四域代码审查 + 运行态实测）
> Scope: 治理 / 学习 / V16 / 记忆 四域过度设计与缺陷审计；后续批次按本文件删除清单执行。
> 方法：4 个并行子代理只读深挖（真实调用方 grep、函数规模、写入者统计、状态机消费核查）+ 主代理交叉验证（DB 实测、写入者分布）。

---

## 0. 总评

四域"骨架"（核心状态机、权限链、唯一写入者）大部分健康且必要；病在**legacy 双轨残留、工具函数跨文件复制、转发空壳、死代码、多写者/多编排者、异常静默吞噬**。没有需要推倒重建的架构。

---

## 1. 跨域共性（优先级最高，先于各域处理）

| # | 问题 | 证据 | 收敛动作 |
|---|---|---|---|
| 1 | 工具函数全项目复制 | `_sql` ×35 文件、`_execute` ×25、`_loads` ×24、`_conn_is_pg` ×22、`_json`/`_p`/`_row_dict` ×5–6（实现相同） | 提取 `backend/core/db_helpers.py`，各文件改为 import |
| 2 | legacy 双轨未清 | 学习域 `autonomous_learning_sample` 直读直写 60+ 处；`evolution_ledger` 整套 SQLite DDL（PG 直接 return）；`experience_pattern_stats` 三写者（policy_suggester 增量 / autonomous_learning 批量 / learning_backfill 重建）算法不一致 | 读取侧全切 canonical；SQLite 兼容仅留测试 fixture 显式路径；pattern_stats 收敛单写者 |
| 3 | 异常静默吞噬 / fail-open 不一致 | `autonomous_learning.py:867` reader 失败 `except: pass` 回退 legacy 无日志；`learning_effect_quality.py:116` 同；`evolution_ledger.py:676` canonical 镜像失败仅 warning 仍 commit（双事实源可能不一致） | 显式告警 + 影响分级；镜像失败不再静默 |
| 4 | 权重变更 4+ 编排者 | `FactorWeightChangeService` 被 factor_governance_orchestrator / evolution_orchestrator×2 / autonomous_learning / live_service 各自拼装 source/evidence 调用 | 统一调用入口语义与 producer 命名，收敛单一编排者 |
| 5 | 巨型文件 | autonomous_learning 6535 / factor_governance_orchestrator 4058 / factor_lifecycle_service 2462 / evolution_orchestrator 1846 / v16_brain_snapshot·planning ~1800 | 见各域拆分项（前提：先提公共层） |

---

## 2. 治理域（10 文件，~12.7k 行）

### ✅ 健康必要（保留）
- `GovernanceMutationCoordinator` —— 唯一 commit 边界，6 个生产调用方
- `GovernanceEligibility` —— 单一 authority + 版本常量被 10+ 文件复用
- `FactorWeightChangeService` —— 唯一权重写入服务（收敛调用方后保留）
- `EvolutionWorkCoordinator` —— PG advisory lock 有实际价值
- `evolution_ledger` —— 公共审计设施，16+ 文件使用

### ⚠️ 可简化
- `factor_governance_orchestrator.py`（4058 行）：单一循环十余阶段各自重建 catalog → 拆 downweight/quarantine/promotion/expansion/rollback 5 子模块
- `GovernanceExpansionControlService`（141 行）封装一个布尔 flag → 内联 `runtime_config_mutation`
- `FactorGovernanceProfile` dataclass（15 字段）一次性投影无缓存 → 直接读 config
- `_update_weights`（evolution_orchestrator.py:1639）下划线前缀却外部导入（api/learning.py:1952、autonomous_learning.py:5224）→ 正名为公共函数
- `EvolutionWorkCoordinator` 锁粒度（单一 LOCK_NAME 串行化全部 autonomous work）→ 评估按工作类型分区

### ❌ 应删除
- `run_autonomous_factor_governance_cycle`（factor_governance_orchestrator.py:4057）—— 零调用方一行转发
- `EvolutionKernel` 类整体 + `run_full_cycle`（evolution_kernel.py:111-139）—— 仅注册 system_health（与 live_service.py:5753 重复），run_full_cycle 零调用
- `evolution_ledger.py:93-189` SQLite 表 DDL、`:227-248` `ensure_evolution_columns` —— PG 路径直接 return 的兼容残留

### 缺陷
- `evolution_ledger.py:676` canonical 镜像 fail-open 后仍 commit（见共性 #3）
- `evolution_ledger.py:639-644` 6 个 evidence/verdict 列硬编码写 `"{}"`（实际数据走 put_mutation_payload，旧列空壳默认值）
- `evolution_ledger.py:423/472/499/566` 每次 start/finish/expire/record 重复执行 `CREATE TABLE IF NOT EXISTS`

---

## 3. 学习域（9 文件，~10.8k 行）

### ✅ 健康必要（保留）
- `learning_worker_capability`（459）、`learning_application_state`（221）、`supervisor_learning_scheduler`（79）、`learning_cycle_watermark`（128）—— 小而清晰
- `learning_application_effect` 账本与 canonical trade_review 事件**非冗余**（变更效果归因 vs 交易结果记录，语义不同）

### ⚠️ 可简化
- `autonomous_learning.py`（6535 行，≥12 独立职责）：拆 `learning_governance_suggestions`（L2383-3440 ~1060 行）/ `trade_review_backfill`（L4066-4700 ~634 行）/ `demo_autonomy_apply`（L4867-6117 ~1250 行）——**前提是先提取共用层**，否则拆分增加更多复制
- `_open_quality_consumer_eligibility`（L522-617，96 行）：逐字段校验 8 个 context 子对象，capture 阶段已有 `ready` 标记 → 只查 `ready=True`
- `learning_fact_views.py:448` 与 `learning_cycle_watermark.py:55` 数据源探测重叠 → 合并
- 5 套 `_sample_from_*` 转换器（L1248/1413/1470/1545/1667，各 60-170 行）局部重复（同构 features/label 拼装）→ 在公共层收敛公共拼装

### ❌ 应删除
- `autonomous_learning_sample` 直读直写 60+ 处（canonical training_sample_row 已有全量 58,640 行）：读取侧全切 canonical reader；SQLite fallback 仅留测试 fixture 显式写入
- `learning_backfill.py` legacy `INSERT INTO trade_outcome_review`（SQLite 路径，PG 已旁路）—— fixture 显式化后删除
- `partially_matured` 样本状态（仅 `_sample_from_counterfactual:1503` 设定，零消费方）
- `_sample_from_counterfactual` 中 `insufficient_future_data + confidence<=0.25` 复合条件（无下游差异化处理）

### 缺陷
- `autonomous_learning.py:934-954` SQLite fallback 直写 legacy、`:3884` repair 直写 UPDATE
- `_upsert_sample` get/save 不一致：legacy ON CONFLICT 仅更新 3 字段（decision_id/label_status/governance_eligible），content_fingerprint 不匹配的存量样本被跳过
- `learning_backfill.py:562-566` 入场质量分硬编码公式、`:757` $50 阈值无配置无注释
- `autonomous_learning.py:6486-6530` `_scheduler_thread` 仅查 `is_alive()`，线程刚结束时二次调度返回 False 不重启（无持久锁）
- `learning_effect_quality.py:40` AWE 24h cutoff 受 effect ledger batch 时间戳跳变影响

---

## 4. V16 域（13 文件，~10.2k 行）

### ✅ 健康必要（保留）
- `V16BrainOrchestrator`（9 调用方）、`V16CommandGate`（10 调用方，状态机 available/claimed/finalized/cancelled 均有真实消费）、`brain_governance_candidates`（13 调用方）、`agent_scorecard`/`agent_briefing`/`agent_authority`（各 4-9 调用方）—— **均非死代码**

### ⚠️ 可简化
- `brain_state_snapshot` 表 2.77GB / 189KB 每行（world_model+perceptions+hypotheses+memory JSON 全量持久化）→ 评估"只留最新、历史归档"（并入 P6 容量阀）
- `v16_command_gate.py:420-455` `consume()` 仅封装 finalize 改名 → 删除，调用方直调 `finalize()`
- `v16_command_gate.py` 魔法数 `821640242`（advisory lock id）→ 提常量
- `brain_governance_candidate_review.py:10` 经 `agent_governance` 壳导入 → 改直连源模块

### ❌ 应删除（9 个空壳转发文件）
| 文件 | 行数 | 生产调用方 | 处理 |
|---|---|---|---|
| `brain_action_evaluator.py` | 4 | 0 | 删，测试改 import |
| `brain_memory.py` | 4 | 0 | 删，4 个测试改 import |
| `brain_state.py` | 5 | 0 | 删 |
| `brain_live_ready_guardrail.py` | 4 | 0 | 删 |
| `brain_low_impact_executor.py` | 4 | 0 | 删 |
| `brain_medium_impact_governance.py` | 4 | 0 | 删 |
| `brain_action_planner.py` | 7 | 0 | 删，2 个测试改 import |
| `agent_authority_registry.py` | 8 | 1（factor_weight_change.py:327） | 改 import 后删 |
| `agent_governance.py` | 17 | 2（ops.py:11、brain_governance_candidate_review.py:10） | 改 import 后删 |

### 缺陷
- `claim()` 过期释放与新建之间 SQLite BEGIN IMMEDIATE 不防同事务读写竞争（TTL+WHERE 兜底，低风险）
- claim/finalize 重复魔法数（同上）

---

## 5. 记忆域（6 文件 + 消费方）

### ✅ 写者唯一性
- `experience_memory`：唯一写者 ExperienceBuilder（`research/learning/experience_builder.py:283` → `trade_lesson_memory.py:318-362`），幂等 —— 成立
- `brain_memory`：唯一写者 `BrainMemoryService._persist_items`（v16_brain_snapshot.py:1488-1514），幂等 —— 成立
- `regime_id`：唯一 owner（factor_governance_orchestrator.py:3534 只读聚合 experience_memory.regime_id）—— 成立
- ✅ 健康必要：`experience_prior`（learning_effect_quality.py:273、factor_weight_change.py:576 调用）、`factor_counter_evidence`（factor_governance_effect_tracker.py:140、factor_pruning_governance.py:586 调用）

### ⚠️ 可简化
- `memory_integrity.py`（282 行）名为 integrity 但**只生成报告不修复** → 重命名或并入 readiness
- `_agent_attribution_for_review`（trade_lesson_memory.py:366-474，108 行）每次 upsert 查 4 表聚合，产出 `decision_context_json` **无独立消费者** → 简化或删除
- `build_trade_lesson()`（trade_lesson_memory.py:209，90 行）—— 死代码（生产永远走 ExperienceBuilder rich 形状，旧 compact 默认值从未使用）→ 删除

### ❌ 应收敛
- `experience_pattern_stats` 写入者收敛为单一 owner（推荐 policy_suggester.py:202，live 路径且带 governance weighting）；`autonomous_learning.py:2569` 批量写与 `learning_backfill.py:823` 重建路径按同一口径并入

### 缺陷
- `trade_lesson_memory.py:394`、`agent_briefing.py:293` 审计路径查询异常被静默忽略

---

## 6. 清空重建兼容性（记忆域用户决策）

用户已决策：样本表删、记忆清空重建。代码层面：
- **必须保留**：ExperienceBuilder（experience_memory 写者）、BrainMemoryService._persist_items（brain_memory 写者）、policy_suggester（pattern_stats 增量写者）
- **同批删除**：`build_trade_lesson()`、memory_integrity（若并入 readiness）、learning_backfill 中 pattern_stats 批量重建路径（统一到 policy_suggester）

---

## 6. 第二批审计：执行/风控、Alpha/信号、接口/运行、数据/脚本（2026-08-18 补）

### 6.1 执行/风控域
- ✅ 三层权力边界合规：Safety（latch）/ Readiness（只读投影）/ Risk sizing 不互算
- ❌ 应删除：`execution/oms.py`（零生产调用，被 broker_execution_intent 替代）、`execution/algos.py`（TWAP/VWAP/POV/IS 零消费者）
- ⚠️ 可简化：live_service.py 中 5 个单次调用的 sizing forwarding 函数 + 2 个 ctrader legacy wrapper

### 6.2 Alpha/信号域
- ✅ 健康必要：StreamingFactorEngine / SignalNormalizer / PortfolioCompositor / live_decision_pipeline / factor_registry（事实源唯一）/ registry_adapter / ICTracker；AWE、shadow_trader、reflection、factor_search 均已接入生产（非死代码）
- ❌ 应删除：`alpha/factor_engine.py`（自标注"已弃用于生产路径"）、`alpha/search/map_elites.py` + `elite_archive.py`（无生产调用方）
- ⚠️ 可简化：`reflection/reviewer.py`（991 行 + connect_sqlite 残留违反 state→PG 纪律）、`attribution_engine.py` DUCKDB_TRADES 路径（应统一 PG）、`backend/services/factor_identity.py` re-export 壳、AWE adapt 触发条件苛刻（early 几乎不触发）
- 缺陷：live_decision_pipeline.py:188 context_policy 失败静默降级；context_policy.py:27 魔法数（delta+=0.08 等）

### 6.3 接口/运行域
- ❌ 应删除：`backend/api/risk.py` POST /var /kelly /stress/run /concentration —— **API 层重算风险指标**（违反只读投影纪律，应改读 runtime_kv 快照）；`api/paper.py` 全部端点（paper trading 已废弃）
- ⚠️ 可简化：`risk.py` 与 `learning.py` 重复 `_humanize_*`（~120 行）；`risk.py:763-1257` 后端生成整段 UI 中文文案（~500 行）应移到前端；`jobs.jsonl` 双写降级路径（manager.py:140-228，flag 关闭时停止）；`live.py:260-466` 日志正则解析重建状态（应读结构化投影）；`learning.py` ~4200 行 / 60+ 端点（拆分+清理假想契约）
- ⚠️ WS 无心跳/超时机制（僵尸连接无法检测）；6 处异常静默吞掉（live.py:308/467、manager.py:213、db_health.py:381 …）
- ✅ 健康必要：auth 三文件职责不同、health 投影链（system_health→runtime_health_projection→readiness）只读、pg_queue lease+SKIP LOCKED、release_preflight 发布门控

### 6.4 数据/脚本域
- ✅ 退役干净：ticks/l2/Dukascopy 无残留；state.db 引用全部安全路由 PG；外部数据/events/bars 写入者唯一、PIT 契约完备
- ✅ `state_schema_migrate --apply` 可安全重建账本（18 条 migration 全 additive + IF NOT EXISTS，幂等）—— S1 账本修复可行
- ❌ 应退役 15 个一次性迁移脚本：`canonical_v2_vertical_backfill.py`、`canonical_v2_legacy_backfill*.py`、各 equivalence/shadow_compare/repair 一次性脚本（数据不要了之后无用途）；保留 3 个生产必需（live_reconcile、consistency、projection_rebuild）
- ⚠️ 12 个 canonical_v2 脚本中仅 3 个生产必需，其余为过渡产物

---

## 7. 学习/进化闭环实测（2026-08-18，canonical 时代）

核心证据表（当前库实测）：

| 环节 | 证据 | 状态 |
|---|---|---|
| 样本采集 | training_sample_row 58,640 / trade_review 724 / counterfactual 576 / supervisor_trace 48k | ✅ 真实运行 |
| 因子搜索 | 8/17 注册 shadow 因子 23 次（governance_effect committed，actor=evolution_orchestrator） | ✅ 真实发生（research 级） |
| 治理建议 | `policy_suggestion` 0 行 | ⚠️ 从未落库 |
| V16 命令 | `v16_brain_command` 0 行 | ⚠️ 从未签发（观察大脑） |
| 治理提交 | `governance_mutation_intent` 0 行 / `runtime_config_snapshot` 0 行 / `runtime_config_overlay` 0 行 | ⚠️ 无生产级变更生效 |
| 效果归因 | `learning_application_effect` 0 行 | ❌ 无法证明"变更=变好" |
| 自动化率 | 8/17 governance_command：manual_api_mutation 19 vs autonomous_mutation 1 | ⚠️ 进化主要靠手动 |

发布姿态（settings.yaml 实测）：autonomy_mode=demo_autonomous、autonomy_demo_auto_apply=true、broker=demo.ctraderapi.com、Safety v2=shadow、governance=dual_record（非 enforce）、generation=false。

---

## 7.5 记忆与后验机制深挖（2026-08-18 主代理实测）

> 回答"记忆记住什么、大脑如何判断仓位好坏、后验是否真在更新判断"。结论：机制完整，归因空洞、后验闭环从未跑通过一次。

### 7.5.1 记忆四层结构（表 schema + 实测规模）

| 层 | 表 | 字段要点 | 实测 |
|---|---|---|---|
| ① 原素材 | `experience_memory`（16 列，721 行，每仓一条） | experience_id / trade_id / regime_id / setup_hash(regime\|主因子\|outcome) / decision_context_json / outcome_label / reward_score / failure_tags_json / recommended_action / evidence_strength / append_source / evolution_run_id | 样本中 `attribution_integrity=missing` 为常态 |
| ② 后验索引 | `brain_memory`（14 列，618 条） | memory_type(procedural/episodic/posterior/historical) / text_summary / structured_json / evidence_score / similarity_score / polarity / last_used_at | 现存多为 procedural（治理决策证据）；posterior 型样本未见 |
| ③ 规律统计 | `experience_pattern_stats`（15 列，107 行） | scope_type/scope_key 每 scope 一行：sample_count/win/bad_loss/avg_reward + weighted 全套 + governance_eligibility_version/fingerprint | 三写者（policy_suggester 增量 / autonomous_learning 批量 / learning_backfill 重建） |
| ④ 先验 | `learning_application_effect`（18 列，**0 行**）→ DecisionPolicy 乘子（0.85–1.15） | post/baseline avg_reward、delta_avg_reward、status(reinforced/effective/ineffective/rolled_back) | **先验恒为 empty（missing_effect_ledger）** |

### 7.5.2 仓位好坏判定链（代码路径）

```
平仓 → classify_outcome(entry_score, pnl)  [learning_backfill.py:296]
  pnl>0 → lucky_win（赚钱一律算运气赢）
  pnl<0 且信心≥0.55 → bad_loss（高信心亏=策略缺陷）
  pnl<0 且信心<0.55 → good_loss（按计划止损=好亏）
→ reward 打分 [research/learning/experience_builder.py:150-205]
  pnl/50 归一化；质量折扣：attribution_integrity=missing ×0.5、context 非 full ×0.5、emergency_close ×0.6
→ 归因 [review_json: primary_factor / primary_responsibility]
  责任在 exit/holding/execution/data_quality/system → 不怪因子（不 downweight）
→ 后验仲裁 [v16_brain_snapshot.py:407 build_posterior_arbitration]
  仅"实际亏损"对入场负责人可行动；supervisor 反事实（confidence≥0.5 且成熟）可独立胜出，entry 结论降级 neutral
→ 按因子聚合 [research/learning/policy_suggester.py]
  effective_sample≥3 且 weighted_avg_reward≤-0.20 → 建议 downweight（置信 0.45+…）
  ≥4 且 win≥3 且 reward≥0.22 → 建议 boost_small
→ 写回记忆（brain_memory posterior 型, final_memory=True, allowed_uses=[memory_retrieval, critic_context, v16_dispatch]）
```

### 7.5.3 断开点实测

| 环节 | 证据 | 判定 |
|---|---|---|
| 归因完整性 | experience_memory：**full 583（80.9%）/ missing 97（13.5%）/ recovered 41（5.7%）**；canonical trade_review：full 485 / missing 100 / absent 98 / recovered 41 | ⚠️ **修正早前"missing 常态"推断**：full 是常态；missing 集中于 restart_replay（56）+ historical_backfill（23）等非实时路径（无因子信号快照） |
| 归因质量（未查完） | full 仅代表"因子信号快照完整"，不代表 primary_factor/responsibility 归因正确 | ⏳ 待查：primary_responsibility 生产链 |
| 后验记忆 | brain_memory 现无 posterior 型条目 | 后验"胜出结论"从未成为记忆主体 |
| 先验 | learning_application_effect 0 行 | ExperiencePriorService 恒返回 missing_effect_ledger |
| 效果闭环 | learning_application_effect 0 行 | 变更→效果→回填 从未完整发生一次 |
| 证据强度 | evidence_strength 实测 0.08–0.125（兜底 0.15 被归因缺失 0.25 折打到更弱） | 根上弱信号 + 多重打折 → 全部 watch 不行动 |

### 7.5.4 结论

- 设计层面对的部分（真实且有价值）：lucky_win 不归功于策略、责任域排除不怪因子、entry vs supervisor 仲裁保留双真相、先验乘子有界。
- 断点：①归因生产链（primary_factor/responsibility/attribution_integrity）为底层输入——integrity 分布已实测（full 81%），missing 主要来自 restart_replay/historical_backfill 等非实时路径；②后验闭环（应用→effect→prior→决策）从未执行；③信号多重打折后无行动；④**归因"完整"≠归因"正确"**——full 只保证因子快照齐全，primary_responsibility 的质量待查。
- 对清空重建的精确含义：**不可信的不是机制，是归因**。清空后重新积累的可信样本必须保证归因链先修好，否则大脑会在谎报归因的数据上学习。
- 待查：**归因链如何生产、primary_responsibility 如何判定** —— 下一步深挖（primary_factor/primary_responsibility 的来源与质量）。

### 7.5.5 归因链深挖（2026-08-18 完成）

**责任域三源生产与合并（代码路径）**：
1. `review_contract.py:440-520`（system_issue_context）：label→primary ∈ {operator_intervention, data_quality, execution_timing, ""}；**execution_timing 来自 signal_to_decision/fill 延迟超阈值**
2. `failure_taxonomy.py:33-205`（failure_taxonomy）：20+ label 规则 → primary 13 值（timing/event_risk/data_quality/execution/exit/signal_quality/factor_conflict/reward_risk/regime/parameter/thesis/holding/unclear）；**`if system_primary: primary = system_primary`——system_issue 优先**
3. review 顶层/`failure_taxonomy.primary`/`system_issue_context.primary` 三处同值（样例实测：operator_intervention 三源一致）

**责任域分布（724 条 canonical trade_review）**：execution_timing 250 / thesis 110 / exit 91 / unclear 88 / data_quality 63 / signal_quality 41 / reward_risk 25 / operator_intervention 23 / holding 18 / timing 11 / factor_conflict 3 / parameter 1。worst_factor absent 159（22%）。

**实证缺陷：系统性误伤因子（核心发现）**
- experience_memory 721 条中 `(recommended_action=downweight, primary=execution_timing)` 共 **123 条**（17%）——最大 downweight 组
- 原因链：signal_execution_delay（系统性能问题）→ execution_timing → failure_taxonomy 优先采用 → experience_builder 排除列表 `{exit, holding, execution, data_quality, system}` **不含 execution_timing**（"execution"≠"execution_timing" 枚举裂缝）→ 触发因子降权建议
- operator_intervention（23 条）实际全部 watch——受 close_reason∈{emergency_close, restart_replay} 强制 watch 保护；但 execution_timing 无此保护
- 含义：**一旦治理闭环接通（suggestion→governance→权重），系统噪声（执行延迟）会被当成因子缺陷系统性降权好因子**；evolution "越用越好" 的方向会被归因裂缝污染
- 附：unclear（88 条，12%）也在排除列表外，同样可能触发 downweight（当前样本中 unclear 全部 watch，因 context 不完整强制 watch 保护）

**归因完整性修正（见 7.5.3）**：full 81% / missing 14%（restart_replay+historical_backfill 等非实时路径）/ recovered 6%；"完整"≠"正确"——full 只保证因子快照齐全，责任域判定存在上述枚举裂缝。

### 7.5.6 清库重建前数据链快检（2026-08-18 主代理）

| # | 发现 | 证据 | 严重度 |
|---|---|---|---|
| 1 | **canonical 无 trade_review 实时写入器**：canonical_v2.py 函数清单只有 decision/order/position/governance/sample/state_version 写入器，**没有 record_review**；canonical trade_review 724 条全部是 8/16 回填（721）+ 8/17（3） | canonical_v2.py 函数清单；trade_review 按天分布 | **High**——重建后评审事实源断链，后验仲裁（build_posterior_arbitration 消费 trade_reviews）将只有历史数据 |
| 2 | **label 口径不统一**：回填透传 legacy 4 类 label（good_win 105 条在 8/16 回填中）；live 新路径 classify_outcome（learning_backfill.py:296）只产 3 类（pnl>0→lucky_win）；parity_replay 又一种（good_win/bad_loss） | learning_backfill.py:296；parity_replay.py:381；8/16 回填含 good_win 105 | **High**——样本标签跨路径不一致 |
| 3 | **样本主体未成熟**：training_sample_row 58,640 中 (eligible=0,pending) 48,605（83%）；可用 (eligible=1,matured) 仅 8,060（13.7%） | SQL 实测 | Med——学习输入主要还在 pending |
| 4 | pnl 数据完整：trade_review payload 顶层 pnl 100% 覆盖；|pnl|<50 无 reward cap 饱和（pos avg 5.74/max 44.92） | SQL 实测 | 无——数据质量比预期好 |
| 5 | 归因完整性：experience_memory full 81% / missing 14%（restart_replay+historical_backfill 路径）/ recovered 6% | SQL 实测 | 修正早前"missing 常态"误判 |

> 待三路深挖子代理（样本质量链 / 后验记忆消费链 / canonical 数据完整性）报告后补全为"清库重建问题点全清单"。

### 7.5.7 清库重建问题点：样本域实证（2026-08-18 主代理补充）

| # | 发现 | 证据 | 严重度 |
|---|---|---|---|
| 1 | **supervisor_execution_trace 永久 pending 44,726 条**（占 pending 92%）：pending 总 48,605 中 44,726 是 trace 型；matured 合计仅 9,175 | SQL：sample_type×label_status 分布 | **High**——样本成熟链结构性断裂（mature 条件对 trace 不满足） |
| 2 | **shadow_open_decision pending 3,812**（需 review 配对，配对失败永 pending）；post_close_counterfactual 570 matured + 5 partial | SQL | Med |
| 3 | **三套 label 口径不统一（High）**：live 路径 alpha/reflection/reviewer.py:413 用 positive_share≥0.55 判 good_win/lucky_win（4 类）；learning_backfill.py:299 classify_outcome pnl>0 一律 lucky_win（不产 good_win）；parity_replay.py:381/420 pnl 二分 | 代码 + canonical label 分布（good_win 107 条在 8/16 回填中） | High——重建后新数据标签仍会不一致 |
| 4 | **canonical 无 trade_review 实时写入器**（见 7.5.6 #1）：live 平仓评审只写 experience_memory + pattern_stats（legacy），不进 canonical | live_closed_position_processing.py:219-248 + canonical_v2.py 函数清单 | **High**——重建后评审事实源断链 |
| 5 | canonical 引用完整性实测良好：event→payload 幽灵引用 0 / 无 payload 事件 0 / 孤儿 mapping 0 / 幽灵事件仅 2 | SQL | 无——canonical 骨架可信 |
| 6 | 样本可用集：eligible&matured 8,060（13.7%）；post-8/15 新样本仅 283 条（277 pending + 5 matured + 1 invalid）——样本重心全在历史 | SQL | Med——重建即从零积累，pending 历史包袱消失 |
| 7 | risk_rejection invalid 256（风控拒绝样本标 invalid，正常语义） | SQL | 无 |
| 8 | attribution recovered 41 条：来自 live_recovery_close（supervisor_reduce 平仓的归因恢复），context=full | SQL | 无——有修复机制 |

## 7.6 清库重建问题点全清单（2026-08-18 三路深挖 + 主代理实证汇总）

> 结论：~~清空存量只解决"旧数据坏"，**A 类 6 项结构性断链会在新数据上原样复现**——重建 = 清空存量 + 修结构。~~ **A 类 6 项全部修复完成（2026-08-18）**，清库后新数据将走正确路径。

### A. 结构性断链（✅ 全部修复 2026-08-18）

| # | 问题 | 修复 | 状态 |
|---|---|---|---|
| A1 | canonical 无 trade_review 实时写入器 | `canonical_v2.record_review` 挂接 `TradeReviewer.review_closed_trade` | ✅ |
| A2 | 三套 label 口径不统一 | `review_contract.classify_4label_outcome` 统一权威 | ✅ |
| A3 | posterior 型记忆 0 条（confidence≥0.5 门槛过高） | 降至 0.3 + 弱后验通道 + positive entry 独立通道 | ✅ |
| A4 | win 单无法产生正向进化结论 | `positive_entry_memory` 路径：pnl>0 + 非系统噪声→正向记忆 | ✅ |
| A5 | effect 归因链断（两表 0 行） | 代码路径已验证正确，等待首次真实 governance mutation 冷启动后自然闭合 | ✅ 代码就绪 |
| A6 | supervisor_execution_trace 永久 pending 92% | `_trace_label_without_counterfactual`：executed outcome→observational matured | ✅ |

### B. 中度问题（Med，重建时一并处理）

| # | 问题 | 证据 |
|---|---|---|
| B1 | 归因语义裂缝：execution_timing(250)/operator_intervention 不在 experience_builder 排除列表 → **123 条误降权因子**（实证） | experience_builder.py:196；SQL (downweight,execution_timing)=123 |
| B2 | 责任域两套枚举不统一（failure_taxonomy 13 值 vs review_contract 4 值）+ system_issue 优先级最高（if system_primary） | failure_taxonomy.py:136；review_contract.py:509 |
| B3 | shadow_open_decision pending 3,812：需 review 配对，配对失败永 pending | autonomous_learning.py:1263,2098 |
| B4 | supervisor 胜出时 entry 结论被覆盖丢失（设计注释说 keep both，实际只落库 selected） | v16_brain_snapshot.py:519,1302 |
| B5 | dimension_evidence 死参数（6 个调用点均未传；correction_contract 维度投影永不生效） | v16_brain_snapshot.py:411,370 |
| B6 | 仲裁 max(evidence_score) 单选，多时间尺度结论被截断 | v16_brain_snapshot.py:446 |
| B7 | review 多重失败标签堆积：单条 review 8-9 个标签（restart_replay+thesis_broken+low_rr…混堆），confidence cap 1.0 稀释归因 | canonical 审计脚本 3.1 抽样 |
| B8 | 样本 source_id 与 canonical event 0% 命中（无直接关联键）；content_fingerprint 94.1% 空（仅 3,470/58,640 有指纹） | canonical 审计脚本 2.4-2.5 |
| B9 | entry_score 语义混杂（入场评分多义）、governance eligibility 不检查 attribution_missing、reviewer recovery 路径可覆盖初始 label | 子代理 A |
| B10 | prior 缓存 300s 无失效钩子（effect 表开始有数据后会有延迟） | experience_prior.py:52-58 |

### C. 低度（Low，可暂缓）
- no_future_bars 打折 0.5 语义模糊（v16_brain_snapshot.py:438）；reward 三重折扣累积 0.15 压缩信号（experience_builder.py:174-185）；pattern_stats 多写者并发覆盖（policy_suggester vs autonomous_learning）——已列 §5；briefing 检索静默降级空列表（agent_briefing.py:345）；lightgbm 模型清空后安全降级（offline_trainer）。

### D. 澄清为非缺陷（设计内，勿误删）
- canonical 引用完整性/外键/唯一约束全部良好：ghost ref 0 / 孤儿 mapping 0 / 悬空边 0 / state 链 2,556 版本连续
- **payload 孤儿 15,118 = 池型**（runtime_config_version/evolution_decision/governance_mutation_intent 内容寻址池，不挂 event 属设计）；未解析 legacy_mapping 27,205 = 池型映射
- 重复事件 5,488（governance_effect stage 拆分 reserved/prepared/committed 各一条，设计内）；每 trade 仅 1 review，无重复
- risk_rejection invalid 256 为正常语义（风控拒绝样本）

### E. 重建数据保留策略（⚠️ 已被"全库清空决策"取代 2026-08-18）

> 用户已拍板：**v1 直接删、v2 数据全部清空**，不保留任何历史数据。以下旧策略仅历史参考，最终以 `final-execution-checklist.md` §0/§4（全库清空版）为准。
- ~~保留~~：~~canonical schema + 引用完整好的部分 + 8/17 后实时事件 + payload 池~~ → **全部清空**（schema 保留，数据 TRUNCATE）
- **清空**：state_v1 事实/学习表 DROP、public audit 4 表 DROP、canonical_v2 10 表 TRUNCATE（legacy_mapping 表整体移除）、/var/tmp 旧 dump 删除；运行态表（overlay/snapshot/jobs/auth/kv）+ 账本迁独立 runtime schema（保留结构）
- **必须连同重建一起修**：A1-A6（结构）+ B1/B2（归因裂缝）+ B5（死参数）——否则新数据复现旧病；A1 trade_review 写入器就绪是全清后后验/先验的唯一前提（无历史回填依赖）

---

## 8. 执行优先级

1. **P0**：db_helpers.py 公共层（共性 #1，其他拆分的先决条件）
2. **P1**：四域清扫（9 空壳 + EvolutionKernel + 死代码 + partially_matured + consume()——净减 ~500+ 行，调用链缩短）
3. **P2**：写者收敛（pattern_stats 单写者、FactorWeightChangeService 调用方统一、learning 读取全切 canonical）
4. **P3**：异常反思（except:pass 显式化、evolution_ledger:676 镜像失败不再静默）
5. **P4**：巨型文件拆分（前提 P0 完成）
6. **P5**：快照/容量治理（brain_state_snapshot 保留最新 + 归档，并入 P6 容量阀）