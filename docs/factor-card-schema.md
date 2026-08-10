# Factor Card Schema

> Status: active
> Last schema verification: 2026-08-10
> Factor-chain gap audit: 2026-08-10
> Scope: factor card schema for governance, attribution, frontend display, and Catalog alignment.

本文定义“因子解释卡片”的统一 schema。目标是固定治理、归因、前端展示和 Factor Catalog 共用的字段边界。

---

## 1. 为什么要有 factor card

当前系统已经能：

- 注册因子
- 记录因子 description
- 跟踪健康分、贡献分、生命周期事件
- 在复盘侧沉淀“参数可疑 / regime 不匹配 / 退出问题”

但系统还缺一个稳定对象，把这些证据汇总成“这个因子到底是谁、适合什么场景、最近出了什么问题”。

factor card 就是这个统一对象。

---

## 2. `factor_card.v1` 顶层结构

```json
{
  "schema_version": "factor_card.v1",
  "factor_id": "rsi_14",
  "display_name": "RSI(14)",
  "factor_family": "momentum_oscillator",
  "source": "builtin",
  "lifecycle_status": "ACTIVE",
  "formula_version": "registry_builtin.v1",
  "parameter_version": "default.v1",
  "parameters": {
    "length": 14
  },
  "expected_regimes": ["range", "mean_reversion"],
  "weak_regimes": ["strong_trend"],
  "expected_holding_profile": {
    "style": "short_swing",
    "min_bars": 2,
    "max_bars": 12
  },
  "failure_modes": [
    "factor_logic_ok_but_param_suspect",
    "regime_changed_during_hold"
  ],
  "governance_state": {
    "weight_state": "active",
    "template_state": "default_only",
    "review_status": "none"
  },
  "evidence_summary": {
    "description": "RSI(14)",
    "health_score": 0.71,
    "shadow_score": 0.0,
    "last_primary_responsibility": "parameter",
    "recent_responsibility_labels": ["holding_too_long"]
  },
  "updated_at": "2026-06-25T21:40:00Z"
}
```

### 2.1 2026-08-10 additive runtime and posterior fields

`factor_card.v1` 保持不变，仅追加以下对象；它们全部是现有 Catalog、运行选择、学习证据和
V16 effect 的只读投影，不产生新的因子选择或治理写入者：

```json
{
  "runtime_binding": {
    "status": "bound|stale|unavailable|unknown",
    "selection_fingerprint": null,
    "config_version": null,
    "config_hash": null,
    "live_generation_id": null,
    "role": null,
    "weight": null
  },
  "definition_lineage": {
    "generation": null,
    "definition_fingerprint": null,
    "artifact_hash": null,
    "mutation_id": null,
    "catalog_snapshot_id": null
  },
  "evidence_counts": {
    "decision_observations": 0,
    "factor_linked_trade_reviews": 0,
    "governance_eligible_mature": 0,
    "contaminated_or_ineligible": 0,
    "effects_observed": 0
  },
  "direction_contract": {
    "raw_sign": null,
    "normalized_sign": null,
    "polarity": null,
    "signed_ic": null,
    "status": "available|unavailable|unknown"
  },
  "posterior_summary": {
    "state": "confirmed|probable|inconclusive|unobservable",
    "action": "no_change|candidate|rollback|quarantine",
    "confidence": null,
    "evidence_refs": [],
    "candidate_id": null,
    "review_id": null
  }
}
```

`decision_observations` 只表示 `decision_factor_snapshot` 覆盖；成熟交易数必须同时满足既有
`LearningFeatureProvider` 的 matured、governance-eligible、未污染合同。没有 canonical signed IC
时 `direction_contract` 保持 `unavailable`，不得由 `abs(IC)` 推导方向；缺失运行投影保持
`stale/unavailable/unknown`，不以默认值掩盖。

---

## 3. 必填字段

以下字段作为 `factor_card.v1` 必填项：

- `schema_version`
- `factor_id`
- `display_name`
- `factor_family`
- `source`
- `lifecycle_status`
- `formula_version`
- `parameter_version`
- `parameters`
- `expected_regimes`
- `weak_regimes`
- `expected_holding_profile`
- `failure_modes`

其中：

- `factor_id` 必须与 `factor_registry` / `registry_adapter` 中的唯一标识一致
- `display_name` 允许先复用现有 `description`
- `factor_family` 先允许显式枚举，不要求自动推断
- `formula_version` 与 `parameter_version` 在 Phase E / E2 之前允许使用占位版本

---

## 4. 字段解释

### `factor_id`

唯一因子标识。必须稳定，不随展示文案变化。

### `factor_family`

用于把因子归到更高一层的治理分组，建议首批枚举：

- `momentum`
- `momentum_oscillator`
- `trend`
- `volatility`
- `volume`
- `pattern`
- `macro`
- `calendar`
- `cross_asset`
- `ml_signal`
- `composite`

### `source`

来源先与 `alpha/registry_adapter.py` 对齐：

- `builtin`
- `discovered`
- `shadow`
- `removed`

### `lifecycle_status`

生命周期状态先复用现有 registry adapter 语义：

- `ACTIVE`
- `DEAD`
- `UNKNOWN`

### `formula_version`

表达“因子逻辑版本”，用于区分公式结构变化。

建议：

- 内置旧因子先统一记为 `registry_builtin.v1`
- 由 DSL / 发现流程产生的因子，可用 `dsl.<family>.vN`
- 由模型注册的信号因子，可用 `ml.<model_type>.vN`

### `parameter_version`

表达“参数模板版本”，用于区分相同公式下的参数切换。

在 E2 之前，默认允许：

- `default.v1`
- `manual.v1`
- `shadow.v1`

### `parameters`

结构化参数对象。要求：

- 可 JSON 序列化
- 字段名稳定
- 不混用展示文案和数值含义

例如：

```json
{
  "length": 14,
  "upper_band": 70,
  "lower_band": 30
}
```

### `expected_regimes` / `weak_regimes`

用于表达因子理论适配区间，而不是近期绩效。

首批 regime 标签建议先与现有风控/复盘口径保持保守映射，例如：

- `trend`
- `range`
- `breakout`
- `high_vol`
- `low_vol`
- `event_risk`
- `macro_drift`

### `expected_holding_profile`

表达该因子的预期持仓形态，最少包括：

- `style`
- `min_bars`
- `max_bars`

可选扩展：

- `time_in_profit_bias`
- `giveback_tolerance`
- `preferred_exit_modes`

### `failure_modes`

这里填“常见失败模式”，不是单笔 review 的实时结论。

首批应允许复用 Phase D 已有责任标签，例如：

- `entry_good_exit_bad`
- `alpha_correct_but_capture_failed`
- `holding_too_long`
- `regime_changed_during_hold`
- `factor_logic_ok_but_param_suspect`
- `thesis_broken`
- `holding_inefficient`

---

## 5. 现有系统字段如何映射

当前已经存在的字段，可先映射到 factor card：

- `factor_registry`:
  - `factor_id`
  - `display_name`（先复用 `_factor_desc`）
- `registry_adapter._meta`:
  - `source`
  - `description`
- `registry_adapter._lifecycle_statuses`:
  - `lifecycle_status`
- `decision_factor_snapshot`:
  - `health_score`
  - `shadow_score`
  - `contribution_score`
- `factor_contribution_review`:
  - `recent_responsibility_labels`
  - `last_primary_responsibility`
- `trade_outcome_review.failure_taxonomy`:
  - `failure_modes` 的候选来源

这意味着 E1 先不用重构交易主链，只需要把“已有分散证据 -> 统一 schema”固定下来。

---

## 6. 当前依赖

`factor_card.v1` 会直接服务后续三类工作：

1. 参数模板系统
2. 因子自治治理与人工覆盖审计工作流
3. 前端 / 运维的人话解释卡片

后续如果 schema 变更，应通过 `schema_version` 升级，而不是静默改字段含义。

---

## 7. 因子链路专项核对与明确缺口

本节记录 2026-08-10 对当前代码和 `state_v1.runtime_kv` 运行投影的专项核对。
它不是新的运行时事实源，也不替代因子选择、健康度、生命周期或 V16 的现有 authority；
它的作用是把“因子能计算”与“因子已经具备可靠生产资格”明确区分开。

### 7.1 当前因子如何参与开仓方向

当前 canonical 路径是：

```text
已闭合 M5 bar
  -> StreamingFactorEngine
  -> SignalNormalizer
  -> runtime_factor_selection / factor_runtime_projection
  -> PortfolioCompositor
  -> ContextPolicy / ExecutionGate
  -> RiskPolicy 与仓位计算
  -> Execution Intent / broker
```

当前职责边界如下：

- `alpha` 因子才参与多空方向；`context`、`gate`、`sizing` 因子不直接投票。
- `PortfolioCompositor` 对有效 alpha 的归一化信号和运行时权重做加权合成。
- 合成分数达到方向阈值才生成多头或空头，否则保持 `direction=0`。
- ContextPolicy、ExecutionGate 和 RiskPolicy 可以阻止或缩小开仓，但不能把多头改成空头，
  也不负责证明某个因子本身正确。
- 当前 LightGBM 因子治理模型的职责是已有因子的治理建议、弱化或降权，不是开仓信号生成器，
  也不是新因子认证器。

2026-08-10 的运行投影中，选择集有 15 个因子，其中真正有正权重并参与方向合成的 alpha
只有 5 个：`vol_ma_ratio`、`wick_rejection`、`morning_evening_star`、
`fib_rejection_confirmation`、`pin_bar`。其余主要是 context/gate，或因权重为零而不投票；
当前发现型 DSL 因子没有进入实际选择集。这个数量是运行快照，不是永久配置，必须以
`runtime_factor_selection.v1` 为准，不能用新进程加载的默认 `RuntimeConfig` 代替。

### 7.2 已具备的能力

- 因子可以从闭合 K 线计算，并经过统一归一化、角色解析、运行时选择和组合器进入开仓门控。
- `decision_factor_snapshot` 已记录 raw value、normalized value、方向、权重、健康分、门控原因
  和贡献分，具备重建一次组合分数的基础。
- 内置因子、发现因子和生命周期状态已有选择边界；发现型因子不能通过隐式默认权重直接成为
  生产 alpha。
- 新因子存在 SHADOW、prepared/loaded acknowledgement、V16、Coordinator、Canary 和 ACTIVE
  的生命周期权力链，`auto_register` 不能直接写 ACTIVE。
- 实时因子、RiskPolicy、执行、持仓监督和后验在架构上已经分层，因子模块没有直接下单权限。

### 7.3 尚未达标的核心缺口

#### A. 因子方向语义没有形成强合同

当前缺少一个由因子卡片、注册表、归一化器和组合器共同遵守的强制合同：

```text
raw_value 的含义
  -> 正值代表什么方向
  -> 负值代表什么方向
  -> normalize 是否保持方向
  -> role 是否允许参与方向
  -> direction sign 的单元/运行时校验
```

具体风险：

- `vol_ma_ratio` 的原始含义是成交量相对均值的偏离，本身没有价格方向；当前却作为 alpha
  参与合成，容易把“放量”直接解释成看多，把量能确认误当成方向信号。
- `SignalNormalizer` 的 `zscore_tanh` 以滚动均值为中心，因子原始值的正负不必然等于归一化后
  的多空含义。历史决策快照中已经观察到部分因子存在 raw/normalized 符号变化。
- `direction` 现在更多是组合结果，而不是每个因子都经过显式方向契约验证后的结果。

因此，当前因子“数值可用”不等于“方向语义可信”。在方向契约落地前，任何自动调权都可能
放大方向反转、角色误标或归一化漂移。

#### B. 因子健康度不等于交易成熟度

- `factor_health.n_obs` 主要反映 bar/observation 数，不是完整、独立、可追溯的成熟交易数。
- 当前健康度允许 `WATCH`/`DECAYING` 因子在有基础 evidence 时继续被选择；这与“样本不足的
  因子只能 shadow、不得驱动 mutation”没有形成统一硬门。
- 现有健康评分大量使用 `abs(IC)`。这会让方向相反或已反转的因子仍可能获得较高健康分；
  例如某些近期 signed IC 为负的因子仍可显示为 `HEALTHY` 或继续拥有投票资格。
- Adaptive Weight Engine 在 signed IC 未达到 floor 时会跳过后续健康/禁用判断，弱因子可能保留
  旧权重，而不是得到明确的降权、shadow 或 quarantine 结果。

缺少的不是又一个分数，而是分层的成熟度合同：

```text
bar observations
  -> 独立交易覆盖
  -> signed IC / direction correctness
  -> walk-forward 与 regime 覆盖
  -> execution evidence 完整性
  -> 可比较的 posterior/effect
```

其中任何一层不足，都只能限制为 shadow/advisory，不能进入模型驱动 mutation。

#### C. 有贡献记录，但还没有严格的因子因果证明

`decision_factor_snapshot` 能回答“当时这个因子贡献了多少分”，但不能单独回答“这笔交易的
结果是否由这个因子造成”。当前还存在以下缺口：

- `factor_contribution_review` 是归因审计和学习输入，不是单笔因果证明；历史记录可能来自
  不同因子集、权重和运行配置，不能直接混合比较。
- 因子责任尚未稳定区分 signal、entry threshold、risk sizing、execution、supervisor、data
  contamination 和 market noise。
- 单笔盈利/亏损不能直接成为生产调权证据；必须跨独立交易、时间窗口、regime、反事实和
  application effect 验证。
- 后验需要在证据不足时明确输出 `no_change`，而不是将“有贡献”误判成“应加权”。

#### D. 决策数值可重建，但版本绑定还不完整

当前快照能重建当次分数，但交易级账本仍有以下追溯缺口：

- 部分 `decision_ledger` 的 `factor_set_version`、`policy_version` 为空。
- 决策未稳定绑定当时的 `runtime_factor_selection.v1` hash、因子 generation、artifact hash
  和加载确认投影。
- 新进程的静态默认配置与生产进程的 live overlay 不是同一事实；如果调用方读取错误入口，
  会得到“看似合理但不是当时生效”的因子权重。

因此，当前可以回放数值，但还不能对每一笔开仓严格证明“使用了哪一版因子公式、参数、角色、
权重和 runtime selection”。这会直接削弱后验对因子权重和开仓阈值的修正能力。

#### E. 因子独立性和覆盖度被高估

- directional portfolio guard 的 fallback `factor:<name>` 只能证明因子 ID 不同，不能证明
  统计独立；当前多个因子都由同一组 M5 OHLCV 派生。
- 当前没有把 active alpha 的有效交易覆盖、非零信号覆盖、方向命中、regime 覆盖和相互冗余
  作为同一个生产准入结果。
- “有 5 个 voter”不能等价为“有 5 个独立证据源”；当它们同时偏向同一错误方向时，组合分数
  反而可能更自信地错误开仓。

#### F. 因子发现与因子生产之间仍是两条不同的证据链

当前自动发现可以产出候选，但发现结果进入生产仍必须完成：

```text
多前向验证 [1, 5, 20]
  -> governance pass
  -> stable artifact lineage
  -> SHADOW
  -> prepared / loaded acknowledgement
  -> V16 single-use
  -> GovernanceMutationCoordinator
  -> Canary
  -> application effect
  -> ACTIVE
```

这条链是正确的安全边界，但目前缺少一个面向因子卡片的统一“准入证据摘要”，能够明确展示
每一关的输入、结果、失败原因和适用 generation。没有摘要时，自动注册、自动加载、自动晋级
容易在运维层被误认为同一件事。

### 7.4 因子模块的优先级缺口

| 优先级 | 必须补齐的合同 | 未补齐的直接风险 | 验收结果 |
|---|---|---|---|
| P0 | raw direction、normalization sign、role 的统一方向合同 | 多空语义反转；错误因子被自动放大 | 每个 alpha 因子有正/负方向测试，归一化不改变声明语义 |
| P0 | 独立成熟交易数与污染/执行证据硬门 | bar 样本很多但交易证据不足，仍可调权 | `<20` 成熟交易只能 shadow；污染或 execution evidence 缺失不得 mutation |
| P0 | signed IC 与 `abs(IC)` 分离 | 反向因子可能被评为健康 | 健康度、准入、降权分别报告 signed direction 与 magnitude |
| P0 | 决策绑定 factor set/config/generation/artifact | 后验无法证明当时到底用了哪版因子 | 每笔 decision 可回放完整因子版本绑定 |
| P1 | 因子级 causal posterior 与 `no_change` | 将贡献误当因果，把单笔结果变成调参 | 至少跨独立交易、窗口和 regime 后才产生候选 |
| P1 | 独立性、覆盖度、非零信号率和 regime 覆盖 | “多个 voter”被误认为多个独立证据 | readiness 同时报告 voter 数与有效独立证据数 |
| P1 | discovery-to-ACTIVE 准入摘要 | 自动发现/加载/生产晋级边界不清 | 每个 generation 可追溯每一关和失败原因 |
| P2 | formula/parameter version 退出占位版本 | 公式变更和参数变更无法做精确归因 | 任何生产因子都绑定不可变 formula/parameter artifact |

### 7.5 当前因子生产结论

当前系统已经具备因子计算、组合、开仓门控、快照和生命周期骨架；但因子还不能被描述为
“已经完成自我认证并能可靠驱动生产”的智能单元。当前最关键的缺口依次是：

1. 先固定每个因子的方向语义和角色，尤其重新审查 `vol_ma_ratio` 是否应当作为 alpha。
2. 把 bar health 与 trade maturity 分开，禁止样本不足、污染或执行证据不完整的因子驱动 mutation。
3. 修正 signed IC/反向因子的治理语义，不能用绝对 IC 掩盖方向错误。
4. 给每笔决策绑定实际 runtime selection、generation 和 artifact，保证后验能复现当时输入。
5. 将因子贡献、交易结果、监督结果和 application effect 串成因子级后验；证据不足时必须
   `no_change`，不能自动猜测权重、阈值或生命周期。

在以上 P0 缺口完成并通过真实独立交易验证前，因子模型和自动发现结果只能处于
`shadow/advisory/canary` 范围，不能被视为生产级自动成长闭环。

---

## 8. 多源因子发现与外部实践借鉴

### 8.1 当前发现能力的真实边界

当前项目并非完全只有 GP 和硬编码因子，已经存在以下研究能力：

- 内置注册因子：提供稳定、可解释的人类先验。
- GP/Random DSL 搜索：在有限的叶子和算子空间内生成候选公式。
- 数学特征衍生、PCA 和 IC/相关性/VIF 筛选：形成特征工程候选，但当前 PCA 结果主要注册为
  `SHADOW`，尚未等价于生产 alpha。
- 因子健康、LightGBM 治理和生命周期：负责已有因子的评估与受控晋级，不是新的发现器。

当前仍有四个结构性限制：

1. GP 只能在 DSL 白名单的变量和算子内搜索；搜索空间没有信息，GP 就不可能发现该信息。
2. 候选评分主要依赖同一批历史 bar 的 `abs(IC)`、稳定性和衰减，尚未把成本、独立交易、
   execution evidence、持仓结果和 application effect 作为统一搜索目标。
3. 多前向 `[1, 5, 20]` 验证发生在初步搜索之后，不能替代候选生成阶段的 walk-forward、
   purged/embargoed 验证和真实 holdout。
4. 代码中已有 `EliteArchive`、`MAP-Elites` 和 novelty 入口，但默认 GP wrapper 没有传入 archive
   或 novelty grid；这些能力当前不是默认生产研究路径。

因此，GP 应被定义为“可解释公式发现通道”，而不能被定义为完整的因子智能。

### 8.2 建议采用的多源发现通道

```text
人类基准因子
      ├─ 受控特征工厂：rolling / multi-timeframe / external / pattern
      ├─ GP / symbolic regression：发现可解释公式
      ├─ 监督模型：发现非线性组合与 meta-label
      ├─ regime / clustering：发现适用市场状态与冗余关系
      └─ 研究资料代理：提出假设、公式和实验计划
                         ↓
                  统一 Candidate Card
                         ↓
          PIT + walk-forward + cost + trade evidence
                         ↓
              SHADOW → V16 → Canary → effect
```

各通道的职责必须分开：

| 通道 | 负责发现什么 | 生产边界 |
|---|---|---|
| Builtin prior | 已知的可解释基础信号 | 可作为基线，但仍需健康和版本追踪 |
| Feature factory | 新变量、变换、窗口、跨周期和外部信息 | 先进入候选/SHADOW，不能批量激活 |
| GP / PySR / DEAP | 新公式、新组合和非线性表达式 | 只生成公式候选，不直接注册 ACTIVE |
| Supervised model | 非线性组合、方向置信度、`take/skip` 和 size bias | 优先作为 meta-label/advisory，不能直接翻转方向 |
| Regime / clustering | 因子适用环境、冗余分组和多样性 | 用于准入、组合和权重约束 |
| RD-Agent 类研究代理 | 从论文、报告和历史实验提出假设 | 只能产生带证据的 proposal，不能成为生产 writer |
| Online learning / bandit | 成熟因子的轻量权重适配和漂移检测 | 仅在成熟交易证据充足后进入 shadow/canary |

### 8.3 GitHub 实践的可借鉴内容

- [Microsoft Qlib](https://github.com/microsoft/qlib)：借鉴 Alpha158/Alpha360 式因子库、
  Point-in-Time 数据、可组合的数据处理器，以及从训练、回测到分析的统一实验工作流。
  不直接引入 Qlib 运行时；当前项目已有自己的 bar、PostgreSQL、执行和治理事实源。
- [Microsoft RD-Agent](https://github.com/microsoft/RD-Agent)：借鉴“提出假设 → 实现 → 实验 →
  根据反馈迭代”的研究闭环，将其作为 V16 的研究候选生成能力，而不是新的生产大脑或 writer。
- [PySR](https://github.com/MilesCranmer/PySR)、[gplearn](https://github.com/trevorstephens/gplearn)、
  [DEAP](https://github.com/DEAP/deap)：借鉴符号回归、多目标 Pareto、复杂度惩罚、强类型树、
  并行评估和 checkpoint。当前自有 DSL 的安全沙箱应保留，不应为了替换 GP 引入第二套生产 DSL。
- [tsfresh](https://github.com/blue-yonder/tsfresh)：借鉴系统化时间序列特征生成和多重检验筛选。
  必须加上当前项目的 PIT、特征预算、污染排除和 factor card 版本，不能把几百个特征直接送入 live。
- [FreqAI feature engineering](https://github.com/freqtrade/freqtrade/blob/develop/docs/freqai-feature-engineering.md)：
  借鉴按周期、滞后、相关品种和外部时间框架受控展开特征，以及显式 label/预测可信度输出。
- [Hudson & Thames meta-labeling](https://github.com/hudson-and-thames/meta-labeling)：这是当前最贴合
  项目的方向。基础因子保留多空方向，第二模型只判断是否执行、置信度和仓位偏置，减少错误信号，
  不让模型直接改变方向。
- [River](https://github.com/online-ml/river) 或 [Vowpal Wabbit](https://github.com/VowpalWabbit/vowpal_wabbit)：
  可作为后续在线学习和 contextual bandit 的研究参考，但必须等真实成熟交易样本和 effect 证据足够。
- [mlfinlab](https://github.com/hudson-and-thames/mlfinlab)：可借鉴 purged/embargoed CV、样本权重、
  特征重要性和反过拟合思想；其仓库的代码许可不能直接复制，当前项目应自行实现并绑定已有 PIT/回放合同。

### 8.4 建议实施顺序

#### P0：先扩大候选质量，不增加生产 authority

1. 保留内置因子作为基线，将现有 `FeatureDeriver`、GP、PCA 和监督模型都输出为统一
   `Candidate Card`，记录 source、formula、parameter、label、PIT lineage、复杂度和适用 regime。
2. 将 GP 评分改为多目标结果：signed IC、稳定性、regime 覆盖、交易成本、换手率、复杂度、
   与现有因子相关性、执行证据完整性和真实交易效果分别报告，避免单一总分掩盖方向错误。
3. 把现有入口质量模型优先发展为 meta-label：输出 `take/skip/confidence/size_bias`，不翻转
   因子方向，不绕过 RiskPolicy。
4. 让 GP 的 archive/novelty 能力真正参与离线搜索，以复杂度、持仓周期、波动环境、相关性和
   regime 作为多样性维度。

#### P1：建立多样性和研究闭环

1. 增加 regime 条件下的因子覆盖、冗余聚类和独立性报告。
2. 以 PySR 或 DEAP 建立离线对照实验，验证当前 GP 是否真的优于其他公式搜索方法。
3. 将 RD-Agent 类能力限制为 V16 的研究 proposal：只能生成候选、实验计划和解释，不能直接
   改 Registry、RuntimeConfig、权重或 ACTIVE 状态。

#### P2：成熟后再做在线适配

只有在真实独立交易、完整执行证据、effect 和 posterior 达到既有门槛后，才评估 River 或
contextual bandit。在线方法首先只允许调整已有因子权重或置信度，不允许自动创造新的生产因子。

### 8.5 明确不采用的方向

- 不直接引入强化学习替代因子发现；它更接近策略/仓位代理，不能解决当前因子语义、版本和因果证据问题。
- 不把 LLM 生成的公式直接写入 Registry 或 ACTIVE。
- 不把 tsfresh/PCA 的大批量特征直接投入生产。
- 不立即用 PySR 替换现有 GP；先做同一 PIT 数据、同一 holdout、同一成本合同下的对照实验。
- 不新增 model-to-production writer、平行 registry、平行表或绕过 V16/Review/Coordinator/Canary 的通道。

### 8.6 结论

因子智能化的正确方向不是“寻找一个更强的 GP”，而是：

> GP 负责公式发现；特征工厂负责扩大信息空间；监督模型负责非线性组合和元标签；regime 模块负责适用环境；V16 负责统一审查和晋级。

所有候选最终仍必须回到现有因子注册、SHADOW、V16、GovernanceMutationCoordinator、Canary
和 application effect 链路。只有这样，因子发现能力增加后，系统的可解释性、可回滚性和生产安全边界才不会下降。

## 9. 因子治理引擎现状评估（2026-08-10）

### 9.1 总体判断

当前因子治理引擎已经具备较完整的安全治理边界，但还不是能够可靠自我成长的智能因子大脑。
它目前更像一个“防止低质量证据进入生产的治理壳”：能够采集证据、过滤样本、训练 shadow
模型、生成治理候选并交给 V16 和现有 mutation 链路；但还不能稳定回答“某个因子为什么失败、
它是否产生了增量贡献、下一轮应该怎样调整因子权重、开仓阈值或持仓策略”。

因此，当前主要矛盾不是 LightGBM 或 GP 的算法数量不够，而是因子语义、成熟交易样本、
因果归因和 application effect 反馈还没有形成稳定闭环。

### 9.2 当前运行事实

以下数据来自 2026-08-10 的 `state_v1.runtime_kv[backend_readiness_snapshot.v1]` 和
`runtime_factor_selection.v1`，属于运行快照，不是永久事实；每次发布或准入判断前必须重新查询。

当前 `factor_governance_lightgbm` 的质量门槛未通过：

| 指标 | 当前值 | 要求 | 结论 |
|---|---:|---:|---|
| 独立交易数 | 177 | ≥300 | 不通过 |
| 留出交易数 | 44 | ≥60 | 不通过 |
| balanced accuracy | 0.519 | ≥0.60 | 不通过 |
| AUC | 0.524 | ≥0.65 | 不通过 |
| majority lift | -0.037 | ≥0.03 | 不通过 |
| 训练/留出差距 | 0.312 | ≤0.15 | 不通过 |

训练集 AUC 为 0.959，而留出集 AUC 只有 0.524，表现为明显过拟合。当前模型仍处于
`shadow`，不能驱动生产 mutation，这是正确的 fail-closed 结果。

训练样本虽然有 2841 行，但只有 177 笔独立交易；留出部分有 563 行，却只有 44 笔独立
留出交易。样本行数不能替代独立成熟交易数。当前训练数据还显示有 15 个因子样本少于 20，
这些因子只能保持 shadow，不能生成模型驱动 mutation。

当前运行因子选择中有 6 个 alpha voter：
`fib_rejection_confirmation`、`morning_evening_star`、`obv_slope`、`pin_bar`、
`vol_ma_ratio`、`wick_rejection`。因子健康投影将这 6 个因子全部标为弱健康，其中
`fib_rejection_confirmation` 为 `DECAYING`，其余为 `WATCH`；它们当前的 signed IC 均为负值。
这不能直接证明因子一定错误，但说明系统还没有把“因子原始含义、归一化方向、反向使用是否有意、
最终 long/short 角色”固化为不可歧义的合同。

此外，当前运行投影显示 `learned threshold=0`、`effective_sample_count=0`、
`live_generation_id` 为空，模型影响阶段仍为 `shadow`。治理 effect 投影中还存在
`orphaned`、`inconclusive`、`approved_waiting_application` 等未完成状态。因此，系统已经
能够记录治理动作，但还不能把“建议产生”或“suggestion applied”解释成“生产效果已确认”。

### 9.3 已经成立的能力

1. 因子快照、决策记录、交易执行证据、平仓复盘和因子贡献审查已经形成基本链路。
2. 训练入口会检查 PIT lineage、execution evidence、污染标记和 generation 一致性，旧代数据
   不直接混入当前训练代。
3. [research/factor_governance_lightgbm.py](../research/factor_governance_lightgbm.py) 中的模型
   只能写 shadow audit 或治理候选，不能直接写 ACTIVE、直接改 RuntimeConfig 或提交订单。
4. 模型候选仍需回到 V16、candidate review、RiskPolicy、GovernanceMutationCoordinator、
   Canary 和 application effect，生产 authority 没有被模型旁路。
5. 当前治理引擎能够在模型不合格时阻止 mutation，安全性明显高于自动放行。

### 9.4 核心缺口

#### 9.4.1 治理对象还不是严格的因子质量

当前模型标签主要是 `next_same_factor_outcome_from_rolling_history`，它回答的是“下一次
相似因子结果是否较好”，不等于“本次交易中该因子是否产生了增量贡献”。

因此模型可能学到市场状态、时段、策略模板或其他共同特征，而不是因子本身的因果作用。下一步
必须把因子贡献拆成：

```text
因子是否被使用
→ 是否影响了组合信号
→ 是否影响了最终开仓决策
→ 是否有完整执行证据
→ 在反事实/对照下是否产生增量结果
```

#### 9.4.2 bar 观测与成熟交易证据混用

健康模块中的 `n_obs` 主要是 K 线观测数，不是成熟交易数。2000 个 bar 不能等价于 2000
次可归因交易，容易造成因子“样本充足”的错觉。

#### 9.4.3 signed direction 不是一等公民

健康评分偏重 `abs(IC)`，可能把方向相反但尚未确认是否应反向使用的因子视为健康。与此同时，
负 signed IC 的因子仍可能继续成为 alpha voter。因子必须明确记录：

```text
raw meaning
→ normalized meaning
→ polarity
→ long/short role
→ runtime contribution
```

#### 9.4.4 独立性判断过于粗糙

当前 `directional_portfolio_guard` 以 `factor:<factor_id>` 作为独立组。不同名称并不代表不同
信息源；形态、影线、反转因子可能是同一统计家族的重复表达。当前运行投影还显示反转类因子暴露
约占 65.7%，说明需要真正的相关性、残差相关性、共同失败区间和 regime 独立性分析。

#### 9.4.5 后验反馈尚未稳定回到训练和生产

当前治理历史中存在大量 superseded、blocked、mutation_failed、orphaned 和 inconclusive
状态。它们可以作为审计事实，但不能直接计为“模型已经学会”。

必须区分：

```text
建议生成 ≠ 候选通过
候选通过 ≠ mutation 提交
mutation 提交 ≠ application 生效
application 生效 ≠ effect 为正
effect 为正 ≠ 已足够支持下一次扩张
```

#### 9.4.6 当前动作空间仍偏单一

模型主要面向因子 `downweight` 候选，权重调整幅度也受到固定边界限制。它还不能可靠区分：

- 全局弱化；
- 只在某个 regime 下禁用；
- 方向反转候选；
- 只降低开仓置信度；
- 只调整持仓监督模板；
- 保持现状并等待更多证据。

动作空间不能在证据不足时直接放大，但应由 V16 统一裁决，而不是让因子模型单独承担全部策略治理。

### 9.5 对 V16 的职责划分

因子治理模型不应替代 V16。更合理的职责分工是：

| 模块 | 职责 |
|---|---|
| 因子治理引擎 | 计算因子证据、健康、方向、regime、冗余和后验候选 |
| LightGBM | 在全局质量门槛通过后，提供因子弱化或治理建议 |
| V16 | 统一比较候选、反证、风险和影响范围，决定是否接受建议 |
| Coordinator | 保证单命令、单 mutation、原子提交和审计绑定 |
| Canary/application effect | 观察实际生效效果，确认是否能进入下一阶段 |

因子治理引擎的输出应是带完整引用的 evidence/candidate，而不是直接写权重或直接修改生产状态。

### 9.6 优先级

#### P0：先修正证据和语义

1. 固化因子方向、角色、归一化和 runtime contribution 合同。
2. 将成熟交易作为治理主证据，明确区分 bar observation、decision sample 和 mature trade。
3. 重做因子贡献标签，至少记录使用、影响、执行、结果和反事实/对照证据。
4. 每个 decision 绑定 factor set、policy、generation、artifact 和 runtime selection fingerprint。
5. effect 缺失、orphaned、superseded 或 rollback 的记录不得计入成熟训练样本。

#### P1：提高治理解释和组合质量

1. 增加 signed IC、regime 分层和因子冗余/独立性报告。
2. 引入 meta-label，让模型先判断 `take/skip/confidence/size_bias`，而不是直接翻转因子方向。
3. 为每个候选提供 keep、downweight、quarantine、no_change 的对照证据。
4. 将 `model quality gate`、sample lineage、review、V16 command、mutation 和 effect 绑定为一条可追溯链。

#### P2：证据成熟后再做在线适配

只有在真实独立交易、完整执行证据和 application effect 足够稳定后，才评估 online learning
或 contextual bandit。在线方法首先只能调整已有因子的置信度或权重，不能直接创造生产因子、改方向、
绕过 V16 或改风险限额。

### 9.7 结论

当前因子治理引擎擅长阻止错误进入生产，但还不擅长解释错误为什么发生，以及下一次应该如何改。
串行治理链路本身不是不能自我成长；真正的瓶颈是成熟样本不足、因果标签不足、方向语义不清和
effect 尚未稳定回流。当前保持模型 shadow 是正确状态，后续应先补齐证据闭环，再扩大模型和自动化能力。
