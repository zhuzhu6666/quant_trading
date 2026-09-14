# 全项目分期修复发布状态

> Status: active current-state index
> Last verified: 2026-09-14 18:45 (恢复表完整度列补写入者批：平仓三条路径透传 review 值、今日 3 行 unknown 按 review 回填（2 full + 1 missing）、全表 0 unknown，后端 18:38 重启生效且健康；回放报告随内容重取；剩余下一次真实平仓验证。此前：反事实复盘流解堵并验收，post-fill 接线修复验收)
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

## 2. 最近一次核对（2026-09-14 开盘复核：发现并修复 post-fill 记录接线断裂）

**开盘姿态**：`market_session` `open_confirmed`（`can_open_positions=true`、`scheduled_open_fresh_quote`、quote age ~1s，09-13 22:00 UTC 开盘）；三服务 active since 2026-09-14 03:21:43（`NRestarts=0`）；migration ledger `current 37 / minimum 37 / ok`。

**当日发现（阻断新增风险 4 小时）**：07:35:20（09-13 23:35 UTC）tick 3033 一笔 LONG 确认成交后，post-fill 记录抛 `TypeError: record_amend_failure_after_fill() got an unexpected keyword argument 'attr_engine'` → 按设计 fail-closed 落 `no_new_risk_latch`（cause `safety_freshness` / `entry_protection_initialization`、blocker `confirmed_open_post_fill_processing_failed`），此后所有开仓被 `no_new_risk_latched` 挡住。该仓位 broker SL 实际已生效（`entry_protection_pending` 释放证据 sl=4334.2 / tp=4351.92），07:49 被 broker 止损平仓（`broker_close`，net −6.45，position 288378714），账户无遗留风险；缺的是 `record_filled_context` 的恢复与归因记录（该笔 `attribution_missing`）。

- **根因**：`backend/services/live_open_pipeline.py` 的 `OpenProtectionRuntime` 把 `record_success` / `record_failure` 接到了 l3a 拆分后的**引擎入口**（`record_amended_open_success_context` / `record_amend_failure_after_fill`，签名 `(request, *, runtime)`），而 `live_open_protection` 状态机按扁平 kwargs 调用；l3a 为此定义的生产适配器（`*_from_live`）自落地起零调用方。
- **修复**（2 行 + 测试 seam）：接线改为 `record_amended_open_success_context_from_live` / `record_amend_failure_after_fill_from_live`；同批修掉 `tests/test_live_service_lifecycle.py` 两处过期 seam（post-fill 用例 patch 引擎名，使不匹配被 fake 吃掉；close 回放用例 patch `live_service` 而符号已在 `live_close_settlement`，该用例自 l3 批次起恒失败）。
- **验证**：把接线改回旧名字，`test_entry_protection_amend_requires_fresh_matching_projection` 两个参数化分支都以生产同款 `TypeError` 失败；新名字下 83 passed。签名探针证明保护状态机产出的扁平上下文只绑定 `*_from_live`（引擎入口必然 `missing a required argument: 'request'`）。
- **待生效**：修复需重启加载；`no_new_risk_latch` 按设计只能由操作者释放（无自动释放路径），当前 `ready_for_live_execution=false` 的唯一 blocker 即它。
- **12:06 受控重启 + 12:08 解闸（用户授权）**：三服务 restart（`ActiveEnterTimestamp=2026-09-14 12:06:31`），启动即 `overlay restored hash=a3aa766d…`、live loop 接回（`recovery bootstrap confirmed broker has no open positions`）、`committed governance projection recovery attempted=100 current=100 degraded=0`；运行进程 `release_identity.head=0d716356`、`clean=true`（即加载的是含修复的工作区）。操作者按 cause 释放 `safety_freshness/entry_protection_initialization`（新增一条 `release_cause` 记录，证据含 position 288378714 已平仓、broker SL 4334.2、reconcile fresh、open_positions 0、fix commit `5833a827`）；随后 `backend_readiness_snapshot.v1` `ok=true/blockers=[]/ready_for_live_execution=true`、`live.loop.blockers=[]`、`phase=running`、`accepting_new_risk=true`。**尾证据（17:30 复核）**：修复后两笔确认成交（15:30 SHORT 288549050、16:00 SHORT 288557466）均记录 `ORDER+AMEND OK` + `attribution recorded open`，保护计划 `applied`（含 applied SL/TP）写入恢复行，`entry_protection_pending` 在 9~10 秒后由券商对账释放，无硬 latch、无 `confirmed_open_post_fill_processing_failed`；学习侧 288549050 产出 `trade_review_outcome` 样本 `integrity=full / train_weight=1.0 / governance_eligible=1`，对照事故单 288378714 的样本为 `integrity=missing`、权重 0（被拒）。

**开盘待验项取数（2026-09-14）**：

- **治理周期（提速批待验①）**：开盘后 11 个完整周期，`evolution_hourly` 总耗时 129.4~180.6s；其中治理清扫段（`mem after catalog` → `mem after redundancy`）43.7~58.0s（中位 45.5s），对照提速批前 09-11 存档同口径 74.2~95.1s（catalog 1895）；段内余量主要是冗余报告与批量预热，其后 `_expansion_preflight` 段 27~33s（未变）。
- **培育回放（待验②）**：02:17 UTC nursery 周期 96.5s 内 `run_bar_replay_evidence` 状态 `completed`；同参数（7d/80）直接实测 **24.7s** ≤ 30s。
- **因子发现闭环（待验③）**：开盘后已落地 21 条 `retire_factor` + 11 条 `promote_factor`（committed，config_version 5013→5036）；dsl 分布 604 RETIRED / 15 QUARANTINED / 1218 SHADOW / **1 PROMOTION_PREPARED**（修复前 594/5/1239/0）；catalog 1896→1939，RegistryAdapter 真实 register/unregister；每周期建议流 4 delegated + 3 `blocked_by_batch_guard` + 1 `blocked_by_evidence`，无证据洪泛。退出条件 (c) 未达：promote 动作证据仍带 `activation_blocker_codes=[promotion_not_prepared]`、`blocker_codes=[application_effect_not_mature_positive, controlled_active_canary_contract_missing, …]`（登记册已知自锁）。
- **学习建议应用（待验④）**：三条降权中 `macd_hist`（`gmut_d979fbe50e54…`）、`di_spread`（`gmut_ce9ca60577e7…`）已落账并 `observing`；`stoch_k` 行以「no actionable live weight」supersede 收口；仅剩 `rsi_14` 一条 `approved`（目标 0.89、delta 0.11 ≥ 回放门 0.10 阈值，当前被 grade C 报告挡在 `blocked_by_replay`）；`learning_workload_gate` 现返回 `run_new_facts`，不再恒 pending。`entry_cluster` live 消费已确认：`learning_same_direction_cooldown` 在本日真实入场路径上触发 2 次（15:35、15:40 同向持仓期间的同向加仓被拒），15:42 平仓后的重开尝试由 `supervisor_reentry_cooldown` 拒 1 次（15:45），16:00 冷却结束后同向重开正常成交。
- **监督候选链（待验⑤）**：自重启起无新 candidate/suggestion（最近一条 `brain_candidate_psv_28f1c69b85d44d32` 09-13 10:00 UTC 已 superseded），`position_supervisor_template` application/effect 仍为 0，暂无可验收的新证据。候选证据政策要求 `requires_clean_mature_counterfactual`，而反事实流自 09-11 起断流（本批已解堵，见 §3.9）；首条新复盘即来自 15:30 的监督员平仓（`protection_too_tight` 0.74、fully matured、绑定已验证），后续候选是否出现按登记册监督闭环退出条件跟踪。
- **慢 tick（待验⑥）**：最近 60 分钟 720 tick 内 0 条慢轮（0%）；06:00~11:55 慢轮占比 0.52%（22/4220；中位 7.0s、max 19.2s），其中 09:57~09:59 有 2 条带 `fresh_account_unavailable`。日志只记慢轮，比例法（慢轮数 ÷ tick 号极差）是唯一可算口径。
- **replay 准入重取**：修复后重取 `bar_replay_141377e246654736`（24.7s，code/config 绑定一致）grade **C** → `replay_evidence_grade_not_admissible`。原因是 7 天窗口切片此时落在 09-07 03:53~12:25 UTC，含 2 条无 RiskPolicy 判定的 `skip` 决策（`dec_007923710a504036` / `dec_7db0f4adb0224443`，09-07 11:25 UTC；覆盖率 0.975）与 5 对「双方都拒绝但理由不同」（live `supervisor_reentry_cooldown`/`learning_weak_signal_threshold` vs 重算 `non_positive_requested_volume`）。属旧行数据缺口（非本次改动引入），切片前移越过该段（约 09-14 19:25 本地）后评级自愈，否则需经治理通道补历史证据。影响面：仅 ≥0.10 的权重变更被 `blocked_by_replay`。另注：报告的 `runtime_config_hash` 绑定会随每次 RuntimeConfig 变更（治理周期 register/unregister、权重落账）漂移，重取后 12 分钟内即出现 `runtime_config_hash_mismatch`；准入要求「报告晚于最后一次 config 变更」。

**03:21 提速批复核（保留）**：改动 4 文件 +171/−10（`data/duckdb_store.py` 范围取数按月筛库、`data/external_loader.py` 等价滑窗 `_trailing_rank`、`backend/services/replay_harness.py` 决策加载时间下推、`backend/runtime/factor_governance_orchestrator.py` 周期内一次实验索引 + 批量预热准入证据）+ `tests/test_bars_month_range_filter.py`；回放整条链 520s → 23.1s、单次范围取 K 线 3.6s → 0.044s、候选筛选 43~49s → 0.02s、候选评估 7s/个 → 0.005s/个；批次验证 55 + 71 + 32 passed、`pytest -m smoke` 215 passed。

**本批（2026-09-14 18:06 反事实复盘流解堵，提交 `79ba699f`）**：

```text
Batch: 反事实复盘流准入（supervisor_counterfactual 的 close_reason 白名单纳入 chain_broken）
Canonical authority: backend.services.supervisor_counterfactual.evaluate_counterfactuals（唯一生产者，learning worker 每 30 分钟调度 materialize）；产物仍是 canonical_v2 counterfactual_review（advisory_only、governance_eligible=false）
Deleted paths: 无删除（白名单旧值 restart_replay 保留，供历史事件读取；L0-0R 后不再新产）
Targeted verification: tests/test_supervisor_counterfactual.py 10 passed（含 2 例新增：chain_broken 产出复盘、未列原因仍被过滤；注入错误过滤后新例以 0 != 1 失败）；监督相关 8 文件 79 passed
Migration/OpenAPI/build: 无变更
Runtime verification: learning worker 重启（18:06:20 本地，NRestarts=0）后 10:09 UTC `[evolution_coordinator] finished supervisor_learning in 10.3s`，即为 15:30 监督员平仓（288549050）产出首条新复盘 `live_counterfactual_scf_0375894ff3c82cbe_…`（protection_too_tight 0.74 / fully_matured / maturity.governance_eligible=true / selection_eligible=true / 模板绑定 binding_verified）；事件 198→199，最新 09-11 00:35 → 09-14 15:42
Remaining compatibility: broker_close 且无执行动作的平仓继续判 not_executed；缺 entry/close 价格的 close 由价格门跳过；只读复算与生产写入共用同一函数（探针一律 materialize=False）
Unresolved live evidence: selection 首个可治理候选（监督闭环退出条件，登记册 §1）
Next batch: 旧债登记册 §1「recovery_position_state.attribution_integrity 无运行时写入者」（用户已裁定下一批修）
```

同内容绑定重取回放报告：`bar_replay_3047a2d6c2f54662`（24.4s，code_version 随本次 Python 内容更新——切片仍落在 09-07 缺口段，`matched 74/80、mismatch 13`，grade 口径见上文「replay 准入重取」）。

**本批（2026-09-14 18:38 恢复表完整度列补写入者）**：

```text
Batch: recovery_position_state.attribution_integrity 在平仓时写入 review 值（原无运行时写入者）
Canonical authority: RecoveryPositionStore.mark_closed（恢复表唯一的平仓写入者，经 mark_recovery_position_closed 单点透传）；取值 authority 仍是 trade review（本列只做镜像，不自创值）
Deleted paths: 无删除（开仓 upsert 不动：未平仓时 unknown 语义正确；retire 外层 mark 不传值：内层 replay 已写，COALESCE 保证不覆盖）
Targeted verification: tests/test_live_recovery_position_store.py 新增 2 例（带值写入 full、不带值保留 chain_broken；去掉写入后两例均失败）；平仓相关 6 文件 195 passed + test_live_service_lifecycle.py 83 passed
Migration/OpenAPI/build: PG 无变更（0037 列已在）；sqlite 建表语句同步加列（测试/开发镜像与 PG 对齐）
Runtime verification: 今日 3 行 unknown 按各自 review 回填（288549050 full、288557466 full、288378714 missing），现全表 0 unknown；后端 18:38:37 重启加载新码，ok=true/blockers=[]/ready_for_live_execution=true，live loop blockers=[]/phase=running/accepting_new_risk=true
Remaining compatibility: 无值调用方（旧 mark/retire 外层）行为不变；列暂无读取方，learning_eligible 仍读 review
Unresolved live evidence: 下一次真实平仓写入与其 review 一致（见 §3.10）
Next batch: 常态观察（selection 首个可治理候选、因子 exit (c)、rsi_14 blocked_by_replay 自愈）
```

## 3. 未完成 / 待复核证据

1. **完整生命周期闭环**：S7.6 终验标准已达成并持续（基准 `trade_review_outcome full/1.0 46`，`2026-08-21 → 08-28`）；后续只做常态观察，当前计数、skip/rejected 双轨与 supervisor trace 分级以现查 `canonical_v2` 为准。
2. **监督治理闭环**：`supervisor_execution_trace` 合格成熟样本 ≥10 笔已满足（2026-09-13 现查 55 笔）；`tighten` 覆盖门 2026-09-08 `ca23580e` 已退役（`close` 经 keep→supportive 计入干预证据；`reduce` 永久关闭）。剩余缺口是候选链：32 条治理合格 advisory 建议因缺 V16 bridge 证据全部 `superseded`，`position_supervisor_template` 的 application/effect=0，`f16024bb`（09-11）删除 medium-impact 产线后该 scope 无候选生产者；2026-09-13 `0d77157d` 已补入专员产线（`delegate_supervisor_template_switch` 产出 candidate+`delegate` 命令），同日回放 09-10 验证了首跳与 bridge，并修复两个门定义缺陷（`6ed0d878`）；2026-09-14 开盘复核：自重启起无新 candidate/suggestion（最近一条 `brain_candidate_psv_28f1c69b85d44d32` 09-13 10:00 UTC 已 `superseded_by_governance`），`position_supervisor_template` application/effect 仍为 0，本月首条 application 仍待新证据；退出条件见 [legacy-debt-register.md](legacy-debt-register.md)。
3. **`policy_suggestion` 无背书 applied 行（2026-09-10 查证结案，用户裁定不处置）**：112 条 `status=applied` 且 `applied_mutation_id=''`（factor `update_weight` 102、`rollback_factor_action` 10，`created_at` 2026-08-23 20:31 → 09-08 12:43），全部 `governance_eligible=0`。查证结论：
   - 该族行只可能由 `FactorGovernanceOrchestrator._record_policy_suggestion()` 写出（`reason` / `review_note` / `fgv` 前缀全仓唯一），但**当前代码写不出**：它在算 `suggestion_id` 之前就对 `applied` 早退（探针实测 `applied → ''` 且 0 次 DB 调用，`proposed` 才触达 DB；该守卫自 2026-07-19 起存在于该文件全部 67 个修订）。
   - `suggestion_id` 是 `(writer, scope, key, action, evidence, status)` 的 sha256，按库里 evidence 重算只与 `status='applied'` 命中 → 写入发生在带守卫之前的代码变体（工作区长期未提交，历史差异不可复原）。
   - 这些行引用的 `decision_id` 在 `evolution_decision` 中不存在，窗口内无 `governance_mutation_intent`，evidence 无 `gmut_`；09-08 12:54 重启后不再新增（其后仍持续产生 decision）。
   - 影响面已 fail-closed：`live_committed_policy` 对空背书行只标 `legacy_quarantined`（且仅限非 strict + 收紧动作）；`brain_governance_candidate_review` 要求 `governance_eligible=1`；`proposal_registry` 中 `fgv*` 来源 0 条。
   - 处置：**裁定不清账、不新增守卫**（现状无授权影响）。旧口径"27 条无背书 applied 幽灵行"作废；`run_artifacts/policy_suggestion_ghost_cleanup.py`（D2 一次性脚本）从未以 `RUN=1` 执行，且快照序列化损坏（写出的是列名）、对象族标注错误，**不得直接复用**，如需清理必须重写。
4. **V16 认知层退役（2026-09-11 完成）**：停写 → 载体解耦（`posterior_fingerprint` + 自校验）→ 代码删除（净 −5.8k 行、−9 端点、`brain_action_plan` 提案来源）→ `pg_dump` 存档（`run_artifacts/v16_cognition_retirement_20260911.dump`，74.9 MB/6 表）→ v35 迁移删除 6 张表（423MB → 15MB）。保留 `v16_brain_command`、`brain_governance_candidate`(+`_review`)、`brain_memory`、`brain_live_ready_guardrail`。详见 [legacy-debt-register.md](legacy-debt-register.md) §1。
5. **索引/契约欠账**：依赖 `factor_name` / `lifecycle_stage` / `runtime_admission` 的 `idx_factor_lifecycle_*` 索引仍未建（0028 已补齐这些列，当前仅 `idx_factor_lifecycle_unique_name` 存在）；是否补建按后续性能证据决定。
5. **每次发布门重取**：process-loaded flags、PID、fingerprint、release preflight 证据；当前源码绑定的 execution/safety fault matrix attestation；Safety 在 enforce 姿态下的连续性与完整 broker position lifecycle 证据。
6. **因子发现闭环（2026-09-13 修复批；2026-09-14 开盘已见首批动作）**：五处结构缺陷已修（方向契约投影、轮转饿死、退役人口判据、证据时钟、评估覆盖）。开盘后第一批真实动作已落地：21 条 `retire_factor` + 11 条 `promote_factor`（committed，config_version 5013→5036），dsl 分布 604 RETIRED / 15 QUARANTINED / 1218 SHADOW / 1 `PROMOTION_PREPARED`（修复前 594/5/1239/0），catalog 1896→1939；无 `blocked_by_evidence` 洪泛。退出条件 (c)（首个 `origin=dsl` 因子达 ACTIVE）仍未达：promote 证据带 `activation_blocker_codes=[promotion_not_prepared]` 与 `blocker_codes=[application_effect_not_mature_positive, controlled_active_canary_contract_missing, …]`。详见 [legacy-debt-register.md](legacy-debt-register.md)。
7. **学习建议应用闭环（2026-09-13 修复批；2026-09-14 开盘部分验收）**：三条降权中 `macd_hist`（`gmut_d979fbe50e54…`）与 `di_spread`（`gmut_ce9ca60577e7…`）已落账并 `observing`；`stoch_k` 行以「no actionable live weight」supersede 收口；`learning_workload_gate` 现返回 `run_new_facts`。未闭环一项：`rsi_14` 仍是唯一 `approved` 行（目标 0.89、delta 0.11），被 grade C 的 replay 切片挡在 `blocked_by_replay`；`entry_cluster` live 消费已确认（2026-09-14 15:35/15:40 `learning_same_direction_cooldown` 拒绝同向加仓、15:45 `supervisor_reentry_cooldown` 拒绝平仓后重开、16:00 冷却结束正常成交）。
8. **post-fill 记录接线断裂（2026-09-14 发现并修复，已验收）**：详见 §2；验收线 = 重启加载新码 + 操作者释放 `no_new_risk_latch` 后，下一笔确认成交的正常走通 `record_*_from_live` 与恢复/归因记录，且不再出现 `confirmed_open_post_fill_processing_failed`。**2026-09-14 17:30 已验收**（15:30 / 16:00 两笔：`ORDER+AMEND OK`、attribution recorded、恢复行 applied、学习样本 `integrity=full`；无硬 latch），条目已从旧债登记册移除。
9. **反事实复盘流断流（2026-09-14 发现并同批修复，已验收）**：`counterfactual_review` 曾停在 2026-09-11 00:35（198 条），因准入白名单缺 `chain_broken`（L0-0R 后恢复路径已不再产 `restart_replay`），监督员主动平仓的 288549050 因此没有复盘，`position_supervisor_selection.v1` 的 `requires_clean_mature_counterfactual` 随之无法满足。修复 `79ba699f`（白名单纳入 `chain_broken`，执行动作与成交价两道门不变）后 learning worker 重启（18:06 本地），10:09 UTC 周期即为该笔产出首条新复盘：`protection_too_tight` 0.74、`fully_matured`、`maturity.governance_eligible=true`、`selection_eligible=true`、绑定 `binding_verified`，事件 198→199。剩余观察：selection 首个可治理候选按 [legacy-debt-register.md](legacy-debt-register.md) §1 监督闭环退出条件跟踪。
10. **恢复表完整度列补写入者（2026-09-14 已修，待一次真实平仓验证）**：`mark_closed` 透传 review 值（`COALESCE` 不覆盖已有值）、三条平仓路径接线、今日 3 行 `unknown` 已回填、全表 0 `unknown`，后端 18:38 重启生效（详见 §2）。退出条件收窄为：下一次真实平仓后该行写入与其 review 一致的取值。

上述证据不能由单测、历史快照或 readiness 替代；未满足前不推进后续静态开关，也不把 readiness ready、单次 bridge 或单次 effect 解释为自治毕业。

## 4. 下一批处理顺序

1. 对 [legacy-debt-register.md](legacy-debt-register.md) 中仍在 `active` / `migrating` / `monitoring` 的路径逐项收集退出证据，canonical 验证后同批删除旧路径。
1b. ~~用户已裁定下一批修：`runtime.recovery_position_state.attribution_integrity` 补写入者~~——2026-09-14 18:38 已执行（见 §2 本批），剩余 §3.10 一次真实平仓验证。
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
