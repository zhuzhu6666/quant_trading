# R0 基线测量报告（2026-09-13）

> Status: complete（一次性只读测量，可随新数据复跑）
> Method: `scripts/research_r0_baseline.py`（只读 canonical_v2 / runtime，无写入）
> Data: `run_artifacts/research_r0_baseline_20260913.json`
> Scope: [systematic-research-roadmap.md](systematic-research-roadmap.md) §R0 的四项测量与优先级判定。
> provenance 说明：v36 迁移前存量事件 `provenance=unknown` 属预期，本次未按 provenance 过滤。

## 0. 事实覆盖

| 事实 | n | 时间跨度 (CST) |
|---|---|---|
| risk_decision | 6,894 | 08-20 → 09-11 |
| broker_execution / position_transition | 474 | 08-20 → 09-11 |
| trade_review | 357 | 08-20 → 09-12 |
| counterfactual_review | 198 | 08-27 → 09-11 |
| supervisor_trace | 112 | — |
| supervisor_evaluation | 52 | — |
| training_sample_row（全部类型） | 13,917 | — |
| factor_contribution_review | 3,462 行 | — |

## 1. 归因质量（R1 输入）

**结果标签（357 复盘）**：`bad_loss` **50.1%**、`good_loss` 21.8%、`good_win` 18.8%、`lucky_win` 9.2%。
一半交易被判为“坏亏损”（`bad_loss` 是入场侧标签，解读修正见 §6）。

**污染与责任域**：
- `system_issue_context.contaminates_learning=true` 占 **35.3%**（126 事件；去重后 **59 对唯一 (review, trade)**，
  多次发射为 review 重发——断代核查见 §5），其中 98.4% 主责任为 `operator_intervention`（restart_replay /
  恢复链，与 v37 回填的 88 chain_broken / 146 restart_affected 同源）。
- 因子贡献表 `factor_contribution_review` 的 entry/hold/exit 三列从未启用且**写入口已于 09-12 17:10 随
  "guess-attribution 产线"删除**（`27c97a6e`），表已冻结只读；现役因子归因是 review payload 内嵌的
  `factor_attribution.v1`（显式 `causal_claim=false` 观察级）。SSoT 要求的"区分入场与退出责任"目前由
  MFE/MAE/outcome_label 承载，因子级责任分解不再有生产者（见 §5 遗留项）。

**证据等级**：trade_review 的 `causal_level` **100% observational**（0.55 权重上限）；反事实证据全部
独立存在于 counterfactual_review（见 §2）。

**数据精度缺陷（具体、可修）**：entry_timing 延迟统计混入 epoch 级脏值（max≈17.87 亿秒，
即填入了时间戳本身）——SSoT §5 已规定混合基准样本不得进入延迟统计，实测至少 1 例漏网
（断代核查见 §5：旧 trade_reviewer 混单位产物，当前合同守卫下不可能再产生）。

**样本资格（training_sample_row，13,917 行）**：
- 治理资格通过率仅 **16.3%**（2,275）；不合格主因 74.6% 为
  `not_matured;integrity_partial;system_contaminated;not_model_ready;executable_governance_not_allowed`。
- 类型分布：`supervisor_execution_trace` excluded 6,607（47.5%）+ pending 2,755（19.8%），
  **matured 仅 104（0.7%）**；`risk_rejection` matured 1,791（12.9%）是当前最大的 matured 池；
  trade_review_outcome matured 241、shadow_open_decision matured 243。

**MFE 捕捉率（pnl/MFE，n=322）**：p25=0.0、**median=0.0**、p75=0.996——双峰分布，
一半以上交易 MFE 完全回吐，顶部四分之一几乎吃满（该比值与输赢分布高度同义，独立解读见 §6）。

## 2. 监督有效性（R2 输入）

**反事实后验（198 条，causal_scope=supervisor）**：

| label | n | pct |
|---|---|---|
| correct_stop | 92 | 46.5% |
| **protection_too_tight** | **69** | **34.8%** |
| noise_stopout | 17 | 8.6% |
| insufficient_future_data | 11 | 5.6% |
| entry_failure_or_correct_stop | 9 | 4.5% |

- 成熟度：fully_matured 53.0% + governance_ready 19.7%；`governance_eligible` 72.7%。
- **负面前验合计 43.4%**（protection_too_tight + noise_stopout）——接近半数监督干预被 M1 后验判定为过早/过紧。
- **绑定状态 invalid 占 25.3%**（50/198），None 14.1%——绑定完整度本身有缺口。

**监督动作分布（112 trace）**：`execution_status=applied` 92.0%，其中 **requested→effective 全部是
close（85.7%）**——**tighten 从未执行过、reduce 未出现在执行 trace**；触发原因 98.1% 是
`thesis_broken`；模板分布 default.v1 80.8% / profit_protection.v1 19.2%。

**解释力**：单一 close 路径 + thesis_broken 单一触发 + 43.4% 过紧后验，三者指向同一结论——监督器目前是
“一刀切止损器”，缺少渐进保护中间态（tighten/profit-protection 路径没有真实执行覆盖）。注意：`pnl/mfe` 捕获率
双峰主要是输赢分布本身（亏损恒 ≤0），不作为独立证据；`tighten` 也**不是**进有界 Demo 的门槛——该门禁已于
2026-09-08 `ca23580e` 退役为“close 经 keep→supportive”，实际缺口是候选链（见 §6）。

## 3. 因子贡献（R3 输入）

- **最大贡献因子平均净贡献全为负**：stoch_k(-0.108)、supertrend_str(-0.048)、macd_hist(-0.122)、
  ema_slope(-0.019)、di_spread(-0.110)；唯一净正的战术因子是 rsi_14(+0.186)。
- **事件/时段 context 因子高度异质**：hours_to_nfp（-0.84，亏损单 -1.10 / 盈利单 +1.62）、
  hours_to_fomc（亏损 -0.83 / 盈利 +1.97）、hour_utc、keltner_width、atr_ratio 同款双峰——
  它们与 outcome 的关系强烈依赖 regime/事件窗口，不适合作为恒定方向输入。
- 决策参与率：17 个 code-owned 因子每单全在（2000 抽样中 1,917 决策含全部）；宏观外部因子
  （COT/cb/gld/real_yield/slv_gld）参与率 73.7%。regime 分布分散（10 档，最大 20.6%，空 regime 4.2%），
  confidence 中位 0.8。
- 注意：贡献表已冻结（写入口随 09-12 guess-attribution 产线删除，见 §5），其数据描述的是
  legacy 时代归因；当前归因以 review payload `factor_attribution.v1` 为准。因子结论的可信度仍受
  R1 修复进度制约。

## 4. 优先级判定（R0 门槛产出）

**排序：R1（归因质量底座）与 R2（监督有效性）并列第一优先，R3（因子）其次。**

1. **R2 拥有最直接的可执行证据**：43.4% 负面反事实、单一 close 路径、tighten 零执行、MFE 双峰。
   监督模板的证据窗/触发阈值改进能直接改写 outcome 分布（bad_loss 50.1% 的"退出责任"部分），
   且其治理通道（trace 成熟化 → selection 投影）已建好，只差合格证据。
2. **R1 是 R2/R3 结论可信性的前提**：35.3% 污染率、责任域分解未启用、延迟统计脏值、绑定 invalid 25.3%——
   这些缺口不修，R2 的反事实样本合格率与 R3 的贡献数据都打折。R1 的修复动作是具体而小的
   （eligibility projection、脏样本标注、绑定完整度核查），不是新建体系。
3. **R3 押后**：最大贡献因子全负、事件因子双峰说明组合有优化空间，但在责任域分解生效前，
   "哪个因子该负什么责任"不可判定；且因子权重变更的治理预算（单实验槽）应留给信号质量修复后的验证。

**R1 首批具体线索**：原三条线索（延迟脏值来源、binding invalid 成因、hold/exit 恒零）已于 §5 断代核查中
解决——均属旧代码产物或 fail-closed 正确行为。R1 剩余工作重心：冻结贡献表的 staleness 消费切换
（market_regime / counter_evidence 是否改读 `factor_attribution.v1` 口径）、重启带仓量的运行纪律观察、
binding hash mismatch 率的持续观察。

## 5. 污染断代核查（2026-09-13，用户要求：确认污染只存在于旧版本）

结论：**五类问题全部断代为旧代码产物或 fail-closed 设计的正确标注，当前版本无同类产生口。**
唯一遗留是冻结贡献表的 staleness 消费（第 5 项）。

| # | 问题 | 断代证据 | 当前版本判定 |
|---|---|---|---|
| 1 | restart_replay 污染复盘（126 事件 / 59 对唯一交易） | 两值化修复 `aced5bf5`（09-12 16:31）+ `27c97a6e`（09-12 17:10，删除 guess-attribution 产线）落地于 **09-12 16:31/17:10**；最后一条 restart_replay 复盘发射于 **09-12 12:43**，早于修复；`build_replayed_close_payloads` 现只产 `broker_close\|chain_broken`，全仓无 restart_replay close_reason 写入口 | 不会再产生。历史 126 条是修复期受控重启（SOP 记录 09-09~09-11 三天 20 次）带仓恢复关闭的 fail-closed 正确标注，全部被资格体系排除。注意：未来重启带仓仍会产生 `chain_broken` 类排除（设计行为，量随重启纪律，非缺陷） |
| 2 | entry 延迟 epoch 级脏值（3 review） | 脏 payload `timing_valid=null`/`timestamp_unit=null`——旧 trade_reviewer 把 bar 相对秒（3372）与 epoch（1.78e9）混算；当前 `review_contract.build_entry_timing_context` 带 unit 校验 + 时序校验，invalid 时延迟一律不制造（`timing_invalid_reasons` 留审计） | 不会再产生 |
| 3 | supervisor binding invalid 25.3%（50 条） | reason **100% = `binding_template_hash_mismatch`**——模板演化 vs 存量仓位旧 hash 的保守 `unknown/hold`，非数据损坏或写入缺口 | 属 fail-closed 正确行为；随旧仓位平仓自然消退。若 9 月后 mismatch 率仍高再立案（R1 观察） |
| 4 | supervisor_execution_trace excluded 47.5%（6,598） | 按月断代：excluded 6,598 全部在 **8 月**（历史 observation/superseded terminalize，设计口径）；9 月 only excluded 9 vs **matured 103** | 当前监督样本生产健康（9 月 matured 率 92%） |
| 5 | 贡献表 entry/hold/exit 分解从未投产 | 写入口 = 09-12 17:10 删除的 guess-attribution 产线；现仅存读取方（market_regime / factor_cards / factor_counter_evidence / api/learning）与污染注记 UPDATE | 当前零写入、表冻结。**遗留项（转 R1）**：market_regime 的因子 regime-fit 聚合与 counter_evidence 仍消费冻结数据，"当前 regime 适合度"判据部分基于已停产的归因——需在 R1 评估是否切到 `factor_attribution.v1` 口径 |

复查方法：数据侧（按月分布 / 去重 / 与修复 commit 时间线对齐）+ 代码侧（全仓 grep 产生口 + payload 合同字段断代）。

## 6. 责任域补充测量与解读修正（2026-09-13）

R0 方法第 1 项（entry/hold/exit/data_quality/parameter 的亏损责任占比）未在 §1–§5 交付；本节用 review payload
顶层字段（`entry_quality/hold_quality/exit_quality/failure_tags`，357 笔）补齐，并修正三处解读。

**质量中位（亏损 257 / 盈利 100）**：

| 口径 | 亏损 | 盈利 |
|---|---|---|
| entry_quality | 0.506 | 0.697 |
| hold_quality | 0.366 | 0.941 |
| exit_quality | **0.25（构造性地板）** | 0.95 |
| MFE>0（曾浮盈） | 222/257（86.4%） | — |
| 利润回吐标签（`profit_giveback`/`alpha_correct_but_capture_failed`） | 139（54.1%） | 12 |
| 入场侧标签（`weak_entry_loss`/`avoidable_loss`/`factor_conflict`/`overweight_noise_factor` 并集） | 181（70.4%） | — |

亏损交叉表（入场侧标签 × 退出侧标签）：仅入场 84（32.7%）、仅退出 42（16.3%）、两者 97（37.7%）、两者都无 34（13.2%）。
35 笔 `mfe<=0` 亏损的 entry_quality 中位 0.328（最低），是“从未走对”的入场失败。

**解读修正**：
1. `exit_quality = 0.25 + 0.7×capture`，亏损单 capture 恒 0 → 该字段对亏损无区分度；亏损的退出侧证据只能看
   `profit_giveback`/MFE>0，不能引用 exit_quality 分位。
2. `bad_loss`（179 笔）是入场侧标签（`review_contract.classify_4label_outcome`：`conviction≥0.55 or avoidable_entry`），
   不携带退出责任；§1“入场质量或退出质量是系统的主要亏损来源”应读作“两者都在场”（重叠 37.7%）。
3. `pnl/mfe` 捕获率双峰不作为独立证据；独立的退出侧事实是 222/257 曾浮盈与 139 笔利润回吐标签。

**监督治理链定位（供 R2/R4）**：样本门已过（55 笔 governance_eligible matured）；32 条治理合格 advisory 建议全部
`superseded`（缺 V16 bridge 证据）；`position_supervisor_template` application/effect=0；`f16024bb` 删除 medium-impact
产线后该 scope 无候选生产者；`selection.v1 candidate_count=0`。详见 `docs/legacy-debt-register.md` 监督治理条目。
