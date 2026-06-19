# Factor Takeover · 机构级自适应 Alpha Factory v4

> 版本：v4.0  
> 目标：以因子系统彻底取代策略系统，建立「因子计算 → 连续信号 → 组合优化 → 执行 → 归因 → 自适应」全闭环。  
> 核心原则：**不预测市场状态，只跟踪因子真实表现。所有模型参数均可被自适应引擎覆盖。**  
> 基准：除卫星/高频行情等大资金独享数据外，信号质量、风控体系、归因方法看齐机构实践。  
> 日期：2026-06-13  

---

## 目录

1. [设计哲学](#1-设计哲学)
2. [现状诊断](#2-现状诊断)
3. [目标架构](#3-目标架构)
4. [Phase 1 — StreamingFactorEngine](#4-phase-1--streamingfactorengine)
5. [Phase 2 — SignalNormalizer 三域归一](#5-phase-2--signalnormalizer-三域归一)
6. [Phase 3 — PortfolioCompositor 分层组合](#6-phase-3--portfoliocompositor-分层组合)
7. [Phase 4 — Live loop 改造](#7-phase-4--live-loop-改造)
8. [Phase 5 — AttributionEngine 归因引擎](#8-phase-5--attributionengine-归因引擎)
9. [Phase 6 — AdaptiveWeightEngine 权重自适应](#9-phase-6--adaptiveweightengine-权重自适应)
10. [风险控制体系](#10-风险控制体系)
11. [GP 因子 AST 分类器](#11-gp-因子-ast-分类器)
12. [RuntimeConfig 配置段](#12-runtimeconfig-配置段)
13. [数据持久化](#13-数据持久化)
14. [退役与回退](#14-退役与回退)
15. [因子分类大全与初始配置](#15-因子分类大全与初始配置)
16. [关键公式汇总](#16-关键公式汇总)
17. [执行路线图](#17-执行路线图)

---

## 1. 设计哲学

### 1.1 不做策略，做 Alpha 组合

```
❌ 传统：
   if RSI < 30 and MACD > 0 → BUY    （硬编码规则，无法自适应）

✅ 机构级：
   因子信号连续化 → 组合权重自适应 → 归因反馈 → 权重再调整
   （全链路闭环，市场变化时权重自动迁移，不需要写 if-else）
```

### 1.2 不预测市场

```
❌ "当前是趋势市，给趋势因子加权"  → 隐含市场预测，精度跟预测价格一样差

✅ 因子 A 最近 50 笔 IR=1.2  → 权重自然上升
   因子 B 最近 50 笔 IR=-0.3 → 权重自然下降
   不需要知道"市场是什么市"，因子收益已包含市场信息
```

### 1.3 PnL 是第一性反馈，IC 是第二性验证

| 信号 | 优点 | 缺点 |
|------|------|------|
| 实盘归因 PnL | 真金白银，最高信度 | 样本慢（M15 日均 2-4 笔），噪声大 |
| 滚动 IC | 统计量成熟，可做假设检验 | 是相关性而非因果，可能滞后 |
| 因子健康分 | 5 维综合，可检测衰减 | 计算密集，不同维度可能冲突 |

**组合方式**：PnL IR 为主权重（70%），IC 健康分为下限约束（IR 再好，IC < 0.02 的因子不参与），因子健康分 < 40 直接禁用。

### 1.4 连续优于离散

v2 的 +1/0/-1 投票把 RSI=51 和 RSI=85 视为同等信号，信息损失严重。v4 全部使用连续信号 domain ∈ [-1, +1]。

---

## 2. 现状诊断

### 2.1 两条平行管

```
alpha/ (因子系统)                 strategies/ (策略)
┌────────────────────┐           ┌──────────────────────────┐
│ 39 个注册因子       │           │ multi_factor_m15         │
│ GP → SHADOW → ACTIVE│           │ 手写 RSI/DI/Stoch/MACD   │
│ IC 跟踪 → 健康分   │           │ 手写 BB/ATR              │
│ 因子生命周期        │           │ 硬编码投票 + 过滤        │
└────────────────────┘           └──────────────────────────┘
       ↓                                ↓
 只写 lifecycle_log              _process_tick → 实盘
 没人读                          这才是真决策路径
```

### 2.2 三个核心背离

1. **39 个因子只用 3 个** — strategy 只用 di_spread/rsi_14/stoch_k，宏观因子全部闲置
2. **信号离散化** — +1/0/-1 投票把 RSI=55 和 RSI=85 视为等价
3. **无反馈闭环** — 因子信号 → 开仓 → 平仓，中间没有归因，因子权重永远不调整

### 2.3 代码复用清单

| 现有模块 | 复用方式 | 改动 |
|----------|---------|------|
| `alpha/registry.py` | 直接复用，39 个因子函数不改 | 0 |
| `alpha/ic_tracker.py` | 保留作健康分下限检查 | 0 |
| `alpha/factor_health.py` | 保留 death/watch/decaying 判断，5 维健康分接入 AWE | 0 |
| `alpha/evaluation/attribution.py` | **核心复用**：Gram-Schmidt 正交归因，替代线性 MC 近似 | 接入 |
| `alpha/evaluation/bootstrap_ci.py` | **核心复用**：Bootstrap CI for Sharpe / IC，给 AWE 和归因提供置信区间 | 接入 |
| `alpha/evaluation/causal_check.py` | **核心复用**：正交性检验 + 衰减率 + cause_vs_corr_score，替代纯 IC 门限退役 | 接入 |
| `alpha/evaluation/purged_walkforward.py` | **核心复用**：López de Prado 清除交叉验证，给离线回测用 | 接入 |
| `alpha/calibration.py` | **核心复用**：DSR (Deflated Sharpe Ratio) + Bonferroni/Holm 校正，给 GP 因子评分和退役用 | 接入 |
| `alpha/probability_calibrator.py` | 核心复用：Platt 缩放 + 桶级校准，给宏因子概率化信号用 | 接入 |
| `alpha/search/blend_search.py` | **核心复用**：SLSQP Sharpe 优化 + IC 加权混合，作为 AWE 的离线基准 | 接入 |
| `alpha/search/map_elites.py` | 核心复用：质量-多样性搜索 3D grid，给 GP 因子发现用 | 0 |
| `execution/_sharpe.py` | **核心复用**：Log returns + Newey-West HAC Sharpe，替代 AWE 中的简单 IR | 接入 |
| `execution/market_impact.py` | 核心复用：Almgren-Chriss 最优执行，给仓位计算用 | 接入 |
| `execution/slippage.py` | 核心复用：动态滑点模型（ATR + 事件 + 流动性），给 SL/TP 用 | 接入 |
| `config/runtime_config.py` | 扩展：新增 signal/portfolio/awe 配置段 | 扩展 |
| `backend/services/live_service.py` | 改造 _process_tick，移除 strategy 依赖 | 重构 |
| `strategies/multi_factor_m15.py` | 保留文件，标记 deprecated | 不改 |
| `execution/ctrader_bridge.py` | 直接复用 | 0 |
| `risk/circuit.py` | 直接复用 | 0 |
| `risk/regime.py` | 直接复用 8 标签 regime 分类 | 0 |
| `execution/router.py` | 复用 OMS + PreTrade + CircuitBreaker | 接入 |

### 2.4 现有数学模型清单（已在代码库中实现，未接入实盘）

框架中已有 15+ 机构级数学模型，但当前实盘决策路径（`multi_factor_m15.py` → `_process_tick`）完全不用这些模型。v4 的核心工作不是"从头写新模型"，而是"把现有模型接入实盘闭环"。

| 模型 | 文件 | 数学核心 | 机构级？ | 当前状态 |
|------|------|---------|---------|---------|
| **Gram-Schmidt 正交归因** | `alpha/evaluation/attribution.py` | 逐因子正交化 → marginal R²，解决因子相关导致的信用分配歧义 | 是 | 离线，未接入 |
| **Newey-West HAC Sharpe** | `execution/_sharpe.py` | log returns + Bartlett kernel HAC 方差，自动 lag=`⌊4(T/100)^(2/9)⌋`，校正自相关偏倚 | 是 | 离线，未接入 |
| **DSR 多重检验校正** | `alpha/calibration.py` | Deflated Sharpe Ratio (Bailey & López de Prado 2014)，偏度+峰度+自相关+E[max|SR|H0]，Bonferroni/Holm 阶梯校正 | 是 | 离线，未接入 |
| **Bootstrap CI** | `alpha/evaluation/bootstrap_ci.py` | 非参数 bootstrap 1000+ resamples，给 Sharpe / IC / Mean 出置信区间 | 是 | 离线，未接入 |
| **CausalCheck** | `alpha/evaluation/causal_check.py` | 正交性检验 (lagged OLS residuals→Pearson p-value) + 衰减率 (early vs late correlation) + `tanh((1-p)×3 - d×2 + r×2)` | 是 | 离线，未接入 |
| **Purged Walk-Forward** | `alpha/evaluation/purged_walkforward.py` | López de Prado 2018 清除交叉验证 (purge + embargo) | 是 | 离线，未接入 |
| **SLSQP Sharpe 优化** | `alpha/search/blend_search.py` | scipy.optimize SLSQP，约束 sum(w)=1, w_i∈[0,0.5]，最大化组合 Sharpe | 是 | 离线，未接入 |
| **IC 加权混合** | `alpha/search/blend_search.py` | 权重 ∝ |IC|，归一化后作为 equal_weight 基准 | 是 | 离线，未接入 |
| **MAP-Elites 搜索** | `alpha/search/map_elites.py` | 3D grid (depth × abs_ic × vol_bucket)，质量-多样性搜索 | 是 | 离线，未接入 |
| **Platt 缩放概率校准** | `alpha/probability_calibrator.py` | 2 参数 logistic (logit transform) + 6 bin isotonic-lite，scipy.optimize.minimize 拟合交叉熵 | 是 | 离线，未接入 |
| **Almgren-Chriss 最优执行** | `execution/market_impact.py` | 临时冲击 η×(Q/V) + 永久冲击 γ×(Q/V)，最优调度 sinh(κ(T-t))/sinh(κT)×Q | 是 | 离线，未接入 |
| **动态滑点模型** | `execution/slippage.py` | base_ticks + ATR×mult + 事件放大 + 低流动性放大 + 硬上限 | 是 | 离线，未接入 |
| **5 维因子健康分** | `alpha/factor_health.py` | mean_abs_ic(40%) + ic_stability(20%) + regime_consistency(20%) + decay_rate(10%) + independence(10%) | 是 | 接入 AWE 门控 |
| **IC 滚动追踪** | `alpha/ic_tracker.py` | Pearson corrcoef 滚动 IC，window=500 | 是 | 部分接入 |
| **Regime 8 标签分类** | `risk/regime.py` | EMA50/200 交叉 + ADX + ATR 百分位 + BB Width + GVZ/VIX + DXY + 事件 | 部分 | 接入宏因子 |

**关键 gap**：以上 15 个模型在代码库中已经实现并测试，但实盘路径 (`multi_factor_m15.py` → `_process_tick`) 用的是 3 因子 +1/0/-1 投票 + 固定阈值 50 + 无归因 + 无权重调整。v4 中凡是标注"**核心复用**"的模型必须是"接线"工作而非"重写"工作。

---

## 3. 目标架构

### 3.1 完整数据流

```
每根新 M15 bar
    │
    ▼
StreamingFactorEngine.append_bar(bar)
    │  输出: {rsi_14: 72.3, di_spread: 12.5, cot_extreme_signal: 1, ...}
    │  (含所有 39+ 注册因子，含 GP 动态因子)
    ▼
SignalNormalizer.normalize(factor_values)
    │  三域归一 → continuous ∈ [-1, +1]
    │  absolute  → tanh(zscore(rolling))
    │  percentile → rank mapping
    │  discrete  → value_map
    │  输出: {rsi_14: +0.76, di_spread: +0.58, cot_extreme_signal: +1.0, ...}
    ▼
PortfolioCompositor.compose(signals)
    │  tactical_signal = Σ(w_i × s_i) / Σ|w_i|   (技术+量价+形态+GP)
    │  macro_bias      = Σ(w_j × s_j) / Σ|w_j|   (COT+央行+利率+美元+持仓)
    │  combined_signal = α × tactical + (1-α) × macro
    │                    α 初始 0.7，AWE 可调整
    │  输出: CompositeSignal(direction, score, tactical, macro,
    │                        signals, tags_breakdown, ...)
    ▼
ExecutionGate.filter(signal, factor_values, bar)
    │  ├─ SignalGate: |score| ≥ threshold (default 0.4)
    │  ├─ VolatilityGate: bb_width percentile < max (default 90th)
    │  ├─ MACD_Reverse: 反向过滤器 (保留)
    │  └─ EventGate: NFP skip / FOMC boost / GVZ drop skip
    ▼
Signal → Risk → OMS → Broker
    │
    ├─ 开仓时: attribution[position_id] = {signal, all factor signals, weights, ATR, ...}
    │
    ▼ (平仓时)
AttributionEngine.record_close(position_id, close_price, close_ts)
    │  每个因子: marginal_contribution_i ≈ signal_i × trade_pnl_scaled
    │  更新滚动窗口
    ▼
AdaptiveWeightEngine.adapt()
    │  IR(50/100/250) → composite_score → weight ← exp(k × score) × anchor_decay
    │  多样性约束 + IC 下限 + 健康分下限
    ▼
RuntimeConfig.hot_patch(weights) → 下一 tick 立刻生效
```

### 3.2 模块全景

| 模块 | 文件 | 操作 | 职责 |
|------|------|------|------|
| StreamingFactorEngine | `alpha/streaming_factor_engine.py` | **新建** | 流式因子计算，增量缓存 |
| SignalNormalizer | `alpha/signal_normalizer.py` | **新建** | 三域归一 → [-1, +1] 连续信号 |
| PortfolioCompositor | `alpha/portfolio_compositor.py` | **新建** | 分层组合 + 信号聚合 |
| ExecutionGate | `alpha/execution_gate.py` | **新建** | 开仓闸门（波动率/事件/MACD 反向） |
| AttributionEngine | `alpha/attribution_engine.py` | **新建包装层** | 实盘归因，复用 `evaluation/attribution.py` Gram-Schmidt 正交归因，回退到线性 MC |
| AdaptiveWeightEngine | `alpha/adaptive_weight_engine.py` | **新建** | Newey-West Sharpe IR 三层窗口 + 锚点回归 + CausalCheck 退役 + DSR 多重检验 |
| GPClassifier | `alpha/gp_classifier.py` | **新建** | AST → 类型标签 |
| RuntimeConfig | `config/runtime_config.py` | **扩展** | 新增 signal/portfolio/awe 配置段 |
| LiveService | `backend/services/live_service.py` | **重构** | 移除 strategy，接入新引擎 |
| FactorHealth | `alpha/factor_health.py` | **保留复用** | 5 维健康分作 AWE 门控约束 |
| ICTracker | `alpha/ic_tracker.py` | **保留复用** | IC 作下限检查 |
| CircuitBreaker | `risk/circuit.py` | **保留** | 日亏损/连亏/滑点/波动率熔断 |
| Gram-Schmidt 归因 | `alpha/evaluation/attribution.py` | **复用** | Phase 5 接入，替代线性 MC 近似 |
| BootstrapCI | `alpha/evaluation/bootstrap_ci.py` | **复用** | Phase 5 接入，给 AWE IR 出置信区间 |
| CausalCheck | `alpha/evaluation/causal_check.py` | **复用** | Phase 6 接入，替代纯 IC 门限退役 |
| DSR / Holm 校正 | `alpha/calibration.py` | **复用** | Phase 6 接入，GP 因子退役前多重检验 |
| BlendSearch | `alpha/search/blend_search.py` | **复用** | 离线 Sharpe 优化基准，给 AWE 权重做先验 |
| Newey-West Sharpe | `execution/_sharpe.py` | **复用** | 替代 AWE 中的简单 mean/std IR |

---

## 4. Phase 1 — StreamingFactorEngine

### 4.1 设计

取代现有 `alpha/factor_engine.py` 的 batch 模式，改为流式增量计算。

```python
class StreamingFactorEngine:
    """流式因子计算引擎。
    
    维护滚动 bar 缓存，每 append 一根 bar 就重算所有因子。
    支持增量计算：EMA/mean 类因子只递推，全量因子按需重算。
    """

    def __init__(self, max_buffer: int = 200):
        self._buffer: deque[dict] = deque(maxlen=max_buffer)
        self._factor_cache: dict[str, float | None] = {}
        self._available_factors: list[str] = factor_registry.list()
        # 增量因子状态（EMA/mean 类型只需保留上一个值）
        self._incremental_state: dict[str, float] = {}
        self._warm: bool = False  # buffer 是否满足最小 bar 数

    def append_bar(self, bar: dict) -> dict[str, float | None]:
        """追加一根 bar，重算所有因子，返回 {name: value}。
        
        单个因子失败 → 该因子返回 None，不影响其他因子。
        buffer 不足 → 返回空 dict（外部应等待 warmup）。
        """
        self._buffer.append(bar)
        if len(self._buffer) < self._min_bars:
            return {}

        self._warm = True
        # 全量计算（Phase 1 简化版，后续优化增量）
        df = pd.DataFrame(list(self._buffer))
        for name in self._available_factors:
            try:
                fn = factor_registry.get(name)
                if fn is None:
                    self._factor_cache[name] = None
                    continue
                series = fn(df)
                val = float(series[-1])
                if math.isnan(val) or math.isinf(val):
                    self._factor_cache[name] = None
                else:
                    self._factor_cache[name] = val
            except Exception as e:
                logger.warning(f"Factor {name} calculation failed: {e}")
                self._factor_cache[name] = None

        return dict(self._factor_cache)

    def get_snapshot(self) -> dict[str, float | None]:
        """返回最近一次计算的因子值快照。"""
        return dict(self._factor_cache)

    @property
    def is_warm(self) -> bool:
        return self._warm

    @property
    def buffer_size(self) -> int:
        return len(self._buffer)

    # ── 动态因子支持 ──
    def refresh_factor_list(self):
        """重新扫描 factor_registry，发现新增因子（GP 动态注册）。"""
        new_factors = set(factor_registry.list()) - set(self._available_factors)
        if new_factors:
            logger.info(f"StreamingFactorEngine: discovered new factors: {new_factors}")
            self._available_factors = factor_registry.list()

    def _min_bars(self) -> int:
        """所有因子所需最小 bar 数（取 max）。"""
        return 50  # rank 窗口 50 需要 50 根 bar

    def reset(self):
        """清空缓冲区（策略切换/重启时）。"""
        self._buffer.clear()
        self._factor_cache.clear()
        self._incremental_state.clear()
        self._warm = False
```

### 4.2 边界情况

| 情况 | 行为 |
|------|------|
| buffer 不足 (< 50 bars) | `is_warm = False`，`append_bar` 返回空 dict |
| 单个因子异常 | 该因子置 None，log 警告，其他因子正常 |
| GP 新因子运行时注册 | 下次 `append_bar` 前 `refresh_factor_list()` 自动发现 |
| 因子被删除/重命名 | `get_snapshot()` 返回旧缓存，`append_bar` 更新后消失 |
| NaN / Inf | 显式检查，置 None |

### 4.3 性能优化路线图

Phase 1 使用全量 DataFrame 重算（39 因子 × 200 bars ≈ 5ms，M15 毫无压力）。

后续优化路径（按需实现）：

| 因子类型 | 增量方案 | 收益 |
|----------|---------|------|
| EMA 类 (rsi, ema_slope, macd) | 只保留上一个 EMA 值 | 不需 DataFrame |
| Mean/Std 类 (bb_width, keltner_width) | rolling 对象 + append | O(1) 更新 |
| Cross-asset (dxy_corr, slv_gld) | 缓存外层列 + 递推相关系数 | 省去全列计算 |
| Discrete (engulfing, pin_bar) | 只需最近 2-3 根 bar | O(1) |

---

## 5. Phase 2 — SignalNormalizer 三域归一

### 5.1 设计哲学

所有因子无论原始值域，统一映射到 `[-1, +1]` 连续域。核心公式：

```
signal = tanh(zscore(raw_value, rolling_window))
```

`tanh` 压缩让极端值不无限放大，`zscore` 保留相对强度信息。这是机构 Multi-Strategy 中最常用的信号归一化方法（WorldQuant、AQR 均采用 tanh 压缩）。

### 5.2 三种归一化模式

#### 模式 A: zscore_tanh（连续有界因子）

```python
def _normalize_zscore_tanh(value: float, history: deque[float],
                           window: int) -> float | None:
    """适用于: rsi_14(0-100), di_spread(-100~100), stoch_k(0-100),
    adx(0-100), atr_ratio, ema_slope, obv_slope, keltner_width,
    vol_ma_ratio, supertrend_str(-1~+1)
    
    原理: 最近 window 根 bar 的 zscore，再 tanh 压缩到 [-1, +1]
    优势: 自适应阈值（RSI=50 在低波动市可能强信号，在高波动市是中性）
    """
    if len(history) < min_samples:
        return None  # 冷启动期弃权
    arr = np.array(list(history)[-window:])
    mean, std = arr.mean(), arr.std()
    if std < 1e-10:
        return 0.0  # 无波动 → 中性
    z = (value - mean) / std
    return float(np.tanh(z))
```

**关键参数**：

| 因子 | window | min_samples | 说明 |
|------|--------|-------------|------|
| rsi_14 | 100 | 50 | RSI 的"超买超卖"水平随市场变化 |
| di_spread | 100 | 50 | DI 差值的均值漂移明显 |
| stoch_k | 100 | 50 | 同 RSI |
| adx | 100 | 50 | ADX 在不同市场均值不同 |
| atr_ratio | 100 | 50 | 波动率比值 |
| ema_slope | 50 | 30 | 趋势斜率变化快 |
| obv_slope | 100 | 50 | 量价斜率 |
| vol_ma_ratio | 100 | 50 | 成交量比 |
| keltner_width | 100 | 50 | 通道宽度 |
| supertrend_str | 50 | 30 | 已经在 [-1,+1] 但需 zscore 重整 |

**为什么 RSI 用 zscore 而不是固定阈值 50？**

RSI 在趋势市均值可达 55-60，在震荡市均值 45-50。固定阈值 50 的"超买超卖"含义完全不同。zscore_tanh 使 RSI 始终表示"相对近期均值偏离多少标准差"，信号含义稳定。

#### 模式 B: rank_mapping（无量纲/宏观因子）

```python
def _normalize_rank(value: float, history: deque[float],
                    window: int, min_samples: int,
                    direction: int = 1) -> float | None:
    """适用于: COT 系列、央行购金、GLD 持仓、DXY 相关、实际利率、
    GP 发现因子
    
    原理: 在最近 window 根 bar 的分布中做百分位排名，映射到 [-1, +1]
    direction: 1=正向 (值大=看多), -1=反向 (值大=看空，如 dxy_corr)
    """
    if len(history) < min_samples:
        return None
    arr = np.array(list(history)[-window:])
    rank = np.searchsorted(np.sort(arr), value) / len(arr)
    # rank ∈ [0, 1]，映射到 [-1, +1]
    signal = 2.0 * rank - 1.0
    return float(np.clip(signal * direction, -1.0, 1.0))
```

**关键参数**：

| 因子 | window | min_samples | direction | 说明 |
|------|--------|-------------|-----------|------|
| dxy_corr_20 | 100 | 30 | -1 | 美元负相关 |
| slv_gld_ratio | 100 | 30 | 1 | 金银比正向 |
| real_yield_chg | 100 | 30 | -1 | 实际利率负相关 |
| gld_tonnes_chg_5d | 100 | 30 | 1 | 持仓增加看多 |
| gld_tonnes_chg_20d | 100 | 30 | 1 | 中期持仓 |
| gld_tonnes_pct_20d | 100 | 30 | 1 | 持仓变化百分比 |
| gld_tonnes_zscore_60d | 100 | 30 | 1 | 极值反转 |
| slv_tonnes_chg_20d | 100 | 30 | 1 | 白银持仓 |
| silver_gold_holdings_ratio | 100 | 30 | 1 | SLV/GLD 持仓比 |
| cb_total_chg_3m | 100 | 30 | 1 | 全球央行购金 |
| cb_china_chg_3m | 100 | 30 | 1 | 中国央行购金 |
| cb_russia_chg_3m | 100 | 30 | 1 | 俄罗斯购金 |
| cb_china_3m_zscore | 100 | 30 | 1 | 中国购金极值 |
| real_yield_pct_rank | 100 | 30 | -1 | 实际利率分位 |
| cot_mm_net | 100 | 30 | 1 | 投机净多 |
| cot_mm_net_pct_oi | 100 | 30 | 1 | 投机净多占比 |
| cot_mm_net_chg_4w | 100 | 30 | 1 | 4 周变化 |
| cot_mm_net_zscore_52w | 100 | 30 | 1 | 极值反转 |
| cot_pm_net | 100 | 30 | -1 | 商业对冲（反向指标） |
| cot_extreme_signal | 不适用 | — | — | 用 discrete 模式 |
| GP 发现因子 | 100 | 30 | 1 | 默认正向 |

#### 模式 C: discrete（分类因子）

```python
def _normalize_discrete(value: float, value_map: dict[str, float]
                       ) -> float | None:
    """适用于: engulfing(-1/0/1), pin_bar(-1/0/1), inside_bar(0/1),
    hour_utc, day_of_week, hours_to_fomc, hours_to_nfp, 
    cot_extreme_signal(-1/0/1)
    """
    key = str(value)
    return value_map.get(key, 0.0)  # 未知值 → 中性
```

**discrete 因子配置**：

| 因子 | value_map | 说明 |
|------|-----------|------|
| engulfing | {"-1": -1.0, "0": 0.0, "1": 1.0} | 看空/中性/看多 |
| pin_bar | {"-1": -0.8, "0": 0.0, "1": 0.8} | 稍弱于 engulfing |
| inside_bar | {"0": 0.0, "1": -0.3} | 整理 → 轻微看空 |
| hour_utc | 特殊处理 → 时段信号 | 见下 |
| day_of_week | 特殊处理 → 周内效应 | 见下 |
| cot_extreme_signal | {"-1": -1.0, "0": 0.0, "1": 1.0} | COT 综合反转 |

**hour_utc 时段信号**：

```python
# 黄金活跃时段权重更高
HOUR_WEIGHTS = {
    range(14, 21): 0.0,  # 亚洲盘 → 中性
    range(0, 4):   0.3,  # 伦敦开盘 → 轻微看多（流动性注入）
    range(8, 13):  0.5,  # 纽约盘上午 → 信号放大
    range(13, 15): 0.0,  # 午间低谷 → 中性
    range(15, 18): 0.3,  # 纽约收盘 → 轻微
    range(19, 24): 0.0,  # 低流动性 → 中性
}
# 最终: hour_signal = 对应时段权重（0.0 ~ 0.5），信息量低，权重建议 0.1~0.2
```

**day_of_week 周内效应**：

```python
DAY_WEIGHTS = {
    0: 0.0,   # Mon: 中性
    1: 0.0,   # Tue: 中性
    2: 0.1,   # Wed: 轻微正向（FOMC 常在周三）
    3: -0.1,  # Thu: 反转日
    4: -0.2,  # Fri: 周末平仓效应
}
# 权重建议 0.1，极低影响因子
```

### 5.3 SignalNormalizer 类

```python
class SignalNormalizer:
    """三域归一引擎。
    
    为每个因子维护独立的滚动历史窗口，用于 zscore_tanh 和 rank_mapping。
    discrete 因子不需要历史窗口。
    """

    def __init__(self, config: dict[str, dict]):
        """config 来自 RuntimeConfig.factor_signal_config"""
        self._configs: dict[str, dict] = config
        self._histories: dict[str, deque[float]] = {}  # 每因子滚动窗口
        self._initialize_histories()

    def normalize(self, factor_values: dict[str, float | None]
                 ) -> dict[str, float | None]:
        """归一化所有因子值到 [-1, +1]。
        
        返回 None 的因子表示冷启动期弃权。
        """
        signals = {}
        for name, raw_value in factor_values.items():
            if raw_value is None or (isinstance(raw_value, float) and math.isnan(raw_value)):
                signals[name] = None
                continue

            cfg = self._configs.get(name)
            if cfg is None:
                # 未配置因子 → 默认 percentile 模式（GP 因子）
                cfg = self._default_gp_config(name)

            # 更新历史窗口
            if name not in self._histories:
                self._histories[name] = deque(maxlen=cfg.get("window", 100))
            self._histories[name].append(raw_value)

            # 按模式归一化
            mode = cfg.get("mode", "rank_mapping")
            if mode == "zscore_tanh":
                signals[name] = _normalize_zscore_tanh(
                    raw_value, self._histories[name],
                    window=cfg.get("window", 100),
                    min_samples=cfg.get("min_samples", 30),
                )
            elif mode == "rank_mapping":
                signals[name] = _normalize_rank(
                    raw_value, self._histories[name],
                    window=cfg.get("window", 100),
                    min_samples=cfg.get("min_samples", 30),
                    direction=cfg.get("direction", 1),
                )
            elif mode == "discrete":
                signals[name] = _normalize_discrete(
                    raw_value, cfg.get("value_map", {}),
                )
            else:
                signals[name] = None

        return signals

    def warmup(self, factor_snapshots: list[dict[str, float | None]]):
        """从历史因子快照预热滚动窗口。
        
        Phase 3 启动时，用 warmup bars 的因子值填充。
        """
        for snapshot in factor_snapshots:
            for name, value in snapshot.items():
                if value is None or (isinstance(value, float) and math.isnan(value)):
                    continue
                if name not in self._histories:
                    self._histories[name] = deque(
                        maxlen=self._configs.get(name, {}).get("window", 100)
                    )
                self._histories[name].append(value)

    def _default_gp_config(self, name: str) -> dict:
        """GP 发现因子的默认配置。"""
        # 尝试从 AST 分类器获取标签
        try:
            tags = gp_classifier.classify(name) if gp_classifier else ["GP发现"]
        except Exception:
            tags = ["GP发现"]
        return {
            "enabled": True,
            "weight": 0.3,
            "mode": "rank_mapping",
            "window": 100,
            "min_samples": 30,
            "direction": 1,
            "tags": tags,
            "source": "gp",
        }
```

---

## 6. Phase 3 — PortfolioCompositor 分层组合

### 6.1 两层架构

```
Tactical Layer (70%)
├── 技术因子 (rsi, di_spread, stoch_k, adx, ema_slope, ...)
├── 量价因子 (obv_slope, vol_ma_ratio, ...)
├── 形态因子 (engulfing, pin_bar, inside_bar)
├── 波动率因子 (atr_ratio, bb_width, keltner_width)
└── GP 发现因子

Macro Layer (30%)
├── 美元因子 (dxy_corr_20)
├── 利率因子 (real_yield_chg, real_yield_pct_rank)
├── 持仓因子 (gld_tonnes_*, slv_tonnes_*, silver_gold_ratio)
├── COT 因子 (cot_mm_*, cot_pm_net, cot_extreme_signal)
├── 央行因子 (cb_total_*, cb_china_*, cb_russia_*)
└── 事件因子 (hours_to_fomc, hours_to_nfp)
```

**为什么宏观和技术分层？**

1. 宏观因子更新频率（周/月）和技术因子（M15 bar）不同，混在一起做 rolling percentile 导致宏观信号在 M15 级别过度噪声
2. 宏观因子的方向稳定性更强（COT 极值的反转信号可持续数周），应该有更大偏置但更少噪声
3. 权重比例（70/30）可被 AWE 调整，但不应该让 AWE 单独调整每个宏观因子的 M15 级权重——那会让 NFP 前的 hour_utc 因子和 cot_extreme_signal 因子竞争同一个"猪食槽"

### 6.2 CompositeSignal 数据结构

```python
@dataclass
class CompositeSignal:
    direction: int           # 1=LONG, -1=SHORT, 0=NO_SIGNAL
    score: float             # 综合信号强度 ∈ [-1, +1]
    tactical_score: float    # 技术层信号强度 ∈ [-1, +1]
    macro_score: float       # 宏观层信号强度 ∈ [-1, +1]
    tactical_weight: float   # 当前战术层权重 (初始 0.7)
    macro_weight: float      # 当前宏观层权重 (初始 0.3)
    factor_signals: dict[str, float | None]  # 所有因子归一化信号
    factor_values: dict[str, float | None]   # 所有因子原始值
    active_weights: dict[str, float]          # 本 tick 实际参与组合的权重
    tags_breakdown: dict[str, float]          # {类型标签: 该类型贡献的 score}
    n_active_factors: int                     # 非 None 信号的因子数
    n_abstain_factors: int                    # 弃权因子数
    timestamp: float                         # bar 时间戳
```

### 6.3 组合公式

```python
class PortfolioCompositor:
    """分层组合引擎。
    
    战术层和宏观层分别做加权归一化，再按比例混合。
    """

    def __init__(self, config: dict):
        self._factor_configs = config  # 来自 RuntimeConfig
        self._gp_classifier = GPClassifier()  # 可选

    def compose(self, signals: dict[str, float | None],
                factor_values: dict[str, float | None]) -> CompositeSignal:
        # 1. 按 tags 分组到战术层和宏观层
        tactical = {}   # name → (signal, weight)
        macro = {}      # name → (signal, weight)

        for name, sig in signals.items():
            if sig is None:
                continue
            cfg = self._factor_configs.get(name, self._default_gp_config(name))
            if not cfg.get("enabled", True):
                continue
            w = cfg.get("weight", 1.0)
            tags = cfg.get("tags", [])
            if "宏观" in tags or "COT" in tags or "央行" in tags or "持仓" in tags or "美元" in tags or "利率" in tags or "事件" in tags or "日历" in tags:
                macro[name] = (sig, w)
            else:
                tactical[name] = (sig, w)

        # 2. 战术层加权平均
        t_num = sum(sig * w for sig, w in tactical.values())
        t_den = sum(abs(w) for _, w in tactical.values())
        tactical_score = t_num / t_den if t_den > 1e-10 else 0.0

        # 3. 宏观层加权平均
        m_num = sum(sig * w for sig, w in macro.values())
        m_den = sum(abs(w) for _, w in macro.values())
        macro_score = m_num / m_den if m_den > 1e-10 else 0.0

        # 4. 混合
        alpha = self._factor_configs.get("_tactical_alpha", 0.7)
        combined = alpha * tactical_score + (1 - alpha) * macro_score

        # 5. 方向判定
        threshold = self._factor_configs.get("_signal_threshold", 0.4)
        if combined >= threshold:
            direction = 1
        elif combined <= -threshold:
            direction = -1
        else:
            direction = 0

        # 6. tags_breakdown
        tags_breakdown = self._compute_tags_breakdown(signals)

        # 7. 构建 CompositeSignal
        all_weights = {name: w for name, (_, w) in {**tactical, **macro}.items()}
        return CompositeSignal(
            direction=direction,
            score=combined,
            tactical_score=tactical_score,
            macro_score=macro_score,
            tactical_weight=alpha,
            macro_weight=1 - alpha,
            factor_signals=signals,
            factor_values=factor_values,
            active_weights=all_weights,
            tags_breakdown=tags_breakdown,
            n_active_factors=sum(1 for s in signals.values() if s is not None),
            n_abstain_factors=sum(1 for s in signals.values() if s is None),
            timestamp=time.time(),
        )

    def _compute_tags_breakdown(self, signals):
        """按类型标签分解信号贡献。"""
        tag_scores = defaultdict(float)
        tag_weights = defaultdict(float)
        for name, sig in signals.items():
            if sig is None:
                continue
            cfg = self._factor_configs.get(name, {})
            w = cfg.get("weight", 1.0)
            tags = cfg.get("tags", [])
            for t in tags:
                tag_scores[t] += sig * w
                tag_weights[t] += abs(w)
        return {
            t: round(tag_scores[t] / tag_weights[t], 3) if tag_weights[t] > 0 else 0.0
            for t in tag_scores
        }

    def _default_gp_config(self, name):
        """GP 因子默认配置。"""
        try:
            tags = self._gp_classifier.classify(name)
        except Exception:
            tags = ["GP发现"]
        return {
            "enabled": True, "weight": 0.3, "mode": "rank_mapping",
            "window": 100, "min_samples": 30, "direction": 1,
            "tags": tags, "source": "gp",
        }

    def refresh_configs(self):
        """GP 新因子动态注册后刷新配置。"""
        self._ensure_all_factors_configured()

    def _ensure_all_factors_configured(self):
        """自动为注册表中无配置的因子添加默认配置。"""
        registered = factor_registry.list()
        for name in registered:
            if name not in self._factor_configs:
                self._factor_configs[name] = self._default_gp_config(name)
```

### 6.4 阈值的统计含义

`threshold=0.4` 不是一个固定价格——它的含义是"至少 40% 的加权信号强度才能开仓"。在 39 个因子的系统里：

- 如果所有因子权重均等(≈0.026 每个)，一个因子从 0 跳到 +1 拉动 score ≈ 0.026
- threshold=0.4 意味着需要约 15 个因子同向 +1 信号
- 如果调控权重让 top-5 因子权重 60%，那只需 3 个强因子同向即可

**建议**：threshold 不做自适应，但在 RuntimeConfig 暴露，可手动调整。

---

## 7. Phase 4 — Live loop 改造

### 7.1 启动流程变化

**现在**：
```python
Phase 2: strategy = strategy_registry.create("multi_factor_m15")
Phase 3: for bar in warmup_bars: strategy.on_bar(bar)
_process_tick: signal = strategy.on_bar(bar)
```

**改为**：
```python
Phase 1: engine = StreamingFactorEngine()
         normalizer = SignalNormalizer(config.factor_signal_config)
         compositor = PortfolioCompositor(config.factor_portfolio_config)
Phase 2: for bar in warmup_bars:
             engine.append_bar(bar)
         # 预热 normalizer 的 rolling 窗口
         snapshots = []
         engine.reset()
         for bar in warmup_bars:
             fv = engine.append_bar(bar)
             snapshots.append(fv)
         normalizer.warmup(snapshots)
Phase 3: # 不需要再重算，engine 和 normalizer 已经 warm
_process_tick:
    engine.refresh_factor_list()  # 发现新 GP 因子
    factor_values = engine.append_bar(bar)
    if factor_values is None or not engine.is_warm:
        return
    signals = normalizer.normalize(factor_values)
    composite = compositor.compose(signals, factor_values)
    gate_result = execution_gate.filter(composite, factor_values, bar)
    if not gate_result.passed:
        return
    signal = _composite_to_signal(composite, gate_result, engine)
    _execute_signal(signal, bridge, ...)
```

### 7.2 Signal 携带完整归因数据

```python
Signal(
    direction=composite.direction,      # 1 / -1 / 0
    sl_atr=config.strategy_sl_atr,      # 2.0
    tp_atr=config.strategy_tp_atr,      # 3.0
    atr=factor_values.get("atr_ratio", 0) * factor_values.get("close", 0),
    price=bar["close"],
    factor_scores=composite.factor_values,   # 所有因子原始值
    meta={
        "signals": composite.factor_signals, # 归一化信号
        "weights": composite.active_weights, # 实际参与权重
        "score": composite.score,           # 综合分
        "tactical_score": composite.tactical_score,
        "macro_score": composite.macro_score,
        "n_active": composite.n_active_factors,
        "n_abstain": composite.n_abstain_factors,
        "tags_breakdown": composite.tags_breakdown,
        "gate_reason": gate_result.reason,  # 通过/被过滤原因
        "cooldown_remaining": gate_result.cooldown_remaining,
    },
)
```

### 7.3 执行闸门 (ExecutionGate)

```python
@dataclass
class GateResult:
    passed: bool
    reason: str
    cooldown_remaining: int = 0

class ExecutionGate:
    """开仓闸门。组合了波动率/事件/MACD 过滤器。"""

    def __init__(self, config: dict):
        self._config = config
        self._cooldown_bars = 0
        self._macd_hist_history: deque[float] = deque(maxlen=100)

    def filter(self, composite: CompositeSignal,
               factor_values: dict, bar: dict) -> GateResult:
        cfg = RuntimeConfig.shared()

        # 1. 信号强度门槛
        if composite.direction == 0:
            return GateResult(False, "signal_below_threshold")

        # 2. 冷却期
        if self._cooldown_bars > 0:
            return GateResult(False, f"cooldown_{self._cooldown_bars}")

        # 3. MACD 反向过滤器 (保留现有逻辑)
        macd_hist = factor_values.get("macd_hist")
        if macd_hist is not None and cfg.filter_macd_enabled:
            if composite.direction == 1 and macd_hist > 0:
                return GateResult(False, "macd_reverse_filter_long")
            if composite.direction == -1 and macd_hist < 0:
                return GateResult(False, "macd_reverse_filter_short")

        # 4. BB 宽度百分位过滤器 (保留)
        bb_width = factor_values.get("bb_width")
        if bb_width is not None:
            # 高波动跳过 → 但不是用固定阈值，而是 rank
            # (在 normalizer 中 bb_width 已被归一化，此处可直接判断)
            # 保留原始百分位逻辑作为备选
            pass

        # 5. 事件过滤器 (从 strategy 移出)
        event_result = self._event_filter(composite.direction, bar)
        if not event_result.passed:
            return event_result

        # 6. 通过 → 设置冷却期
        self._cooldown_bars = cfg.strategy_cooldown_bars

        return GateResult(True, "passed")

    def tick(self):
        """每根 bar 调用，减冷却计数。"""
        if self._cooldown_bars > 0:
            self._cooldown_bars -= 1

    def _event_filter(self, direction, bar) -> GateResult:
        """NFP skip / FOMC boost / GVZ gate。"""
        cfg = RuntimeConfig.shared()
        bar_ts = bar.get("time", time.time())
        bar_date = datetime.utcfromtimestamp(bar_ts).strftime("%Y-%m-%d")

        if cfg.strategy_enable_nfp_skip:
            if bar_date in _nfp_dates:
                return GateResult(False, "nfp_skip")

        if cfg.strategy_enable_gvz_gate:
            gvz_chg = _get_gvz_change(bar_date)
            if gvz_chg is not None and gvz_chg < cfg.strategy_gvz_drop_pct:
                return GateResult(False, "gvz_gate")

        return GateResult(True, "passed")
```

---

## 8. Phase 5 — AttributionEngine 归因引擎

### 8.1 归因方法：Gram-Schmidt 正交归因为主，线性 MC 为回退

**为什么不用 v2 的 `vote_dir × direction × abs(pnl)`？**

v2 的归因有三问题：（1）同方向因子无区分度（所有 LONG 因子分到相同信用）；（2）大波动交易惩罚放大（PnL 振幅乘以投票方向）；（3）弃权因子永远无反馈。

**为什么用 Gram-Schmidt 正交归因为主？**

框架中已有 `alpha/evaluation/attribution.py` 的完整实现，它通过**顺序正交化（Gram-Schmidt）**解决因子相关性问题：

1. 对因子按指定顺序逐个正交化，消除因子间相关
2. 每个正交化后的因子对残差做单变量回归，得到 marginal R²
3. 各因子 marginal R² 之和 = 总 R²（可加性）
4. 提供独立贡献（marginal）和边际贡献（standalone）的对比

这比线性 MC 近似 `signal_i / Σ|signal_j| × pnl` 更准确——当因子相关性高时，线性近似会把两个高度相关的因子各分一半信用，而正交归因只把独立贡献的部分归给每个因子。

**回退机制**：实盘中如果正交归因计算量太大（因子数 > 20 且每笔交易都要算），回退到线性 MC 近似：

```python
from alpha.evaluation.attribution import Attribution

class AttributionEngine:
    """实盘归因引擎。
    
    主方法: Gram-Schmidt 正交归因 (来自 alpha/evaluation/attribution.py)
    回退方法: 线性 MC 近似 (signal_i / Σ|signal_j| × pnl)
    """

    def __init__(self):
        self._open_trades: dict[int, TradeAttribution] = {}
        self._per_factor: dict[str, FactorAttributionStats] = {}
        self._orthogonal_attribution = Attribution(demean=True)
        self._factor_history: dict[str, deque[float]] = {}  # 滚动因子值历史
        self._pnl_history: list[float] = []  # 滚动 PnL 历史
        self._trade_log_path = "data/charts/factor_trades.jsonl"
        
        # 正交归因因子排序（IC 高的优先，因为 IC 高的因子更可能提供独立信息）
        self._factor_order: list[str] | None = None  # None = 自动按 IC 排序

    def record_close(self, position_id: int, close_price: float,
                     close_ts: float) -> dict[str, float]:
        """平仓时计算归因。
        
        优先使用 Gram-Schmidt 正交归因（如果有足够因子值历史），
        回退到线性 MC 近似。
        """
        attrib = self._open_trades.pop(position_id, None)
        if attrib is None:
            return {}

        trade_pnl = (close_price - attrib.open_price) * attrib.direction

        # ── 尝试 Gram-Schmidt 正交归因 ──
        marginal_contributions = self._orthogonal_attribution_close(
            attrib, trade_pnl
        )
        
        # ── 回退到线性 MC 近似 ──
        if marginal_contributions is None:
            marginal_contributions = self._linear_mc_close(attrib, trade_pnl)

        # ── 更新滚动统计 ──
        for name, mc in marginal_contributions.items():
            if name not in self._per_factor:
                self._per_factor[name] = FactorAttributionStats(name)
            self._per_factor[name].record(mc, trade_pnl > 0, attrib.tags_breakdown)

        self._write_trade_log(position_id, attrib, close_price, close_ts,
                               marginal_contributions, trade_pnl)
        return marginal_contributions

    def _orthogonal_attribution_close(self, attrib: TradeAttribution,
                                       trade_pnl: float) -> dict[str, float] | None:
        """使用 alpha/evaluation/attribution.py 的 Gram-Schmidt 正交归因。
        
        条件: 最近 N 笔交易的因子值矩阵可用且因子数 ≥ 3。
        返回 {factor_name: marginal_contribution}_PnL
        """
        # 检查因子值历史是否充足
        active_factors = [n for n, s in attrib.factor_signals.items()
                          if s is not None and abs(s) >= 1e-10]
        if len(active_factors) < 3:
            return None  # 因子太少，正交化意义不大

        # 构建因子值矩阵 (最近 N 笔 × K 因子)
        factor_matrix, factor_names, pnl_series = self._build_factor_matrix()
        if factor_matrix is None or len(pnl_series) < 10:
            return None  # 样本不足

        # 调用现有 Gram-Schmidt 归因
        try:
            report = self._orthogonal_attribution.attribute(
                factor_matrix, pnl_series, factor_names=factor_names
            )
            # 将 marginal_r² × trade_pnl 作为归因信用
            total_r2 = report.total_r2 if report.total_r2 > 1e-10 else 1.0
            return {
                c.name: round(c.marginal_r2 / total_r2 * trade_pnl, 6)
                for c in report.contributions
                if c.name in attrib.factor_signals and c.marginal_r2 > 0
            }
        except Exception:
            return None  # 正交化失败，回退

    def _linear_mc_close(self, attrib: TradeAttribution,
                         trade_pnl: float) -> dict[str, float]:
        """线性 MC 回退：signal_i / Σ|signal_j| × pnl"""
        total_abs_signal = attrib.total_signal_abs
        if total_abs_signal < 1e-10:
            return {}
        return {
            name: round((signal / total_abs_signal) * trade_pnl, 6)
            for name, signal in attrib.factor_signals.items()
            if signal is not None and abs(signal) >= 1e-10
        }
```

**正交化 vs 线性 MC 的选择逻辑**：

| 条件 | 使用方法 | 原因 |
|------|---------|------|
| 因子数 ≥ 3 且样本 ≥ 10 笔 | Gram-Schmidt 正交归因 | 解决因子相关，信用分配准确 |
| 因子数 < 3 或样本 < 10 笔 | 线性 MC 近似 | 样本不足正交化不稳定 |
| 正交化数值失败 | 线性 MC 近似 | 保底回退 |

### 8.2 归因数据结构

```python
@dataclass
class TradeAttribution:
    """开仓时记录，平仓时匹配。"""
    position_id: int
    open_ts: float
    open_price: float
    direction: int                    # 1=LONG, -1=SHORT
    factor_signals: dict[str, float]  # 归一化信号 {name: signal ∈ [-1,+1]}
    factor_values: dict[str, float]   # 原始值
    active_weights: dict[str, float]  # 实际权重
    composite_score: float             # 综合分
    tactical_score: float
    macro_score: float
    tags_breakdown: dict[str, float]
    total_signal_abs: float           # Σ|signal_j| 用于 MC 计算

class AttributionEngine:
    """实盘归因引擎。"""

    def __init__(self):
        self._open_trades: dict[int, TradeAttribution] = {}
        self._per_factor: dict[str, FactorAttributionStats] = {}
        self._trade_log_path = "data/charts/factor_trades.jsonl"

    def record_open(self, position_id: int, attribution: TradeAttribution):
        """开仓时记录归因。"""
        self._open_trades[position_id] = attribution

    def record_close(self, position_id: int, close_price: float,
                     close_ts: float) -> dict[str, float]:
        """平仓时计算归因。返回 {factor_name: marginal_contribution}。
        
        MC_i = signal_i / Σ|signal_j| × pnl
        
        其中 pnl = (close_price - open_price) × direction
        (对于多头，close > open → 正 pnl；对于空头反向)
        """
        attrib = self._open_trades.pop(position_id, None)
        if attrib is None:
            logger.warning(f"No attribution for position {position_id}")
            return {}

        # 交易 PnL（简化为价格变化，不含 lot size——AWE 用的是 PnL 方向和比例）
        trade_pnl = (close_price - attrib.open_price) * attrib.direction

        # 归一化分母
        total_abs_signal = attrib.total_signal_abs
        if total_abs_signal < 1e-10:
            return {}  # 没有因子信号，无法归因

        # 边际贡献
        marginal_contributions = {}
        for name, signal in attrib.factor_signals.items():
            if signal is None or abs(signal) < 1e-10:
                continue
            mc = (signal / total_abs_signal) * trade_pnl
            marginal_contributions[name] = round(mc, 6)

            # 更新因子滚动统计
            if name not in self._per_factor:
                self._per_factor[name] = FactorAttributionStats(name)
            self._per_factor[name].record(mc, trade_pnl > 0, attrib.tags_breakdown)

        # 写出逐笔明细
        self._write_trade_log(position_id, attrib, close_price, close_ts,
                               marginal_contributions, trade_pnl)

        return marginal_contributions

    def get_factor_stats(self, name: str) -> "FactorAttributionStats | None":
        return self._per_factor.get(name)

    def get_all_factor_stats(self) -> dict[str, "FactorAttributionStats"]:
        return dict(self._per_factor)
```

### 8.3 因子归因统计

```python
from execution._sharpe import sharpe_ratio_log_nw, TF_BARS_PER_YEAR
from alpha.evaluation.bootstrap_ci import BootstrapCI
from alpha.evaluation.causal_check import CausalCheck
from alpha.calibration import deflated_sharpe_ratio

@dataclass
class FactorAttributionStats:
    """单因子归因滚动统计。
    
    核心指标使用 Newey-West HAC Sharpe ratio (来自 execution/_sharpe.py)，
    而非简单 mean/std IR，因为：
    1. M15 黄金 equity 序列强自相关 (Lo 2002: iid Sharpe 虚高 20-50%)
    2. NW HAC 用 Bartlett kernel 自动选择 lag，校正自相关偏倚
    3. 用 log returns 保证跨期可加性
    
    同时提供 Bootstrap CI (来自 alpha/evaluation/bootstrap_ci.py) 用于
    IR 置信区间和 DSR 多重检验 (来自 alpha/calibration.py) 用于退役判断。
    """
    name: str
    n_trades: int = 0
    n_voted: int = 0          # 非弃权次数
    wins: int = 0
    total_mc: float = 0.0      # 边际贡献累计
    recent_mcs: deque = field(default_factory=lambda: deque(maxlen=250))
    recent_pnls: deque = field(default_factory=lambda: deque(maxlen=250))  # MC PnL 序列（构造 equity curve）
    recent_pnl_directions: deque = field(default_factory=lambda: deque(maxlen=50))

    def record(self, mc: float, is_win: bool, tags: dict):
        self.n_trades += 1
        self.total_mc += mc
        self.recent_mcs.append(mc)
        self.n_voted += 1
        if is_win:
            self.wins += 1
        self.recent_pnl_directions.append(1 if is_win else -1)

    @property
    def win_rate(self) -> float:
        return self.wins / self.n_voted if self.n_voted > 0 else 0.0

    @property
    def avg_mc(self) -> float:
        return self.total_mc / self.n_voted if self.n_voted > 0 else 0.0

    # ── Newey-West HAC Sharpe (替换简单 IR) ──
    
    @property
    def sharpe_short(self) -> float:
        """Sharpe(50): 最近 50 笔 MC 构造 equity curve 的 NW-HAC Sharpe。"""
        return self._compute_nw_sharpe(50)

    @property
    def sharpe_mid(self) -> float:
        """Sharpe(100): 最近 100 笔 MC 的 NW-HAC Sharpe。"""
        return self._compute_nw_sharpe(100)

    @property
    def sharpe_long(self) -> float:
        """Sharpe(250): 最近 250 笔 MC 的 NW-HAC Sharpe。"""
        return self._compute_nw_sharpe(250)

    @property
    def composite_sharpe_score(self) -> float:
        """综合 Sharpe 分数: 0.5×S50 + 0.3×S100 + 0.2×S250
        
        替代原 v4 的 IR(50/100/250) 简单 mean/std，
        使用 Newey-West HAC Sharpe 校正自相关偏倚。
        """
        s50 = self.sharpe_short
        s100 = self.sharpe_mid
        s250 = self.sharpe_long
        score = 0.0
        weight_sum = 0.0
        for s, w in [(s50, 0.5), (s100, 0.3), (s250, 0.2)]:
            if not math.isnan(s):
                score += s * w
                weight_sum += w
        return score / weight_sum if weight_sum > 0 else 0.0

    def _compute_nw_sharpe(self, window: int) -> float:
        """从 MC 序列构造 equity curve，计算 NW-HAC Sharpe。
        
        使用 execution/_sharpe.py 的 sharpe_ratio_log_nw()，
        M15 timeframe → bars_per_year = 24192。
        """
        data = list(self.recent_mcs)[-window:]
        if len(data) < 10:
            return float('nan')
        # MC 序列转 equity curve: eq_t = eq_{t-1} × (1 + mc_t)
        # 对小 mc 值，1+mc ≈ exp(mc)，所以 log returns ≈ mc 本身
        equity = np.cumsum(data)  # 简化：MC 已经是 PnL 比例
        equity = equity - equity[0] + 1000  # 归一化起始净值
        # 确保全部 > 0（避免 log 负数）
        if np.any(equity <= 0):
            equity = equity - equity.min() + 100
        return sharpe_ratio_log_nw(equity, "M15")

    # ── Bootstrap CI (来自 alpha/evaluation/bootstrap_ci.py) ──

    def sharpe_ci(self, window: int = 100, alpha: float = 0.05) -> tuple[float, float] | None:
        """MC Sharpe 的 Bootstrap 置信区间。
        
        用 BootstrapCI.ci_sharpe() 计算，返回 (lo, hi)。
        如果 CI 包含 0，则该因子 Sharpe 在统计上不显著。
        """
        data = list(self.recent_mcs)[-window:]
        if len(data) < 30:
            return None
        try:
            ci = BootstrapCI(alpha=alpha, n_iterations=1000, random_seed=42,
                           annualization_factor=TF_BARS_PER_YEAR["M15"])
            result = ci.ci_sharpe(np.array(data))
            return (result.ci_lower, result.ci_upper)
        except Exception:
            return None

    # ── DSR 多重检验 (来自 alpha/calibration.py) ──

    def is_statistically_significant(self, n_trials: int = 39) -> dict:
        """用 Deflated Sharpe Ratio 检验该因子的 Sharpe 是否显著 > 0。
        
        DSR 校正了：
        1. 多重检验偏倚 (39 个因子同时测试)
        2. 收益分布非正态 (skew/kurtosis)  
        3. 收益自相关
        
        返回 {dsr, p_value, significant, emax_null}
        """
        data = list(self.recent_mcs)[-100:]
        if len(data) < 20:
            return {"dsr": 0.0, "p_value": 1.0, "significant": False, "emax_null": 0.0}
        returns = np.array(data)
        observed_sharpe = self.sharpe_mid
        if math.isnan(observed_sharpe):
            return {"dsr": 0.0, "p_value": 1.0, "significant": False, "emax_null": 0.0}
        try:
            result = deflated_sharpe_ratio(
                observed_sr=observed_sharpe,
                returns=returns,
                n_trials=n_trials,
            )
            return result
        except Exception:
            return {"dsr": 0.0, "p_value": 1.0, "significant": False, "emax_null": 0.0}

    # ── CausalCheck (来自 alpha/evaluation/causal_check.py) ──

    def causal_quality(self, factor_values: np.ndarray,
                       forward_returns: np.ndarray) -> dict:
        """检验因子的预测关系是否因果（而非伪相关）。
        
        返回 CausalReport: {cause_vs_corr_score, orthogonality_pvalue,
                             decay_rate, raw_correlation, ...}
        """
        checker = CausalCheck(n_lags=1)
        report = checker.check(factor_values, forward_returns)
        return {
            "cause_vs_corr_score": report.cause_vs_corr_score,
            "orthogonality_pvalue": report.orthogonality_pvalue,
            "decay_rate": report.decay_rate,
            "raw_correlation": report.raw_correlation,
            "early_correlation": report.early_correlation,
            "late_correlation": report.late_correlation,
        }

    # ── 向后兼容：保留简单 IR 作为快速参考 ──

    @property
    def ir_short(self) -> float:
        """IR(50): 最近 50 笔 MC 的 mean/std（快速参考，非正式统计量）。"""
        return self._compute_ir(50)

    @property
    def ir_mid(self) -> float:
        """IR(100): 最近 100 笔 MC 的 mean/std。"""
        return self._compute_ir(100)

    @property
    def ir_long(self) -> float:
        """IR(250): 最近 250 笔 MC 的 mean/std。"""
        return self._compute_ir(250)

    def _compute_ir(self, window: int) -> float:
        """滚动 IR = mean(mc) / std(mc)。
        
        注意：简单 IR 不校正自相关，M15 equity 序列自相关严重时会偏乐观。
        AWE 权重调整应优先使用 composite_sharpe_score (NW-HAC)。
        此方法保留作为快速诊断和向后兼容。
        """
        data = list(self.recent_mcs)[-window:]
        if len(data) < 10:
            return float('nan')
        arr = np.array(data)
        mean = arr.mean()
        std = arr.std()
        if std < 1e-10:
            return 0.0
        return float(mean / std)
```

---

## 9. Phase 6 — AdaptiveWeightEngine 权重自适应

### 9.1 核心算法：Newey-West Sharpe 驱动 + 锚点回归 + CausalCheck 退役 + DSR 多重检验

```python
from execution._sharpe import sharpe_ratio_log_nw
from alpha.evaluation.causal_check import CausalCheck
from alpha.evaluation.bootstrap_ci import BootstrapCI
from alpha.calibration import deflated_sharpe_ratio
from alpha.search.blend_search import BlendSearch

class AdaptiveWeightEngine:
    """权重自适应引擎。
    
    核心: Newey-West HAC Sharpe 驱动的权重调整，带锚点回归。
    因子退役使用 CausalCheck (正交性检验+衰减率) + DSR 多重检验，
    而非简单的 IC 门限或 win_rate 门限。
    
    离线基准: BlendSearch SLSQP Sharpe 优化（来自 alpha/search/blend_search.py）
    提供 IC 加权和最优权重作为 AWE 调权的先验参考。
    """

    def __init__(self, config: dict):
        self._config = config
        self._base_weights: dict[str, float] = {}  # 锚点权重（初始配置）
        self._current_weights: dict[str, float] = {}  # 当前权重

    def initialize(self, factor_configs: dict[str, dict]):
        """记录初始权重作为锚点。"""
        for name, cfg in factor_configs.items():
            self._base_weights[name] = cfg.get("weight", 1.0)
            self._current_weights[name] = cfg.get("weight", 1.0)

    def adapt(self, attribution: AttributionEngine,
              factor_configs: dict[str, dict]) -> dict[str, dict]:
        """权重自适应调整。
        
        返回: {factor_name: {"weight": new_weight, "reason": str}}
        只返回有变化的因子。
        """
        cfg = RuntimeConfig.shared()
        patches = {}

        all_stats = attribution.get_all_factor_stats()

        for name, stats in all_stats.items():
            cfg_entry = factor_configs.get(name)
            if cfg_entry is None:
                continue

            # ── 最低交易笔数门槛 ──
            if stats.n_trades < cfg.awe_min_trades:
                continue  # 样本不足，不调

            # ── IC 下限检查 ──
            ic_status = ic_tracker.status(name) if ic_tracker else None
            if ic_status and abs(ic_status.get("rolling_ic", 0)) < cfg.awe_ic_floor:
                # IR 再好，IC < 0.02 的因子不参与
                continue

            # ── 健康分下限 ──
            health = factor_health.evaluate(name) if factor_health else None
            if health and health.score < cfg.awe_health_floor:
                # 健康分 < 40 的因子直接禁用
                new_weight = 0.0
                if abs(new_weight - self._current_weights.get(name, 0)) >= 0.01:
                    patches[name] = {"weight": 0.0, "reason": f"health={health.score:.0f}<_floor"}
                continue

            # ── IR 综合分数驱动（使用 composite_sharpe_score 替代 composite_ir_score）──
            composite_score = stats.composite_sharpe_score

            old_weight = self._current_weights.get(name, cfg_entry.get("weight", 1.0))
            base_weight = self._base_weights.get(name, 1.0)

            # ── 核心公式: new = old × exp(k × composite_score) ──
            # composite_score 现在是 Newey-West HAC Sharpe 三层窗口加权和
            # 而非简单的 mean/std IR，校正了自相关偏倚
            k = cfg.awe_sensitivity  # 默认 0.5
            raw_new = old_weight * math.exp(k * composite_score)

            # ── 锚点回归 ──
            # 权重不应该永远漂移，要定期回归到基础权重
            # 每次调整后向 base 混合，混合系数 = anchor_pull（默认 0.15）
            anchor_pull = cfg.awe_anchor_pull  # 默认 0.15
            new_weight = raw_new * (1 - anchor_pull) + base_weight * anchor_pull

            # ── 单次调整限幅 ──
            max_change = cfg.awe_max_single_change  # 默认 0.15
            if abs(new_weight - old_weight) > max_change:
                new_weight = old_weight + math.copysign(max_change, new_weight - old_weight)

            # ── 权重上下限 ──
            min_w = cfg.awe_weight_min  # 默认 0.1
            max_w = cfg.awe_weight_max   # 默认 3.0
            new_weight = max(min_w, min(max_w, new_weight))

            # ── 禁用条件（使用 CausalCheck + DSR 多重检验）──
            # 替代原 v4 的简单 win_rate < 25% 门限
            # 三重检查：CausalCheck 因果性 + DSR 多重检验 + 健康分
            if stats.n_trades >= cfg.awe_disable_min_trades:
                should_disable = False
                reason = ""
                
                # 检查 1: CausalCheck 因果性 (来自 alpha/evaluation/causal_check.py)
                # cause_vs_corr_score ∈ [-1, +1]，负值意味着因子预测力可能是伪相关
                causal = stats.causal_quality(factor_values, forward_returns) if _has_factor_values else None
                if causal and causal["cause_vs_corr_score"] < -0.3:
                    should_disable = True
                    reason = f"causal_score={causal['cause_vs_corr_score']:.2f}<-0.3"
                
                # 检查 2: DSR 多重检验 (来自 alpha/calibration.py)
                # DSR 校正了 39 个因子同时测试的多重检验偏倚
                dsr_result = stats.is_statistically_significant(n_trials=39)
                if dsr_result.get("significant") is False and dsr_result.get("p_value", 1.0) > 0.95:
                    # Sharpe 不显著且 DSR p-value 极高 → 因子可能是噪声
                    should_disable = True
                    reason = f"dsr_p={dsr_result['p_value']:.3f}>0.95"
                
                # 检查 3: 健康分兜底
                if health and health.score < cfg.awe_health_floor:
                    should_disable = True
                    reason = f"health={health.score:.0f}<_floor"
                
                if should_disable:
                    new_weight = 0.0

            if abs(new_weight - old_weight) >= 0.01:
                patches[name] = {
                    "weight": round(new_weight, 3),
                    "reason": f"ir_score={composite_score:.2f},wr={stats.win_rate:.1%}",
                }

        # ── 多样性约束 ──
        patches = self._enforce_diversity(patches, factor_configs, all_stats)

        # ── 更新当前权重 ──
        for name, patch in patches.items():
            self._current_weights[name] = patch["weight"]

        # ── 写入权重历史 ──
        self._write_weight_history(patches)

        # ── 热更新 RuntimeConfig ──
        if patches:
            self._patch_runtime_config(patches)

        return patches

    def _enforce_diversity(self, patches, factor_configs, all_stats):
        """同一类型因子总权重不超过 max_type_weight_pct（默认 40%）。"""
        cfg = RuntimeConfig.shared()
        max_pct = cfg.awe_max_type_weight_pct  # 默认 0.4

        # 合并当前配置 + 补丁
        merged = {name: dict(cfg_entry) for name, cfg_entry in factor_configs.items()}
        for name, p in patches.items():
            if name in merged:
                merged[name]["weight"] = p["weight"]

        # 按类型聚合
        total_weight = sum(c.get("weight", 0) for c in merged.values()
                          if c.get("enabled", True))
        if total_weight <= 0:
            return patches

        type_weights = defaultdict(float)
        type_factors = defaultdict(list)
        for name, c in merged.items():
            if not c.get("enabled", True) or c.get("weight", 0) <= 0:
                continue
            for tag in c.get("tags", []):
                type_weights[tag] += c["weight"]
                type_factors[tag].append((name, c, all_stats.get(name)))

        # 检查每个类型
        for tag, tw in type_weights.items():
            pct = tw / total_weight
            if pct > max_pct:
                # 超限 → 该类型中 IR 最低的降权
                factors_of_type = sorted(
                    type_factors[tag],
                    key=lambda x: x[2].composite_sharpe_score if x[2] else -999
                )
                worst_name, worst_cfg, worst_stats = factors_of_type[0]
                if worst_name in patches:
                    old_patch_w = patches[worst_name]["weight"]
                    patches[worst_name] = {
                        "weight": max(old_patch_w * 0.5, 0.1),
                        "reason": f"diversity_{tag}_{pct:.0%}>max",
                    }
                # 递归检查（因为降权可能使其他类型超限）
                return self._enforce_diversity(patches, factor_configs, all_stats)

        return patches

    # ── 禁用因子复活 ──
    def _check_resurrection(self, name, stats, factor_configs):
        """被禁用的因子在 CausalCheck + DSR 恢复后以减半权重复活。
        
        复活条件（全部满足）：
        1. CausalCheck cause_vs_corr_score > 0（预测关系正向）
        2. DSR p-value < 0.05（Sharpe 统计显著，即使在多重检验下）
        3. health_score > 60（5 维健康分达标）
        4. 冷却期满（默认 7 天）
        """
        cfg = RuntimeConfig.shared()
        health = factor_health.evaluate(name) if factor_health else None
        
        # 健康分检查
        if not health or health.score <= cfg.awe_resurrect_health_threshold:
            return None
        
        # DSR 多重检验检查
        dsr = stats.is_statistically_significant(n_trials=39)
        if dsr.get("p_value", 1.0) >= 0.05:
            return None  # Sharpe 不显著
        
        # CausalCheck 因果性检查（需要因子值数据）
        # 如果无数据则只检查 health 和 DSR
        days_since = self._days_since_disabled(name)
        if days_since < cfg.awe_resurrect_cooldown_days:
            return None
            
        base_weight = self._base_weights.get(name, 1.0)
        return base_weight * 0.5  # 减半起步

    # ── 离线 Sharpe 优化基准（来自 BlendSearch）──

    def compute_blend_baseline(self, factor_returns: np.ndarray,
                                forward_returns: np.ndarray,
                                factor_names: list[str]) -> dict[str, float]:
        """用 alpha/search/blend_search.py 的 SLSQP 优化计算离线最优权重。
        
        作为 AWE 在线调权的先验基准：
        - AWE 的 base_weight 不应该是手工拍脑袋，而应来自 BlendSearch 离线优化
        - BlendSearch 提供 equal_weight / ic_weighted / slsqp_optimal 三组基准
        - AWE 的锚点回归使用 BlendSearch 的最优权重而非手工配置
        """
        bs = BlendSearch()
        # 等权基准
        eq = bs.equal_weight_blend(factor_names)
        self._blend_baselines["equal_weight"] = eq
        # IC 加权基准
        ics = [ic_tracker.rolling_ic(n) for n in factor_names] if ic_tracker else None
        if ics:
            ic = bs.ic_weighted_blend(factor_names, ics)
            self._blend_baselines["ic_weighted"] = ic
        # SLSQP 最优 Sharpe
        opt = bs.optimize(factor_returns, forward_returns, factor_names,
                         max_single_weight=0.5)
        self._blend_baselines["slsqp_optimal"] = opt
        
        # 返回最优权重作为 AWE 锚点参考
        return {n: w for n, w in zip(opt.factor_names, opt.coefficients)}
```

### 9.2 为什么 NW-HAC Sharpe + CausalCheck 优于 win_rate × 乘法？

| 维度 | v2: win_rate × 乘法 | v4 NW-HAC Sharpe + CausalCheck + DSR |
|------|---------------------|---------------------------------------|
| 统计量 | 二项分布 P(win)，20 笔标准差 11% | NW-HAC Sharpe 三层窗口 (50/100/250)，校正自相关偏倚 |
| 退役判据 | win_rate < 25% AND n ≥ 20 | CausalCheck cause_vs_corr < -0.3 **或** DSR p > 0.95 **或** health < 40，三者之一即禁用 |
| 退役统计效力 | 39 因子同时测试不做校正 | DSR 校正多重检验偏倚 (E[max|SR|H0])，Holm-Bonferroni 阶梯校正 |
| 伪相关检测 | 无 | CausalCheck 正交性检验 (lagged OLS residual p-value) |
| 因子衰减检测 | 无 | CausalCheck decay_rate (early vs late IC) + health decay_rate (10%) |
| 权重漂移 | 无锚点，永远向 0 或 3.0 漂移 | 每次调整 15% 回归 base_weight |
| 权重离线基准 | 无 | BlendSearch SLSQP 最优 Sharpe 权重作为先验 |
| 复活机制 | win_rate > 50% + health > 60 | CausalCheck cause_vs_corr > 0 + DSR p < 0.05 + health > 60 + 冷却期 |
| CIS 置信区间 | 无 | Bootstrap CI for Sharpe，CI 包含 0 则标记不显著 |

### 9.3 触发条件

- 每 50 笔交易（默认 `awe_adapt_interval=50`）
- 或每日 UTC 04:00 定时触发（scheduler job）
- 首次 10 笔交易后开始统计（但不调权）

---

## 10. 风险控制体系

### 10.1 分层风控

```
单因子层 → max_factor_weight = 3.0（单因子不可超过总权重 3 倍）
类型层   → max_type_weight_pct = 40%（同类因子总权重上限）
组合层   → signal_threshold = 0.4（信号太弱不开仓）
仓位层   → risk_pct = 0.5%（单笔风险不超过净值 0.5%）
日度层   → daily_loss_limit = 3%（日内亏损达 3% 暂停交易）
熔断层   → equity_dd > 10%（净值回撤超 10% 停止交易）
```

### 10.2 与现有 CircuitBreaker 集成

```python
# live_service.py 中 _process_tick 调用链
def _process_tick(composite, factor_values, bar, bridge):
    # 1. 信号闸门
    gate_result = execution_gate.filter(composite, factor_values, bar)
    if not gate_result.passed:
        return
    
    # 2. 熔断检查（复用现有 circuit.py）
    tripped, reason = circuit_breaker.check_all()
    if tripped:
        logger.warning(f"Circuit breaker tripped: {reason}")
        return
    
    # 3. 仓位计算
    risk_pct = RuntimeConfig.shared().risk_pct  # 0.5%
    equity = _live_state["account"]["balance"]
    atr = factor_values.get("atr_ratio", 0) * factor_values.get("close", 4500)
    sl_distance = RuntimeConfig.shared().strategy_sl_atr * atr  # 2.0 × ATR
    volume = (equity * risk_pct / 100) / sl_distance
    
    # 4. 执行
    ...
```

---

## 11. GP 因子 AST 分类器

与 v2 相同，此处不重复。标签体系：

| AST 模式 | 标签 |
|----------|------|
| ts_corr(close, volume, n) | 量价 |
| ts_corr(close, dxy, n) + 衰减 | 宏观_美元 |
| delta(close, n) | 动量 |
| zscore(close, n) | 均值回归 |
| ts_std(close, n) | 波动率 |
| power/log/sqrt | 非线性 |
| 混合 3+ 叶子 | 复合 |
| 无匹配 | GP发现 |

宏观标签的 GP 因子自动进入 Macro Layer。

---

## 12. RuntimeConfig 配置段

在现有 `RuntimeConfig` 中新增以下字段：

```python
# ════════════════════════════════════════════════
# Signal Normalizer 配置
# ════════════════════════════════════════════════
factor_signal_mode: str = "zscore_tanh"  # 全局默认模式
factor_signal_config: dict = field(default_factory=lambda: {
    # 模式 A: zscore_tanh（连续有界因子）
    "rsi_14":         {"mode": "zscore_tanh", "window": 100, "min_samples": 50, "tags": ["技术", "均值回归"]},
    "di_spread":      {"mode": "zscore_tanh", "window": 100, "min_samples": 50, "tags": ["技术", "趋势"]},
    "stoch_k":        {"mode": "zscore_tanh", "window": 100, "min_samples": 50, "tags": ["技术", "动量"]},
    "adx":            {"mode": "zscore_tanh", "window": 100, "min_samples": 50, "tags": ["技术", "趋势"]},
    "atr_ratio":      {"mode": "zscore_tanh", "window": 100, "min_samples": 50, "tags": ["技术", "波动率"]},
    "ema_slope":       {"mode": "zscore_tanh", "window": 50,  "min_samples": 30, "tags": ["技术", "趋势"]},
    "supertrend_str":  {"mode": "zscore_tanh", "window": 50,  "min_samples": 30, "tags": ["技术", "趋势"]},
    "keltner_width":   {"mode": "zscore_tanh", "window": 100, "min_samples": 50, "tags": ["技术", "波动率"]},
    "obv_slope":       {"mode": "zscore_tanh", "window": 100, "min_samples": 50, "tags": ["量价"]},
    "vol_ma_ratio":    {"mode": "zscore_tanh", "window": 100, "min_samples": 50, "tags": ["量价"]},

    # 模式 B: rank_mapping（宏观/持仓/COT 因子）
    "dxy_corr_20":             {"mode": "rank_mapping", "window": 100, "min_samples": 30, "direction": -1, "tags": ["宏观", "美元"]},
    "slv_gld_ratio":           {"mode": "rank_mapping", "window": 100, "min_samples": 30, "direction": 1,  "tags": ["宏观", "金银比"]},
    "real_yield_chg":          {"mode": "rank_mapping", "window": 100, "min_samples": 30, "direction": -1, "tags": ["宏观", "利率"]},
    "real_yield_pct_rank":     {"mode": "rank_mapping", "window": 100, "min_samples": 30, "direction": -1, "tags": ["宏观", "利率"]},
    "gld_tonnes_chg_5d":       {"mode": "rank_mapping", "window": 100, "min_samples": 30, "direction": 1,  "tags": ["持仓", "黄金"]},
    "gld_tonnes_chg_20d":      {"mode": "rank_mapping", "window": 100, "min_samples": 30, "direction": 1,  "tags": ["持仓", "黄金"]},
    "gld_tonnes_pct_20d":      {"mode": "rank_mapping", "window": 100, "min_samples": 30, "direction": 1,  "tags": ["持仓", "黄金"]},
    "gld_tonnes_zscore_60d":   {"mode": "rank_mapping", "window": 100, "min_samples": 30, "direction": 1,  "tags": ["持仓", "黄金", "极值"]},
    "slv_tonnes_chg_20d":      {"mode": "rank_mapping", "window": 100, "min_samples": 30, "direction": 1,  "tags": ["持仓", "白银"]},
    "silver_gold_holdings_ratio": {"mode": "rank_mapping", "window": 100, "min_samples": 30, "direction": 1, "tags": ["持仓", "金银比"]},
    "cb_total_chg_3m":         {"mode": "rank_mapping", "window": 100, "min_samples": 30, "direction": 1,  "tags": ["央行", "购金"]},
    "cb_china_chg_3m":         {"mode": "rank_mapping", "window": 100, "min_samples": 30, "direction": 1,  "tags": ["央行", "购金"]},
    "cb_russia_chg_3m":        {"mode": "rank_mapping", "window": 100, "min_samples": 30, "direction": 1,  "tags": ["央行", "购金"]},
    "cb_china_3m_zscore":      {"mode": "rank_mapping", "window": 100, "min_samples": 30, "direction": 1,  "tags": ["央行", "购金", "极值"]},
    "cot_mm_net":              {"mode": "rank_mapping", "window": 100, "min_samples": 30, "direction": 1,  "tags": ["COT", "投机"]},
    "cot_mm_net_pct_oi":       {"mode": "rank_mapping", "window": 100, "min_samples": 30, "direction": 1,  "tags": ["COT", "投机"]},
    "cot_mm_net_chg_4w":      {"mode": "rank_mapping", "window": 100, "min_samples": 30, "direction": 1,  "tags": ["COT", "投机"]},
    "cot_mm_net_zscore_52w":   {"mode": "rank_mapping", "window": 100, "min_samples": 30, "direction": 1,  "tags": ["COT", "投机", "极值"]},
    "cot_pm_net":              {"mode": "rank_mapping", "window": 100, "min_samples": 30, "direction": -1, "tags": ["COT", "商业"]},
    "cot_extreme_signal":      {"mode": "discrete", "value_map": {"-1": -1.0, "0": 0.0, "1": 1.0}, "tags": ["COT", "反转", "综合"]},

    # 模式 C: discrete（形态/事件因子）
    "engulfing":               {"mode": "discrete", "value_map": {"-1": -1.0, "0": 0.0, "1": 1.0},    "tags": ["形态", "反转"]},
    "pin_bar":                 {"mode": "discrete", "value_map": {"-1": -0.8, "0": 0.0, "1": 0.8},    "tags": ["形态", "反转"]},
    "inside_bar":              {"mode": "discrete", "value_map": {"0": 0.0, 1": -0.3},                "tags": ["形态", "整理"]},
    "hour_utc":                {"mode": "discrete", "value_map": "hour_weights",                     "tags": ["日历", "时段"]},
    "day_of_week":             {"mode": "discrete", "value_map": "day_weights",                     "tags": ["日历", "周内"]},
    "hours_to_fomc":           {"mode": "discrete", "value_map": "fomc_weights",                    "tags": ["事件", "FOMC"]},
    "hours_to_nfp":            {"mode": "discrete", "value_map": "nfp_weights",                     "tags": ["事件", "NFP"]},
})

# ════════════════════════════════════════════════
# Portfolio Compositor 配置
# ════════════════════════════════════════════════
factor_portfolio_weights: dict = field(default_factory=lambda: {
    # 技术因子（Tactical Layer）
    "di_spread":      1.75,
    "rsi_14":         1.0,
    "stoch_k":        1.0,
    "adx":            0.5,
    "ema_slope":       0.5,
    "supertrend_str":  0.8,
    "atr_ratio":      0.5,
    "bb_width":       0.0,   # 只做过滤器，不参与投票
    "macd_hist":      0.0,   # 只做过滤器
    "keltner_width":   0.3,
    "obv_slope":       0.5,
    "vol_ma_ratio":    0.3,
    "engulfing":       1.0,
    "pin_bar":         0.8,
    "inside_bar":      0.3,

    # 宏观因子（Macro Layer）
    "dxy_corr_20":             0.8,
    "slv_gld_ratio":            0.5,
    "real_yield_chg":           0.5,
    "real_yield_pct_rank":      0.5,
    "gld_tonnes_chg_5d":        0.7,
    "gld_tonnes_chg_20d":       0.7,
    "gld_tonnes_pct_20d":       0.5,
    "gld_tonnes_zscore_60d":    1.0,
    "slv_tonnes_chg_20d":       0.4,
    "silver_gold_holdings_ratio": 0.3,
    "cb_total_chg_3m":          0.8,
    "cb_china_chg_3m":          0.6,
    "cb_russia_chg_3m":         0.4,
    "cb_china_3m_zscore":       0.5,
    "cot_mm_net":               0.8,
    "cot_mm_net_pct_oi":        0.6,
    "cot_mm_net_chg_4w":        0.6,
    "cot_mm_net_zscore_52w":    1.2,
    "cot_pm_net":               0.4,
    "cot_extreme_signal":       1.5,

    # 事件/日历（Macro Layer，低权重）
    "hours_to_fomc":  0.3,
    "hours_to_nfp":   0.3,
    "hour_utc":       0.1,
    "day_of_week":    0.1,
})
factor_portfolio_tactical_alpha: float = 0.7   # 战术层权重
factor_portfolio_macro_alpha: float = 0.3      # 宏观层权重（= 1 - tactical_alpha）
factor_portfolio_signal_threshold: float = 0.4  # 开仓信号阈值

# ════════════════════════════════════════════════
# Adaptive Weight Engine 配置
# ════════════════════════════════════════════════
awe_sensitivity: float = 0.5          # exp(k×score) 中的 k
awe_anchor_pull: float = 0.15         # 每次调整后向 base 混合 15%
awe_max_single_change: float = 0.15   # 单次调整最大变化
awe_weight_min: float = 0.1          # 权重下限
awe_weight_max: float = 3.0          # 权重上限
awe_min_trades: int = 10              # 最少交易笔数才开始调权
awe_adapt_interval: int = 50          # 每 N 笔交易触发一次调权
awe_ic_floor: float = 0.02           # IC 下限（低于此不参与调权）
awe_health_floor: float = 40.0       # 健康分下限（低于此直接禁用）
awe_disable_min_trades: int = 20     # 禁用最少交易笔数
awe_causal_threshold: float = -0.3   # CausalCheck cause_vs_corr 禁用门槛（< -0.3 禁用）
awe_dsr_p_threshold: float = 0.95    # DSR p-value 禁用门槛（> 0.95 视为噪声）
awe_resurrect_health_threshold: float = 60.0  # 复活健康分阈值
awe_resurrect_dsr_p: float = 0.05           # 复活时 DSR p-value 阈值（< 0.05 才可复活）
awe_resurrect_cooldown_days: int = 7          # 禁用冷却天数
awe_max_type_weight_pct: float = 0.40         # 单一类型权重上限 40%

# ════════════════════════════════════════════════
# Execution Gate 配置（从 strategy_* 迁移）
# ════════════════════════════════════════════════
signal_threshold: float = 0.4
risk_pct: float = 0.5
filter_macd_enabled: bool = True
strategy_cooldown_bars: int = 3
strategy_sl_atr: float = 2.0
strategy_tp_atr: float = 3.0
```

---

## 13. 数据持久化

### 13.1 factor_attribution.json（因子归因统计，原子更新）

```json
{
  "rsi_14": {
    "n_trades": 47,
    "n_voted": 45,
    "wins": 27,
    "total_mc": 3.21,
    "avg_mc": 0.071,
    "recent_mcs": [0.05, -0.03, 0.08, ...],
    "ir_short": 0.85,
    "ir_mid": 0.72,
    "ir_long": 0.61,
    "composite_sharpe_score": 0.76,
    "last_trade_ts": 1781350000.0
  }
}
```

### 13.2 factor_trades.jsonl（逐笔归因明细，append-only）

```json
{"ts": 1781350000, "trade_id": 101, "factor": "rsi_14", "signal": 0.65, "mc": 0.054, "pnl_direction": 1, "tags": ["技术", "均值回归"]}
{"ts": 1781350000, "trade_id": 101, "factor": "dxy_corr_20", "signal": -0.31, "mc": -0.026, "pnl_direction": 1, "tags": ["宏观", "美元"]}
```

### 13.3 factor_weight_history.jsonl（权重变更记录）

```json
{"ts": 1781350000, "factor": "rsi_14", "old": 1.0, "new": 1.08, "reason": "ir_score=0.76,wr=60%"}
{"ts": 1781350000, "factor": "gld_tonnes_chg_5d", "old": 0.7, "new": 0.0, "reason": "health=25<_floor"}
{"ts": 1781350000, "constraint": "diversity", "type": "技术", "pct": 0.45, "capped": true}
```

---

## 14. 退役与回退

### 14.1 strategies/ 目录处理

- `multi_factor_m15.py` 等文件保留，标记 `@deprecated("已由 PortfolioCompositor 替代")`
- `strategy/registry.py` 和 `strategy/base.py` 保留，不删除
- Phase 4 上线后，`live_service.py` 不再 import `strategy_registry`

### 14.2 回退方案

| Phase | 上线后验证 | 风险 | 回退 |
|-------|-----------|------|------|
| 1+2+3 | 因子系统取代策略开仓 | 信号质量不如旧策略 | RuntimeConfig 增加 `use_legacy_strategy=True`，一秒切回 |
| 4 | 归因数据可观测 | 无风险（只写日志） | 删掉归因文件 |
| 5+6 | 权重自适应 | 权重突变 | `awe_sensitivity=0` → 冻结权重；`awe_anchor_pull=1.0` → 全部回归基础权重 |

### 14.3 并跑验证

Phase 3 上线后，**并跑 1-2 周**：

```
旧策略 (multi_factor_m15) → 实盘发单（不变）
新因子系统                → DRY-RUN 模式：只算信号，不下单
                            → 每笔 bar 对比两边信号
                            → 记录 CompositeSignal + 旧 Signal 到对比日志
                            → 确认新系统信号不差于旧系统 → 切到 LIVE
```

并行日志格式：

```json
{"ts": 1781350000, "legacy_signal": {"direction": 1, "strength": 2}, "new_signal": {"direction": 1, "score": 0.55, "tactical": 0.62, "macro": 0.39}, "match": true}
```

---

## 15. 因子分类大全与初始配置

### 15.1 战术层因子（Tactical Layer）

| 因子 | 归一化模式 | 初始权重 | 类型标签 | 说明 |
|------|-----------|---------|---------|------|
| di_spread | zscore_tanh(100) | 1.75 | 技术,趋势 | IC 最高 |
| rsi_14 | zscore_tanh(100) | 1.0 | 技术,均值回归 | 自适应阈值 |
| stoch_k | zscore_tanh(100) | 1.0 | 技术,动量 | 自适应阈值 |
| adx | zscore_tanh(100) | 0.5 | 技术,趋势 | 趋势强度 |
| ema_slope | zscore_tanh(50) | 0.5 | 技术,趋势 | EMA 斜率 |
| supertrend_str | zscore_tanh(50) | 0.8 | 技术,趋势 | SuperTrend |
| atr_ratio | zscore_tanh(100) | 0.5 | 技术,波动率 | 波动率比率 |
| macd_hist | **过滤器** | — | — | M15 MACD 反向过滤 |
| bb_width | **过滤器** | — | — | 高波动跳过 |
| keltner_width | zscore_tanh(100) | 0.3 | 技术,波动率 | 通道宽度 |
| obv_slope | zscore_tanh(100) | 0.5 | 量价 | OBV 斜率 |
| vol_ma_ratio | zscore_tanh(100) | 0.3 | 量价 | 成交量异常 |
| engulfing | discrete | 1.0 | 形态,反转 | 吞没形态 |
| pin_bar | discrete | 0.8 | 形态,反转 | 针形反转 |
| inside_bar | discrete | 0.3 | 形态,整理 | 内包整理 |

### 15.2 宏观层因子（Macro Layer）

| 因子 | 归一化模式 | 初始权重 | 类型标签 | direction |
|------|-----------|---------|---------|-----------|
| dxy_corr_20 | rank(100,30) | 0.8 | 宏观,美元 | -1 |
| slv_gld_ratio | rank(100,30) | 0.5 | 宏观,金银比 | 1 |
| real_yield_chg | rank(100,30) | 0.5 | 宏观,利率 | -1 |
| real_yield_pct_rank | rank(100,30) | 0.5 | 宏观,利率 | -1 |
| gld_tonnes_chg_5d | rank(100,30) | 0.7 | 持仓,黄金 | 1 |
| gld_tonnes_chg_20d | rank(100,30) | 0.7 | 持仓,黄金 | 1 |
| gld_tonnes_pct_20d | rank(100,30) | 0.5 | 持仓,黄金 | 1 |
| gld_tonnes_zscore_60d | rank(100,30) | 1.0 | 持仓,黄金,极值 | 1 |
| slv_tonnes_chg_20d | rank(100,30) | 0.4 | 持仓,白银 | 1 |
| silver_gold_holdings_ratio | rank(100,30) | 0.3 | 持仓,金银比 | 1 |
| cb_total_chg_3m | rank(100,30) | 0.8 | 央行,购金 | 1 |
| cb_china_chg_3m | rank(100,30) | 0.6 | 央行,购金 | 1 |
| cb_russia_chg_3m | rank(100,30) | 0.4 | 央行,购金 | 1 |
| cb_china_3m_zscore | rank(100,30) | 0.5 | 央行,购金,极值 | 1 |
| cot_mm_net | rank(100,30) | 0.8 | COT,投机 | 1 |
| cot_mm_net_pct_oi | rank(100,30) | 0.6 | COT,投机 | 1 |
| cot_mm_net_chg_4w | rank(100,30) | 0.6 | COT,投机 | 1 |
| cot_mm_net_zscore_52w | rank(100,30) | 1.2 | COT,投机,极值 | 1 |
| cot_pm_net | rank(100,30) | 0.4 | COT,商业 | -1 |
| cot_extreme_signal | discrete | 1.5 | COT,反转,综合 | 1 |
| hours_to_fomc | discrete | 0.3 | 事件,FOMC | 1 |
| hours_to_nfp | discrete | 0.3 | 事件,NFP | 1 |
| hour_utc | discrete | 0.1 | 日历,时段 | 1 |
| day_of_week | discrete | 0.1 | 日历,周内 | 1 |

---

## 16. 关键公式汇总

```
═══════════════════════════════════════════════════════
信号归一化
═══════════════════════════════════════════════════════
zscore_tanh:  signal = tanh((value - rolling_mean) / rolling_std)
rank_mapping: signal = 2 × (rank_in_window / window_size - 0.5) × direction
discrete:     signal = value_map[str(value)]

═══════════════════════════════════════════════════════
组合公式
═══════════════════════════════════════════════════════
tactical_score = Σ(w_i × s_i) / Σ|w_i|    (技术/量价/形态/GP因子)
macro_score     = Σ(w_j × s_j) / Σ|w_j|    (宏观/持仓/COT/央行/事件)
combined_score  = α × tactical + (1-α) × macro     (α 初始 0.7)
direction        = +1 if score ≥ threshold (0.4)
                  = -1 if score ≤ -threshold
                  =  0 otherwise

═══════════════════════════════════════════════════════
归因（Gram-Schmidt 正交归因为主，线性 MC 为回退）
═══════════════════════════════════════════════════════
【主方法】Gram-Schmidt 正交归因 (来自 alpha/evaluation/attribution.py)
  对 K 个因子按 IC 排序，逐个 Gram-Schmidt 正交化
  对正交化后的因子 f⊥_i 做单变量回归: y = β_i × f⊥_i + ε
  marginal_R²_i = Var(β_i × f⊥_i) / Var(y)
  MC_i = (marginal_R²_i / Σ marginal_R²_j) × trade_pnl
  
  条件: 因子数 ≥ 3 且样本 ≥ 10 笔才使用正交归因

【回退】线性 MC 近似
  MC_i = (signal_i / Σ|signal_j|) × trade_pnl
  
  条件: 因子数 < 3 或样本 < 10 笔或正交化数值失败

═══════════════════════════════════════════════════════
Sharpe 计算与统计检验
═══════════════════════════════════════════════════════
【核心指标】Newey-West HAC Sharpe (来自 execution/_sharpe.py)
  r_t = ln(P_t / P_{t-1})              # log returns (跨期可加)
  lag = ⌊4(T/100)^{2/9}⌋               # Bartlett kernel 自适应窗口
  V̂_HAC = γ̂_0 + 2Σ_{j=1}^{lag} (1 - j/(lag+1)) × γ̂_j    # HAC方差
  SR_NW = (μ_r × √(bars_per_year)) / √V̂_HAC
  
  TF_BARS_PER_YEAR = {M5: 105120, M15: 35040, M30: 17520, H1: 8760, D1: 252}

【综合分数】三层窗口加权
  composite_sharpe_score = 0.5 × SR_NW(50) + 0.3 × SR_NW(100) + 0.2 × SR_NW(250)

【Bootstrap CI】(来自 alpha/evaluation/bootstrap_ci.py)
  B = 1000 次重采样, 计算 Sharpe 的 (1-α)% 置信区间
  如果 CI 包含 0 → 该因子 Sharpe 不显著

【DSR 多重检验】(来自 alpha/calibration.py)
  偏度 SR: SR̂_skew = SR̂ × [(1 - 1/(3T) × γ̂_1 × SR̂ + γ̂_2/(6T) × SR̂²)]
  E[max|SR|_H0] ≈ (2logN)^{1/2} - (1/2)(2logN)^{-1/2} × (γ̂_1/6 - γ̂_2/6 × (2logN)^{1/2})
  DSR p-value = 1 - Φ_c(SR̂_skew, E[max|SR|_H0])  # 正态右尾
  DSR significant if p < 0.05 (原型 I 类错误率控制在 5%)

═══════════════════════════════════════════════════════
因子退役判据（CausalCheck + DSR + 健康分，三选一即禁用）
═══════════════════════════════════════════════════════
禁用条件 (任一满足即禁用):
  1. CausalCheck cause_vs_corr_score < -0.3
     → 正交性检验: lagged OLS residual Pearson p-value
     → 衰减率: early_IC vs late_IC 斜率
     → 综合: tanh((1-p)×3 - d×2 + r×2)
  
  2. DSR p-value > 0.95 (39 因子多重检验后 Sharpe 仍不显著)
     → 该因子的 NW-HAC Sharpe 可能是纯噪声
  
  3. 5维健康分 < 40 (mean_abs_ic 40% + ic_stability 20%
     + regime_consistency 20% + decay_rate 10% + independence 10%)

复活条件 (全部满足):
  1. CausalCheck cause_vs_corr_score > 0 (预测关系正向)
  2. DSR p-value < 0.05 (多重检验后 Sharpe 显著)
  3. health_score > 60
  4. days_since_disabled ≥ 7 (冷却期)
  → 重置为 base_weight × 0.5 (减半起步)

═══════════════════════════════════════════════════════
权重自适应 (AWE)
═══════════════════════════════════════════════════════
composite_sharpe_score = 0.5 × SR_NW(50) + 0.3 × SR_NW(100) + 0.2 × SR_NW(250)
raw_new = old_weight × exp(k × composite_sharpe_score)    [k = awe_sensitivity, 默认 0.5]
new_weight = raw_weight × (1 - anchor_pull) + base_weight × anchor_pull
                                                                             [anchor_pull = 0.15]
clamp: |new - old| ≤ max_single_change (0.15)
clamp: 0.1 ≤ new_weight ≤ 3.0

【离线基准】BlendSearch SLSQP (来自 alpha/search/blend_search.py)
  离线计算最优 Sharpe 权重作为 AWE 的 base_weight 先验：
  max  w'μ / √(w'Σw)           # 最大化组合 Sharpe
  s.t. Σw_i = 1, 0 ≤ w_i ≤ 0.5  # 约束：等权和且单因子上限 50%
  AWE 的 base_weight 默认取 BlendSearch SLSQP 结果，而非手工拍脑袋

多样性约束: 单一类型标签总权重 / 总权重 ≤ 40%
超限时：该类型中 composite_sharpe_score 最低的因子降权 50%

═══════════════════════════════════════════════════════
IC + 健康分 + 统计显著性（三重门控）
═══════════════════════════════════════════════════════
因子参与 AWE 调权:  IC ≥ 0.02  AND  health_score ≥ 40  AND  DSR p < 0.10
因子参与组合信号:  always（权重可为 0）
因子被强制禁用:  CausalCheck cause_vs_corr < -0.3  OR  DSR p > 0.95  OR  health < 40
```

---

## 17. 执行路线图

```
Phase 1 ─── StreamingFactorEngine    无外部依赖
   │        • 改造现有 alpha/factor_engine.py → streaming 模式
   │        • 保持 factor_registry 不变
   │        • NaN/Inf 防御
   ▼
Phase 2 ─── SignalNormalizer         依赖 Phase 1
   │        • 三域归一化
   │        • 滚动窗口预热
   │        • GP 因子自动配置
   ▼
Phase 3 ─── PortfolioCompositor      依赖 Phase 1+2
   │        • Tactical/Macro 两层
   │        • ExecutionGate
   │        • Live loop 改造
   │        • Kill switch: RuntimeConfig.use_legacy_strategy=True
   │        ★ 并跑验证 1-2 周
   ▼
Phase 4 ─── AttributionEngine        依赖 Phase 3（开仓时存归因）
   │        • Gram-Schmidt 正交归因（复用 evaluation/attribution.py）
   │        • 回退到线性 MC 近似（因子 < 3 或样本 < 10）
   │        • 逐笔明细 JSONL
   │        • 无风险（只写日志，不改实盘逻辑）
   ▼
Phase 5 ─── AdaptiveWeightEngine     依赖 Phase 4
   │        • Newey-West HAC Sharpe 三层窗口（复用 execution/_sharpe.py）
   │        • CausalCheck 因果性退役（复用 evaluation/causal_check.py）
   │        • DSR 多重检验退役（复用 calibration.py）
   │        • BlendSearch SLSQP 离线基准（复用 search/blend_search.py）
   │        • Bootstrap CI 置信区间（复用 evaluation/bootstrap_ci.py）
   │        • 多样性约束
   │        • RuntimeConfig 热更新
   │        • 先设 awe_sensitivity=0 观察一周
   │        → 再逐步放开到 0.5
   ▼
Phase 6 ─── 完整闭环
            • GP 新因子自动注册 → SignalNormalizer → AWE
            • 因子退役 → AWE 禁用 → health 复活
            • 前端 dashboard 展示归因和权重变化
```

### 并跑验证阶段（Phase 3 后必做）

| 指标 | 旧策略 | 新系统 | 判断标准 |
|------|--------|--------|---------|
| 信号方向一致率 | — | — | ≥ 70% |
| 信号强度相关性 | — | — | ≥ 0.5 |
| 新系统独立正确率 | — | — | ≥ 旧策略 |
| 冷启动期间信号数 | — | — | 前 50 bars 信号占比 ≥ 旧策略 |

### 上线顺序

```
Week 1: Phase 1 (StreamingFactorEngine)
Week 2: Phase 2 (SignalNormalizer)  
Week 3: Phase 3 (PortfolioCompositor + Live loop)
Week 4-5: 并跑验证，收集对比数据
Week 6: Phase 4 (AttributionEngine, 无风险)
Week 7-8: Phase 5 (AWE, sensitivity=0 观察一周)
Week 9+: Phase 5 放开 sensitivity, 完整闭环
```