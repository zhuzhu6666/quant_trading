# 全项目分期修复发布状态

> Status: active current-state index
> Last verified: 2026-09-10 (文档收敛批：删除已完成批次流水；补入 2026-09-10 只读核对；§3.3 无背书 applied 行查证结案、用户裁定不处置)
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
| S7 启动验证 + 进化闭环首验 | complete（核心闭环） | 转常态观察；监督治理闭环仍需合格成熟样本与 `tighten` 覆盖（见 §3.2） |

静态发布开关（`config/settings.yaml[features]`，operator-only、随重启生效）：`live_safety_plane_v2_mode=enforce`、`governance_mutation_coordinator_v2_mode=enforce`；PG job queue 状态见 [legacy-debt-register.md](legacy-debt-register.md) 与 SSoT §2。

## 2. 最近一次只读核对（2026-09-10）

- **服务**：`quant-backend` active since 2026-09-10 13:48、`quant-learning-worker` active since 2026-09-10 14:14、`quant-job-worker` active since 2026-09-08 18:49（`systemctl is-active` / `ActiveEnterTimestamp`）。
- **状态库**：migration ledger `current 35 / minimum 35 / ok / mismatches 0`（`scripts/state_schema_migrate.py --check`），`0035_retire_v16_cognition_ledgers` 已应用（2026-09-11，runner `v16-cognition-retirement-20260911`）。
- **代码**：HEAD `ae7a2341`（2026-09-10 14:46）晚于最近一次生产重启；是否需要重启按实际 diff 与 release flags 判定，不引用本页快照。
- **readiness**：`backend_readiness_snapshot.v1` `blocking_components` 为空；`runtime_health_projection.v1` ctrader connected。
- 本轮为文档收敛批，未改代码、配置或运行态。

## 3. 未完成 / 待复核证据

1. **完整生命周期闭环**：S7.6 终验标准已达成并持续（基准 `trade_review_outcome full/1.0 46`，`2026-08-21 → 08-28`）；后续只做常态观察，当前计数、skip/rejected 双轨与 supervisor trace 分级以现查 `canonical_v2` 为准。
2. **监督治理闭环**：需 `supervisor_execution_trace` 合格成熟样本 ≥10 笔且 `tighten` 覆盖真实执行（`reduce` 已按用户决定永久关闭），才具备自动进有界 Demo 的资格；退出条件见 [legacy-debt-register.md](legacy-debt-register.md)。
3. **`policy_suggestion` 无背书 applied 行（2026-09-10 查证结案，用户裁定不处置）**：112 条 `status=applied` 且 `applied_mutation_id=''`（factor `update_weight` 102、`rollback_factor_action` 10，`created_at` 2026-08-23 20:31 → 09-08 12:43），全部 `governance_eligible=0`。查证结论：
   - 该族行只可能由 `FactorGovernanceOrchestrator._record_policy_suggestion()` 写出（`reason` / `review_note` / `fgv` 前缀全仓唯一），但**当前代码写不出**：它在算 `suggestion_id` 之前就对 `applied` 早退（探针实测 `applied → ''` 且 0 次 DB 调用，`proposed` 才触达 DB；该守卫自 2026-07-19 起存在于该文件全部 67 个修订）。
   - `suggestion_id` 是 `(writer, scope, key, action, evidence, status)` 的 sha256，按库里 evidence 重算只与 `status='applied'` 命中 → 写入发生在带守卫之前的代码变体（工作区长期未提交，历史差异不可复原）。
   - 这些行引用的 `decision_id` 在 `evolution_decision` 中不存在，窗口内无 `governance_mutation_intent`，evidence 无 `gmut_`；09-08 12:54 重启后不再新增（其后仍持续产生 decision）。
   - 影响面已 fail-closed：`live_committed_policy` 对空背书行只标 `legacy_quarantined`（且仅限非 strict + 收紧动作）；`brain_governance_candidate_review` 要求 `governance_eligible=1`；`proposal_registry` 中 `fgv*` 来源 0 条。
   - 处置：**裁定不清账、不新增守卫**（现状无授权影响）。旧口径"27 条无背书 applied 幽灵行"作废；`run_artifacts/policy_suggestion_ghost_cleanup.py`（D2 一次性脚本）从未以 `RUN=1` 执行，且快照序列化损坏（写出的是列名）、对象族标注错误，**不得直接复用**，如需清理必须重写。
4. **V16 认知层退役（2026-09-11 完成）**：停写 → 载体解耦（`posterior_fingerprint` + 自校验）→ 代码删除（净 −5.8k 行、−9 端点、`brain_action_plan` 提案来源）→ `pg_dump` 存档（`run_artifacts/v16_cognition_retirement_20260911.dump`，74.9 MB/6 表）→ v35 迁移删除 6 张表（423MB → 15MB）。保留 `v16_brain_command`、`brain_governance_candidate`(+`_review`)、`brain_memory`、`brain_live_ready_guardrail`。详见 [legacy-debt-register.md](legacy-debt-register.md) §1。
5. **索引/契约欠账**：依赖 `factor_name` / `lifecycle_stage` / `runtime_admission` 的 `idx_factor_lifecycle_*` 索引仍未建（0028 已补齐这些列，当前仅 `idx_factor_lifecycle_unique_name` 存在）；是否补建按后续性能证据决定。
5. **每次发布门重取**：process-loaded flags、PID、fingerprint、release preflight 证据；当前源码绑定的 execution/safety fault matrix attestation；Safety 在 enforce 姿态下的连续性与完整 broker position lifecycle 证据。

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
