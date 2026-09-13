# 全项目分期修复发布状态

> Status: active current-state index
> Last verified: 2026-09-14 (学习任务提速批：回放整条链 520s→23.1s、治理收紧阶段 75~150s→~10s；受控重启后重取 replay 准入 ok/fresh；§2 更新)
> Scope: current phase, last verified evidence, next batch, and unresolved runtime acceptance
> Source of truth: 运行状态必须在每次实施前重新读取服务、PostgreSQL、`runtime_kv`、日志和 broker

本文只保留当前状态和可复核的未完成证据，不保存逐时操作流水，也不重复架构合同。已完成批次通过 Git 历史追溯；权力边界见 [system-source-of-truth.md](system-source-of-truth.md)，修改流程见 [change-impact-checklist.md](change-impact-checklist.md)，实施计划见 [planning/production-autonomy-repair-optimization-plan.md](planning/production-autonomy-repair-optimization-plan.md)，仍在跟踪的旧债见 [legacy-debt-register.md](legacy-debt-register.md)。

## 1. 当前阶段

| 阶段 | 状态 | 剩余工作 |
|---|---|---|
| S0 冻结 / S1 账本修复 / S2 公共层+四域清扫 / S3 代码单轨+结构修复 | complete | 无（A1–A6 / B1–B5 已完成） |
| S4 全量验证 | complete | 回归计数以最新一次全量运行为准，不在本页固化 |
| S5 清库物理 | complete | 无（10.8GB → 9.7MB；旧事实表已退役） |
| S6 容量阀 P6 | pending | 归档/分区设计冻结，先看真实增量后定案；观测脚本 `scripts/capacity_observe.py` 自 2026-08-26 起停更。决策项见 production plan §11 D21 |
| S7 启动验证 + 进化闭环首验 | complete（核心闭环） | 转常态观察；监督治理闭环仍需候选链打通（见 §3.2） |

静态发布开关（`config/settings.yaml[features]`，operator-only、随重启生效）：`live_safety_plane_v2_mode=enforce`、`governance_mutation_coordinator_v2_mode=enforce`；PG job queue 状态见 [legacy-debt-register.md](legacy-debt-register.md) 与 SSoT §2。

## 2. 最近一次只读核对（2026-09-14 03:21 受控重启：学习任务提速批）

- **服务**：`quant-backend` / `quant-learning-worker` / `quant-job-worker` active since 2026-09-14 03:21:43（`systemctl is-active` / `ActiveEnterTimestamp`；`NRestarts=0`）。启动即 `overlay restored hash=a03704a7…`、`DataStore warmed up`、live loop 自动接回（`recovery bootstrap confirmed broker has no open positions`）、三服务 journal 零 warning。
- **状态库**：migration ledger `current 37 / minimum 37 / ok / mismatches 0`（`scripts/state_schema_migrate.py --check`）。
- **代码**：学习任务提速批（4 文件 +171/−10，**尚未提交**）——`data/duckdb_store.py` 有范围读取按 UTC 月份筛月库（±1 月保险边距）、`data/external_loader.py` 用等价滑窗 `_trailing_rank` 替换宏观排名逐行循环、`backend/services/replay_harness.py` 决策加载时间过滤下推、`backend/runtime/factor_governance_orchestrator.py` 治理周期内一次性实验台账索引 + 候选评估前批量预热准入证据；新增 `tests/test_bars_month_range_filter.py`。
- **实测提速**（同机同数据，改动前后对比）：回放整条链 520s → **23.1s**；单次范围取 K 线 3.6s → 0.044s；治理收紧阶段候选筛选 43~49s → 0.02s、候选评估 7s/个 → 0.005s/个（批量预热 9.0s 覆盖全部 1896 个 id）。
- **readiness**：`backend_readiness_snapshot.v1` `ok=true`、`blockers=[]`、`ready_for_release=true`；`ready_for_live_execution=false` 仍为休市 `market_session_blocks_open`（`closed_confirmed`）；ctrader connected、无持仓。
- **replay 准入**：改动后重取报告 `bar_replay_c0d4abe3eac042ea` grade A（decisions 80 / matched 80 / bar-window mismatch 12），`ReplayHarnessService.status()` `ok=True / fresh / blockers=[]`、code/config 绑定一致、指标与上一份报告逐项相同。
- **本批验证**：新增测试 1 passed（并注入错误过滤确认该测试会失败）；`tests/backend/runtime/test_factor_governance_orchestrator.py` 55 passed；回放/监督/外部数据 71 passed；`tests/data/` 32 passed；`pytest -m smoke` 215 passed；migration/OpenAPI 无变更。
- **待开盘复核（本批）**：① 第一个完整治理周期的实测耗时（休市轮次 0.26s 是门控跳过，未走收紧阶段）；② 培育周期触发回放时的实际耗时（预期 ≤30s）；③ 与既有的开盘待验项（因子闭环、学习建议应用、监督候选链、慢 tick 采样）一并取数。

## 3. 未完成 / 待复核证据

1. **完整生命周期闭环**：S7.6 终验标准已达成并持续（基准 `trade_review_outcome full/1.0 46`，`2026-08-21 → 08-28`）；后续只做常态观察，当前计数、skip/rejected 双轨与 supervisor trace 分级以现查 `canonical_v2` 为准。
2. **监督治理闭环**：`supervisor_execution_trace` 合格成熟样本 ≥10 笔已满足（2026-09-13 现查 55 笔）；`tighten` 覆盖门 2026-09-08 `ca23580e` 已退役（`close` 经 keep→supportive 计入干预证据；`reduce` 永久关闭）。剩余缺口是候选链：32 条治理合格 advisory 建议因缺 V16 bridge 证据全部 `superseded`，`position_supervisor_template` 的 application/effect=0，`f16024bb`（09-11）删除 medium-impact 产线后该 scope 无候选生产者；2026-09-13 `0d77157d` 已补入专员产线（`delegate_supervisor_template_switch` 产出 candidate+`delegate` 命令），同日回放 09-10 验证了首跳与 bridge，并修复两个门定义缺陷（`6ed0d878`）；首条 application 待开盘新证据验收；退出条件见 [legacy-debt-register.md](legacy-debt-register.md)。
3. **`policy_suggestion` 无背书 applied 行（2026-09-10 查证结案，用户裁定不处置）**：112 条 `status=applied` 且 `applied_mutation_id=''`（factor `update_weight` 102、`rollback_factor_action` 10，`created_at` 2026-08-23 20:31 → 09-08 12:43），全部 `governance_eligible=0`。查证结论：
   - 该族行只可能由 `FactorGovernanceOrchestrator._record_policy_suggestion()` 写出（`reason` / `review_note` / `fgv` 前缀全仓唯一），但**当前代码写不出**：它在算 `suggestion_id` 之前就对 `applied` 早退（探针实测 `applied → ''` 且 0 次 DB 调用，`proposed` 才触达 DB；该守卫自 2026-07-19 起存在于该文件全部 67 个修订）。
   - `suggestion_id` 是 `(writer, scope, key, action, evidence, status)` 的 sha256，按库里 evidence 重算只与 `status='applied'` 命中 → 写入发生在带守卫之前的代码变体（工作区长期未提交，历史差异不可复原）。
   - 这些行引用的 `decision_id` 在 `evolution_decision` 中不存在，窗口内无 `governance_mutation_intent`，evidence 无 `gmut_`；09-08 12:54 重启后不再新增（其后仍持续产生 decision）。
   - 影响面已 fail-closed：`live_committed_policy` 对空背书行只标 `legacy_quarantined`（且仅限非 strict + 收紧动作）；`brain_governance_candidate_review` 要求 `governance_eligible=1`；`proposal_registry` 中 `fgv*` 来源 0 条。
   - 处置：**裁定不清账、不新增守卫**（现状无授权影响）。旧口径"27 条无背书 applied 幽灵行"作废；`run_artifacts/policy_suggestion_ghost_cleanup.py`（D2 一次性脚本）从未以 `RUN=1` 执行，且快照序列化损坏（写出的是列名）、对象族标注错误，**不得直接复用**，如需清理必须重写。
4. **V16 认知层退役（2026-09-11 完成）**：停写 → 载体解耦（`posterior_fingerprint` + 自校验）→ 代码删除（净 −5.8k 行、−9 端点、`brain_action_plan` 提案来源）→ `pg_dump` 存档（`run_artifacts/v16_cognition_retirement_20260911.dump`，74.9 MB/6 表）→ v35 迁移删除 6 张表（423MB → 15MB）。保留 `v16_brain_command`、`brain_governance_candidate`(+`_review`)、`brain_memory`、`brain_live_ready_guardrail`。详见 [legacy-debt-register.md](legacy-debt-register.md) §1。
5. **索引/契约欠账**：依赖 `factor_name` / `lifecycle_stage` / `runtime_admission` 的 `idx_factor_lifecycle_*` 索引仍未建（0028 已补齐这些列，当前仅 `idx_factor_lifecycle_unique_name` 存在）；是否补建按后续性能证据决定。
5. **每次发布门重取**：process-loaded flags、PID、fingerprint、release preflight 证据；当前源码绑定的 execution/safety fault matrix attestation；Safety 在 enforce 姿态下的连续性与完整 broker position lifecycle 证据。
6. **因子发现闭环（2026-09-13 修复批，待开盘验证）**：五处结构缺陷已修（方向契约投影、轮转饿死、退役人口判据、证据时钟、评估覆盖），积压 1,239 未动；只读探针：CANARY_50 样本 6/25 `_promotion_evidence` eligible、退役扫描 711/1,896 行可达、轮转选择含 SHADOW 135 行（修复前分别为 0、16、0）。首条 `prepare/activate` 或 `retire` 动作与退出条件见 [legacy-debt-register.md](legacy-debt-register.md)。
7. **学习建议应用闭环（2026-09-13 修复批，待开盘验证）**：replay 报告分级改为只统计可比决策（live-only 闸门拒绝记 `live_state_gap_count`），新报告 grade A 且 admission fresh；不可执行 approved 建议改 supersede 收口；`entry_cluster` 已补 actuator（stepper step `apply_entry_cluster_control` + runner allowlist + Coordinator 对 `same_direction_cooldown` 的收紧分类），生产验证首条控制 `psg_entry_cluster_7a728f32…` → `gmut_5c0b3daa78954bc39be13698d17f8544`（`committed`/`risk_tightening`）、application `observing`、live 投影 `active=True min_same_direction_open_count=1`。开盘后首个 stepper 周期应把 rsi_14 / macd_hist / di_spread 降权落账、supersede `stoch_k` 行，并让 `learning_workload_gate` 不再恒 pending；届时确认 `learning_same_direction_cooldown` 在真实重开仓路径上触发。详见 [legacy-debt-register.md](legacy-debt-register.md)。

上述证据不能由单测、历史快照或 readiness 替代；未满足前不推进后续静态开关，也不把 readiness ready、单次 bridge 或单次 effect 解释为自治毕业。

## 4. 下一批处理顺序

1. 对 [legacy-debt-register.md](legacy-debt-register.md) 中仍在 `active` / `migrating` / `monitoring` 的路径逐项收集退出证据，canonical 验证后同批删除旧路径。
2. 仅在真实证据满足后运行对应 release gate；保持 active supervisor 走 `governed_execute` 单轨。
3. 需要修改历史 review、command、sample、maturity 或 `policy_suggestion` 状态时，先走治理通道并获得用户确认口令；不得用 SQL 直接改写。

## 5. 每批状态更新格式

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
