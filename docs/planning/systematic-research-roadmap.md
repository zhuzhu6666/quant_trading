# 系统性研究路线（2026-09）

> Status: active（研究领域活动计划，与 production-autonomy-repair-optimization-plan.md 并行，不替代全局生产计划）
> Last updated: 2026-09-13
> Scope: 因子 × 持仓监督 × 复盘归因 三个控制变量的系统研究路线、阶段证据门、模块合作地图。
> 权力边界、资格体系、治理通道的权威定义一律引用 system-source-of-truth.md §1/§3/§4/§5 与
> learning-evidence-contract.md / position-supervisor-contract.md，本文不复制、只链接。

## 1. 为什么是系统研究，不是因子研究

生产闭环是一个被三个控制变量共同决定的系统：

```text
因子层(开仓方向/时机) ──► 风险层(sizing/闸门) ──► 执行层(broker/reconcile)
        ▲                                            │
        │                                            ▼
学习/治理层 ◄── 复盘归因层 ◄────────────── 持仓监督层(持仓路径干预)
(权重/模板/候选)   (review/counterfactual/    (MFE/MAE path metrics,
                    sample/eligibility)        tighten/reduce/close)
```

三个变量互相中介，单维度研究会互相污染：

1. **监督动作改变 outcome 分布**：tighten/reduce/close 直接改写单笔 PnL 与路径。若归因不区分责任域（`entry/exit/holding/data_quality/parameter`，SSoT §5），
   "监督太紧砍掉了本会盈利的单"会被记成"因子方向错"→ 错误降权好因子。
2. **因子质量决定监督样本分布**：入场质量差 → 监督干预频率升高 → 监督 trace 的动作分布偏斜，
   监督模板学习会被"收拾烂摊子"的动作主导。
3. **归因质量是学习信号的总闸**：`learning_eligible` 谓词、`causal_level` 权重（observational 0.55 →
   intervention_observed 1.00）、污染即零权重——归因层产出的证据等级决定一切下游学习能学到什么。

结论：阶段之间只有两条真实依赖——R1 的归因分解是 R3 因子结论的前提（R1→R3），R4 是 R2 候选进入生产治理后的效果验证（R2→R4）；
R2 的反事实证据由未来价格路径直接分类（`supervisor_counterfactual._classify_counterfactual`），不经过归因层，与 R1 并行（R1∥R2）。
不存在的依赖不设顺序门；每阶段仍需自己的证据门（见 §3）。

## 2. 控制变量与模块合作地图（一个事实一个计算者）

研究只读 canonical 事实、只产出候选与证据；下列 owner 表是研究的"读取地址"和"交付地址"。

### 2.1 三个控制变量的生产链与回流

| 控制变量 | 生产 owner（live 决策用） | 学习回流 owner | 研究的读取事实 | 研究产出的交付通道 |
|---|---|---|---|---|
| 因子（方向/时机） | `LiveDecisionFrame`（live decision pipeline，SSoT §3） | `DecisionPolicy` + `FactorWeightChangeService`（权重）、`FactorGovernanceOrchestrator`（lifecycle）、`ExperiencePriorService`（0.85–1.15 先验） | canonical_v2 `factor_snapshots`（决策内嵌，带 lineage）、`factor_contribution_review`、`factor_health` | `FactorGovernanceOrchestrator` 证据/候选 → typed mutation |
| 持仓监督（干预） | `PositionSupervisor` 裁决族（`live_supervision_runtime`，绑定模板 `position_supervisor_binding.v1`） | learning worker：`mature_position_supervisor_traces()` 成熟化 + `position_supervisor_governance` 写 `runtime_kv[position_supervisor_selection.v1]`（唯一 memory→live 投影） | `supervisor_trace` / `counterfactual_review` events、`recovery_meta.position_path`（MFE/MAE 唯一累计写边界）、成熟化样本 `sample_type=supervisor_execution_trace` | 监督模板候选 → V16 bridge → `PositionSupervisorGovernanceMutationService`（Coordinator 事务） |
| 复盘归因（信号质量） | `live_closed_position_processing`（review/experience/suggestion 写入顺序 owner） | `ExperienceBuilder` → `trade_lesson_memory.v1`、`learning_backfill`、eligibility 物化（`learning_eligible` 谓词唯一） | `trade_review`（entry_timing/decision_freshness/system_issue context）、`counterfactual_review`（M1 后验：correct_stop vs protection_too_tight）、`broker_deal`（真实成交价/净额） | 不直接"交付"——它是上游；质量问题走 suppression/eligibility projection，不删事实（SSoT §5） |

### 2.2 学习与治理底座（研究产出必须经它生效）

- 样本资格：`training_sample_row` + `learning_eligible` 谓词（attribution/context integrity 双 full + 无污染 close_reason）。
- 治理效果后验：`learning_application_effect`（经 `LearningApplicationStore`）——`posterior_expansion_verdict`
  与 V16 `posterior_fingerprint` 的唯一事实源。
- 实验预算：同 scope 单 active experiment + 全局 24 槽（`learning_experiment_admission`）——研究必须排队，不能并行轰。
- 治理执行：typed plan → `DecisionPolicy`/`RiskPolicyService` → `GovernanceMutationCoordinator` 事务 → effect 观察窗。
- regime：`project_current_market_regime()`（experience_memory.regime_id 只读投影）——研究分桶必须复用它，不建第二个 regime 计算者。
- 重任务执行：`runtime.jobs` 五类（`discover/external_refresh/sync/factor_health/parameter_template_validation`，事实源 system-source-of-truth.md §5），job worker 唯一执行，evolution_work_coordinator advisory lock 错峰；`backtest/tuning/ab_test` 三类入口已于 2026-09-06 `99c9c327` 删除，parity replay 仅作 `parameter_template_validation` 内部库。
- 研究证据信任边界：`research_evidence` 统一 fail-closed 校验；历史 parity 回测恒 `governance_eligible=false`，回测样本 `causal_level=replay` 且只允许回测增强训练（权重 ≤ 真实、同 holdout 比较）。

## 3. 阶段路线（每阶段有证据门；只保留 R1→R3、R2→R4 两条真实依赖，R1 与 R2 并行）

### R0 基线测量（只读，先行）— **done，见 [research-r0-baseline-report.md](research-r0-baseline-report.md)**
- 结论：R1 与 R2 并列第一优先、R3 押后；监督呈“单一 close 路径 + 43.4% 过紧后验”，
  归因层有 35.3% 污染率与未启用的责任域分解。2026-09-13 补充测量（报告 §6）：257 笔亏损中 86.4% 曾浮盈、
  54.1% 带利润回吐标签、70.4% 带入场侧标签（两者重叠 37.7%）——退出侧不是唯一来源，`bad_loss` 是入场侧标签。
  细节以报告为准，本节不再复制。
- 目标：量化当前学习信号里最大的信息损失来自哪一层。
- 方法：只读聚合现有事实（474 position_transition / 357 trade_review / 198 counterfactual_review /
  112 supervisor_trace / 52 supervisor_evaluation，截至 2026-09-13）：
  1. 责任域分布：entry vs exit vs holding vs data_quality vs parameter 的亏损责任占比；
  2. 归因质量：`factor_attribution` 完整率、`system_issue_context.contaminates_learning` 触发率、
     eligibility 通过率（contaminated/pending/missing lineage 各占多少）；
  3. 监督有效性：`counterfactual_review` 中 correct_stop vs premature_tighten/protection_too_tight 比例；
  4. 因子贡献：10 个外部低频因子（dxy_corr/gld_tonnes 族/real_yield 族/slv_gld 族）在
     factor_contribution_review 中的真实贡献与 regime 分布。
- 产出：一份只读研究报告（脚本入 `scripts/` 或 `run_artifacts/`，结果入 docs；不新增表、线程、调度器）。
- 门槛：报告结论必须指明 R1–R3 的优先级排序依据。

### R1 归因质量研究（复盘层先行）
- 假设：学习信号的损失集中在归因层与监督层（污染/证据不足/责任域混淆）；因子层的贡献需在责任域分解后才能判定（R0 §6：70.4% 亏损带入场侧标签，不能排除因子层）。
- 方法：R0 报告 §5 已把原三条线索（延迟脏值、binding invalid、hold/exit 恒零）断代为旧代码产物或
  fail-closed 正确标注，R1 不再重复核查；剩余为 ① 冻结 `factor_contribution_review` 的消费者决策
  （`factor_regime_prior` 复活先验、`factor_counter_evidence` keep/prune 打分——`factor_attribution.v1`
  只有 largest factor + score，无 per-regime 净贡献，不是 1:1 替代）；② 继续观察重启带仓 `chain_broken`
  纪律与 binding hash mismatch 率；同时验证 `good_win/lucky_win/bad_loss` 标签与 MFE/MAE/giveback
  路径的一致性。其中 ① 的执行（改/退役消费者）属生产修复批，按 authority、删除清单和测试合同走，研究轨只提供事实与选项。
- 产出：归因改进证据或明确的“保留/退役”决策 → 若发现系统性误标签，走 suppression/eligibility
  projection（不删事实）。
- 门槛：指标在 matured 且非设计排除的样本上可复算（全量 eligibility 16.3% 的分母被 excluded/pending
  主导，不作为 R1 指标）；责任域一致性只认 `entry_quality` + `failure_tags` 口径——`exit_quality` 对亏损单恒为
  0.25（构造性地板），不得用作亏损退出质量的分位证据。

### R2 监督有效性研究
- 假设：监督实际走单一 close 路径（112 trace 的 requested_action 全部是 close、98.1% `thesis_broken` 触发），
  close 触发点存在系统性过早/过紧（43.4% 负面反事实），且缺少 tighten 渐进保护中间态。
  （`tighten` 从未被请求或执行，不存在可评估的 tighten/reduce 时机数据，不作该假设。）
- 方法：按绑定模板分层比较可区分路径指标（MFE>0 曾浮盈比例、giveback_ratio、time_in_profit_ratio、
  holding_efficiency）与 counterfactual 判定；`pnl/mfe` 捕获率对亏损恒 0，不作为分层指标。
  regime 条件化（复用 `project_current_market_regime()`）。
- 产出：监督模板证据复核与候选质量评估；交付通道是生产侧既有 advisory（`build_position_supervisor_advisories`
  写 `policy_suggestion`）→ V16 bridge/Governor → Coordinator。`position_supervisor_selection.v1` 是已应用模板的
  effect 投影，不是候选入口。
- 产出边界（2026-09-13 只读定位）：数量门已满足（55 笔 governance_eligible matured）；`tighten` 覆盖门已随 2026-09-08
  `ca23580e` 退役（close 经 keep→supportive 计入干预证据），README/rollout status 旧口径以 register 与代码为准。
  当前断在首跳：32 条治理合格 advisory 建议全部 `superseded`（`autonomous_learning.py:5079` 要求 V16 bridge 证据），
  `position_supervisor_template` 的 application/effect=0，`f16024bb`（09-11）删除 medium-impact 产线后该 scope
  无 `brain_governance_candidate` 生产者。修链属生产修复项，研究批只交付证据与评估，不闭环进 Demo。
  2026-09-13 该产线已按选项① 补入（`0d77157d`：专员 `delegate_supervisor_template_switch` 产出 candidate+command），
  首条 application 待下一个带新事实的周期做运行态验收。
- 门槛：每候选单 regime stratum、完整成熟反事实、无污染（position-supervisor-contract §8，成熟化口径 §7.4）；
  分层前先声明各 stratum 可用样本，样本不足的 stratum 只报观察不产候选（binding invalid 25.3% 先排除或标注）。

### R3 因子研究
- 前提：因子贡献结论需要 R1 的责任域分解（当前无因子级分解生产者）。注意 R0 的 `bad_loss 50.1%` 本身就是
  入场侧标签（`classify_4label_outcome`：`conviction≥0.55 or avoidable_entry`）——高信心入场后亏损占一半，
  因子层已有直接可测信号；R3 押后的理由是“归因分解未就绪”，不是“退出侧优先”。
- 方法（现有通道内）：
  1. 用 parity replay 通道（`parameter_template_validation` job 内部库 `backtest_service`/`parity_replay`，
     无独立 job/API 入口）验证 10 个外部低频因子的独立贡献与成本后净值；
  2. `discover` 只在背压解除后有意义：非终态 dsl 候选 2026-09-13 为 1,239，达到 `QUANT_CANARY_EVALUATION_LIMIT`
     即停止 GP 注册，排空由 learning worker 的 canary 轮转完成；研究只读观察注册/排空，不制造注册；
  3. 参数与组合假设同样走 `parameter_template_validation` 离线验证（`tuning`/`ab_test` job 已删除）；
  4. factor regime 条件绩效已有模型承担（`pit.v4.factor_regime_decision_lineage`），研究不建第二个
     regime 绩效计算者。
- 产出：因子权重/生命周期候选 → `FactorWeightChangeService` / `FactorGovernanceOrchestrator` 通道。
- 门槛：扩张候选自动过 `posterior_expansion_verdict`（上次这么干亏了就不重复）+ regime fit 闸；准入证据
  按 `factor_admission_evidence.v1`（≥20 独立成熟干净证据、PIT/walk-forward/cost、multi-forward、lineage）。

### R4 端到端闭环验证
- 定位：生产治理/operator 门禁段；研究只提供前置证据与复评输入，不拥有 mutation。
- 方法：parameter_template_validation（parity replay 内部通道）→ 有界 Demo effect 窗口 → posterior 复评。
- 产出：可回滚的 committed mutation + effect 终态（reinforced/rolled_back/inconclusive）。
- 门槛：效果比较优先精确 regime；样本不足保持 observing，不声称因果（SSoT §5 效果归因质量）。

## 4. 节奏与观察指标

- 监督治理自动进 Demo 的计数门已满足（55 笔 eligible matured），当前不是数据积累问题而是候选链修复问题（见 R2 产出边界）；开盘后只确认 supervisor_evaluation/trade_review 事件流恢复，不人为制造 tighten。
- 学习 worker 内存峰、dsl_auto 排空速度按登记册口径只读观察，不设新监控。
- safety timing p95 采样与 tick 耗时归因（登记册 active 债）在开盘后顺带完成，属运行态观察不属研究批。

## 5. 明确不做

- 不新建研究框架/表/线程/调度器/第二套回测；一切重任务走 `runtime.jobs` 现有五类。
- 不把生产链修复（如 supervisor 候选/桥接缺口）和 operator 治理决策当作研究阶段；研究只把事实与选项交给对应轨道。
- 不从模型输出反推原始事实；研究只读 canonical events 与其 payload。
- 不绕过 `research_evidence` 信任边界；回测证据永远不自带 executable 资格。
- 不并行轰同一 scope（单实验槽）；不做无法回滚的"直接改配置"。
- 物理删除污染/重试事实永远禁止（SSoT §5），只能追加受治理的 suppression/eligibility projection。
