# 全项目分期修复发布状态

> Status: active current-state index
> Last verified: 2026-09-27 00:25 CST（**参数模板自锁修复已加载并双向验证**：23:47:41 backend / 23:48:18 learning worker（用户授权），`release_identity.head=e72584d2`、`clean=true`、两端 `overlay hash=4af21662…` 一致、`recovery bootstrap attached 1 live positions after restart`；治理侧 23:53 轮 `evolution_hourly` **114.9s** 且 `handoff_parameter_template_*` 计数冻结在 4,024 / 7,076 ⇒ 不再写交接单；学习侧 00:12 轮 `parameter_template_auto_materialize` **`skipped_existing` 20 → 0、`suggested` 0 → 6** ⇒ 物化腿跑通。同时暴露下游两道闸（`job_enqueue_did_not_return_row` ×13 与 governor 判切换建议 `eligibility_contract_invalid` ×6），待下一批修；`observation_window_in_flight` 首次产线命中已取得。**2026-09-27 续：第一道闸（eligibility 契约）已修在代码侧（未加载，待受控重启），见 §2 本批**。逐项证据与展开见 §2）
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
- **慢 tick（待验⑥，已达退出线并销账）**：19:00 复核 17:45~18:45 窗口 0 条慢轮（0.00%）、`account_blockers=[]`；早间窗口 720 tick 0 慢轮、全天 33/6246 = 0.53%，均 ≤5%。该条已从旧债登记册移除（追溯走 Git）；若慢轮重现则按 `regressed` 重新登记。日志只记慢轮，比例法（慢轮数 ÷ tick 号极差）是唯一可算口径。
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

**本批（2026-09-14 19:36 回放缺输入分歧改记 input gap，提交 `626a146e`）**：

```text
Batch: 回放风控重算中"双方都拒绝、重算栽在手数短路"改记 input gap（原记 disagreement，单条即判整份 C）
Canonical authority: ReplayHarnessService（唯一生产者；learning worker 调度 + 手工重取共用同一函数）；分级语义 authority 仍是模块自带分类学（缺输入≠分歧，沿 e537c96f 原则）
Deleted paths: 无删除（门重算、子动作重算不动：前者无手数概念且 0 分歧，后者不进分级）
Targeted verification: tests/test_replay_release_evidence_contract.py 新增 2 例（分类器单测 + 接线级 wiring 测试，注入破坏后均失败）；回放/权重/学习 88 passed。修中自检出接线 bug（else 分支丢失致双计）一次，重取报告前已修复并由 wiring 测试锁住
Migration/OpenAPI/build: 无变更
Runtime verification: 重取报告 `bar_replay_43bcab2d43a14a70`（23.1s）grade **B**（分歧 0、input gap 22、覆盖 0.95、门 0 分歧）→ 准入 ok/fresh；learning worker 19:36 重启；11:42 UTC 周期 175.1s 结束，`rsi_14` 落账 applied（`gmut_e0b45ded26be4a5d833fac7b843cbf62`，1.0→0.89）
Remaining compatibility: 真翻转（允许↔拒绝）、有手数下的门分歧照样记分歧；该批当时把"无 verdict 的 skip 只影响覆盖率"写进了口径，实际代码同时计入 mismatch（见 §2 2026-09-18 批的订正）
Unresolved live evidence: 无（学习闭环 exit (a)(b) 全齐，条目已从旧债登记册移除）
Next batch: 常态观察（selection 首个可治理候选、因子 exit (c)、恢复表首平仓验证）
```

**本批（2026-09-14 22:35 受控重启 + 回放重取 + 发布登记，用户逐项授权）**：

```text
Batch: 三服务重启加载 420a7609（含 f1b6647a 平仓归因写入者 + 626a146e 回放 input gap）→ 同参数重取回放 → 新开并完成发布登记
Canonical authority: ReleaseControlService.start_release（发布登记唯一写入者，checklist 自当前 readiness 现算）；ReplayHarnessService（回放唯一生产者）；RecoveryPositionStore.mark_closed（恢复表平仓唯一写入者）
Deleted paths: 无删除（release_run 只增新行供追溯；旧回放报告保留）
Targeted verification: 无新增测试（ops 动作无行为变更；前两批回归测试已锁住改动）
Migration/OpenAPI/build: 无变更（migration 37/37 ok）
Runtime verification: 三服务 22:35:24 重启（NRestarts=0），接口与 worker 心跳 head=420a7609/clean=true；回放 bar_replay_45e518d892da4fe1（23.6s，同 7d/80 参数）grade A（分歧 0，input gap 口径生效）；发布 release_08c0611f46874feb completed；快照 ok=true/ready_for_live_execution=true/ready_for_release=true/blockers=[]；live loop 自 tick 1 重跑，22:50 已 tick 171、无持仓
Remaining compatibility: 发布登记每小时被 nursery 周期（:17 UTC）刷新属常态机制；下次生产 .py 改动后发布灯会重新变红，需重取回放 + 新登记
Unresolved live evidence: 无（本批全部闭环）
Next batch: 常态观察（selection 首个可治理候选、因子 exit (c)）
```

**本批（2026-09-17 发布门拆除，用户批准"拆门，验证挪到使用点"）**：

```text
Batch: 删除 release 准入红绿判定（ReleaseControlService.status/close_stale_started_release）、nursery 自动补发布与 6h 回放节流、ops 透传参数与 OpenAPI 快照字段；账本只做审计，实盘解锁改读当前配置快照 + replay 有效性
Canonical authority: ReplayHarnessService（回放唯一生产者；权重写入独立准入与解锁证据共用）；current_runtime_config_snapshot + read_runtime_config_payload（回滚证据）；ReleaseControlService.start/finish（审计账本唯一写入者）
Deleted paths: status 重验、watchdog 收口、_create_release_evidence/_release_missing/_replay_interval_gate、cycle release blockers/steps、readiness release 段、v15/brain release 门、contract 与 watchdog 旧测
Targeted verification: 统一重跑 129 passed（cycle/contract/platform_phase0/live_autonomy/readiness 85 + ops/api 44）；live _blockers 隔离 smoke（缺 snapshot 挡/有效通过/坏 replay 挡）；git diff --check 干净、migration 37/37 ok
+Runtime verification: 2026-09-17 01:09:45 三服务受控重启（NRestarts=0），接口 head=4b53a42d 指纹已变（新码加载）；readiness 01:12:07 ok=true、无 release 段、可交易 true；nursery 01:18:53 新进程完成 113.7s，窗口内无 release/blocker 日志，cycle 文件 release 引用 0；live loop 重跑并正常开仓（pos=1）
Unresolved live evidence: 无（本批闭环；启动期两个慢轮 total 6~7s、account_blockers=none，属重启预热单样本，不断言回归）
Next batch: 常态观察（selection 首个可治理候选、因子 exit (c)）
```

**本批（2026-09-17 持仓监督反射层，用户批准整批执行）**：

```text
Batch: tick级P&L反射（breakeven/lock/close三档，给回吐0.35/0.55/0.90，姿态独立，安全门之下姿态分支之上）；reflex_policy进三份代码模板（默认开，总开关可治理关闭）；弱入场短绳（entry_score|<0.55|时阈值x0.6，entry_score由开仓内存直通，不新增热循环读库）；hold心跳（结论不变每900s留痕，复用trace通道）；姿态tag在反射触发时保留
Canonical authority: 模板mutation（唯一参数写入者）；tick内tighten/close通道（唯一执行者）；ReplayHarnessService（唯一回放裁判）
Deleted paths: 无（trend_hold打标保留，只加动作分支）
Targeted verification: 302 passed（supervisor全家+live保护/生命周期+v16+因子治理，含新增反射/短绳/节拍3用例；2旧用例按梯度语义更新，动作一致）；bar_replay A x2（80/80，mismatch全系旧窗口数据缺口）；git diff --check干净
Scoreboard baseline（30d/403笔，冻结对照）: 期望值/笔-0.94，PF 0.785，MFE捕获率-0.04，maxDD -444.45，bad_loss 200/403
+Runtime verification: 20:26:37与20:37:19两轮三服务受控重启（NRestarts=0，overlay restored，无闩）；持仓289905563对账找回；health全绿；启动瞬态latch 25s内自解；当日回撤11.8%告警为通知
Unresolved live evidence: 首条reflex_*真实动作日志+洗出率（24h观察）；MFE捕获率是否抬头；Phase3（第二意见/日内适应）视数据再定
Next batch: 反射观察 + selection首个可治理候选 + 因子exit (c)
```

**本批（2026-09-18 回放分级改用"RiskPolicy 未到达"显式权威 + entry-cluster denial reason 漏配；2026-09-19 01:04 CST 重启后验收）**：

```text
Batch: 回放分级不再用"缺 verdict"反推 RiskPolicy 未运行；补 entry-cluster 同向冷却这一 live-only denial reason
Canonical authority: `review_contract.risk_policy_not_reached`（新增只读谓词；该事实由 `live_tick_pipeline.build_skip_ledger_payload` 以 `risk_stage=not_reached` + `risk_policy_reached=false` 写入，API 与 replay 共用同一判据）；分级唯一计算者仍是 `ReplayHarnessService._build_report` + `_p1_replay_grade`
Deleted paths: `backend/api/risk.py._recent_policy_verdicts` 内联的三条 `skip_stage/risk_stage/risk_policy_reached` 反推检查（改调谓词，行为不变）；`_build_report` 中"无 verdict 即 row_issue"的单一分支（改为 pre-policy skip / 真空缺两分支）
Targeted verification: tests/test_replay_release_evidence_contract.py 14 passed（新增 2 例：显式标记的 skip 既不 mismatch 也不进覆盖率分母且评级可到 A；缺标记的 skip 仍记 mismatch 并落 C —— 后者注入破坏即失败；`_is_live_state_only_denial` 增补 learning_same_direction_cooldown 断言）；phase0/risk api/fact views 合计 77 passed；`pytest -m smoke` 215 passed
Migration/OpenAPI/build: 无 schema 变更；migration check ok（37/37，无 mismatch）；ASGI smoke 通过；`scripts/check_openapi_snapshot.py` 不在服务器 sparse checkout 内未跑（本次未改任何端点签名，只在 replay_report.metric_summary_json 内新增两个键）
Runtime verification: **2026-09-19 01:04:44 CST（17:04 UTC）三服务受控重启已加载本批**（NRestarts=0，recovery bootstrap 确认券商无持仓，tick 从 1 重跑，无闩无 blocker）；同批重取回放 `bar_replay_d3dabc1e3b7345ee`（29.9s，lookback 7d/limit 80）grade **A**，`pre_policy_skip_count=20`、`risk_verdict_decision_count=60`、`risk_verdict_coverage=1.0`，`status()` → ok/fresh/blockers=[]（重启前该报告恒 C，20 条 skip 型 mismatch 全部消失）
Remaining compatibility: 报告新增 `pre_policy_skip_count` / `risk_verdict_decision_count` 两键；历史 replay_report 行不回填；全为 pre-policy skip 时 verdict 覆盖率按 fail-closed 记 0（不升 A）
Unresolved live evidence: 本批两条均已收口——① 回放重取达 A（见上）；② ≥0.10 权重通道恢复落账（重启后 17:24:39 UTC 两条 `factor_governance_update_weight` application 状态 applied，此前自 09-17 重启起 0 条）
Next batch: canary 自振荡与 effect 积压终态化（现查 active effect 55 / 预算 24，`learning_experiment_admission.reserve_scope` 因此对任何单 scope 实验恒返回 blocked_global_experiment_budget —— 这是监督模板链上独立于 claim 的第二道闸）
```

**本批（2026-09-18 B2：实验预算积压终态化 + canary 晋升纳入同一预算；2026-09-19 01:04 CST 重启后验收）**：

```text
Batch: 观察窗时钟改用账本行 `created_at` 单一权威（恢复"24h 未成可比较 baseline 即收口 inconclusive"的既有合同）；因子激活 canary 实验纳入全局实验预算
Canonical authority: 观察窗起点 = `RuleEvolutionGovernor._app_ts`（cycle_ts→`learning_application_log.created_at`），`research.learning.effect_reconciliation.evaluate_application_effect` 只接收该值（参数 `observation_start_ts`）不再自算；全局预算唯一计算者 `LearningExperimentAdmissionService.global_budget()/global_slot_available()`（新增，收口原 4 处重复读 env + pytest 例外）；分级语义仍由 `research.learning.application_effects.observation_window_expired`（纯分类器，参数随之改名）
Deleted paths: `effect_reconciliation` 内两处 `app.get("cycle_ts")` 反推（生产 136 条 application 的 details_json 全部无该键 → 时钟恒判 invalid → 过期腿永不触发）；`reserve_batch` / `reserve_batch_in_transaction` / `reserve_scope` / `evaluate` 四处重复的预算解析与 pytest 例外块（`evaluate` 与 `reserve_scope` 由此获得原本只有 reserve_batch 才有的同一条例外，规则统一到一处）
Targeted verification: 150 passed（research governor/application_effects/rule_learning_pipeline + learning_experiment_admission + learning_effect_quality + factor_governance_effect_tracker + 因子治理编排全家 + factor_lifecycle_service）+ 146 passed（weight_change/autonomous_learning/governance_runtime_controls/acceleration_flow/nursery_exploration_budget/proposal_registry/supervisor_templates/agent_scorecard/entry_quality）；新增 2 例并做注入破坏：① 生产形态行（无 details cycle_ts）旧测试判 observing 不收敛、新例判 inconclusive 且 `observation_clock_valid=true`、start_ts 与行时间一致；② 预算满时激活腿不产出 mutation 且审计落 `blocked_global_experiment_budget`（去掉门后 `activated` 多一项即失败）；`pytest -m smoke` 215 passed；`git diff --check` 干净
Migration/OpenAPI/build: 无 schema 变更（migration 37/37 ok 未受影响）；无端点签名变更
Runtime verification: **已加载**（2026-09-19 01:04:44 CST 三服务受控重启，与 B3/B1 同批）。首个周期（17:17 UTC nursery）实测：active 实验 **54 → 13**、`global_slot_available()` False→True，`causal_status=observation_window_expired_inconclusive` 计数从 **0 → 41**（重启前全表从未出现过该状态，证明过期腿在生产里从未触发过）；剩余 13 条 observing 行龄全部 <24h，即观察窗按合同回收。`blocked_global_experiment_budget` 不再恒发（17:04 后新增 0 条该理由事件），17:24:39 UTC 两条 `factor_governance_update_weight` application 落 applied
Remaining compatibility: 历史 effect 行内已 stamped 的 `observation_window.start_ts=0` 不回填（读取侧只用新行）；`observation_window_expired` 参数改名不影响调用方（全 keyword-only，仅 1 处生产调用 + 2 处测试）；canary 激活被预算挡住时因子保留 `PROMOTION_PREPARED` 租约（168h）等下一周期，不做退役/隔离
Unresolved live evidence: ① 已验：active 54→13 落到预算内，≥0.10 权重通道恢复落账（17:24 两条 applied）；② 仍待观察：canary 稳态占用实测值（当前 13，估算区间 12~24：约 0.9 次激活/小时 × 24h 观察窗，再扣约 28% 提前隔离 churn —— 21/74 激活的 effect 已 rolled_back）—— 若长期贴着 24 挤占预算，操作者可用既有旋钮（`QUANT_LEARNING_MAX_ACTIVE_EXPERIMENTS` / `factor_governance_max_promotions_per_cycle`）调节，本批不新增阈值
Next batch: B1（V16 命令 authority 寿命 1800s vs 每小时领取；桥落在已被顶替的 candidate 代际上）—— 监督模板链三道闸的最后一道；随后一次性受控重启 + 回放重取验收 B3/B2/B1
```

**关于"canary 自振荡"的实测更正（本批只读定位，不改代码）**：旧口径「25.5h 内 50 promote / 12 rollback / 105 quarantine = 同一因子反复晋升」不成立。`promote_factor` 对同一因子合法地出现两次（SHADOW→`prepare_promotion` 与 PREPARED→`activate` 两条腿都记这个动作），dsl 全量 `generation` 均为 1（无代际反复）。真实抖动来自另一条腿：激活后 canary 投影在 OOS 判据（`canary_min_oos_pnl=0.0`，80 根）下不过 → 注册表投影落 `SHADOW/QUARANTINED` → `_rollback_failed_actions` 判 `persisted canary regression` 隔离 → 生命周期行跟随隔离并把该因子的 effect 立即终态化（rolled_back）。这部分属梯子判据（R3 因子轨），不是预算账本缺陷，本批不动。积压的 53 条**不是**被抖出来的，而是存活 canary 的观察窗永远不到期（时钟死腿）。

**本批（2026-09-18 B1：终态候选遗留的孤儿桥建议把监督模板链锁死；2026-09-19 01:04 CST 重启后验收）**：

```text
Batch: 候选终态后闭合其未执行的 bridge 建议，释放同 control surface 的后继候选
Canonical authority: `BrainGovernanceCandidateService.reconcile_submitted_bridges`（候选↔桥建议这一对的双向对账唯一写入者，唯一调用者 `V16BrainOrchestratorService.run_once`，`persist=True` 才写）；建议状态机本身不变（`policy_suggestion` 仍由桥接写入、由 apply 路径落 applied/rollback），候选状态仍只由 `sync_candidate_suggestion_lifecycle` 正向投影
Deleted paths: 无删除（只把原查询遗漏的"候选终态 + 建议仍 open + 无 mutation"这一族纳入同一对账；未新增 reconciler、线程或调度器）
Targeted verification: tests/test_agent_coordination_fixes.py 9 passed（新增 1 例同时锁两个方向：superseded 候选的 approved 桥建议被 supersede、`awaiting_execution` 候选的建议保持 approved；把判据短路后该例以 0 != 1 失败）；v16/governance/supervisor/pruning/registry/scorecard/ops-api 合计 104 passed；`pytest -m smoke` 215 passed
Migration/OpenAPI/build: 无 schema 变更、无端点签名变更；`reconcile_submitted_bridges` 返回新增 `orphan_bridge_count` 一键（`run_once` 非 persist 分支同步补 0）
Runtime verification: **已加载并验通首跳+桥接**（2026-09-19 01:04:44 CST 受控重启，与 B3/B2 同批）。重启前死锁实况：唯一 approved 桥建议 `brain_bridge_676cd401…`（09-17 13:20）的候选 `psv_7598badad8101059` 已于 09-18 13:20 因 24h TTL 被 `reconcile_expired_candidates` supersede，建议仍 approved 且 `applied_mutation_id=''`；当前 ACTIVE 候选 `psv_acb298d43854f933`（09-17 17:46，`min_thesis_break_seconds` 900→300 / transition_confirming）四次评审全部 `conflict_detected`（`bridge_ready=0`、`evidence_gaps=[]`，冲突项就是那条孤儿建议），而孤儿自身因候选终态不可 claim —— 双向死锁。17:17:xx UTC 首次 orchestrator 对账后逐项落地：孤儿建议 → `superseded`（note "superseded: owning candidate is superseded"）；后继候选评审 → **`bridge_ready=1`**（"supervisor template evidence ready"）；候选 → `awaiting_execution` 并写出新建议 `brain_bridge_332b34da…`（approved）；同时签发新命令 `v16cmd_8ede18e17f4c02a4ceef_rfbc623c9dc56`，状态 **`available`/`claim_attempts=0`** —— 本 lane 全历史 80 条命令此前**无一条**处于可用态（全部 cancelled）
Remaining compatibility: 只闭合 `proposed/approved` 且无 mutation 的建议；`applied` 建议与已 finalize 链不动；历史 `supervisor_experiment_admission_blocked`/`candidate_not_active` 命令行保持终态审计（不复活）
Unresolved live evidence: ① 已由 §2 末批（B1-末）接管：01:47 周期实测 claim **成功**并抵达 Coordinator，被事务内复验判 `v16_command_candidate_binding_invalid` 而 abort —— 桥孤儿死锁确已解除，断点后移到复验漏列；② `test_supervisor_hold_trace_is_deduplicated_by_decision_evidence` 在 HEAD 即为红（见旧债登记册），与本批无关
Next batch: 见下方 B1-末（同一受控重启批）；随后按实测数据处置反射层与 hold 心跳（含上述红测）
```

**本批（2026-09-19 B1-末：`validate_claim_in_transaction` 漏选 `evidence_json`，真实候选链在 Coordinator 事务内必被误判绑定无效）**：

```text
Batch: 补齐事务内复验的命令列，使 review-bound 候选链可消费自己 claim 的命令
Canonical authority: `V16CommandGate.validate_claim_in_transaction`（Coordinator 事务内复验的唯一执行者；绑定判据本身仍只由 `_candidate_binding_is_valid` 计算，claim 路径 :366 与复验路径 :536 共用同一函数，本批只补它读取的列）
Deleted paths: 无删除（未新增校验、阈值或第二套复验；同时把 `observation_window_expired` 的参数改名回退，避免动 `tests/research/*` 冻结验收测试——时钟权威修复保持在 `governor`→`evaluate_application_effect` 的 `observation_start_ts` 传参上）
Targeted verification: tests/test_v16_command_finalize.py 8 passed —— 新增 `test_candidate_bound_claim_survives_in_transaction_revalidation`（真实 candidate+review+suggestion+带 `governance.candidate_review.evidence_fingerprint` 的命令；把 SELECT 里的 `evidence_json` 去掉即复现为 `v16_command_candidate_binding_invalid`，与生产同一状态串）；tests/research/* 两个冻结文件已 `git checkout` 回 HEAD，frozen+entry/v16/agent-coordination/factor-governance 合计 107 passed；B2-1 观察钟回归从 `tests/research/test_rule_evolution_governor.py` 迁到 `tests/test_entry_quality_governance.py::test_ledger_row_created_at_is_the_observation_clock`（把传参改回 0.0 即复现"永不到期"，红→绿已验证）；`pytest -m smoke` 215 passed
Migration/OpenAPI/build: 无 schema 变更，纯 SELECT 列补齐
Runtime verification: **已加载（2026-09-19 02:04:46 CST 三服务受控重启，NRestarts=0，recovery bootstrap 确认券商无持仓，治理投影 attempted=100/current=100/degraded=0，指纹由 `4ea81bc5…` 变 `f701468c…`）**。重启前实测（01:42 CST 周期，352.3s）：`demo_autonomy_apply` 于 01:47:51 落 `supervisor_templates.items[].mutation.status=aborted`，`error=GovernanceMutationError: v16_command_candidate_binding_invalid`，`mutation_id=gmut_3666adad7004467095d8386efd3c0c6c`（intent 现查 `error_stage=transaction`），命令 release 回 `available` 后于 01:55 被清扫判 `candidate_not_active`。重启后第一轮（02:12 周期，02:17:39 落 `demo_autonomy_apply`）：该 lane 现无可用命令，apply 侧正确 fail-closed 为 `v16_command_required`（不是伪造消费），同候选链未产生新 application；02:18 `reconcile_expired_candidates` 把 24h TTL 已到期的候选 `psv_acb298d43854f933` 置 `superseded`，其仍 approved 的桥建议 `brain_bridge_332b34da…` 随即被本批 B1 反向腿写成 `superseded`（note "superseded: owning candidate is superseded"）——**同一判据在生产里第二次独立验通**。B2 侧同时可见预算排队生效：canary 激活恢复后 active 实验 13 → 15（`activate_factor_canary` 01:55:51），`global_slot_available()` 仍 True。
Remaining compatibility: 无 candidate 行的合成专家命令仍走 `_candidate_binding_is_valid` 早退分支（`factor_governance_update_weight` 等权重通道历来如此），本批不改变其语义
Unresolved live evidence: 首条 `position_supervisor_template` application + effect + rollback 痕迹。链路本身已无已知阻断，缺的是**一次新的授权**：需先有新的成熟监督复盘 → advisory 产出新 candidate → 评审 `bridge_ready=1` → 桥接 approved 建议 → V16 签发 `available` 命令（SSoT 规定旧命令 cancelled 后必须有新的 `bridge_ready` review 才允许新建），随后在 `12,42` 周期被 claim 并由本批修复后的复验放行。属数据依赖，不以 SQL 或人工改状态代为产出。
Next batch: 观察该 lane 首条 application（含 effect/rollback 痕迹）；随后按实测数据处置反射层与 hold 心跳
```

**本批（2026-09-19 C：监督上下文的 broker 组件状态透传 + a8f3f0df hold 心跳退役）**：

```text
Batch: 把 broker 已发布的 current_price_state / pnl_state 带入 position supervisor 评估上下文，恢复 live 与 replay 的数值分支可达性；按裁定退役无产出的 hold 心跳腿
Canonical authority: 组件状态生产者 `execution/ctrader_bridge.py`（spot/execution 事件写 price 状态、专用未实现盈亏 RPC 写 pnl 状态）→ 唯一读取者 `live_position_lifecycle.position_component_state` → 唯一带入点 `build_position_supervisor_context_payload`；判定仍只在 `position_supervisor._component_known`（缺状态 unknown，fail-closed 语义不变）；开仓时点 composite score 的唯一读取者收口为 `live_position_lifecycle.entry_score_for_position` + `live_service._position_entry_score`（与既有 `_max_abs_entry_score_for_positions` 同族）
Deleted paths: a8f3f0df 的 hold 心跳腿整体删除（`SUPERVISION_HEARTBEAT_INTERVAL_SECONDS`、`_SUPERVISION_HEARTBEAT_LAST_TS`、`supervision_heartbeat_due`、hold 分支 `else` 的 `stage="heartbeat"` 写入）及其专用 cadence 测试 `tests/test_position_supervisor.py::test_supervision_heartbeat_cadence_rule`；`live_supervision_runtime` 内联的 `_pos_entry_scores` ad-hoc getattr 读取块；旧债登记册"hold 心跳去重合同红测"条目（退出条件已按"退役该腿"取用，追溯走 Git）。保留：hold 的 `stage="evaluated"` 首次去重 trace、safety plane / market_closed_pending / readiness / worker 四类同名心跳
Targeted verification: 185 passed（test_live_position_lifecycle + test_position_supervisor）、83 passed（test_live_service_lifecycle，含原 HEAD 红测 `test_supervisor_hold_trace_is_deduplicated_by_decision_evidence` 转绿并补 `log_position_supervisor_evaluation` 假件与 bar 级单写断言）、74 passed（supervision actions / path metrics / safety planner / safety plane / candidate execution / shadow observation / binding）；两处上下文契约测试分别钉住 known 透传与"缺状态仍 unknown"
独立审查: 监督 runtime 变更按钩子要求经独立审查后加载。审查纠正了一处本批定位错误：**replay 侧同样经这个 builder，所以它的 `"known"` 硬写此前也被丢弃**——live 与 replay 一直同步失效（不是此前记的"replay 能跑 live 不能"）；后果是本批同时改变 replay 判定，既有 bar_replay A 级证据必须重取后才可再用于治理
Migration/OpenAPI/build: 无 schema 变更、无端点签名变更；trace `context.position` 增加两个来源明确的只读字段（合同 §3.2 已同步）
Runtime verification: **已加载并当场产出首条反射动作**（2026-09-19 11:37:33 CST 三服务受控重启，NRestarts=0；治理投影 attempted=100/current=100/degraded=0；`recovery bootstrap attached 1 live positions after restart`——周末持有的空单 290571145 被重新对账找回，未丢仓）。tick 1 即执行 `supervisor tighten pos=290571145 sl->4380.22`（原 SL 4387.69），canonical trace：`stage=executed action=tighten summary_reason=reflex_profit_lock`、evidence `current_price_component_state=known / pnl_component_state=known / reflex_window_ready=True`、mfe 10.47、current_pnl 4.38、giveback 0.5817 ≥ lock 档 0.55、`execution_class=applied`、`is_real_execution=True`。**这是全历史第一条 `reflex_*` 动作**（此前 619 条 trace 恒 0）。对照同一位置重启前形态：`price/pnl state=unknown`、`reflex_window_ready=False`，只可能由 regime_shift/thesis_broken 触发。启动瞬间一条 `startup safety fail-closed: broker_position_price_unknown` 属启动预热（spot 未到）常规路径，tick 1 后即放行；`degraded/0.90/errors=1/closed_pending_positions + account_blockers=none` 在重启前 11:32–11:34 同样存在，是"闭市持仓"既有姿态，非本批引入。
Remaining compatibility: 不修 Safety 侧三处平行组件状态读取者（`live_safety_planner.py:41-47`、`live_loop_v2.py:44-56`、`live_supervision_runtime.py:1349-1366`），已单独登记为 monitoring 旧债
Unresolved live evidence: ① 已闭合：首条 `reflex_*` 动作已在加载后 tick 1 落地（见上），RiskPolicy `risk_reducing_action` 放行、`amend_position_sltp_success` + `reconcile_confirmed` 连续；② 该 reflex 动作未带 TP extension amend（`target_take_profit_changed=false`），向外放宽路径仍未在 live 出现过，继续观察；③ 监督模板 lane 首条 application（承接 B1-末，属数据依赖；当前该 lane 无 ACTIVE 候选，等下一条候选证据链）；④ 新暴露：已确认的 SL amend 不回写 `recovery_position_state.recovery_meta_json.sl`，见旧债登记册同名单元
回放重取: 本批改变 replay 判定后已重取 `bar_replay_51bd8c952ccb439d`（12:55:03 CST，lookback 7d/limit 80）grade **A**：80 决策 80 对齐、mismatch 6、`pre_policy_skip_count=20`、`risk_verdict_decision_count=60`、`risk_verdict_coverage=1.0`、`risk_verdict_disagreement_count=0`、gate/factor coverage 1.0，代码绑定 `784f9755…`。重取前 `status()` 因重启后代码绑定改变而 `degraded`（旧 A 级报告 `45e1c068…` 已按合同不可复用），重取后回放证据恢复可用；闭市期 nursery 在 `run_pending_governance` 早退，不会自行触发该重取
Next batch: 按重放与首条 reflex 动作的实际结果决定反射梯阈值是否进入治理候选；组件状态读取者收口批
```

**本批（2026-09-21 监督模板 lane 首条 application 落地 + 2026-09-22 操作者受控重启；2026-09-23 现查）**：

```text
Batch: 无代码改动批（B1-末之后的"新授权"数据依赖兑现）：09-21 首条 switch 落地并完成 24h effect 观察；09-22 两次操作者受控重启；09-23 现查暴露同 surface 新的 applied 占面卡点
Canonical authority: 不变（`GovernanceMutationCoordinator` 事务内 finalize 是唯一 mutation 写入者；`BrainGovernanceCandidateService.reconcile_submitted_bridges` 是候选↔桥建议对账唯一写入者）
Deleted paths: 无（本批无代码改动）
Targeted verification: 无新增测试（无代码改动）；跑与不跑测试均不改变本批结论
Migration/OpenAPI/build: 无变更（现查 migration 37/37 ok；无端点签名变化）
Runtime verification: ① 09-21 14:50:00 CST `gmut_e5f0cd3a3a174245a9245989724db761` `committed`/`projection_status=current`（`committed_config_version=5672`），target `position_supervisor_template_id=position_supervisor:auto_overprotection_relief.aa33d5c8ad.v1`，绑定命令 `v16cmd_4721706213ed2c5ed61b_rf0fd4ce95737`；该 lane 全历史首条 application（`applied_at=09-21 14:49:22 CST`），24h 观察窗于 09-22 14:50:19 CST 收口 `inconclusive`；`governance.active_template_mode=governed_execute`、selection `default_binding` 指向该模板。② 09-22 01:31:10 CST 三服务 `systemctl restart`、02:07:42 stop → 02:08:40 start backend（均为操作者手动，journal 有 sudo 记录），此后 `NRestarts=0`、运行码 `head=a2d87a3c` clean、worktree 指纹与 API 一致。③ 09-23 08:5x 现查：反射梯在 canonical trace 里真实产出（近 7 天 `reflex_breakeven_lock 35 / reflex_profit_lock 17 / transition_profit_protection_tighten 23`，stage `executed 94 / no_op_suppressed 23`）；`system_health overall=healthy score=1.00`、慢轮 0、`alerts.log` 自 09-22 01:31 无新条目；样本持续写入（`training_sample_row` 16.9k，最新 08:22 CST）
Remaining compatibility: 无新增
Unresolved live evidence: ① **applied 建议占面（新卡点，只读定位见旧债登记册同名条目）**：自 09-22 起 `position_supervisor_governance` 24 条命令全部未 claim 即 cancelled（15 `candidate_not_active` + 9 `authority_expired`，`claim_attempts=0`），09-22 02:21~10:23 监督候选人评审全部 `conflict_detected/bridge_ready=0`，冲突项是 09-21 那条已 `applied` 的桥建议 `brain_bridge_b312ee6c…`（同 surface 149 superseded + 1 applied）。② 候选仍按 ~30 分钟节奏产出并互相 `superseded_by_newer_candidate`（最新 active `brain_candidate_psv_b84589e0d3db6e51`，09-23 07:53；其命令 `v16cmd_8bae7815…` 09-23 08:28 授权过期取消）。③ `evolution_hourly` 周期耗时较 09-14 提速批基线（129~180s）回归（近 24h p50 507.9s / p90 762.5s / max 901.2s），已单列登记。④ **nursery 管线自 09-22 18:25 起未再完成整轮**：近 24h `autonomous_evolution_nursery` 13 次 `skip: another autonomous work item is active`、唯一一次 09-23 05:17 启动即以 `run_pending_governance` 早退（`offmarket_position_quality_lightgbm` 同因跳过 11 次），review/bridge/apply 三步随之停摆；与 ③ 的周期占用同源，是 applied 占面之外的第二道"不产出"原因，两者需分别定位
Next batch: applied 占面处置（判据 vs reconciler，待用户裁定）+ 周期耗时只读拆阶段；随后按裁定结果决定是否动代码
```

**本批（2026-09-23 applied 占面判据收窄，用户裁定方案 a；代码已写，加载待授权）**：

```text
Batch: 候选评审冲突集不再计入"完成链"（`applied` + `applied_mutation_id` 非空 + 归属候选终态），恢复监督模板全局 surface 的后继候选通路
Canonical authority: `BrainGovernanceCandidateReviewService._active_policy_suggestions()` 是冲突集唯一计算者；候选终态集合复用 `brain_governance_candidates.CANDIDATE_TERMINAL_STATUSES`（未新增第二份定义）；V16 claim → Coordinator → apply 裁决链未动
Deleted paths: 无代码删除（判据收窄，未替代任何实现）；不新增表/线程/调度/阈值/开关/service/wrapper
Targeted verification: `tests/test_agent_coordination_fixes.py` 新增 `test_applied_bound_chain_releases_the_control_surface_for_successors`（双向：完成链不冲突 + pending 链仍 `conflict_detected`），短路新判据即红（实测落在 `assert True is False`）；治理/监督相关 7 个测试文件 99 passed；`tests/test_position_supervisor_governance.py` 夹具补 canonical 列 `applied_mutation_id`（夹具对齐 DDL，未在生产代码加兼容分支）
Migration/OpenAPI/build: 无 schema/端点变更（纯 SELECT 列 + Python 判据）；migration 37/37 未受影响
Runtime verification: 全量回归 **3004 passed / 11 skipped**（`TMPDIR=/tmp`；同日一次 1 failed 为 `/run/user/1000` tmpfs 满的 `database or disk is full` 环境故障，非本批引入）；**2026-09-23 09:39:29 CST 用户授权三服务受控重启已加载**——启动即 `overlay restored`、`committed governance projection recovery attempted=100 current=100 degraded=0`、`recovery bootstrap confirmed broker has no open positions`（重启前持仓 291585966 于 09:36:16 由 broker 平仓，`broker_close` +0.76、`attribution_integrity=full`；预检时 loop 的 `positions_reconcile_stale` 属 `LIVE_SAFETY_FRESHNESS_SEC=20s` vs tick 间隔 14~60s 的间歇自解，重启后未再出现）；backend 与 learning worker `release_identity` 一致 `head=a2d87a3c` / `worktree_fingerprint=6370e0ac…` / `clean=false`（工作区含未提交修复）/ `identity_match=true`；readiness 09:42:07 `ok=true/blockers=[]/ready_for_live_execution=true`、loop `running/accepting_new_risk=true`、`system_health` 六组件全 ok、`market_session=open_confirmed`、migration 37/37；**判据生产验证**：只读探针 `_active_policy_suggestions()` 的 `position_supervisor_template` 行数 **0**；启动后 tick 12~24 出现 `stale_waiting_for_live_trendbar`（重启窗口内 01:35 M5 bar 缺失、本地缓存至 01:30，属既有 fail-closed 等待路径，不做 history RPC），trendbar 帧随 01:45 bar 到来自愈；tick 25 起恢复正常交易（`v4 SHORT ORDER+AMEND OK pos=291594684` + `attribution recorded open`，恢复行 `context_integrity=full`），`system_health` 全 ok；**只读复算探针**（`review_candidate(persist=False)`，与生产写入共用同一函数）对当前活跃候选 `brain_candidate_psv_b84589e0d3db6e51` 返回 `review_status=bridge_ready`/`bridge_ready=true`/`has_conflict=false`/`evidence_gaps=[]`（修复前该 lane 每次评审恒 `conflict_detected`）
Remaining compatibility: `proposed/approved` 与"无 mutation 的 applied"照旧占面（B1 保护与 §3.3 幽灵族不变）；无 candidate 归属的合成建议不让面
Unresolved live evidence: 该 lane 首个真实候选的 review → bridge → command → claim 连续证据——**已被 nursery 结构性停摆挡住**：重启后 10:17 实测 `autonomous_learning`（cron `12,42`，873.5s）占锁到 `:27`，nursery（cron `17`，锁预算 480s）必然被 skip，故持久化评审行不会自然产出（判定见旧债登记册周期耗时条目；本批判据本身已由只读复算探针验证）
Next batch: 取首个候选评审行；随后周期耗时只读拆阶段
```

**本批（2026-09-24 治理块根因定位 + 读侧收窄：counterfactual 下推 + 训练样本轻投影）**：

```text
Batch: 旧债登记册「evolution_hourly 周期耗时回归」的治本批（用户 2026-09-24 裁定一起做 P0+P1）；定位手段只读（journal/sar/py-spy 采样 + EXPLAIN），未加监控表、调度或第二套计时
Canonical authority: 不变（counterfactual 唯一写入者仍是 `record_counterfactual_event`；training sample 唯一写入者与资格字段不变；本批只改读侧 `iter_counterfactual_rows` 与 `factor_evidence_summary` 的物化方式）
Deleted paths: `iter_counterfactual_rows` 在 `review_id` 下「全流 flatten 后过滤」的旧路径；`factor_evidence_summary` 对 `iter_training_sample_rows(limit=0)` 的整表物化与 `linked[factor].append(s)` 保留（dict 变体保留给其余 12 处调用方，未删除）；`research/features/feature_provider.py` 的 `iter_training_sample_rows` 导入
Not added: 无新表 / 新索引 / 新缓存 / 新配置旋钮 / 第二套读路径；明确不走 `367aa062` 的 decode 缓存老路
Root cause: 块 = `_apply_parameter_template_actions` + `_expansion_preflight`；带 filter 的 `_iter_canonical_payload_events` 丢弃 LIMIT ⇒ 每次 `agent_context` 全流 flatten 682 行 × 91KB（单次 2.5~3.9s），块耗时 ∝ 模板推荐条数（n 7→20 与 86s→244s 同步阶跃）；内存 +1.24GB 来自 `iter_training_sample_rows(limit=0)` 单次物化（实测 +1266MB），块内 pswpout=0 证明时间不是换页造成
Targeted verification: `tests/test_canonical_v2.py` 25 passed（含新断言「过滤读只解码命中的 payload」，短路下推即红实测 AssertionError）；counterfactual 消费域 5 文件 97 passed；治理/卡片 109 passed（`test_factor_evidence_summary_uses_embedded_decision_snapshots_once` 的 spy 按替代清单改指新投影）；全量回归 **3004 passed / 11 skipped**（`TMPDIR=/tmp`，与上一批基线一致）
Parity on production data: P0 146/146 review 0 mismatch（id + label/position/maturity），单次 2.5~3.9s → 8.7ms；P1 轻投影 +1266MB → +10MB、最新 200 行字段 parity OK，`factor_evidence_summary`（60 因子）0.72s / RSS +0MB
Remaining compatibility: 只带 `position_id`/`label` 的 counterfactual 读仍走全流（该流除 lineage 与 `entity_id` 外无可索引列）；`iter_training_sample_rows` 的 dict 变体保留给其余调用方
Runtime verification（2026-09-24 受控重启 + 现查）: ① 重启 03:54:14 CST backend / 03:56:02 job worker / 03:58:56 learning worker（用户授权），启动即 `overlay restored hash=3dbb4d27…`、`committed governance projection recovery attempted=100 current=100 degraded=0`、`recovery bootstrap confirmed broker has no open positions`（重启前持仓 292115881 由 broker 侧 SL 于 03:52:54 平仓 +1.16，重启窗口无挂单）；readiness `ok=true/blockers=[]/ready=true`、loop 正常推进、两 worker 0 ERROR。② **P1 生产兑现**：`[autonomous] mem after list_recommendations: rss=663.1MB`（04:29:25）vs 修复前同口径 1772.3/1777.4/1778.1MB（02:30/03:00/03:30）＝ **-63%**；04:42 轮 5s 间隔全程采样 RSS 546~785MB（峰值 785MB），同轮 `VmHWM` 1745MB 未再抬升——该 1745MB 系 04:03:37 `655MB` → 04:30:38 `1510MB` → 04:41:22 `1745MB` 两段抬升，落在 04:12 轮内与 04:39 `supervisor_learning` 之间，**不与 04:42 轮同源**，对照修复前 ≥1778MB 峰值仅小幅下降（P1 消掉的是探针确定的 +1266MB 单次物化，floor 之上其余峰值仍未归因，只许只读观测）。③ **P1 时间兑现有限**：`list_recommendations` 阶段 1099~1134s → **1045s**（-75~-90s），`finished autonomous_learning in` 1247.9~1260.0s → **1098.1s**——轻投影消的是内存不是时间，本批未把 `autonomous_learning` 拉回锁预算内。④ **P0 块无法在产线观测**：04:30:31 轮 `[governance] mem after catalog: catalog=1945` 后 10s 即 `finished evolution_hourly in 541.2s`（541.2 = 04:23:00 起算含 450s 锁等待，实际执行 90s），**全程无 `mem after redundancy`/`redundancy invariant`** ⇒ 该轮走 `observation_only` 早退。原因链（只读现查）：posture 自 09-23 18:24 起 `shadow_only`（最后一次 `full` 在 18:22:10，blockers `["replay_missing_or_stale"]`、`replay_freshness=0.0`）→ 最后一份 `bar_replay_evidence` 报告停在 09-22 18:2x，`stale_after=86400s` 到期即翻 → replay 由 nursery 的 `run_bar_replay_evidence` 产出，而 nursery **自 09-22 18:25 起未再完成整轮**（09-22 00:00 起 33 skip / 20 完成，重启后 04:25 继续 skip）。故本批治理块收益仅由生产数据只读探针与等价性证据承载，产线复现需先恢复 nursery。
Remaining live findings（只读 py-spy 活体采样 04:42 轮 132.7s @50Hz）: 剩余 ~1045s 仍是同一类 JSON 往返：`raw_decode` 53.0% + `iterencode` 22.9%；最重栈 = `mature_position_supervisor_traces → iter_counterfactual_rows → _iter_canonical_payload_events → _flatten_canonical_event`（encode 21.8% + decode 15.6%，**每条 supervisor trace 各做一次全流 flatten**，`autonomous_learning.py:2594`，最多 500 条 trace × 693 counterfactual 事件）；`_reviews_desc → iter_review_rows_desc → iter_reviews → _review_resolution_from_rows → read_payload` 23.1%；`get_position_supervisor_template → _generated_templates_from_state → iter_applications` ~10%。三者都不在本批替代清单内（P0 只覆盖带 `review_id` 的 counterfactual 读）。
Unresolved live evidence: ① ~~治理块产线复现（依赖 posture 回到非 shadow_only）~~ **2026-09-24 13:23/13:53 已复现**（第二刀把 nursery 拉回预算 → replay 13:17 刷新 → posture `full`；块 39s/30.1s、块内 RSS +325/+308MB、周期 154.9/146.6s，见 §2 下一批）；② ~~nursery 存活线~~ **已达标**（`autonomous_learning` 1212.2s → 294.9s，13:17:00 nursery 即刻取锁并 157.3s 完成整轮）；③ 连续 24h 无单轮 >900s 的棘轮观察自 2026-09-24 13:58 起计时。
Next batch: 三处读侧调用点按同族收窄（counterfactual 无 review_id 路径 / review 流 / template applications）＋ 锁竞争/调度节奏（容量模型见旧债登记册同名条目）；监督 lane 首个持久化评审行
```

**本批（2026-09-24 读侧放大收窄：样本成熟/物化路径，同一治本族第二刀）**：


```text
Batch: 上一批 py-spy 定位的三处调用点按同族收窄（用户 2026-09-24 裁定"该动哪些直接动，动前查 Git 防重复"）；先查 Git：`_generated_templates_from_state`/`iter_applications` 无既有性能修复，`review_by_entry/review_by_position` 的"索引每循环建一次"是 `9d53ab6f` 既有模式（本批沿用），明确不走 `367aa062` 的 TTL/decode 缓存老路
Canonical authority: 不变（counterfactual/review/trace 读仍由 `canonical_v2_reader` 唯一提供；binding 校验仍由 `verify_position_supervisor_binding` 唯一判定；本批只改"每个循环读几次"，不改任何计算者与判据）
Deleted paths: `_latest_review_row`（每查一个 review 全流重扫一次）；`_review_for_open_decision` 的无索引回退分支与 `conn` 形参（两个调用点都已传索引，回退分支为死代码）；`mature_position_supervisor_traces` 里按 trace 逐个 `iter_counterfactual_rows(position_id=…)` 的全流读；`_auto_apply_position_supervisor_template_suggestions` 里按 suggestion 逐个重读 counterfactual 流的写法；`verify_position_supervisor_binding` 内第二次 `normalize_position_supervisor_template`（同一事实重复读）
Changed shape: counterfactual 流每函数读一次并按 `position_id` 分组（`autonomous_learning.py:2597-2604`）；review 流每函数读一次并建 `_latest_review_by_id` 索引后传入 `_attach_source_review(row, *, review_by_id)`；`position_supervisor_template_hash` 拆出 `_normalized_template_hash`，binding 校验对已归一化对象直接取哈希（少一次 state 读）
Not added: 无新表 / 新索引 / 新缓存 / 新配置旋钮 / 新调度；本批不碰锁相位、等待预算与 posture 门
Targeted verification: `tests/test_autonomous_learning.py` + `tests/test_position_supervisor_binding.py` 61 passed；全量回归 **3004 passed / 11 skipped**（与基线一致）
Parity on production data（只读探针 `run_artifacts` 外 /tmp）: A 分组读 vs 逐 position 过滤读 40 样本 **0 mismatch**（逐次 1143ms → 一次 2.9s）；B 索引查找 vs 全流重扫 60 样本 **0 mismatch**（逐次 596ms → ~0ms，索引构建 1.9s）；C 模板哈希 6/6 一致、`verify_position_supervisor_binding` 仍 `valid=True/binding_verified`（249ms → 131ms）
Runtime verification: 2026-09-24 12:43:44 CST backend、13:02:26 CST learning worker 受控重启加载（job worker 12:44 一并重启；预检 migration 37/37、readiness `ok=true/ready_for_live_execution=true`、broker 无持仓 `pos=0`、job worker 空闲）；两服务 `overlay restored hash=d86bcd07…` 一致、投影 `attempted=100 current=100 degraded=0`、0 ERROR。**加载前同口径基线**（旧码）：12:42 轮 `finished autonomous_learning in 1212.2s`、`mem after list_recommendations rss=623.1MB`（`list_recommendations` 阶段 ≈1181s）、`evolution_hourly` 12:53 轮被 skip。**加载后 13:12 轮实测**：`mem after list_recommendations rss=635.1MB`（13:16:09，阶段 **249s**）、`finished autonomous_learning in **294.9s**`（13:16:54）⇒ 周期 **1212.2s → 294.9s（-76%）**，nursery 存活线（≤780s）余量 2.6 倍；**13:17:00 `autonomous_evolution_nursery` 即刻拿到锁并启动**（无 `waiting`/`skip`，自 09-22 18:25 停摆后首次整轮运行）。
Unresolved live evidence: ① 连续 24h 无单轮 >900s 的棘轮观察（本批后开始计时）；② 治理块产线复现仍待 posture 回到非 `shadow_only`（replay 需 nursery 重新产出报告）；③ floor 之上 ~1.7GB 残留峰仍未归因（只许只读观测）
Next batch: 观察 nursery 恢复后 replay → posture → 治理块链路是否自愈；监督 lane 首个持久化评审行
```


**本批（2026-09-26 监督治理环收敛 + 恢复行 SL/TP 回写，用户裁定"提交后按方案直接做完再重启"）**：

```text
Batch: 一个控制表面同时只允许一条观察窗（P1），以及 broker 已确认的止损回写恢复行（P2）；P3 为只读归因，未动代码
Canonical authority: 观察窗状态 = `research.learning.application_effects`（`IN_FLIGHT_EFFECT_STATUSES` 收口为 `observing/mixed`，与 `observation_window_expired` 同一集合）；切换候选唯一生产者仍是 `position_supervisor_governance.build_position_supervisor_advisories`，它只读 application 账本不自创判据；effect 收口时钟唯一 owner 仍是 `RuleEvolutionGovernor._app_ts → evaluate_application_effect` 的既有 24h 腿；"SL/TP 现在停在哪里"唯一记录点 `live_service._track_local_sl_tp`，恢复行唯一 meta 写入者仍是 `RecoveryPositionStore.merge_meta`
Deleted paths: 三处 demo 路径的即时终态化参数（`autonomous_learning.py` nursery 直调 + `_effect_reconcile_kwargs`、`evolution_orchestrator.py` 同族 kwargs 中的 `mixed_recheck_after_seconds=0.0` 与 `terminalize_mixed_after_recheck=True`）——09-19 B2 修活观察钟后这条腿已从"防无限占面"变成"把每次切换在 2–5 笔上判死"；`observation_window_expired` 内重复的状态集合字面量（改引用常量）；`_track_local_sl_tp` 只写进程内镜像、恢复行停留在开仓快照的旧行为
Targeted verification: `tests/test_position_supervisor_governance.py` 新增 `test_supervisor_switch_waits_for_the_open_observation_window`（双向：clear 时仍产候选、in_flight 时只记 `observation_window_in_flight`；短路该判据即红 `all(...) is False`），并把 overprotection 夹具提为 `_seed_overprotection_reviews` 复用；`tests/test_live_service_tick.py::test_track_local_position_adds_entry` 加断言"镜像与持久投影同时更新"；定向 156 + 147 passed；全量回归 **3005 passed / 11 skipped**（`TMPDIR=/tmp`，= 基线 3004 + 本批新增 1）
Migration/OpenAPI/build: 无 schema 变更（migration ledger `execution_schema.ok=true`、37/37）；无端点签名变更；恢复行仅复用既有 `recovery_meta_json.sl/tp` 两键，`entry_protection_plan` 保持开仓计划语义不改写
Runtime verification: 2026-09-26 16:13:44 CST backend / 16:14:02 job worker / 16:14:26 learning worker 受控重启（预检：无未收口 execution intent、无正在跑的自主工作项、`execution_schema ok`）；启动即 `overlay restored hash=1f4ac0967b0f39ec…`（backend 与 learning worker 同一 hash）、`committed governance projection recovery attempted=100 current=100 degraded=0`、`live loop started` 并 **`recovery bootstrap attached 1 live positions after restart`**（周末空单 292777426 未丢仓），tick 已推进到 4、`account_blockers=none`、`runtime_health_projection` 新鲜、三单元 `NRestarts=0`；`/api/health` 的 `release_identity.head=5cc93c59`（即本批 P2 提交），生产首次运行在已提交代码上；闭市姿态保持 `loop_phase=degraded` + `broker_position_price_unknown`/`market_session_blocks_open`（quote 停更 ~13h，属设计内），无新闩、无 quarantine 事件
Remaining compatibility: `governor.terminalize_mixed_after_recheck` 这条分类腿仍有 `tests/research/test_rule_evolution_governor.py:883` 冻结验收测试锁住，本批只停掉生产调用方、未删该腿；同 surface 观察窗判据只在 advisory 侧拦新候选，已落地链路的 review→bridge→command→claim 不动
Unresolved live evidence: ① **首次产线命中已取得（2026-09-26 晚）**：16:21 清场后 lane 于 17:49 CST 又落地一次切换（`psv_apply_7ca7f6fa5844b3804b5e`，target `auto_overprotection_relief.674fc119a1.v1`，状态 `observing`、至今仍在刷新），随后 ~5.6 小时（约 11 个周期）**零新候选**（同日 18:20 CST 那条 `brain_bridge_d55f7c94…` approved 属开窗前已生成的候选，按设计不在拦截范围）；只读复算探针（`build_position_supervisor_advisories(day="2026-09-25", materialize=False)`，与生产共用同一函数）直接给出 `skipped` 项：`{"action": "switch_position_supervisor_template", "reason": "observation_window_in_flight", "application_id": "psv_apply_7ca7f6fa5844b3804b5e", "target_template_id": "position_supervisor:auto_overprotection_relief.674fc119a1.v1"}`，`suggestions=0`——即闸门在产线按设计拦下新候选且指向正确窗口。说明：该 reason 只存在于返回值，不落库也不进 journal（advisory 阶段仅回报 summary），所以"命中"只能由复算探针或候选空窗期观测。**仍未证**：窗口跨周期存活到出正向终判、`demo_mixed_terminal_inconclusive` 指纹在开盘后仍停在 61。①b **本批删除的即时终态化腿，其在产线的历史命中已被直接证实**：账本里 61 条 `learning_application_log` 带 `evidence_quality.causal_status=demo_mixed_terminal_inconclusive`（`update_weight` 56 / `switch_position_supervisor_template` 5），最后一条 `updated_at` 分别为 09-25 16:50:30 UTC 与 09-25 04:50:12 UTC；重启（08:14 UTC）后该指纹命中数 **0**。即"mixed 窗在下一周期被判死"不是推断而是已发生的产线事实，且这条腿确已停摆（`governor.py:1482` 该分支只在 `evaluation.status == "mixed"` 时触发，产线现存 0 条 `mixed` 行，与"一旦出现即被终态化"完全一致）。② **已由产线数据证实**：重启后首个 effect 周期（08:21–08:25 UTC = 16:21–16:25 CST）把 4 条 `observing` 留在账上并被反复 touch（`ema_slope`/`macd_hist` 建单 09-25 13:27–16:56 UTC、窗口龄 15.5–19h 未到 24h 腿），同时把超龄窗按 24h 时钟终态化为 `inconclusive`（16:21:35–38），并把 09-02 `ema_slope`、09-15 `macd_hist` 两条成熟为 `effective`（16:24:13/15）——观察窗现在停在窗口里、由自己的时钟收口、并能攒到可比样本出终判；16:25:43 另有新 `activate_factor_canary` `applied` 落账，链路未停。③ **已收口，且推翻"周末轮次早退"这一前提**：周六周期确实执行了成对 reconcile，占面临时桥 `brain_bridge_87b43f92…` 于 16:21:20 CST 被置 `superseded`（`review_note="superseded: owning candidate is superseded"`），`position_supervisor_template` 全域 263 superseded / 11 applied / 1 rolled_back、0 条非终态，该 surface 不再被已完成链占面。④ `recovery_meta_json.sl` 的新值要等下一笔真实确认 amend 才现查得到（闭市无 quote）；⑤ P3：`autonomous_learning` 同口径 294.9s → 623~635s 的 516s 未打点区域，已用只读探针排除 `list_recommendations` 本体（冷进程实测 9s），结论待开盘首轮 py-spy 活体取样
Next batch: ①②③ 本批内已结案（见上），周一开盘只剩 ④⑤ 与 ① 的两项未证部分。① 剩余口径 = 该窗（`psv_apply_7ca7f6fa…`，17:49 CST 开）能否跨周期存活到出正向终判、且 `demo_mixed_terminal_inconclusive` 指纹停在 61 不再增长；同时盯 18:20 CST 那条开窗前生成的 approved 桥 `brain_bridge_d55f7c94…` 会不会落地成第二次切换把窗提前判死（它不在闸门拦截范围，属设计边界）。④ 等真实确认 amend；⑤ P3 取样后再定第四刀
```

**本批（2026-09-26 深夜 A 批 + 参数模板自锁修复，提交 `3887e5a1` / `1372c66f` / `e72584d2`）**：

```text
Batch: 按用户裁定的建议顺序推进——A 批删除不可达的 scheduler 回退（纯删除）；随后把 B 批只读定位得到的参数模板自锁按用户裁定（删 handoff 写入）修掉
Canonical authority: ① `InProcessScheduler` 的唯一后端 = `apscheduler.BackgroundScheduler`（`requirements.txt` 声明且已装，生产 `live_service.py:3178/3286` 启停）；② parameter_template 切换建议的唯一生产者 = `ParameterTemplateService.create_switch_suggestion`（learning 链经 `create_suggestion_from_recommendation` 落 `policy_suggestion(scope_type='parameter_template', action='switch_parameter_template')`），治理侧不再写"交接单"；`agent_authority.py` 该 surface 的 `delegated_execution_owner=autonomous_learning` 声明不变
Deleted paths: ① `backend/runtime/scheduler.py` 的 `HAS_APSCHEDULER` 导入守卫 + `_TimerJob`（含自写 cron 解析 `_parse_interval_seconds`）+ `_jobs_timer` 全部 else 分支（514 → 284 行；apscheduler 分支只去缩进，逻辑逐字保留）；`tests/test_scheduler.py` 的 fallback 参数化用例与 `skipif`（110 → 86 行）；② `factor_governance_orchestrator._apply_parameter_template_actions` 整函数及其调用点（该函数除写 `handoff_parameter_template_switch` / `handoff_parameter_template_validation` 两条 `delegated_to_autonomous_learning` 建议外无其他产出，且两 action 全仓无消费者）与同文件死方法 `_submit_offline_template_validation`（零调用方）——共 −95 行；③ `autonomous_learning._recommendation_already_materialized` 的宽口径判据：`evidence_json LIKE '%<rec_id>%'`（全表任意提及即判"已物化"）收窄为 `scope_type='parameter_template' AND action='switch_parameter_template' AND evidence_json LIKE`
Not added: 无新表 / 新索引 / 新缓存 / 新旋钮 / 新抽象；未新增第二套判据（窄化即替换原判据）；未动 `parameter_template_release_candidate` 与 `jobs` 两条既存检查
Root cause（自锁，只读定位）：治理侧每周期为 `online_light` 推荐写 4,024 条 `handoff_parameter_template_switch`（另有 7,076 条 `_validation`，最新 09-26 15:25:52 UTC），其 `evidence_json` 顶层带跨周期稳定的 `recommendation_id`（现查样本 `ptr_ema_slope_ema_slope_conservative.v1_default`）；学习侧判"已物化"时全表 LIKE 命中的正是这些交接单 → 真物化腿永不执行（`skipped_existing=20/周期`）→ `parameter_template_switch_log` 恒 0 行。写单的人没人读，单子又让执行者以为自己做过
Targeted verification: ① A 批定向 88 passed（`test_scheduler` / `test_backend_runtime_lifecycle` / `test_factor_autonomy_hardening` / `test_live_scheduler_jobs` / `test_live_service_tick`），存量 skipif 用例改为常跑；② 自锁修复：`tests/test_autonomous_learning.py::test_parameter_template_recommendations_auto_materialize_and_dedupe` 扩为三段（首轮物化 → 真实切换建议去重 → **新增**治理侧 handoff 行长在时仍须物化），红绿双证——把判据改回宽口径即红（`assert 0 == 1`，正是产线 `suggested=0` 症状），窄化后 59 passed；同族更宽定向 `test_autonomous_learning` + `tests/backend/runtime/` + `test_factor_cards_api` **160 passed**（`TMPDIR=/tmp`）
Migration/OpenAPI/build: 无 schema 变更；无端点签名变更；`/api/control/scheduler` 只读 `running/jobs`、不暴露后端形态，无需改；被删的 handoff 写入无任何读侧消费者
Remaining compatibility: ① `evolution_hourly` 的"静默块"从此消失——旧债登记册 2026-09-23/24 的性能条目所指 `_apply_parameter_template_actions` 已整函数删除，其历史耗时序列（244~416s → 30~39s）不再可比，后续该周期耗时只看整轮值；② 4,024 + 7,076 条历史 handoff 行保留在库（只读审计痕迹，无消费者），本批不清账
Runtime verification: **2026-09-26 23:47:41 backend / 23:48:18 learning worker 受控重启已加载**（用户授权；预检：无未收口 broker/governance intent、无 pending/running job、协调器空闲、三服务 active）；`/api/health` 的 `release_identity.head=e72584d2`（即本批修复提交）、`clean=true`（首次出现已提交工作区直接加载）、两端 `overlay restored hash=4af21662…` 一致（相对 16:13 的 `1f4ac096…` 变化来自 17:49 CST 那次受控切换的 governed mutation，非漂移）、`committed governance projection recovery attempted=100 current=100 degraded=0`、readiness projection `fresh`、`live loop started` + `recovery bootstrap attached 1 live positions after restart`、两单元 `NRestarts=0`；learning worker 启动日志出现 `[InProcessScheduler] add_job …` + `Scheduler started`，即 A 批删除回退后 apscheduler 路径在生产实跑。**验证①（治理侧）已通过**：23:53 轮 `evolution_hourly` 跑完（**114.9s**，对照 23:23 轮 155.8s），`handoff_parameter_template_validation` / `_switch` 计数**冻结在 7,076 / 4,024**、`MAX(created_at)` 仍是修复前的 15:25:52 UTC ⇒ 治理侧不再写交接单。**验证②（学习侧）已通过且暴露下游两道闸**：00:12 CST 轮 16:18:59 UTC 的 `parameter_template_auto_materialize` 事件为 `skipped_existing` **20 → 0**、`suggested` **0 → 6**、`offline_jobs` 1 ⇒ 物化腿真的执行了（自锁解除生效）。同时暴露：① `errors=13` 全部是 `job_enqueue_did_not_return_row`——生产 `idx_jobs_kind_idempotency` 是全量唯一索引而代码按部分索引（`WHERE idempotency_key <> ''`）写 ON CONFLICT，导致**同 kind 第二条空 key 入队必失败**（现查 `jobs` 表每 kind 至多 1 条空 key 行），是系统性缺口；② 6 条 `switch_parameter_template` 建议全部被 governor 判 `rejected: executable evidence eligibility version/fingerprint missing`（`governance_eligible=0`/`eligibility_contract_invalid`）——写入者用裸 INSERT 不带 eligibility 契约。两者详情与修复方向见 [legacy-debt-register.md](legacy-debt-register.md) V16 条目 2026-09-27 段
Unresolved live evidence: 首个真实 `switch_parameter_template` 落账（`parameter_template_switch_log` 非空 + `parameter_template_active` 非空）——本批解开自锁后该判据仍受上述两道闸阻挡，需先修 eligibility 契约与入队键；另：restart 冲突（`CanonicalV2ConflictError`，确定性事件 id × 墙钟 `event_ts`）若反复出现，值得单列一条幂等边界条目
Next batch: 修上述两道闸（eligibility 契约优先，它是切换能否落账的直接前置；入队键同批修）；随后 B 批收尾（平行 authority 清单已出，见登记册）与 C 批组件状态三读者收口
```

**本批（2026-09-27 凌晨：parameter_template 切换建议 eligibility 契约，用户裁定"先修 eligibility 契约"）**：

```text
Batch: 修第一道闸——`switch_parameter_template` 建议的 eligibility 契约（写者补契约 + 物化去重判据排除 rejected）；入队键（jobs 索引口径）按用户裁定单列一批
Canonical authority: eligibility 唯一计算者 = 建议写者 `ParameterTemplateService.create_switch_suggestion`，形状与 `position_supervisor_governance` advisory 车道同源（同一 `GOVERNANCE_ELIGIBILITY_VERSION`、同一 sha256 json sort_keys 口径）；读侧不新增判据——governor `research/learning/governor.py:589-601/:688-698` 与 `_suggestion_is_approved` 三列检查原样复用
Deleted paths: `tests/test_factor_cards_api.py::_approve_parameter_template_suggestion`（手工把 `governance_eligible=1` + `'pytest-eligibility'` 指纹补进库的测试旁路，8 处调用点收敛为既有 governor `set_status`）——该旁路是"写者不做、测试代做"的产物，删除后写者契约成为建议合格的唯一来源
Not added: 无新表 / 新列 / 新迁移 / 新开关 / 第二套判据；fingerprint 计算复用既有 `hashlib.sha256(json.dumps(..., sort_keys=True, default=str))` 形状，同一 fingerprint 兼作 `qualification_fingerprint` 生成确定性 suggestion_id（不新增随机 id 面）
Root cause: 写者用裸 INSERT 只写 evidence/status，governor 要求的三列全空 ⇒ 经 `create_suggestion_from_recommendation` 物化出的每条切换建议天生 `eligibility_contract_invalid`（live 6/6 被拒）。第二面：物化去重判据 `_recommendation_already_materialized` 不排除 `rejected` ⇒ 被拒建议永久占住"已物化"判据，同一 recommendation 无法在契约修好后重新物化（6 条 live 拒绝行正落在此面）
Targeted verification: ① `test_factor_cards_api.py::test_parameter_template_activation_syncs_runtime_signal_config` 落库即断四列（eligible=1 / version / 64 位 fingerprint / ineligible_reason 空）；红证：HEAD worktree（59f995e7）上跑新断言 `assert 0 == 1`（写者缺契约），当前树绿；② materialize 测试补第三段——把既有建议置 `rejected` 后重跑，`suggested` 回到 1（判据不排除 rejected 即红）；③ 定向 `test_factor_cards_api` + `test_autonomous_learning` **102 passed**，同族 `test_governance_eligibility_weighting` / `test_governance_contract_convergence` / `test_governance_mutation_coordinator` / `test_governance_control_plans` / `test_v16_brain_orchestrator` **60 passed**
Migration/OpenAPI/build: 无 schema 变更（`runtime.policy_suggestion` 四列 supervisor 车道已在用，2026-09-25 只读枚举确认存在）；无端点签名变更；物化路径经 `create_switch_suggestion` 自动携带契约，无需调用方改动
Remaining compatibility: 6 条被拒历史行保留（`governance_eligible=0` 审计痕迹）；去重判据排除 rejected 后它们不再挡重新物化，新建议 payload 含 `governance_eligibility` 块 ⇒ 确定性 id 与旧行不同，不复用旧 id
Runtime verification: **未加载**——需用户授权的受控重启（learning worker；backend 无本批代码路径可不动，但如需 release_identity 一致可同批）。加载后现查口径：新一轮 `parameter_template_auto_materialize` 的 `suggested` 不再被 governor 判 `eligibility_contract_invalid`、`policy_suggestion` 出现 `governance_eligible=1` 的 `switch_parameter_template` 行、进而首个真实切换落账判据（`parameter_template_switch_log` 非空）
Unresolved live evidence: 首个真实 `switch_parameter_template` 落账仍待验（未加载）；`offline_jobs` 入队键缺陷（`job_enqueue_did_not_return_row`）按裁定单列一批
Next batch: 受控重启加载本批 + 现查 6 条建议能否进 approved/claim；随后修入队键（第二道闸）与 B 批收尾（平行 authority 清单见登记册）、C 批组件状态三读者收口
```

## 3. 未完成 / 待复核证据

1. **完整生命周期闭环**：S7.6 终验标准已达成并持续（基准 `trade_review_outcome full/1.0 46`，`2026-08-21 → 08-28`）；后续只做常态观察，当前计数、skip/rejected 双轨与 supervisor trace 分级以现查 `canonical_v2` 为准。
2. **监督治理闭环**：样本数量门（≥10 笔 `governance_eligible matured`）与 `tighten` 覆盖门均已退役。**2026-09-21 14:50 CST 首条 `switch_position_supervisor_template` 已落地**（`gmut_e5f0cd3a…` committed/projection `current`，active 模板 `position_supervisor:auto_overprotection_relief.aa33d5c8ad.v1`、`governed_execute`，application 行存在、24h 观察窗 09-22 14:50 `inconclusive`）。当前缺口改为 **applied 建议占面**：该 lane 自 09-22 起 24 条命令全部未 claim 即 cancelled（`claim_attempts=0`）、候选人评审全部 `conflict_detected/bridge_ready=0`，冲突项即上述已 `applied` 的桥建议（surface 全局 `position_supervisor_template`）；只读定位（判据口径、涉及的 runtime 行与不可自愈原因）见 [legacy-debt-register.md](legacy-debt-register.md) 同名条目。**2026-09-23 用户裁定方案 a 并已实现**：冲突集不再计入 completed chain，定向 + 全量回归通过（见 §2 本批）；生产尚未加载，待受控重启后现查首个候选评审。原候选链/产线缺口（`f16024bb` 后无生产者 → `0d77157d` 补产线 → B3/B2/B1/B1-末 四闸）已随首条 application 验收收口，历史追溯走 Git。**2026-09-26 现状**：候选链已不构成缺口（26 小时内 12 条切换 committed，review→bridge→command→claim→Coordinator→application→effect 连续）；本批把退出线改为"观察窗能收口"——demo 路径不再在下一周期把 mixed 直接终态化，advisory 在该 surface 仍有 `observing/mixed` 窗口时只记 `observation_window_in_flight`，权威见 [system-source-of-truth.md](system-source-of-truth.md) §1.2、判据与证据见 [legacy-debt-register.md](legacy-debt-register.md) §1 同名条目。
3. **持仓监督早锁导致小赚大亏（2026-10-10 登记，代码已修、候选被 in-flight 门冻结）**：近 7 天 64 单 31 胜/32 负、胜均 +4.41/负均 −4.86，赢单 MFE 实收中位仅 32%，counterfactual `protection_too_tight` 116/218。根因是 reflex 阶梯（`reflex_min_mfe=3.0` + giveback 0.35/0.55/0.90）命中几乎所有有意义赢单；第二轮问题是 effect 观察窗 24h 内 0 个可用 post 样本，7 次切换全部 `inconclusive`。本批已落地：`reflex_policy` 成为可学习控制区段、`_generated_reflex_min_mfe_template` 按 giveback severity 抬 trigger（10-09 数据 severity=1.0 → 候选 6.0，单控契约通过）、`DEMO_EFFECT_OBSERVATION_WINDOW_SECONDS=5 天`。10-10 04:47 受控重启加载；probe 确认 `observation_window_in_flight` 生效：reflex 候选已生成但在 10-09 模板的观察窗关闭（约 10-14）前不会被委派，切换在这之前保持冻结。退出线与 KPI 见 [legacy-debt-register.md](legacy-debt-register.md) §「持仓监督模板早锁导致小赚大亏」。
4. **`policy_suggestion` 无背书 applied 行（2026-09-10 查证结案，用户裁定不处置）**：112 条 `status=applied` 且 `applied_mutation_id=''`（factor `update_weight` 102、`rollback_factor_action` 10，`created_at` 2026-08-23 20:31 → 09-08 12:43），全部 `governance_eligible=0`。查证结论：
   - 该族行只可能由 `FactorGovernanceOrchestrator._record_policy_suggestion()` 写出（`reason` / `review_note` / `fgv` 前缀全仓唯一），但**当前代码写不出**：它在算 `suggestion_id` 之前就对 `applied` 早退（探针实测 `applied → ''` 且 0 次 DB 调用，`proposed` 才触达 DB；该守卫自 2026-07-19 起存在于该文件全部 67 个修订）。
   - `suggestion_id` 是 `(writer, scope, key, action, evidence, status)` 的 sha256，按库里 evidence 重算只与 `status='applied'` 命中 → 写入发生在带守卫之前的代码变体（工作区长期未提交，历史差异不可复原）。
   - 这些行引用的 `decision_id` 在 `evolution_decision` 中不存在，窗口内无 `governance_mutation_intent`，evidence 无 `gmut_`；09-08 12:54 重启后不再新增（其后仍持续产生 decision）。
   - 影响面已 fail-closed：`live_committed_policy` 对空背书行只标 `legacy_quarantined`（且仅限非 strict + 收紧动作）；`brain_governance_candidate_review` 要求 `governance_eligible=1`；`proposal_registry` 中 `fgv*` 来源 0 条。
   - 处置：**裁定不清账、不新增守卫**（现状无授权影响）。旧口径"27 条无背书 applied 幽灵行"作废；`run_artifacts/policy_suggestion_ghost_cleanup.py`（D2 一次性脚本）从未以 `RUN=1` 执行，且快照序列化损坏（写出的是列名）、对象族标注错误，**不得直接复用**，如需清理必须重写。
4. **V16 认知层退役（2026-09-11 完成）**：停写 → 载体解耦（`posterior_fingerprint` + 自校验）→ 代码删除（净 −5.8k 行、−9 端点、`brain_action_plan` 提案来源）→ `pg_dump` 存档（`run_artifacts/v16_cognition_retirement_20260911.dump`，74.9 MB/6 表）→ v35 迁移删除 6 张表（423MB → 15MB）。保留 `v16_brain_command`、`brain_governance_candidate`(+`_review`)、`brain_memory`、`brain_live_ready_guardrail`。详见 [legacy-debt-register.md](legacy-debt-register.md) §1。
5. **索引/契约欠账**：依赖 `factor_name` / `lifecycle_stage` / `runtime_admission` 的 `idx_factor_lifecycle_*` 索引仍未建（0028 已补齐这些列，当前仅 `idx_factor_lifecycle_unique_name` 存在）；是否补建按后续性能证据决定。
5. **每次发布门重取**：process-loaded flags、PID、fingerprint、release preflight 证据；当前源码绑定的 execution/safety fault matrix attestation；Safety 在 enforce 姿态下的连续性与完整 broker position lifecycle 证据。
6. **因子发现闭环（2026-09-13 修复批；2026-09-14 开盘已见首批动作）**：五处结构缺陷已修（方向契约投影、轮转饿死、退役人口判据、证据时钟、评估覆盖）。开盘后第一批真实动作已落地：21 条 `retire_factor` + 11 条 `promote_factor`（committed，config_version 5013→5036），dsl 分布 604 RETIRED / 15 QUARANTINED / 1218 SHADOW / 1 `PROMOTION_PREPARED`（修复前 594/5/1239/0），catalog 1896→1939；无 `blocked_by_evidence` 洪泛。退出条件 (c)（首个 `origin=dsl` 因子达 ACTIVE）仍未达：promote 证据带 `activation_blocker_codes=[promotion_not_prepared]` 与 `blocker_codes=[application_effect_not_mature_positive, controlled_active_canary_contract_missing, …]`。详见 [legacy-debt-register.md](legacy-debt-register.md)。
7. **学习建议应用闭环（2026-09-13 修复批；2026-09-14 19:45 已闭环，条目已从旧债登记册移除）**：三条降权全部落账——`macd_hist`（`gmut_d979fbe50e54…`）、`di_spread`（`gmut_ce9ca60577e7…`）与 `rsi_14`（`gmut_e0b45ded26be4a5d833fac7b843cbf62`，11:42 UTC 周期落账 1.0→0.89）；`stoch_k` 行以「no actionable live weight」supersede 收口；当前 0 条 `approved` 且无落账的因子建议（仅剩 `same_direction_ge_1` 一条 entry-cluster 口径 `approved`，非因子权重）；`learning_workload_gate` 返回 `run_new_facts`。`rsi_14` 的放行链：回放缺输入分歧改记 input gap（`626a146e`）→ 重取报告 `bar_replay_43bcab2d43a14a70` grade B（分歧 0、input gap 22、覆盖 0.95）→ 准入 ok/fresh → stepper 落账。`entry_cluster` live 消费已确认（15:35/15:40 同向加仓被拒、15:45 重开被拒、16:00 正常成交）。
8. **post-fill 记录接线断裂（2026-09-14 发现并修复，已验收）**：详见 §2；验收线 = 重启加载新码 + 操作者释放 `no_new_risk_latch` 后，下一笔确认成交的正常走通 `record_*_from_live` 与恢复/归因记录，且不再出现 `confirmed_open_post_fill_processing_failed`。**2026-09-14 17:30 已验收**（15:30 / 16:00 两笔：`ORDER+AMEND OK`、attribution recorded、恢复行 applied、学习样本 `integrity=full`；无硬 latch），条目已从旧债登记册移除。
9. **反事实复盘流断流（2026-09-14 发现并同批修复，已验收）**：`counterfactual_review` 曾停在 2026-09-11 00:35（198 条），因准入白名单缺 `chain_broken`（L0-0R 后恢复路径已不再产 `restart_replay`），监督员主动平仓的 288549050 因此没有复盘，`position_supervisor_selection.v1` 的 `requires_clean_mature_counterfactual` 随之无法满足。修复 `79ba699f`（白名单纳入 `chain_broken`，执行动作与成交价两道门不变）后 learning worker 重启（18:06 本地），10:09 UTC 周期即为该笔产出首条新复盘：`protection_too_tight` 0.74、`fully_matured`、`maturity.governance_eligible=true`、`selection_eligible=true`、绑定 `binding_verified`，事件 198→199。22:53 现查 205 条、最新仍 11:30 UTC（22:23 broker_close 平仓无监督执行动作，按设计不产复盘）。剩余观察：selection 首个可治理候选按 [legacy-debt-register.md](legacy-debt-register.md) §1 监督闭环退出条件跟踪。
10. **恢复表完整度列补写入者（2026-09-14 已修，22:25 真实平仓验证通过已 resolved，条目已从旧债登记册移除）**：`mark_closed` 透传 review 值（`COALESCE` 不覆盖已有值）、三条平仓路径接线、今日 3 行 `unknown` 已回填、全表 0 `unknown`（详见 §2）。验证：22:15 开多 288659853 → 22:23 平仓（review：broker_close、pnl −13.48、`attribution_integrity=full`），恢复行 `closed_replayed`/`full` 与 review 一致；该笔无监督执行动作，按设计不产反事实复盘（非断流）。

上述证据不能由单测、历史快照或 readiness 替代；未满足前不推进后续静态开关，也不把 readiness ready、单次 bridge 或单次 effect 解释为自治毕业。

## 4. 下一批处理顺序

1. 对 [legacy-debt-register.md](legacy-debt-register.md) 中仍在 `active` / `migrating` / `monitoring` 的路径逐项收集退出证据，canonical 验证后同批删除旧路径。
1b. ~~用户已裁定下一批修：`runtime.recovery_position_state.attribution_integrity` 补写入者~~——2026-09-14 18:38 已执行（见 §2 本批），22:25 真实平仓验证通过已 resolved（见 §3.10）。
1c. ~~applied 建议占面处置~~——用户已裁定方案 a，判据收窄已实现并跑过定向 + 全量（见 §2 本批）；剩余动作 = 受控重启加载并现查首个候选评审；不得用 SQL 直接改 `policy_suggestion` 状态或人工制造候选。
1d. `evolution_hourly` 周期耗时回归（09-14 基线 129~180s → 现 p50 ~508s，见旧债登记册同名条目）：先只读拆阶段，不动计算者、不加监控表。
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
