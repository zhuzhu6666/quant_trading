# 持仓监督确认链修复批 · 影响面清单（待用户审批）

> Status: draft — 2026-08-26，仅清单未动代码
> 触发：2026-08-26 复盘发现监督器对最近 10 笔仓位"诊断正确但从未动手"，深挖确认为结构性断线而非冷启动数据不足。
> 审批人：用户。批准后按 change-impact-checklist 流程实施。

---

## 0. 一句话结论

持仓监督的动态调仓能力（tighten/reduce/close）在当前生产代码里**结构性地无法触发**：5 个失效证据族中 4 个的"生产者"缺失或数学上不可达，唯一活路（接近止损）与 broker 原始止损完全冗余；且反事实学习链只消费已执行的监督动作，动作恒为零 → 学习恒为空 → 治理候选永远凑不齐证据 → 模板永不更新。**这是死循环，继续积累交易数据不会自愈。**

## 1. 问题事实（全部有运行数据/代码行佐证）

| # | 断线 | 证明 |
|---|---|---|
| F1 | `signal_reversal` 无生产者 | 全仓库只有读取方（`position_supervisor.py:454-458`、`live_risk_sizing.py:435`），无任何生产路径置 true；实测所有仓位 verdict 中恒为 false |
| F2 | `entry_regime` 开仓时不落盘 → `regime_shift` 恒 none | 判定在 `position_metrics.py:81`（entry≠current 才 confirmed）；但开仓路径无人写 entry_regime——`build_position_path_metrics_inputs`（lifecycle.py:2370）只收 current_regime，recovery meta 的 entry_regime 只在已有值时透传（:2311）；实测 29/29 笔已平仓位 entry_regime 全空串、regime_shift 全 none |
| F3 | 时间衰减证据族数学不可达 | 需 timeout_ratio≥0.80，即持仓≥19.2h（risk_max_holding_bars=288×M5=24h）；实际平均持仓<1h（20 笔全 SL/TP 出场，最长 2.6h），实测比例 0.006–0.109 |
| F4 | thesis 连续确认计数无递增者 | `thesis_broken_confirmations` 从 risk 上下文读取（supervisor.py:446-451），无生产者逐 bar 递增；实测恒 0 |
| F5 | transition_confirming 姿态下几乎禁用一切主动动作 | posture 判定（supervisor.py:174-240）：weakening/broken/regime_shift 即入 transition_confirming；giveback/time-decay 驱动的 tighten/reduce/close 分支全部要求 range_capture（:643,669,674,690），transition_confirming 分支（:622-628）只追加观察标签；唯一例外是 near_take_profit 分支（:632-639，不检姿态），但其要求 TP 进度≥0.92——实测近期仓位 MFE 时点进度最高 0.802，同样不可达。出问题的仓位姿态恰全是 transition_confirming |
| 后果链 | 反事实恒空 | `supervisor_counterfactual.py:479-486,533-546` 只认 supervisor_tighten/reduce/close 且 stage=executed+broker/reconcile 双确认的真实执行 trace；动作零 → counterfactual 零 → 治理候选要求 replay+trace_count>0（brain_governance_candidates.py:1254）永不满足 |

运行证据：284997494 平仓前 5 分钟 verdict 记录显示 giveback=1.0 / capture=0 / efficiency=0.15 / thesis=broken / break_ready=true / evidence_families=[]（四族全空）→ hold(thesis_break_unconfirmed)；最终 -9.54 出场，复盘自动打标 avoidable_loss + alpha_correct_but_capture_failed。

## 2. canonical authority 声明（修复后）

一个事实一个计算者，全部落在既有模块内，不新增权威：

- **入场 regime 快照**：owner = 开仓链现有的 `_upsert_recovery_position_state` 写入口；值来源 = 既有 `resolve_market_regime(composite)`（market_regime.py:76，决策时点已可计算）。存 recovery_meta.entry_regime，与现有 current_regime 同一存储位置，不新增表/字段。
- **信号反转**：owner = 监督器上下文构建处（live_service.py:_build_position_supervisor_context 链）。值 = 决策因子合成的当前方向与开仓方向相反（composite.direction × entry direction < 0，closed-bar 确认）。写入 recovery_meta.supervisor_state，消费方不变。注：composite.direction 在开仓决策点可用（live_service.py:8160 等已消费），开仓方向可从 entry_protection_plan.direction（recovery_meta 已有）读取。
- **thesis 失效连续计数**：owner = 既有 path-metrics 状态机（position_metrics.py:update_position_path_metrics，本就每 tick 迭代 position_path 状态并持久化到 recovery_meta）。计数随 broken 状态连续出现递增、转 intact 归零。状态机已有，只补一个计数器字段。
- **transition_confirming 姿态解锁 tighten**：owner = 既有 evaluate_position_supervisor 动作仲裁（position_supervisor.py:591+）。只放宽该姿态下的 tighten 授权（带独立防护阈值），close/reduce 权力不动。

权力层归属：全部属于 Risk sizing/监督执行层，不触碰 Safety 与 Readiness；执行仍走唯一链 supervisor → RiskPolicy → cTrader → lifecycle → fresh reconcile（事实源 §135 已固定），本批不改执行链任何环节。

## 3. 调用方影响（全部只读或既有消费者）

- `evaluate_position_supervisor`：verdict 新增非 hold 动作的可能性 → 下游 RiskPolicy/broker/lifecycle/trace 本就支持这三种 action（legacy-debt-register §23 明确这是待验证闭环），无需改动。
- 反事实复盘 `evaluate_counterfactuals`：零代码改动，开始自然收到真实执行 trace。
- 治理候选链（brain_governance_candidates → V16 → PositionSupervisorGovernanceMutationService）：零代码改动，证据开始积累。
- 学习样本层（_sample_from_supervisor_trace / _dynamic_tpsl_labels）：零改动，标签映射已覆盖 tighten/reduce 的成功与失败形态（over_protected / missed_protection / profit_protected 等）。

## 4. 替代对象与删除清单

本批为"补齐缺失生产者"，无可删除的旧实现，但明确删除以下隐性假象：

- 删除"`signal_reversal` 是可用证据"的隐含假设：修复前它在文档/阈值语境中被当作有效输入，修复后要么有真实写入者、要么从证据族文案中降级说明（随实施确定，不新增兼容层）。
- 不保留任何"直接置 true 的测试旁路"进生产路径；测试用注入 context 的方式构造反转场景。

## 5. 不新增项（硬承诺）

不新增：表、线程、调度器、service/wrapper、阈值常量（复用模板 thresholds 现有键）、RuntimeConfig 键、canonical 事件类型、第二套 regime 计算（复用 resolve_market_regime 单一权威）。

## 6. 安全边界与风险控制

- 所有新动作仍过 RiskPolicy + governed_execute + fresh reconcile，风控拒绝即 fail-closed（现状合同不变）。
- tighten 在 transition_confirming 下启用时加双保险：① 仅盈利单（current_pnl>0）允许收紧保利润；② 沿用 `_tightened_sl`（supervisor.py:49-72）的现有数学——目标 SL 恒取 max(current_sl, breakeven, lock_price)（多头）/对称 min()（空头），结构上只收紧不放松，模板 breakeven_lock_ratio=0.25 为下限。
- 修正说明（自查发现）：near_take_profit 的 tighten 分支本就不检姿态，但其 TP 进度≥0.92 门槛实测不可达（近期最高 0.802），因此 F5 解锁针对的是 giveback/time-decay 驱动的收紧路径，不与该分支重叠。
- reduce/close 权力范围本批不变（仍在 range_capture/exit_commit 内）。
- 观察姿态 unknown_observe 行为不变（市场维度未知仍然只观察）。

## 7. 验证门（实施时逐条打勾）

1. 单测：五个断线各有针对性测试（entry_regime 落盘→regime_shift 可 confirmed；方向反转→signal_reversal=true；连续 broken 计数≥2→persistent_price_path 入族；transition_confirming+盈利+tighten 门槛→tighten verdict；以上任一组合→evidence_families 非空且 thesis_break_confirmed 可达）。
2. 既有回归：test_position_supervisor_templates / test_factor_pruning_governance / test_autonomous_evolution_cycle / test_governance_contract_convergence 及监督域相邻测试零回退。
3. 运行验收（重启加载后）：首笔真实持仓的 recovery_meta 中 entry_regime 非空；首笔触发 tighten 的仓位产出 stage=executed/is_real_execution=true 的 supervisor_trace；首个 counterfactual 事件落库；治理侧 brain_governance_candidate_review 对 supervisor 模板候选不再报 missing_canonical_v2.counterfactual_review。
4. 回滚：单批提交，git revert 即整体回退；无 schema 变更、无配置迁移，回滚无残留。

## 8. 文档同步义务（实施完成时）

- system-source-of-truth.md：监督器小节补记三个生产者的 owner 声明（§2 配置事实源附近）。
- legacy-debt-register.md：§23"仍需一次真实 Demo supervisor 动作闭环"与本批衔接；新增一条 resolved 记录描述四根断线。
- phased-repair-rollout-status.md：新增批次记录（问题事实/canonical authority/deleted paths/targeted verification/runtime verification）。
- README 当前结论：S7.6 之后的主线第 2 条更新为监督闭环证据收集。

## 9. 明确不做的事

- 不调 risk_max_holding_bars（参数水位是策略决策，即使修完断线也由治理通道另行讨论）。
- 不改 close/reduce 的授权姿态范围。
- 不动 Safety shadow 发布门、不动 V16 合同（批次 B 刚收紧的 review 门不受影响——本批复活的正是它等待的证据流）。
- 不在本批引入模型驱动监督（quality advisor 投影保持现状）。

## 10. 给用户的决策点

1. 批准全部四个修复点（F1/F2/F4 生产者补齐 + F5 姿态解锁 tighten）？
2. 还是先只做 F2（entry_regime 落盘，纯记录无行为变化，风险最低）观察一天再做其余？
3. 修完后的观察期（预计需 ≥10 笔含监督动作的仓位才能产生首批反事实）是否接受？
