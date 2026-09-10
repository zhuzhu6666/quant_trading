# Active Legacy Debt Register

> Status: active
> Last verified: 2026-09-10 (文档收敛批：按本文件 scope 删除 19 条已 resolved/complete 条目和 1 条占位标题，40→20 条；overlay 操作边界迁入 server-backend-sop §8)
> Scope: 只登记尚未退出的兼容、重复 authority、隔离数据和回归（active / migrating / monitoring / quarantined / regressed）。

已完成旧债不在本文保留；Git 历史和测试是追溯依据。新增条目必须写清 canonical 路径、剩余旧路径、退出条件和验证。

## 1. 全局收敛

### demo 放量批（只跑demo，2026-09-07 批准落地中）

- 状态：`monitoring`（2026-09-08 只读：09-07 22:11与09-08 12:54两次重启均一次成功NRestarts=0，overlay两次重绑后current，system_health healthy 1.0，live tick新鲜pos=0，readiness ready_for_live_execution/ autonomous_mutation true且blockers空；tighten硬门已退役，待selection首现候选）。
- 已生效：risk_cvar_threshold_pct 2.5->3.5 经 Coordinator 合法通道 gmut_4cdc71001e23426ea1ca74559fc37cd1（demo 豁免，projection current，可逆）。
- 暂存待重启：cooldown 3->2（risk/strategy/supervisor_reentry，保留loss熔断）、weak cap demo 0.55->0.45、canary 25->20；live_execute 选项及两处死门、parameter_templates off 三处旁路（含直写绕过）、backtrader 声明已删；APScheduler 验明真实启用保留。
- 未能直达：protect 试点改走 08-11 静态基线先例：settings 切 profit_protection.v1；22:11 受控重启三服务一次成功（NRestarts=0），base 变更致 overlay 短暂隔离符合预期；22:23 经 Coordinator 同值重绑 gmut_f4da3b75（no_change 免 V16，hash 188a02 三处对齐）→ worker 22:25:38 自愈、backend 告警清零；22:28 起 system_health healthy 1.0，活循环新代际正常出 tick，CVaR 3.5 / cooldown 2 / canary 20 / weak-demo 0.45 / protect 模板全部加载。60 分钟观察窗 22:29-23:29 通过：0 新 ERROR、health 全程 healthy 1.0、隔离 0、活循环正常出 tick；22:56 首笔重启后开仓 LONG 286638811（score 0.77，intent 确认）持仓中；tighten 待持仓形成 MFE，protect 模板已加载。
- 退出：受控重启后 overlay 重绑成功且 60 分钟 0 新 ERROR，tighten 硬门已于 2026-09-08 退役，转 resolved 待 selection 首现候选。

### 单仓 supervisor template 开仓绑定（代码与重启验收已完成，真实生命周期证据待收口）

- 状态：`monitoring`（2026-09-01 代码层复核：live_service 已写 / 并 ，重启后 ；等一次真开/重启/平仓闭环后 resolved）
- canonical：`position_supervisor_binding.v1` 由 live open path 在成交前绑定，保存于现有
  `entry_protection_plan.supervisor_binding` 和 `recovery_position_state.recovery_meta_json`；监督计算仍由
  `PositionSupervisor` 唯一负责，风险裁决仍由 `RiskPolicyService` 负责。
- 当前：新仓位保存完整规范化 template snapshot、version、hash、source、selection key 和 evidence refs；
  重启/恢复会校验 hash。旧仓位只标记 `legacy_global_fallback`，损坏或未知 binding 进入
  `unknown/hold`，硬风险仍可收口。全局 `position_supervisor_template_id` 只作为新仓位基线，不改写已绑定仓位。
  2026-08-27 双服务受控重启后，backend/worker 均 `active/running` 且 `NRestarts=0`，既有 learning
  周期真实发布 `position_supervisor_selection.v1`；当前状态 `insufficient_evidence`、候选 `0`、自动模式
  `off`，本次选择链没有发生 broker mutation。
  2026-08-28 只读复核：最新持仓 `285427255` 的 `recovery_position_state.recovery_meta_json` 已验证 `entry_regime=trend=weak|vol=low`、`selection_key`、`supervisor_binding.template_hash=cdfe2bf...`、`thesis_broken_confirmations=136`、`signal_reversal`、`current_regime` 三生产者均有值；`live_position_lifecycle.build_position_supervisor_context_payload` 与 `position_metrics` 状态机打通。`recovery_position_state 61 行` 最新 3 笔均携带完整 binding。
- 退出：完成至少一次真实 open/restart/recovery/close lineage 验证，并证明所有新 supervisor trace 都能
  回溯 binding；不得新增第二个 supervisor writer、表或调度器。

### supervisor 经验已进入记忆索引，但自动模板准入仍未达标

- 状态：`monitoring`（2026-09-05 复核修正：验收线收紧为 **tighten 覆盖**——reduce 已按 2026-09-02 用户决定永久关闭（最小手数不可减），不得再作为验收项。2026-09-02：eligible 38 / matured 47，数量超 10 笔门槛；缺 tighten 真实执行证据——55 笔 trace 全为 close（thesis_broken/timeout/regime），盈利仓全由 broker TP 单触发；trend_hold 回吐信号缺失已修（5ba55b47 bar 级评估事件后干预需求可见）；准入仍缺真实干预执行，等待受控试点或策略变更。tighten 代码可达性已验证：`range_capture`/`transition_confirming` 姿态下盈利+回吐≥giveback_tighten_threshold 均有真实 tighten 路径；trend_hold 盈利仓按设计只打标不动作）
- canonical：原始事实由 `canonical_v2.supervisor_trace/counterfactual_review` 承载，学习资格由 `canonical_v2.training_sample_row` 承载，经验检索使用 `experience_memory`，V16 检索/后验使用 `brain_memory` 和 `posterior_arbitration`。
- 当前（2026-09-08 只读）：`training_sample_row 12978`，`supervisor_execution_trace 9458`中`governance_eligible=1 & matured=53`（数量已超10笔门槛；`matured/full 96`，其余`pending 2755 + excluded 6607`）；`trade_review_outcome 224（eligible 1.0共162，08-28为67/46）`；`event supervisor_trace 104/counterfactual 136/broker_execution 440/position_transition 440/trade_review 256`；`brain_memory 294`、`experience_memory 219`；`selection.v1 candidate_count=0`但已有`profit_protection.v1` governed基线绑定（`bound`，此前`insufficient_evidence/off`），`origin=supervisor` lifecycle行0（出生钩已随09-08 12:54重启加载，待首个auto模板）。数量门已过，缺候选review→V16/Coordinator→effect/rollback连续链。
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

### dsl_auto SHADOW 积压 1712（2026-09-08 复核：1838→1712，源头节流持续生效，排空慢待收口）

- 状态：`active`（2026-09-08 只读：`factor_lifecycle_state` origin dsl/shadow/discovered非终态1712、`can_register=false`；GP注册持续停止直到积压 < 200）
- 问题事实：旧背压口径走 registry/canary_state 窄投影长期误报 0，dsl_auto 生成侧持续注册（每周期最多 10 个），
  积压 1712（09-03为1838）且 shadow 晋升门因 OOS PnL 全负实质关闭——队列只进不出。
- canonical：入册节流唯一口径 = `factor_lifecycle_state` origin `dsl/shadow/discovered` 非终态行数；
  消化侧 = 治理周期收紧动作（2026-09-05 提额：retire ≤15/周期、disable ≤9/周期，此前 5/3——
  09-03~05 实测排空 14-80/天，提额后预期约 3 倍，<200 退出线预计 ~12 天可达）+ canary 晋升/退休。
- 退出条件：`nonterminal_candidate_count < QUANT_CANARY_EVALUATION_LIMIT(200)` 且连续一周 GP 注册可正常进行。
- 剩余观察：09-03~08实测约25/天（1838→1712），低于提额后3倍预期；`canary SHADOW 925/CANARY_* 897/QUARANTINED 54`。已知约束：`_retire_quarantined_discovered` 要求 `0 < health_score < 30`，
  积压中健康分为 0（UNKNOWN）的因子不满足该条件，只能依赖模型 `weak_for_disable` 判定进入候选——这是排空速度的
  最大不确定项；若 1712 → <200 耗时不可接受，再评估批量退休通道（需另行确认）。

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

### emergency close 严格完成语义

- 状态：`migrating`
- canonical：先持久化 no-new-risk latch；只有 fresh post-reconcile 确认目标 position ID 消失才算 completed。
- 已收口：`refresh_positions()` / `refresh_account_info()` 兼容入口已删除；安全、恢复和显示读取统一使用显式 reconcile 结果或其只读投影。
- 退出：所有安全/恢复调用只接受 immutable authoritative reconcile contract。

### live_service 领域重力

- 状态：`migrating`
- canonical 模块：reconciliation、serial loop、emergency、position protection、open submission/protection/processing、execution recovery 已分离；fresh position reconcile 是既有 `recovery_position_state.recovery_meta.position_path` 的唯一 live 累计写入边界，event/API 投影不写入。
- 剩余：`live_service` 仍保留 process wiring、兼容状态发布和少量 lifecycle wiring；仓位路径持久化失败必须显式降级为 unknown，不得把单次观测伪装成累计 MFE/MAE。
- 验证：启动暖机优先使用 cTrader 在线历史，月初当月月库为空或 broker history 不可用时再通过 `bars_monthly_read_paths()` 回读最近历史闭合 bar；live bar freshness、风险和 readiness 以 online trendbar frame 为准，月库只作低频副本与离线兜底。
- 退出：只迁出真实决策/状态机；不为“拆文件”新增 wrapper。稳定发布后删除旧 globals 和 compatibility authority。

## 3. 治理、研究与客户端

### 因子治理重复重算与固定候选错配（2026-08-30）

- 状态：`monitoring`（2026-09-01 代码层复核+重构：`build_factor_catalog` 10处分支已收敛为 `_maybe_refresh_catalog` 单helper，条件重建；剩余130秒为审计/投影写入，非目录重建）
- canonical：因子目录仍由 `build_factor_catalog` 唯一生成；Canonical 决策快照仍是冗余分析和学习证据的只读事实；V16 仍以单候选、固定 manifest 委派扩张 mutation。
- 已确认根因：`run_cycle` 用累计 action 触发后续目录重建；shadow 绩效、决策快照、review payload 和 admission evidence 存在 N+1/重复全量扫描；冗余 group 数被当作候选数但没有具体候选，导致 V16 固定候选合同无法执行。
- 已修复：按阶段 action 只在真实 mutation 后刷新目录；shadow 绩效和冗余快照改为批量读取；学习/卡片复用已解码的 Canonical payload；review freshness 一次聚合；参数模板在同一卡片快照内复用；冗余报告生成一个具体配置 mutation candidate，并限制执行只能消费该 V16 candidate；无变化时不生成 mutation 或重复目录重建。
- 验证：历史实测治理轮次约 `697.3s`；本次只读复测目录约 `0.5–3.4s`、冗余约 `2.6s`、因子卡片约 `5.8s`、参数推荐约 `7.0s`；受影响测试 `179 passed`，全量 `2966 passed, 11 skipped`。
- 真实复测：手动写入型 run `manual_perf_dcb5122c200b` 总耗时 `142.091s`，治理 run `132.040s`，因无当前 V16 command 正常 `blocked_by_v16_command` 且无 mutation；随后正式 `evolution_hourly` 日志 `138.6s`、治理 run `130.787s`。这相对历史 `697.3s` 已显著下降，但仍高于只读探针，剩余主要是逐 action 审计/投影写入耗时，不能宣称已完成性能收口。
- 测试垃圾清理：仅删除上述唯一 run 的 20 条 suggestion、1 条 catalog snapshot、40 条 runtime decision、20 个独占 API mutation payload 和 1 条 run；Canonical V2 40 条不可变事件保留，未删除任何有用事实或仍被引用的 payload。
- 剩余：发布后仍需以正式版本 PID/日志和真实 V16 claim/finalize、Coordinator projection/effect 证据完成治理验收。不因性能修复删除 Canonical 事实、治理账本或仍有引用的历史记录。

### PostgreSQL 重复写入与写放大（2026-08-30）

- 状态：`monitoring`；`factor_runtime_projection` 已用 v33 将 `projection_id` 固定为主键，保留进程身份唯一约束；`RuntimeKVStore`、确定性 policy/model audit identity、治理周期合并和停盘 workload gate 已接入生产写入路径。
- 清理结果：权限审计旧重复累计删除 `97889+3+1` 条，保留 `2` 条当前语义结果；policy `1824` 组、因子目录 `50` 组、runtime config payload 孤儿 `0`、projection 失效 coordinator `0`；canonical_v2 未触碰。
- 运行证据：停盘且 watermark 无新事实时当前仅有 pending governance，四类重型任务均在约 `0.10s` 内返回；readiness/selection 两个大 JSON 的相同语义写入只更新行级时间，不重写 value body。
- 后续：保留至少 24 小时的表大小、WAL、TOAST、写入次数和调度周期证据；若 pending governance 清空，再确认状态稳定落到 `skip_closed_no_new_facts`。

### 因子扩张后验降级应用未完成

- 状态：`monitoring`（2026-09-02 398d3126：执行端受限权重降级已落地——posterior_degraded 激活按 factor_governance_posterior_degraded_weight_scale（默认0.5, 上限0.50）打折进入受控再试验, 样本流不断; blocked（样本足）在执行端兜底禁止; 无 apply 历史因子不受影响; 待真实 degraded 激活周期与 effect 观察闭环后 resolved）
- canonical：`FactorGovernanceOrchestrator._posterior_expansion_guard` + `posterior_expansion_verdict`，复用 `learning_application_effect` 的最新有效 factor effect；V16 delegate 粒度和既有后验阈值不变。
- 当前：因子扩张候选已统一经过 posterior preflight；`blocked_by_posterior` 会阻断，样本不足只标记 `posterior_degraded`，查询不确定时 fail-closed。
- 剩余：`posterior_degraded` 的受限权重/scope 应用路径尚未落地，不能把标记解释为已执行降级治理。
- 退出：降级应用经过现有 RiskPolicy、V16、Coordinator 和 effect observation 连续真实周期验证后，从本登记册删除。

### 部署重启 overlay hash 绑定冻结与 worker 崩溃风暴（2026-09-02 修复）
- 状态：`monitoring`（2026-09-02 登记：f9da796a + 47c6e682 已修复并重启演练验证；**2026-09-10 更新：机制已被 schema v34（`0034_governance_mutation_intent_overlay_hash`，2026-09-08 应用）取代——base-only 变更不再导致失配，冻结根因消除；本条目待 v34 下多次真实部署重启验证后转 resolved**）
- canonical（v34 后）：`runtime_config_overlay` 启动校验改为 overlay 行内容 hash 绑定（`committed_overlay_hash`）+ intent committed/current；无该列的旧 intent 仍走全量 base+overlay hash 比较（过渡期）。overlay 直写仍 fail-closed。`runtime_config.refresh_from_overlay` 每 5s 全量重试；learning worker 启动 restore 失败必须 fail-closed 存活而非退出。
- 当前：部署重启（YAML/config 结构变化）会使 register_shadow 提交的 hash 绑定失配 → 全量校验失败 → backend fail-closed 冻结新风险最长 38 分钟（2026-09-02 实测），learning worker 直接退出触发 systemd 重启风暴（17 次尝试、evolution 停机 ~5h）。修复：① learning worker overlay 失败改 fail-closed（quarantined YAML base 继续 observation/research，mutation 由 capability 门控，心跳 30s 重试完整 restore）——重启风暴消除；② `_auto_projection_key_compatible` fallback：对 factor_lifecycle.register_shadow 自动投影，overlay 键 ⊆ base 键且 patch 键 ⊆ overlay 行时接受 committed/current intent（hash_compatibility=auto_projection_key_compat）——冻结从 ~38min 降为秒级；operator/风控 mutation 与死键 overlay 仍严格 fail-closed。重启演练（真实注入失配 hash）验证：fallback 路径 restore 成功、还原后绑定回 current、零 ERROR。
- 禁止：把 fallback 扩展到 operator/risk 类 mutation；用来源名或"看起来保守"恢复扩张/未知 overlay。
- 退出：连续 ≥3 次真实部署重启无冻结（启动即 restored）且无 fallback 误放行后，评估是否可收紧（register_shadow 提交时重绑或局部键校验替代全量 hash）；worker fail-closed 路径经 ≥1 次真实 overlay 失配周期验证后转 resolved。

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
  1. GP/canary 因子工厂宽度机器——`dsl_auto` SHADOW 积压（2026-09-08为1712）排空后，退役 GP 注册、canary 阶梯批量评估、DSL 批量准入路径；supervisor出生钩（`register_supervisor_shadow`，origin=supervisor隔离alpha/背压）已随09-08 12:54重启加载但生产0行，待首个auto模板；
  2. `backtrader` / `APScheduler` 依赖声明（各仅 1 处引用）——二选一：删除声明或真实启用，不得长期双挂；
  3. `backend/services/` 中 18 个 <120 行单调用方壳层——内联；
  4. `live_service.py`（12.6k 行）领域重力——按执行/恢复/投影拆分归主；
  5. 451 个 reason code 收敛与 72 张 runtime 投影表并表——生产因子收缩后审计面同步收敛。
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
- canonical：重任务唯一 owner 仍为 learning worker（`evolution_orchestrator` + `autonomous_learning`，不新增 authority）；已落地 F3（research_df 延迟拷贝，live 验证 research=deferred）、F5（coordinator `request_stop` 中断 480s 锁等待并补单测，21:09 式 stop 超时 SIGKILL 根因已除）、F1a（`list_cards`/`list_recommendations` 加 `use_cache` 开关且默认 True，autonomous materialize 旁路 60s TTL 全局缓存，stage +420→+15MB 四轮连续）、choke 点 arena trim（`release_free_memory`，`_run_compact_learning_stage` + evolution finally）、全套 RSS+HWM 分段日志（纯 stdlib，无 schema/契约变更）；backend 侧本轮另修 readiness 路径两处 `policy_suggestion` 全表 `evidence_json` 扫描（`autonomy_health._action_stats`、`backend_readiness._governance_status`，只对 `status='approved'` 带 evidence），build 热态峰值 +452→+33MB、生产 2 分钟尖峰 +224~438→+41~93MB；backend 启动突发 1.6~2.3GB 实测以可回收 page cache 为主，不另设条目、不处置。F4 已有据撤销（raw 8000-bar 仅 MB 级）；F1b（catalog 只索引幸存 id）已实现并经生产数据 A/B 证输出一致，但峰值无变化，按收敛规则回滚，不保留。
- 剩余（按 ROI）：① redundancy：in-scan HWM 峰仍 ~380-466MB（payload 解码+parse 为主，text cache 段清已证非主因），post-stage RSS 已 272/336MB；`groups=1（macd_hist/swing_distance）`连续三轮，“恒为空”前提已破，跳过门须按“上轮有 groups ∨ signal_cfg 有残留键 ∨ 有非 redundancy 候选”重定条件，继续攒 invariant 证据，不得先斩；② provider 全表扫描（f60b96aa 已收敛决策侧：重路径只扫新 3000 decision，reviews 721 行/training 窄行/effects 保持全量，mature 计数不动；materialize 绝对值 ~1.1GB 系进程 floor 驱动，stage 增量 ±20MB 内已平；扫描量此后不再随历史无限增长）；旧的全量描述作废，以本句为准；③ floor 构成：进程 floor ~1.2GB 稳定但拆解未完成（registry 1712 因子元数据、各调用方 card 缓存键等），只许只读观测，不许加新监控表/线程（`_process_memory_snapshot` + 分段日志已够用）。
- 退出：单轮 HWM 增量 redundancy <300MB 且 materialize <100MB；进程 24h 零重启下无 +300MB/h 式棘轮（以 DB `memory_profile` + cgroup MemoryPeak 为准）；针对性测试绿；旧债本条转 resolved，追溯走 Git（83efda02 起）与审计事实。
- 验证：`journalctl -u quant-learning-worker --grep "mem (after redundancy|after list)"` 分段 HWM；`SELECT payload_json FROM evolution_events WHERE event_type='autonomous_learning_cycle' ORDER BY timestamp DESC LIMIT 1` 看 stage delta；`systemctl show quant-learning-worker -p MemoryPeak`；改动前后各跑 `tests/test_evolution_work_coordinator.py`、`tests/test_evolution_cycle_watermark_v1.py`、`tests/test_evolution_governance_handoff.py`、`tests/backend/runtime/test_factor_governance_orchestrator.py`、`tests/test_factor_cards_api.py`。

## 4. 明确退役，禁止恢复

- 旧 PostgreSQL `state_v1` schema 及其数据；生产只使用 `runtime` 与 `canonical_v2`；
- SQLite `data/state.db` 运行态主库；
- 历史 tick 采集与 `ticks.duckdb`；
- L2 collector、depth 风控字段与历史 L2 库；
- MT5 并行执行路线；
- 旧 Web Console/H5 web-view；
- 旧 cloud deploy/docker-compose 打包路线；
- 临时前端 smoke/debug 脚本和仓库内历史回测输出。

## 5. 登记模板

```text
