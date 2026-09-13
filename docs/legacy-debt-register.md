# Active Legacy Debt Register

> Status: active
> Last verified: 2026-09-13 (深夜第二批：学习建议应用闭环两处修复——治理 replay 报告分级不再把 live-only 闸门拒绝（reentry cooldown/连亏冷却/学习阈值）计为 disagreement（此前 80 决策中 47 条，使报告恒为 C、全部权重变更 `blocked_by_replay`），新报告 grade A 且 admission fresh；不可执行 approved 建议（`old_weight<=0`）改为 supersede 收口；`entry_cluster` 缺 actuator 登记待裁。同日闭环修复批：`dsl_auto` 积压条目重写为“因子发现闭环缺陷”——方向契约投影、轮转饿死、退役人口判据、证据时钟五处结构缺陷同批修复并留探针证据；同批删除 catalog config-only direction、轮转阶段优先级、快车道 `fresh_evidence_bars`/`updated_at` 判据与 `source=='discovered'` 人口判据。同日早前：清理批删除 5 条退出条件已满足的旧账（V16 认知层退役/schema 37-37 ok、CVaR overlay 3.5、emergency close 旧入口零调用方、live_service L 系列收口、因子治理重复重算落地），21→15 条；dsl_auto 积压复核 1,239 并修正退出预算口径；supervisor 治理链首跳缺口已补入专员产线（`0d77157d`/`6ed0d878`），首条 application 待开盘验收；CVaR 原始丢失根因未定，留观无动作)
> Scope: 只登记尚未退出的兼容、重复 authority、隔离数据和回归（active / migrating / monitoring / quarantined / regressed）。

已完成旧债不在本文保留；Git 历史和测试是追溯依据。新增条目必须写清 canonical 路径、剩余旧路径、退出条件和验证。

## 1. 全局收敛

### supervisor 经验已进入记忆索引，但自动模板准入仍未达标

- 状态：`monitoring`（2026-09-05 复核修正：验收线收紧为 **tighten 覆盖**——reduce 已按 2026-09-02 用户决定永久关闭（最小手数不可减），不得再作为验收项。2026-09-02：eligible 38 / matured 47，数量超 10 笔门槛；缺 tighten 真实执行证据——55 笔 trace 全为 close（thesis_broken/timeout/regime），盈利仓全由 broker TP 单触发；trend_hold 回吐信号缺失已修（5ba55b47 bar 级评估事件后干预需求可见）；准入仍缺真实干预执行，等待受控试点或策略变更。tighten 代码可达性已验证：`range_capture`/`transition_confirming` 姿态下盈利+回吐≥giveback_tighten_threshold 均有真实 tighten 路径；trend_hold 盈利仓按设计只打标不动作；2026-09-08 `ca23580e` 退役 tighten 硬门——close 经 keep→supportive 计入干预证据，本条剩余验收线为候选链，见下方 2026-09-13 定位）
- canonical：原始事实由 `canonical_v2.supervisor_trace/counterfactual_review` 承载，学习资格由 `canonical_v2.training_sample_row` 承载，经验检索使用 `experience_memory`，V16 检索/后验使用 `brain_memory` 和 `posterior_arbitration`。
- 当前（2026-09-08 只读）：`training_sample_row 12978`，`supervisor_execution_trace 9458`中`governance_eligible=1 & matured=53`（数量已超10笔门槛；`matured/full 96`，其余`pending 2755 + excluded 6607`）；`trade_review_outcome 224（eligible 1.0共162，08-28为67/46）`；`event supervisor_trace 104/counterfactual 136/broker_execution 440/position_transition 440/trade_review 256`；`brain_memory 294`、`experience_memory 219`；`selection.v1 candidate_count=0`但已有`profit_protection.v1` governed基线绑定（`bound`，此前`insufficient_evidence/off`），`origin=supervisor` lifecycle行2（09-11 只读：`auto_tpsl.d9daf3a89f.v1`、`auto_mfe_capture_protection.088df78668.v1`，均 SHADOW，出生钩已真实产出）。binding 回溯：新 trace 全 verified（`287557780` 09-10 23:16、`287621843` 09-11 00:35），09-10 00:50 及更早的历史 trace 仍为 `binding_missing`（历史 payload 无 binding，不回填、不伪造）。数量门已过，缺候选review→V16/Coordinator→effect/rollback连续链。
- 2026-09-13 只读定位（链路断在首跳）：32 条治理合格 suggestion（`policy_suggestion` scope=`position_supervisor_template`）截至 09-10 23:43 全部 `superseded`、零 applied——`autonomous_learning.py:5079` 要求该 scope 建议携带 V16 bridge 证据，而 advisory 产出（`build_position_supervisor_advisories`，evidence 含 replay_summary + counterfactual_summary）无 candidate_id/bridge；`learning_application_log` / `learning_application_effect` 中该 scope 为 0；`brain_governance_candidate` 仅 1 条 supervisor 历史行（08-19 创建、`posterior_not_selected` superseded、24 条 delegated 命令全部 `claim_status=cancelled`/`claim_attempts=0`）；`f16024bb`（09-11）删除 medium-impact 产线后全仓无生产者为该 scope 造 candidate（`create_candidate` 仅 `factor_pruning_governance` 调用）。修链三选一（待决）：① 按新架构补专员自有 `delegate_*` 命令/候选生产者；② 改该 scope 准入条件，承认 advisory 自带 replay+counterfactual 证据；③ 明确停车该 surface。2026-09-13 处置：采用 ①（commit `0d77157d`）——专员经 `V16BrainOrchestratorService.delegate_supervisor_template_switch` 从 advisory 证据产出 candidate+`delegate` 命令，bridge/RiskPolicy(approved)/Coordinator 仍逐层门控；定向测试 197 + smoke 215 通过；首条 candidate/command/application 待下一个带新事实的学习周期验收（闭市周期在 watermark 当前时直接跳过）。同日人工回放 2026-09-10：首跳、复审（修轮换记分 `6ed0d878` 后 `bridge_ready`）、bridge 证据均成立；bridge 因缺 `candidate_template`/`generation_context` 被 apply 侧 supersede 的问题已随 `6ed0d878` 修复；最后一跳 application 待开盘新证据。本条当前待验证项：① 首条 application（含 effect observation 与 rollback 痕迹）；② apply 侧通用门 `release_run_required_for_governed_apply` / `replay_freshness_required` 在该 lane 的真实行为未实测（仅在闭市只读 cycle blockers 中观察到）。
- 自动开启：`off` 仅是无证据时的安全基线；证据投影达到资格后，由 learning worker 自动经 V16、RiskPolicy 和 Coordinator 切入有界 Demo，不需要人工再改一个模式开关。单条 brain memory、提案或未成熟后验仍不能直接授权。
- 退出：`≥10 笔 governance_eligible matured supervisor_execution_trace` 真实干预（close/tighten 均可，tighten 硬门 2026-09-08 已退役：executed correct close 经 keep→supportive 计入干预证据）+ 候选 review、V16/Coordinator application、effect observation 和 rollback 连续可追溯；selection projection 新鲜且可解释；任何单条记忆不得直接改模板或放大交易权限。

### V16 parameter_template 通道（V16 链存在代码死点；learning 链可达）

- 状态：`active`（2026-09-05 路线②已执行：planner 目录移除 `shadow_parameter_template_review`，`_materialize_eval` 对该 scope 只观察不产候选——V16 链死点按路线②退役，learning 链成为唯一 owner；首个真实切换（`parameter_template_switch_log` 非空）后转 resolved。2026-09-05 复核修正：**V16 链存在代码级死点，仅注册模板不可达**；2026-09-02 登记：V16 因子通道已打通并落地首笔降权 stoch_k 0.35→0.3115）
- canonical：`parameter_template_registry` 为模板唯一注册源；V16 经 `switch_parameter_template`（scope_key=online_light）切换，命令门已支持（`v16_scope_key=online_light`），应用端 `_auto_apply_parameter_template_suggestions` 与 governor 规则（需 `target_template_id` + `recommended_scope=online_light` + confidence≥0.55）已就绪。
- 当前：`runtime.parameter_template_registry` 与 `runtime.parameter_template_active` 均为 0 行。**V16 链死点（2026-09-05 实证）**：`v16_brain_planning._map_action` 对 `scope=parameter_template` 硬编码 `target_template_id=""`，而 bridge 评审要求 mapped target 非空（`brain_governance_candidate_review` gap `missing_target_template_id`）——**仅注册模板不能解封该链**，planner 侧必须先填 target 或不再映射该 scope。备用活链：`autonomous_learning._build_recommendation` 从 `_MANUAL_TEMPLATE_LIBRARY`（rsi_14/macd_hist 各 default/conservative/aggressive）选真实 target，switch mutation 在同一 Coordinator 事务内物化 registry 行（`parameter_templates.py` switch 分支），**该链不需要预先注册**，只等因子卡片出现 primary responsibility=parameter（或 `factor_logic_ok_but_param_suspect` 标签）的归因证据。
- 退出（三选一）：① 修复 V16 链死点（planner 填 target）+ 注册模板后验证一次真实切换；② 路由到 learning 链：V16 不再映射 parameter_template scope，由备用链完成首个切换后本条转 resolved；③ 明确不走模板治理路线，关闭该 surface 的观察并标记 resolved（路线决定）。
- 验证：`parameter_template_switch_log` 出现新记录且 `parameter_template_active` 非空（任一链均可）；或明确标注"不启用模板路线"。

### supervisor 决策链三缺陷复盘（2026-09-02 已修 3/3）
- 状态：`monitoring`（2026-09-02 57690a2f 修复 ③ 后：near_tp 在 trend_hold 内优先处理，default 模板 close 落袋、protect 模板 tighten；测试 20 passed 含 protect 变体；待真实 trend_hold 盈利仓 near-TP 动作与评估事件积累验证）
- canonical：`position_supervisor` 决策链 + bar 级 `supervisor_evaluation` 事件
- 当前：① near_tp tighten 分支要求模板 `near_take_profit_action=protect`，default 模板配置 close（有意策略，protect 留作模板能力）；② trend_hold 回吐只打标签无动作——已修：`trend_hold_giveback_intervention_requested` 标记进评估事件；真 reduce 因最小手数不可减，按用户决定关闭（2026-09-02）；③ trend_hold 截胡 near_tp——已修：trend_hold 盈利仓 ≥92% 到止盈时走模板驱动路径（default close / protect tighten），与其它 posture 同语义
- 退出：评估事件积累 ≥1 周后验证复盘可用性；真实 trend_hold 盈利仓 near-TP 动作样本 ≥10 后从 monitoring 转 resolved

### 因子发现闭环缺陷（dsl_auto 积压 1,239；2026-09-13 定位并同批修复）

- 状态：`monitoring`（本批修复五处结构缺陷；积压行数未动，改由修复后的晋升/退役路径消化）
- 事实：历史上 0 个 dsl 因子到过 ACTIVE（`factor_lifecycle_state` origin=dsl：RETIRED 594 / QUARANTINED 5 / SHADOW 1,239），
  `canary_promotion_blocked_unbacked` 115 条——晋升门与退役门同时不可达，队列只进不出。
- 根因（只读探针 + 代码定位，全部有据）：
  1. 方向契约投影丢失：catalog 的 `direction` 只读 runtime config，注册期写进 `evidence_json.candidate_validation` 的
     signed-IC/方向从不进入 catalog → 1,239/1,239 候选判 `direction_contract_invalid`（探针 59/59，CANARY_50 档除该码外无其他 blocker）。
  2. 轮转饿死：候选按阶段优先级排序 + 500 硬截断，897 行排在 SHADOW 之前 → 496 个 SHADOW 永不评估；
     `_update_shadow_performance` 只覆盖进程 registry 的 ~42 个 callable（09-08~09-11 日志实测）。
  3. 退役不可达：`_retire_quarantined_discovered` / `_rollback_canary_regressions` 的人口判据是 catalog `source=='discovered'`，
     而 dsl+SHADOW 投影为 `source=='shadow'`；快车道要求 `fresh_evidence_bars==0` 且以轮转会刷新的 `updated_at` 为年龄口径；
     健康窗要求 `0<health_score<30`，而 SHADOW 候选不进健康评估集（`runtime.factor_health` 52 行、dsl 0 行）。
  4. 门槛三套（阶梯 `deployment/canary.py`、治理 config `factor_governance_shadow_*`、准入卡），无单一终审。
  5. 自锁：PROBATION→ACTIVE 要求 lifecycle 已有 committed ACTIVE backing，而 lifecycle ACTIVE 又只能由依赖准入卡的晋升路径产出。
- canonical：候选定义/方向 = `factor_lifecycle_state`（`metadata_json.expression`、`evidence_json.candidate_validation`）；
  OOS 证据 = `shadow_factor_perf`（唯一）；阶梯状态 = `canary_state`；退役执行 = `FactorLifecycleService.retire`（唯一写者）。
- 本批删除/替换：catalog 的 config-only direction（改 candidate_validation 优先）；轮转阶段优先级（改
  `_selected_canary_candidates` 按评估年龄单一选择，shadow 刷新与 canary 轮转共用）；快车道 `fresh_evidence_bars`+`updated_at`
  判据（改 `_shadow_evidence_clock` 读 `evidence_end_at`，并豁免当前晋升合格者）；`source=='discovered'` 人口判据（改 durable
  lifecycle origin）；`evaluate_shadow_factors` 增 `expressions=` 入口，评估不再依赖进程 registry 覆盖面。
- 退出条件：(a) 开盘后首批真实 `prepare/activate` 或 `retire` 动作落地且无 `blocked_by_evidence` 洪泛；
  (b) `nonterminal_candidate_count < QUANT_CANARY_EVALUATION_LIMIT` 且连续一周 GP 注册正常；
  (c) 首个 `origin=dsl` 因子在 `factor_lifecycle_state` 达到 ACTIVE。
- 验证：修复后只读探针——CANARY_50 样本 25 中 6 个 `_promotion_evidence` eligible（修复前 0）；退役扫描 711/1,896 行满足
  证据停滞判据（修复前 16）；轮转选择 500/1,234 且含 SHADOW 135 行（修复前 0）；`evaluate_shadow_factors(expressions=…)`
  实测 oos_bars 249。针对性测试 `tests/test_factor_catalog_governance.py`、`tests/test_factor_governance_acceleration_flow.py`、
  `tests/test_evolution_closure_fixes.py`、`tests/backend/runtime/`（55 passed）+ `pytest -m smoke`（215 passed）。

### 学习建议应用闭环（approved 建议不落地；2026-09-13 定位，两处已修、一处待裁）

- 状态：`monitoring`（replay 准入语义与不可执行建议收口已修；`entry_cluster` 缺 actuator 待用户裁定）
- 事实（2026-09-13 只读）：5 条 `approved` 且 `governance_eligible=1` 的建议长期没有 `applied_mutation_id`（最早 2026-08-31 `stoch_k boost_small`），
  且 `learning_workload_gate` 因此把每轮休市维护判为 `run_pending_governance` 而跳过。
- 根因：
  1. **replay 准入门不可达**：`FactorWeightChangeService._replay_admission` 要求 `ReplayHarnessService.status()` ok，而治理 replay 报告连续 6 份全为 C——
     80 个决策的 47 个 disagreement 全是「live 因 live-only 闸门拒绝（39× `supervisor_reentry_cooldown`、5× `learning_weak_signal_threshold`、
     3× `loss_cooldown_active`）、离线重算允许」。这是缺失 live 状态（input gap），不是重算分歧；按 disagreement 统计使 grade 恒为 C，
     于是 rsi_14 / macd_hist / di_spread 三条降权建议每次都被 `blocked_by_replay` 拦在应用账本之前。
  2. **不可执行建议永不收口**：`_apply_approved_factor_suggestions_for_demo` 对 `old_weight<=0`（如已隔离的 `stoch_k`）只记 `skipped_non_actionable_weight`、
     不 supersede → 该行永久 `approved` → workload gate 永久 pending。
  3. **（未修，待裁）** `entry_cluster`（`increase_same_direction_cooldown`）没有 actuator：stepper 的 step 与 pending 口径都不含该 scope，
     批准后无人应用；live 侧 `_active_entry_cluster_learning_policy` 只能读到已应用控制，所以该建议永远不生效。
- canonical：replay readiness = `ReplayHarnessService.status()`（唯一）；建议状态机 = `policy_suggestion.status` + `applied_mutation_id`；
  应用写入者 = `FactorWeightChangeService`（因子权重）与各 scope 的 typed mutation/Coordinator。
- 本批替换/删除：replay 比较语义（live-only 拒绝改记 `live_state_gap_count`，不进 grade；真正分歧仍记 disagreement）；非可执行建议从 `skipped` 改为 supersede 并保留原因；`superseded` 计数覆盖两类收口。
- 现查证据（修复后）：新报告 `bar_replay_90cfb475bdb242fe` grade A、`status ok=True fresh blockers=[]`、config/code 绑定一致、risk disagreement 0 / live_state_gap 47、sub-action 0。
- 退出：(a) 开盘后首个 stepper 周期内三条因子降权落到 `applied_mutation_id`（或按审批口径 superseded），`stoch_k` 行被 supersede；
  (b) `entry_cluster` 要么补 actuator 并落地首个真实控制，要么明确退役该 surface。
- 验证：`tests/test_replay_release_evidence_contract.py`、`tests/test_autonomous_learning.py`、`tests/test_governance_contract_convergence.py`、
  `tests/test_factor_weight_change_service.py`、`tests/test_replay_gate_downstream_block.py` + `pytest -m smoke`。

### 平行 authority、重复门控和无退出兼容层

- 状态：`migrating`（2026-08-18；P4 单轨写入完成，P5 DROP 因代码引用未完成而回滚）
- canonical：一个事实只有一个生产计算者和一个写入者；Safety、Risk、Readiness、API、前端不得平行重算同一授权事实。
- 当前：2026-08-10 已将账户/持仓 freshness blocker 收敛到 `live_reconciliation.evaluate_reconciliation_snapshot`，最终开仓 admission 与 readiness 复用同一结果；loop/readiness 只投影一个失败 blocker，`loop_status()` 不再通过读状态写入诊断事实。持仓对账、unknown execution、no-new-risk latch、generation 和 authority 校验仍保持独立 fail-closed。
- 剩余：Safety/Generation/Execution Outcome/Governance/PG Job Queue 仍有发布期开关或旧兼容；客户端仍有少量旧 fact 字段迁移。
- 退出：新路径通过各自运行门后，同批删除旧 authority、fallback、同义 blocker 和 pass-through wrapper。
- 验证：调用链、静态入口扫描、合同测试、运行 snapshot 与 `git diff --stat`。

### shadow/discovered/live 生命周期兼容

- 状态：`migrating`
- canonical：`factor_lifecycle_state` + `factor_runtime_projection` + Factor Card `factor_admission_evidence.v1`；ACTIVE 必须经 typed Coordinator/V16、稳定 artifact、fresh health、loaded ack、至少 20 个独立成熟干净证据和受控 observing effect，成熟正向真实 effect 前不得扩权。
- 当前：代码已使 legacy ACTIVE 缺完整准入证据时以 `legacy_evidence_incomplete` 排除选择，并由治理 owner 使用同 generation `demote_to_shadow`；context/gate 不投方向票，alpha 以 signed IC 校验方向。Evolution 只由 learning worker 在 `23,53` 运行，使用 `evolution_cycle_watermark.v1` 幂等 GP，并按 `QUANT_CANARY_EVALUATION_LIMIT` 背压；Backend 重任务注册和启动补偿已删除。
- 剩余：运行态尚需应用代码/迁移并观察遗留 ACTIVE 的真实排除与退回；切入 typed lifecycle 前的 native builtin fallback、领域服务 coordinator-off 隔离兼容和静态开关关闭兼容仍在。不得通过数据库回填 ACTIVE 或伪造 PIT/walk-forward/cost/lineage/effect 证据。
- 退出：现有 ACTIVE builtin 按 code-bound identity、V16、prepared、真实 loaded ack 和 fresh health 分批重入 lifecycle 后删除 builtin fallback；稳定 enforce 发布后删除领域服务的 generic restore 兼容。启动层的旧 template/supervisor/Registry restore 已删除；除六因子有界 Demo 经典种入外，不得用直接数据库回填 ACTIVE 绕过晋升证据。

## 2. 执行与运行时

### live tick safety 阶段耗时远超节奏（2026-09-10 登记）

- 状态：`active`（只读观测：tick 名义节奏 5s，`safety timing` 显示单 tick `total` 常在 5~122s，`safety=` 段是主因；内存/readiness 批次与该耗时无关，修完尖峰后耗时无改善）
- 事实与 owner：三个分段在 `backend/services/live_loop_tick_runtime.py` 串行测量并打印（`positions`=reconcile_positions、`account`=reconcile_alpha_account、`safety`=`runtime.run_safety_cycle`）；owner 仍是 live loop 串行 tick，不新增 authority、不为提速并行化 tick。
- 影响：循环长期追赶（5s 节奏被打成 1 tick/10~120s），决策与保护延迟随之放大，backend 常驻 CPU 被占；属执行链问题，不是内存问题。
- 证据：`grep -a "safety timing" logs/live_loop.log`——`19:59:33 tick 146 ... safety=119.69s total=121.71s`、`19:48:50 tick 12887 ... safety=43.06s total=45.07s`、修复前 `19:14:12 tick 113 ... safety=71.32s total=72.67s`。
- 剩余：先只读归因 safety 段内部（broker RPC 等待 / Safety 计算 / 锁等待），不得先动节奏、加线程或降门控。
- 已排除：`live_safety_state` 的 latch 全量重放（4GB 账本、每次 append 后 23~29s，且在模块锁内）已在 `ff4e0ecc` 用重放游标消除（append 后只折尾部 + 重放移出锁）；写入侧已在 `3593fd19`（逐值上限）+ `09ea95e1`（整条 metadata 64KB 上限）封顶，旧账本已于 2026-09-10 压缩 3.94GB → 3.2KB（归档保留于 `data/safety/archive/`，按 `scripts/compact_safety_latch_ledger.py` 校验折叠一致）。safety 段剩余耗时继续归因 broker RPC / Safety 计算本身。
- 退出：连续 60 分钟内 `safety timing` 的 p95 `total` < 5s 且无 `account_blockers`；针对性测试绿。
- 验证：`grep -a "safety timing" logs/live_loop.log | tail -50`。

## 3. 治理、研究与客户端

### PostgreSQL 重复写入与写放大（2026-08-30）

- 状态：`monitoring`；`factor_runtime_projection` 已用 v33 将 `projection_id` 固定为主键，保留进程身份唯一约束；`RuntimeKVStore`、确定性 policy/model audit identity、治理周期合并和停盘 workload gate 已接入生产写入路径。
- 清理结果：权限审计旧重复累计删除 `97889+3+1` 条，保留 `2` 条当前语义结果；policy `1824` 组、因子目录 `50` 组、runtime config payload 孤儿 `0`、projection 失效 coordinator `0`；canonical_v2 未触碰。
- 运行证据：停盘且 watermark 无新事实时当前仅有 pending governance，四类重型任务均在约 `0.10s` 内返回；readiness/selection 两个大 JSON 的相同语义写入只更新行级时间，不重写 value body。
- 后续：保留至少 24 小时的表大小、WAL、TOAST、写入次数和调度周期证据；若 pending governance 清空，再确认状态稳定落到 `skip_closed_no_new_facts`。

### 因子扩张后验降级应用未完成

- 状态：`monitoring`（2026-09-02 398d3126：执行端受限权重降级已落地——posterior_degraded 激活按 factor_governance_posterior_degraded_weight_scale（默认0.5, 上限0.50）打折进入受控再试验, 样本流不断; blocked（样本足）在执行端兜底禁止; 无 apply 历史因子不受影响; 待真实 degraded 激活周期与 effect 观察闭环后 resolved）
- canonical：`FactorGovernanceOrchestrator._posterior_expansion_guard` + `posterior_expansion_verdict`，复用 `learning_application_effect` 的最新有效 factor effect；V16 delegate 粒度和既有后验阈值不变。
- 当前：因子扩张候选已统一经过 posterior preflight；`blocked_by_posterior` 会阻断，样本不足只标记 `posterior_degraded`，查询不确定时 fail-closed。
- 未触发（2026-09-11 只读）：近 7 天 `posterior_degraded` 相关 canonical 事件 0 条、backend/worker 日志 0 命中，受限权重降级路径尚未被真实样本触发，退出条件待首次触发后评估；此期间不得把 `posterior_degraded` 标记解释为已执行降级治理。
- 退出：降级应用经过现有 RiskPolicy、V16、Coordinator 和 effect observation 连续真实周期验证后，从本登记册删除。

### parity replay 尚非 live-equivalent

- 状态：`migrating`
- 当前：复用 closed-bar、RiskPolicy 与保护纯原语，并绑定 config/data/code/factor artifact hash，但缺 broker/tick/safety/account/cost/projection-ack 的完整 PIT 事实。
- 权限：固定 `diagnostic_only`、治理数量为零；runner 永不自授权。
- 退出：只有独立 certification 重验完整 live lifecycle 后才能讨论 live-parity evidence。

### Tauri/React 前端替换

- 状态：`migrating`（2026-09-01 代码层复核：服务器为 `sparse checkout` 无 `web_frontend` 目录，属本地 Tauri 2 个人自用桌面债，非服务器运行债；保留 migrating 但不计入服务器未退出数）
- canonical：web_frontend 内的 Tauri 2 + React 19 renderer；服务器端 API、fact.v1、
  /ws/state、认证和 mutation contract 继续作为唯一权威。
- 当前：新五工作区和直接废弃路由已经落在 React 19 + Vite/Tauri renderer；Workbench Shell、Safety
  rail、唯一 `/ws/state`、强类型 endpoint decoder、IndexedDB 研究缓存、Tauri 2 壳和
  signed NSIS updater artifact 均已落地。旧页面、旧 AppShell、`src/lib/compat.ts`、旧 route alias、
  旧页面绑定 accessibility 样式和关键 endpoint 的宽泛 decoder 已删除。`/api/market/bars`
  已补齐 `market.bars.v1`，缺数据明确返回 unknown；默认月库路径保持兼容，交易页的
  `source=live` 只读 cTrader trendbar 内存 feed，不把月库作为实时替代；本次 live 投影尚未随远程服务重载，当前远端仍可能返回 `bars_monthly`，需下一次后端发布后完成运行验收。2026-08-13 的 static artifact
  切换到公网 Caddy 根目录属于历史验证；当前迁移要求撤下浏览器静态入口、服务器只保留
  API/WSS 与后端工作树；2026-08-14 已完成 sparse checkout、blob 过滤、Caddy API/WSS-only
  和前端产物清理。API 合同补丁已部署并重启验证，旧 dist 和 API pre-change
  文件已留存仓库外 rollback archive；factor cards 已改为优先复用最新持久 catalog
  snapshot，远程 44 个 factor-card 测试通过。
- 替代：Workbench Shell、Trade Ops、Risk Desk、Research Lab、Governance、Ops
  五个工作区、全局 Safety rail、强类型 endpoint decoder、唯一 live store 和
  IndexedDB 研究只读缓存。
- 剩余：代码层离线读取缓存、动作禁用、Credential Manager bridge 和 updater wiring
  已实现，但真实 Tauri 断网恢复、缓存 hash/schema、Windows 安装卸载、WebView2
  缺失路径、GitHub Actions Secret/manifest（workflow 已准备，Secret 未配置）、签名成功/失败回退、Linux API/WS/auth 全量
  运行验证和完整验收仍未完成。
- 退出：五个工作区通过 frontend-refactor-acceptance-matrix.md，生产入口一次性
  切换，新旧 route 不再并存，旧页面/fallback/import/宽泛类型删除，签名包和
  updater 回退通过；回滚使用 commit/artifact，不恢复长期旧地址别名。

### API/frontend 旧事实字段

- 状态：`migrating`
- canonical：endpoint-specific `fact.v1`；unknown/stale/error 不得显示绿色或授权 start/unlock，最后 known 值可带时间保留。
- 剩余：Web/小程序 recursive compat 和旧字段窗口。
- 退出：客户端迁移完成且满足两个小程序版本或 30 天取更长者，删除旧回退。

### meta 环收敛期退役清单（2026-09-05 盘点，只列未删）

- 状态：`active`（A3 盘点草案；学习主环切换为开仓证据 meta-labeling 后，以下对象失去存在理由，按批次退役）
- canonical：本清单为唯一退役对象列表；替代对象为 3–6 个生产信号的最小健康监控 + `open_quality_lightgbm` live shadow 链（本批已接入 `live_service._evaluate_open_quality_model_veto` 无策略分支，mode=`live_shadow`，纯观察 fail-open）。
- 剩余（按退役顺序）：
  1. GP/canary 因子发现产线**保留**（2026-09-13 用户裁定：让发现链路完整走通到实盘或正常退役，不以削宽度为目标；同日闭环修复批见上方“因子发现闭环缺陷”）；本项退役对象改为产线内的重复实现本身（stage-priority 轮转、`source=='discovered'` 人口判据、config-only direction 已删）。supervisor 出生钩（`register_supervisor_shadow`，origin=supervisor隔离alpha/背压）保持，已真实产出（2026-09-13 advisory 回放 committed `auto_overprotection_relief.bfabfc4bed.v1`）；
  2. `backtrader` / `APScheduler` 依赖声明（各仅 1 处引用）——二选一：删除声明或真实启用，不得长期双挂；
  3. `backend/services/` 中 18 个 <120 行单调用方壳层——内联；
  4. 451 个 reason code 收敛与 72 张 runtime 投影表并表——生产因子收缩后审计面同步收敛。
- 退出：每批满足"替代已运行 + 针对性测试 + 全量回归绿 + 净删除为正"方可标记 resolved；shadow 写入分支的退出条件为模型 influence veto（demo_canary）稳定运行 ≥100 笔后复核是否保留为 fail-open 兜底。
- 验证：`run_artifacts/baseline_comparison/`（A2 基线 PASS，2026-09-05）与 `run_artifacts/open_quality_validation/`（holdout AUC 0.40，过拟合实证，enforce 不准入）为本批决策留档。

### open_quality 模型过拟合隔离（2026-09-05 登记）

- 状态：`active`（模型以 shadow-only 运行；enforce 被证据门阻断，属设计内隔离而非故障）
- 事实：153 条 matured 样本重训实证过拟合——train AUC 0.986 → holdout AUC 0.40（差于随机），正类召回 0.13；holdout 尾段上多数类（全盘接受）0.605 优于模型 0.526 与规则基线 0.421。L1 特征快照（1,332 条路径标签，与执行真相一致率 0.725）：信号强度家族反向/无信号，结构家族（factor_conflict_ratio 0.595、negative_contribution_abs 0.586、n_active_alpha_factors 0.572，真相 AUC）为唯一双标签一致候选；≥8 个特征为零方差或无信号。
- canonical：live shadow 链（mode=`live_shadow`，纯观察 fail-open）为唯一 active 写入者；veto 权力仍要求 influence policy 过 `ACTIVE_STAGES` + canary 治理，未激活。
- 剩余：下次重训前把特征砍至 10–12（保留结构家族 + bar 形态 + action_score 家族之一，剔除死特征）；等 1,265 条 pending 样本成熟 + live shadow ≥50 笔 fresh 对账。
- 退出：重训后 holdout AUC 稳定 ≥0.55–0.6 且 train/holdout 落差收窄、独立样本 ≥300，才进入 enforce 的治理讨论；在此之前任何 enforce/veto 配置变更均视为违规。
- 验证：`run_artifacts/open_quality_validation/`、`run_artifacts/feature_ic_snapshot/`、`run_artifacts/baseline_comparison/`（同窗口四基线全净亏，系统 +$65/Sharpe 2.40）。

### 学习 worker 内存瞬时峰待收敛（2026-09-10 登记，83efda02 部分收敛；761319c3+b5e44220 本轮收敛中）

- 状态：`active`（滞留已止；f60b96aa C 批已上：证据决策扫描限新 3000/6387，治理候选名单跨周期一致，单因子 FULL==窗口双算、资格相同，0 ERROR/NRestarts=0；post-redundancy RSS 稳定 272~336MB；in-scan HWM 计算轮峰仍 ~380MB+ 未达 300MB 线；24h 棘轮待观察）
- canonical：重任务唯一 owner 仍为 learning worker（`evolution_orchestrator` + `autonomous_learning`，不新增 authority）；已落地 F3（research_df 延迟拷贝，live 验证 research=deferred）、F5（coordinator `request_stop` 中断 480s 锁等待并补单测，21:09 式 stop 超时 SIGKILL 根因已除）、F1a（`list_cards`/`list_recommendations` 加 `use_cache` 开关且默认 True，autonomous materialize 旁路 60s TTL 全局缓存，stage +420→+15MB 四轮连续）、choke 点 arena trim（`release_free_memory`，`_run_compact_learning_stage` + evolution finally）、全套 RSS+HWM 分段日志（纯 stdlib，无 schema/契约变更）；backend 侧本轮另修 readiness 路径两处 `policy_suggestion` 全表 `evidence_json` 扫描（`autonomy_health._action_stats`、`backend_readiness._governance_status`，只对 `status='approved'` 带 evidence），build 热态峰值 +452→+33MB、生产 2 分钟尖峰 +224~438→+41~93MB；backend 启动突发 1.6~2.3GB 实测以可回收 page cache 为主（同一 cgroup file 887→156MB 由内核自行回收，refault_file 36k、pgmajfault 1、0 OOM），首次 readiness 增量仅 +71MB，build 后 `release_free_memory` 每轮回收 ~71MB 但下次 build 即被重新吃掉；三项均判无收益，不另设条目、不落地、不改 readiness 路径。F4 已有据撤销（raw 8000-bar 仅 MB 级）；F1b（catalog 只索引幸存 id）已实现并经生产数据 A/B 证输出一致，但峰值无变化，按收敛规则回滚，不保留。
- 剩余（按 ROI）：① redundancy：in-scan HWM 峰仍 ~380-466MB（payload 解码+parse 为主，text cache 段清已证非主因），post-stage RSS 已 272/336MB；`groups=1（macd_hist/swing_distance）`连续三轮，“恒为空”前提已破，跳过门须按“上轮有 groups ∨ signal_cfg 有残留键 ∨ 有非 redundancy 候选”重定条件，继续攒 invariant 证据，不得先斩；② provider 全表扫描（f60b96aa 已收敛决策侧：重路径只扫新 3000 decision，reviews 721 行/training 窄行/effects 保持全量，mature 计数不动；materialize 绝对值 ~1.1GB 系进程 floor 驱动，stage 增量 ±20MB 内已平；扫描量此后不再随历史无限增长）；旧的全量描述作废，以本句为准；③ floor 构成：进程 floor ~1.2GB 稳定但拆解未完成（registry 1712 因子元数据、各调用方 card 缓存键等），只许只读观测，不许加新监控表/线程（`_process_memory_snapshot` + 分段日志已够用）。
- 退出：单轮 HWM 增量 redundancy <300MB 且 materialize <100MB；进程 24h 零重启下无 +300MB/h 式棘轮（以 DB `memory_profile` + cgroup MemoryPeak 为准）；针对性测试绿；旧债本条转 resolved，追溯走 Git（83efda02 起）与审计事实。
- 验证：`journalctl -u quant-learning-worker --grep "mem (after redundancy|after list)"` 分段 HWM；`SELECT payload_json FROM evolution_events WHERE event_type='autonomous_learning_cycle' ORDER BY timestamp DESC LIMIT 1` 看 stage delta；`systemctl show quant-learning-worker -p MemoryPeak`；改动前后各跑 `tests/test_evolution_work_coordinator.py`、`tests/test_evolution_cycle_watermark_v1.py`、`tests/test_evolution_governance_handoff.py`、`tests/backend/runtime/test_factor_governance_orchestrator.py`、`tests/test_factor_cards_api.py`。

## 4. 明确退役，禁止恢复

- 旧 PostgreSQL `state_v1` schema 及其数据；生产只使用 `runtime` 与 `canonical_v2`；
- V16 认知层账本（`brain_action_plan`(+`_eval`/`_payload`)、`brain_medium_impact_governance`、`brain_state_snapshot`、`brain_low_impact_execution`）与 medium-impact 候选产线（2026-09-11 退役，证据留 `run_artifacts/v16_cognition_retirement_20260911.dump`）；
- emergency close 的 `refresh_positions` / `refresh_account_info` 旧兼容入口（零调用方，严格完成语义只能由 fresh post-reconcile 证明）；
- SQLite `data/state.db` 运行态主库；
- 历史 tick 采集与 `ticks.duckdb`；
- L2 collector、depth 风控字段与历史 L2 库；
- MT5 并行执行路线；
- 旧 Web Console/H5 web-view；
- 旧 cloud deploy/docker-compose 打包路线；
- 临时前端 smoke/debug 脚本和仓库内历史回测输出。

## 5. 登记模板

```text
