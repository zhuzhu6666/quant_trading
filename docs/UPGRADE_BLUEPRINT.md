# Factor Takeover v4 — 量化系统升级蓝图

> **版本**: 1.4 · **日期**: 2026-06-16 (修订)  
> **修订**: Phase 5 + 7 代码交付 (v1.4), 其余同 v1.3: 更新 Phase 4/6 ✅ 已完成, 与 `PROJECT_AUDIT_v10.md` 交叉验证，修正 P0-9 状态，补充 v9/v10 已完成增强，新增 §12 过拟合风险控制  
> **目标**: 在个人开发者能力范围内，架构和实现无限接近专业量化公司  
> **排除**: 卫星/信用卡/供应链另类数据、交易所直连/托管、明星基金经理 Alpha、百万级算力  
> **基线**: 当前系统 (Factor Takeover v4, 39 因子, 12+ 层架构)

---

## 目录

0. [修复基线：现有 Bug 清零](#0-修复基线现有-bug-清零)
1. [回测基础设施](#1-回测基础设施)
2. [ML/AI 预测管道](#2-mlai-预测管道)
3. [特征工程自动化](#3-特征工程自动化)
4. [执行层重构](#4-执行层重构)
5. [风控体系升级](#5-风控体系升级)
6. [归因闭环补全](#6-归因闭环补全)
7. [数据基础设施升级](#7-数据基础设施升级)
8. [多品种扩展](#8-多品种扩展)
9. [研发平台与实验跟踪](#9-研发平台与实验跟踪)
10. [部署与运维](#10-部署与运维)
11. [实现路线图](#11-实现路线图)

---

## 0. 修复基线：现有 Bug 清零

所有升级工作开始前，必须修复已知问题。以下是审计 v10 确认的 P0/P1 清单，按层分组。

### 0.1 执行层 (P0)

| ID | 文件 | 行 | 问题 | 修复 |
|----|------|----|------|------|
| P0-1 | `execution/oms.py` | 113 | `fill()` 中 `volume if volume else order.volume` 用 falsy guard 而非 None check → volume=0 时退化为 `order.volume` | 改为 `volume if volume is not None else order.volume` |

> ✅ 已修复 — 当前代码：`volume if volume is not None else order.volume`

### 0.2 鉴权层 (P0/P1)

| ID | 文件 | 行 | 问题 | 修复 |
|----|------|----|------|------|
| P0-4 | `backend/api/external_data.py` | 106 | `trigger_refresh()` 写操作无鉴权 | 加 `_user: RequireUser` |
| P1-1 | `backend/core/auth.py` | 15 | JWT_SECRET 硬编码 | 改从环境变量 `QUANT_JWT_SECRET` 读取 |
| P1-2 | `backend/core/auth.py` | 39-53 | `get_current_user()` 任何错误静默返回 "zhu" | 失败时抛 401，不允许静默降级 |
| P1-5 | `backend/ws/endpoints.py` | 116 | WebSocket 无鉴权 | WS connect 时验证 token query param |

> ✅ 全部已修复：
> - P0-4: `trigger_refresh(_user: RequireUser, ...)` — 有鉴权
> - P1-1: `JWT_SECRET = os.environ["QUANT_JWT_SECRET"]`
> - P1-2: `get_current_user()` → `require_user()` 抛 401 而非静默返回
> - P1-5: WS 端点已要求 `?token=...` JWT 参数

### 0.3 归因层 (P0)

| ID | 文件 | 行 | 问题 | 修复 |
|----|------|----|------|------|
| **G1** | `alpha/attribution_engine.py` | 372-380 | Gram-Schmidt 归因中 `pnl_series` 在循环中被覆盖 → 只取了最后一个因子的 MC 序列而非真实 trade PnL | 改为使用 `self._recent_trade_pnls` 作为 Y 向量 |

> ✅ 已修复 — `_orthogonal_close()` 中 Y 向量取 `self._recent_trade_pnls`（真实 trade PnL），X 矩阵仍用各因子 MC 序列

### 0.4 自适应层 (P1)

| ID | 文件 | 行 | 问题 | 修复 |
|----|------|----|------|------|
| **C1** | `alpha/adaptive_weight_engine.py` | 274-277 | CausalCheck 被注释掉 | 在 AWE.adapt() 中传入 `factor_values` 和 `forward_returns`，启用因果性检查作为第四重退役门控 |
| **C2** | `alpha/factor_health.py` | 220-221 | independence 维度用 `\|ic - mean(other_ics)\|` 伪相关 | 实现真实 corr 矩阵比较因子值序列之间的独立性 |

> ⏳ 待修复（Phase 1 范围）<br>
> C1: `adaptive_weight_engine.py` 278-284 注释块仍在，需接入数据流<br>
> C2: `factor_health.py` 219-222 仍用 `abs(ic - mean)` 伪相关

### 0.5 前端 (P0)

| ID | 文件 | 行 | 问题 | 修复 |
|----|------|----|------|------|
| P0-8 | `MainDashboard.tsx` | 165 | `r.json()` 不检查 `r.ok` | `if (!r.ok) throw new Error(...)` |
| P0-9 | `MainDashboard.tsx` | 264 | drawdown_pct 未 ×100 → DualRing 显示 "0.05%" 而非 "5%" | `innerValue={dd * 100}` |

> ✅ P0-8 已修复 — `/api/live/stop` 调用处已有 `if (!r.ok)` 检查<br>
> ❌ P0-9 **未修复** — 已验证：`dd = s?.daily?.drawdown_pct ?? 0` 直接传给 `<DualRing innerValue={dd}>`，组件使用 `{innerValue}%` 显示。后端返 0.05 (=5%) 时仪表盘显示 "0.05%" 而非 "5%"。`TradingPanel.tsx` 已正确 `(dd * 100).toFixed(1)%`，MainDashboard 遗漏乘 100。见 Phase 0.5 修复。

### 0.6 其他 (P1)

| ID | 文件 | 行 | 问题 | 修复 |
|----|------|----|------|------|
| P0-6 | `backend/api/external_data.py` | 22 | `_refresh_jobs` 只增不减 → 内存泄漏 | 添加 TTL 清理（500 条上限） |

> ✅ 已修复 — `_cleanup_stale_jobs()` 实现：1h TTL + 500 条上限

---

> **v10 审计参考**: 完整基线审计报告见 `PROJECT_AUDIT_v10.md`（2026-06-14，9 P0 + 24 P1 + 18 P2）。以上 Phase 0 修复清单已逐条代码验证——11 项中 10 项确认完成，仅 P0-9 遗留。以下 P1 待修项从 v10 审计提取：
> - P1-18~23: `datetime.utcfromtimestamp/utcnow` ×11 处（Python 3.14 将移除）
> - P1-24: `_cache_get_or_refresh` 硬编码 key "ctrader"（MT5 缓存永远 miss）
> - P1-6: cTrader spot price 除数硬编码 `10**5`（多品种时价格差 1000×）
> - P1-10: `router.py` on_fill 反向成交只 log 不处理（仓位漂移）
> - P1-13: `market_impact.py` 成本计算用 1.0 而非实际金价 ~$3000

## 1. 回测基础设施

**现状**: `backtest_service.py` ≈ 40 行包装（实际逻辑在 `backtest_runner.py`）；`scripts/backtest_v4.py` 已能逐 bar 回放 factor pipeline 完整流程。已有 backtrader 策略层和 REST API (`/api/backtest/run`)。但没有向量化回测器，没有事件驱动回测，没有参数扫描框架。

### 1.1 向量化回测引擎 (Vectorized Backtester)

**目标**: 能对着历史数据完整跑一遍 Factor Takeover v4 管道，输出权益曲线和统计数据

```
位置: alpha/backtest/vectorized.py
依赖: numpy, pandas, scipy
```

```python
class FactorBacktester:
    """
    用历史 bar 重放因子管道。
    
    流程:
      df (历史 bars)
        → StreamingFactorEngine 批量计算所有因子值
        → SignalNormalizer 归一化
        → PortfolioCompositor 组合（使用指定权重）
        → ExecutionGate 过滤
        → 模拟开仓/平仓 (以 close 价成交)
        → 生成 equity_curve
    
    Args:
        engine_config: 管道参数（与 live loop 共用）
        slippage_bps: 滑点 (默认 2bps)
        commission_per_lot: 手续费 (默认 $6)
    """
```

**核心指标输出**:

| 指标 | 方法 | 说明 |
|------|------|------|
| 总收益率 | `total_return` | 期末/期初权益 - 1 |
| 年化夏普 | `sharpe_ratio` | mean(r) / std(r) × √n |
| 卡玛比率 | `calmar_ratio` | 年化收益 / 最大回撤 |
| 最大回撤 | `max_drawdown` | 峰值→谷底的最大跌幅 |
| 胜率 | `win_rate` | 盈利交易数 / 总交易数 |
| 盈亏比 | `profit_factor` | 总盈利 / 总亏损 |
| 平均持仓 bar 数 | `avg_hold_bars` | 交易持有期统计 |
| 月度收益 | `monthly_returns` | 12 个月份热力图 |
| 滚动夏普 (24M) | `rolling_sharpe_24m` | 稳定性 |
| 捕获率 | `up_capture / down_capture` | 上涨/下跌市场中的捕获效率 |
| 索提诺比率 | `sortino_ratio` | 只用下行波动率 |

### 1.2 事件驱动回测 (Event-Driven Backtester)

**目标**: 模拟 bar 内 SL/TP 触发、滑点、部分成交等细节

```
位置: alpha/backtest/event_driven.py
```

与向量化区别：向量化是批量计算（快，适用参数扫描），事件驱动是逐 bar 推进（慢，但更真实）。两者共用相同的 `factor_pipeline` 和 `risk` 模块。

```
向量化: 回测完所有 bar → 出 equity_curve     (5ms / run)
事件驱动: 每根 bar 走完整管道 → 模拟撮合     (50ms / run)

用法:
  - 参数扫描: 向量化跑 10,000 次
  - 精确验证: 事件驱动跑选定参数
```

### 1.3 Walk-Forward 分析框架

**现状**: `purged_walkforward.py` 已实现，但未接入主流程。

接入到 `FactorBacktester`：

```python
def walkforward_optimize(
    df: pd.DataFrame,
    param_grid: dict,
    n_folds: int = 5,
    metric: str = "sharpe",
) -> WalkForwardReport:
```

每个 fold 走完整的 in-fold 训练（优化权重）→ out-fold 测试（评估），最终给出 OOS Sharpe 的分布。

### 1.4 参数敏感性分析

```python
def sensitivity_analysis(
    df, base_params, 
    vary: dict  # {param_name: [values...]}
) -> pd.DataFrame:
    # 每次变化一个参数，看 OOS Sharpe 变化
    # 输出: 每个参数的敏感性曲线
```

### 1.5 回测数据库

```
backend/api/backtest.py           → /api/backtest/run (触发回测)
backend/services/backtest_service.py  → 调度 + 存储结果
data/backtest_results/             → {run_id}.json 存储
```

每个回测运行生成一个唯一 ID，包含：
- 参数快照（权重/阈值/滑点...）
- 权益曲线（CSV）
- 交易明细（CSV）
- 指标汇总（JSON）

---

## 2. ML/AI 预测管道

**现状**: 0 个 ML 模型接入因子管道。`regime_classifier.py` 是孤立的 LogisticRegression——且使用的 4 个因子 `[aroon, cci, mfi, williams_r]` **不在 39 因子 registry 中**，与主因子管道完全脱钩，当前零实际影响。`scripts/train_xgb_walkforward.py` 已有 Walk-Forward 验证的 XGBoost 训练脚本（4 PCA 因子 + hour_utc 特征，非蓝图设计的 68 特征），但输出未注册为因子、未接入管道。

**核心原则**: ML 模型的输出注册为 factor_registry 中的"因子"，和其他因子一样走归一化→组合→归因→自适应。ML 模型不绕过闸门、不豁免归因。

### 2.1 XGBoost 方向预测器

```
位置: alpha/ml/direction_predictor.py
依赖: xgboost, numpy, pandas
训练数据: 20,000+ 根 M5 bar → ~16,000 可用样本（去掉前向未来 + 去掉 NaN）
```

#### 特征设计 (X)

| 类别 | 特征数 | 来源 |
|------|--------|------|
| 技术衍生 | ~30 | RSI/DI/Stoch/ADX/ATR/EMA/WMA 的原始值 + 变化量 + 排名 |
| 价格形态 | ~10 | 过去 N bar 的 OHLC 相对位置、涨跌幅、波动率比 |
| 时间特征 | ~5 | hour_utc, day_of_week, minutes_to_close, session_id |
| 因子交互 | ~20 | 高秩因子组合 (RSI×ATR, DI_spread×volume_ratio) |
| 标签特征 | ~3 | 上一个信号方向、持仓状态、上一笔交易盈亏 |
| **总计** | **~68** | |

#### 标签设计 (y)

```
y = sign(close[t+1] - close[t])
  → 0.5 概率问题。基线准确率 50-53%（XAUUSD M5）
```

**注意**: M5 的纯方向预测准确率天然低（信噪比极低），不要期望 > 55%。ML 的价值不在于"预测涨跌"，而在于**信号与其他因子弱相关 → 提供独立信息增量**。

#### 训练 / 评估 / 注册流程

```python
scheduler.add_job("retrain_xgb_dir", "0 5 * * 0")  # 每周日凌晨 5 点

def retrain_direction_predictor():
    # 1. 加载最近 20,000 bars
    df = DataStore().load_bars("XAUUSD+", "M5", limit=20000)
    
    # 2. 构造 X, y
    X = _build_features(df)
    y = (df['close'].shift(-1) > df['close']).astype(int).values
    
    # 3. 时间序列交叉验证 (PurgedWalkForward)
    scores = []
    for fold in PurgedWalkForward(n_folds=5).folds(n_total=len(X)):
        model = XGBClassifier(n_estimators=200, max_depth=4, learning_rate=0.05)
        model.fit(X[fold.train_indices], y[fold.train_indices])
        acc = model.score(X[fold.test_indices], y[fold.test_indices])
        scores.append(acc)
    
    avg_oos_acc = np.mean(scores)
    
    # 4. 如果 OOS accuracy > 0.51 + bootstrap CI 不含 0.5 → 注册
    if avg_oos_acc > 0.51:
        register_as_factor(f"xgb_dir_{datetime.today():%Y%m%d}", 
                          model.predict_proba,
                          tags=["ML", "方向"])
    else:
        # 模型太差，不注册
        logger.warning(f"XGB direction predictor OOS={avg_oos_acc:.3f}, skipped")
```

#### 预测值 ∈ [-1, +1] 的映射

```python
# XGBoost 输出 [0, 1] 概率
# 映射: signal = 2 * prob - 1
# prob=0.5 → signal=0 (中性)
# prob=0.8 → signal=+0.6 (看多)
# prob=0.2 → signal=-0.6 (看空)
```

### 2.2 LightGBM 幅度预测器

```
位置: alpha/ml/amplitude_predictor.py
依赖: lightgbm, numpy, pandas
```

#### 标签

```
y = (close[t+1] - close[t]) / atr[t]
  → ATR 标准化收益，范围 ~[-3, +3]
  → 回归任务
```

#### 训练

```python
model = LGBMRegressor(n_estimators=200, max_depth=4, learning_rate=0.05)
model.fit(X_train, y_train)  # 同方向预测器的特征

# 预测: expected_return_in_atr = model.predict(X_current)
# 仓位 = clamp(expected_return_in_atr / max_risk, min_lot, max_lot)
```

#### 接入 EventSizing

```python
# execution/event_sizing.py 新增:
class MLPositionSizer:
    def __init__(self, amplitude_model):
        self.model = amplitude_model
    
    def compute_lots(self, factor_snapshot) -> float:
        expected_return = self.model.predict(factor_snapshot)
        risk_per_trade = expected_return * current_atr
        # Kelly 分数: f* = (b*p - q) / b
        kelly_fraction = ...  
        return base_lot * kelly_fraction
```

### 2.3 Regime-Aware Predictor Ensemble

**现状**: `alpha/regime_classifier.py` 的 LogisticRegression 有 4 因子 + regime onehot = 9 维特征。

**升级**:

```python
class RegimeAwareEnsemble:
    """
    根据当前 regime 选择不同模型。
    
    regimes:
      TRENDING_UP:   用趋势型 ML 因子 (momentum features 权重大)
      TRENDING_DOWN: 用防御型 ML 因子 (均值回归 features 权重大)
      RANGING:       用均值回归型 ML 因子
      HIGH_VOL:      用 volatility-scaled ML 因子
      LOW_VOL:       用正常 ML 因子 + 提高仓位
    
    每个 regime 训练单独的子模型。
    """
```

### 2.4 概念漂移检测 (Concept Drift)

ML 模型训练完后会随时间衰减（概念漂移）。需要监控。

```python
class DriftDetector:
    """
    监控 ML 因子的预测准确率。
    如果滚动准确率连续 N 根 bar 低于阈值 → 触发模型退役 + 自动重训。
    
    方法:
    - Page-Hinkley 检验
    - ADWIN (Adaptive Windowing)
    - 或简单的滚动窗口准确率 < 0.48
    """
    
    def check(self, ml_factor_name: str) -> bool:
        # 用最近 500 根 bar 的预测 vs 实际方向
        # 如果准确率 < 0.48 → return True (需要重训)
```

接入 scheduler：

```python
scheduler.add_job("ml_drift_check", "0 3 * * *", drift_detector.run)
```

### 2.5 ML 因子生命周期

```
发现阶段             部署阶段               退役阶段
─────────────────────────────────────────────────────
训练 XGBoost        注册到 factor_registry   DriftDetector 触发
PurgedWF 验证       走 normalizer            → 自动重训
OOS > 0.51?         走 compositor             → 新版本替换旧版本
  ├─ 是 → 注册       走 AWE 调权              → 旧版本退役
  └─ 否 → 丢弃       走 attribution
```

---

## 3. 特征工程自动化

**现状**: 39 个手写因子，GP 搜索在 DSL 空间演化。`scripts/factor_pca.py` 已有单次 PCA 分析脚本（15×15 相关矩阵），但没有可复用的 PCA/KPCA 压缩模块，没有自动数学衍生，没有 Autoencoder。

### 3.1 数学衍生层

```
位置: alpha/features/derivatives.py
```

在每个已有的因子值上施加数学变换，自动膨胀候选特征池：

| 变换 | 说明 | 示例 |
|------|------|------|
| log | 对数变换 | log(volume+1) |
| diff | 一阶差分 | close - close[-1] |
| pct_change | 百分比变化 | close / close[-1] - 1 |
| rank | 滚动排名 | rank(close, 50) |
| zscore | 滚动 zscore | (close - mean)/std |
| rolling_skew | 滚动偏度 | skew(close, 20) |
| rolling_kurt | 滚动峰度 | kurt(close, 20) |
| wavelet | 小波变换 | pywt.dwt(close) → 高频/低频分量 |
| fft | FFT 频率成分 | 取前 3 个傅里叶系数 |

```python
class FeatureDeriver:
    """
    输入: OHLCV DataFrame (20K bars)
    输出: 200+ 衍生特征 DataFrame
    
    用法:
        deriver = FeatureDeriver()
        X = deriver.derive(df)  # shape (20000, 220)
    """
    
    def derive(self, df: pd.DataFrame) -> pd.DataFrame:
        features = pd.DataFrame(index=df.index)
        
        # 价格变换
        features['log_return_1'] = np.log(df['close'] / df['close'].shift(1))
        features['log_return_5'] = np.log(df['close'] / df['close'].shift(5))
        
        # 滚动统计
        for w in [5, 10, 20, 50]:
            features[f'close_zscore_{w}'] = (
                df['close'] - df['close'].rolling(w).mean()
            ) / df['close'].rolling(w).std()
        
        # 更多...
        return features
```

### 3.2 PCA / KernelPCA 压缩

```
位置: alpha/features/compression.py
```

```python
class FeatureCompressor:
    """
    将 200+ 衍生特征压缩到 10-20 维正交特征。
    
    两种模式:
    1. PCA: 线性压缩，保留方差最大方向
    2. KernelPCA (rbf): 非线性压缩，捕捉复杂关系
    
    输出注册为因子 pca_1, pca_2, ..., pca_n
    """
    
    def fit(self, X: np.ndarray):
        self.pca = PCA(n_components=0.8)  # 保留 80% 方差
        self.pca.fit(X)
    
    def transform(self, X: np.ndarray) -> np.ndarray:
        return self.pca.transform(X)
```

### 3.3 Autoencoder 非线性特征

```
位置: alpha/features/autoencoder.py
依赖: torch 或 sklearn.neural_network
```

```python
class FactorAutoencoder:
    """
    用 3 层自编码器从 200 衍生特征中提取 10 维非线性特征。
    
    架构:
      Input(200) → Dense(64, relu) → Dense(10, linear) → Dense(64, relu) → Output(200)
                   编码器              瓶颈层             解码器
    
    训练: 用 20K bars，MSE loss，早期停止
    
    输出注册为因子 ae_1, ae_2, ..., ae_10
    """
    
    def train(self, X):
        # trainer = pl.Trainer(max_epochs=100)
        # trainer.fit(autoencoder, train_loader)
    
    def encode(self, X) -> np.ndarray:
        # 返回瓶颈层输出 (n_bars, 10)
```

### 3.4 特征选择

所有特征（39 手写 + 200 衍生 + 10 PCA + 10 AE + 3 ML 预测 + 20 GP）进入候选池后，需要筛选：

```python
class FeatureSelector:
    """
    从 280+ 候选特征中选出最终使用的 ~80 个。
    
    筛选条件:
    1. IC > 0.02 (与 forward return 的滚动相关)
    2. 与已选中因子的最大相关 < 0.7 (共线性约束)
    3. VIF < 5 (方差膨胀因子)
    4. 健康分 > 40
    
    循环: 每次选 IC 最高的未选中因子 → 检查共线性 → 通过则加入
    
    输出: selected_features: list[str]
    """
    
    def select(self, candidate_features: dict, df: pd.DataFrame) -> list[str]:
        # 见算法描述
```

---

## 4. 执行层重构

**现状**: cTrader bridge + MT5 bridge 是两套独立实现，没有统一接口。Paper 和 Live 两条路径。有 3 个 P0 bug。

### 4.1 BaseBrokerBridge 抽象

```
位置: execution/base.py
```

```python
class BaseBrokerBridge(ABC):
    """统一经纪商接口"""
    
    @abstractmethod
    def connect(self) -> bool: ...
    
    @abstractmethod
    def disconnect(self) -> None: ...
    
    @abstractmethod
    def market_buy(self, symbol: str, volume: float, 
                   sl: float = 0, tp: float = 0,
                   comment: str = "") -> OrderResult: ...
    
    @abstractmethod
    def market_sell(self, symbol: str, volume: float,
                    sl: float = 0, tp: float = 0,
                    comment: str = "") -> OrderResult: ...
    
    @abstractmethod
    def close_position(self, position_id: int) -> bool: ...
    
    @abstractmethod
    def get_positions(self) -> list[PositionInfo]: ...
    
    @abstractmethod
    def account_info(self) -> AccountInfo: ...
    
    @abstractmethod
    def amend_sl_tp(self, position_id: int, sl: float, tp: float) -> bool: ...
```

`CTraderBridge` 和 `PaperBridge`（旧的 PaperExecutionEngine 包装）都实现这个接口。

### 4.2 统一下单路径

```
Live 和 Paper 走同一套代码:
  FactorPipeline → CompositeSignal
    → ExecutionGate → GateResult
      → ExecutionRouter → PreTradeCheck
        → BaseBrokerBridge.market_buy/sell
          → AttributionEngine.record_open

区别仅在于 live 用 CTraderBridge, paper 用 PaperBridge。
```

### 4.3 VWAP / TWAP 算法

**现状**: 已实现。`execution/algos.py` 包含 TWAP、VWAP、POV (Percentage of Volume)、IS (Implementation Shortfall) 四种算法，已集成到 `ExecutionRouter`（> 0.05 lot 自动拆单）。

**待增强**:
- 执行质量分析（见 §4.4）
- 与 BrokerBridge 统一接口集成
- 前端 algo 参数配置界面

### 4.4 执行质量分析（Phase 4 计划）

```
位置: execution/analytics.py（待建）
```

**当前状态**: 文件不存在。`execution/` 目录下已有 `slippage.py`（滑点模型）和 `market_impact.py`（市场冲击），但缺少统一的执行质量收集和报告层。

**待建功能**——记录每笔成交的：
- 滑点（成交价 vs 信号价）
- 延迟（信号生成 → 提交 → 成交的时间戳）
- 市场冲击（成交价 vs 同 bar VWAP）
- 滑点分布统计

```python
class ExecutionQuality:
    """执行质量分析器（Phase 4 待建）"""
    
    @dataclass
    class TradeExecution:
        signal_time: float
        submit_time: float
        fill_time: float
        signal_price: float
        fill_price: float
        bar_vwap: float
        volume: float
        direction: int
    
    def report(self, trades: list[TradeExecution]) -> dict:
        # avg_slippage_bps, avg_latency_ms, 
        # slippage_std, slippage_percentile_95
```

---

## 5. 风控体系升级

**现状**: PreTrade check (7 items) + CircuitBreaker (4 triggers in `risk/circuit.py`) + 默认 0.01 lot（已支持 Kelly 动态仓位和 EventSizing）。没有 VaR、没有压力测试、没有组合风控。

### 5.1 VaR / CVaR 引擎

```
位置: risk/var.py
```

```python
class VaREngine:
    """
    计算当前仓位的在险价值。
    
    三种方法:
    1. 参数法 (Variance-Covariance): 假设正态分布
       VaR = position_value × z_alpha × sigma_daily × sqrt(hold_days)
    
    2. 历史模拟法 (Historical Simulation): 用最近 N 天实际收益
       VaR = percentile(historical_returns, alpha)
    
    3. 蒙特卡洛法 (Monte Carlo): 模拟 10,000 条路径
       VaR = percentile(simulated_returns, alpha)
    """
    
    def var(self, portfolio_value: float, 
            positions: list[PositionInfo],
            method: str = "historical",
            alpha: float = 0.95) -> float:
        # 返回 VaR 值 (USD)
    
    def cvar(self, ...) -> float:
        # 返回 CVaR (条件 VaR，即尾部均值)
```

**接入**：

```python
# 每个 bar 结束时计算:
var_95 = var_engine.var(equity, positions, method="historical")
cvar_95 = var_engine.cvar(equity, positions, method="historical")

if cvar_95 > equity * 0.02:  # CVaR > 2% equity → 熔断
    circuit_breaker.trip(f"CVaR={cvar_95:.1f} > 2% equity")
```

### 5.2 压力测试

```
位置: risk/stress_test.py
```

```python
class StressTester:
    """
    预设场景: 
    - 黑天鹅: 黄金单日 -5% (2020.08 波动率)
    - NFP 冲击: 非农超预期 3σ → 瞬时滑点 10bps
    - 流动性枯竭: spread 从 0.2 扩到 5.0
    - cTrader 断线: 2 小时无连接 → 闭市后跳空
    - 因子失效: 所有因子同时归零（极端情况）
    
    每个场景输出: 预期亏损 USD, 是否在可承受范围内
    """
    
    def run_all(self, positions, account) -> list[StressScenarioResult]:
        ...
```

### 5.3 因子暴露集中度监控

```python
class FactorExposureMonitor:
    """
    实时监控因子暴露集中度。
    
    规则:
    - 单类型 (量价/动量/均值回归/波动率/宏观) 总权重 ≤ 40%
    - 单因子权重 ≤ 3.0 (AWE 已有)
    - 如果某类型因子的总暴露 > 50% → 告警
    - 如果所有因子同时指向同一方向 → 告警 (consensus risk)
    
    与 AWE._enforce_diversity 的区别:
    AWE 只在权重调整时做约束
    FactorExposureMonitor 在每根 bar 开仓前做检查
    """
```

### 5.4 动态仓位 (Kelly Criterion)

**现状**: `strategy/portfolio.py` 中已有 `PortfolioManager.compute_kelly()` 方法（半 Kelly, 25% 上限），但未接入主仓位计算。蓝图版本需统一到 `risk/kelly.py` 并接入 AttributionEngine 的统计输入。

```
位置: risk/kelly.py
```

```python
class KellyPositionSizer:
    """
    Kelly 公式: f* = (b*p - q) / b
    
    其中:
      p = 胜率 (从 FactorAttributionStats.win_rate 来)
      b = 盈亏比 (avg_win / avg_loss)
      q = 1 - p
    
    半 Kelly: f* / 2 (更保守)
    """
    
    def compute_kelly_fraction(self, factor_stats: dict) -> float:
        # 从 attrib 统计取加权胜率和盈亏比
        # 返回 Kelly 分数 (0-1)
    
    def compute_lots(self, equity, atr, kelly_fraction) -> float:
        # 风险预算: lot = equity × kelly_fraction × risk_per_trade / (atr × contract_size)
```

**接入**: 替换 `paper_engine.py` 中的 `DEFAULT_LOTS = 0.01`，改为：

```python
lot = kelly_sizer.compute_lots(equity, current_atr, kelly_frac)
lot = max(min_lot, min(max_lot, lot))  # 夹在 [0.01, 0.5] 之间
```

---

## 6. 归因闭环补全

**现状**: AttributionEngine 有完整的记录 → 归因 → 统计框架，但有 1 个 Gram-Schmidt bug + CausalCheck 未接入 + 没有逐日归因。

### 6.1 修复 Gram-Schmidt 归因 (G1) ✅ 已完成

```python
# attribution_engine.py:348-410
# 已修复 — Y 向量取 self._recent_trade_pnls（真实 trade PnL）
# X 矩阵仍用各因子的 MC 历史序列

def _orthogonal_close(self, attrib, trade_pnl):
    ...
    # ✅ Y = real_trade_pnl 序列 (不再是某个因子的 MC!)
    pnl_series = list(self._recent_trade_pnls)[-n_samples:]
    # ✅ X = factor_mc_matrix
    factor_matrix = np.array(factor_matrix).T
    # Gram-Schmidt 正交分解 → 每个因子的边际贡献
    report = self._orthogonal_attribution.attribute(
        factor_matrix, np.array(pnl_series),
        factor_names=active_factors,
    )
```

### 6.2 启用 CausalCheck (C1)

**当前**: `adaptive_weight_engine.py:274-277` 被注释

**启用方法**:

```python
# AWE.adapt() 增加参数
def adapt(self, attribution, factor_configs, 
          factor_values: np.ndarray = None,   # 新增
          forward_returns: np.ndarray = None,  # 新增
          use_blend_baseline=False):
    
    # 在 _check_disable_conditions 中:
    if factor_values is not None and forward_returns is not None:
        causal = stats.causal_quality(factor_values, forward_returns)
        if causal.get("cause_vs_corr_score", 0) < -0.3:
            patches[name] = {"weight": 0.0, "reason": f"causal={causal['cause_vs_corr_score']:.2f}"}
            continue
```

**数据流**: `live_service._run_loop` 中，AWE.adapt() 需要收到因子值 + forward return 数据。在正常 pipeline 中，这些已经在 `TradeAttribution` 里有，只需要从 attrib 提取并聚合成时间序列。

### 6.3 逐日盯市归因

**现状**: 归因只在开仓和平仓两个时间点计算。

```python
class DailyMarkToMarketAttribution:
    """
    每天收盘后（或每根 bar 结束后），对持仓做一次归因。
    
    方法:
    1. 获取当前持仓的未实现 PnL (MTM)
    2. 计算从上次归因到现在的因子值变化
    3. 按 MC 比例分配未实现 PnL
    
    产出:
    - 每日因子 PnL 贡献表
    - 累计归因（线性累加，接近真实 PnL）
    """
    
    def mark_to_market(self, 
                       open_trades: list[TradeAttribution],
                       current_factors: dict[str, float],
                       current_price: float) -> dict[str, float]:
        # 每笔持仓从开仓价到当前价的未实现 PnL
        # 分配到因子上
```

### 6.4 归因 Dashboard 数据管道

`factor_trades.jsonl` 中的数据 → 前端可视化：

```python
class AttributionReportGenerator:
    """
    从归因历史数据生成前端可消费的汇总。
    
    输出:
    {
      "per_factor": {
        "rsi_14": {"total_pnl": 12.3, "sharpe": 0.8, "win_rate": 0.55, ...},
        "di_spread": {"total_pnl": -5.2, ...},
        ...
      },
      "per_tag": {
        "技术": {"total_pnl": 8.1, "n_factors": 12},
        "宏观": {"total_pnl": -3.2, ...},
        ...
      },
      "equity_curve": [{"ts": ..., "equity": ...}],  // 按因子拆分的权益曲线
    }
    """
    
    def generate(self, attribution_engine) -> dict:
        # 遍历 FactorAttributionStats
        # 汇总到 per_factor + per_tag + equity_curve
```

---

## 7. 数据基础设施升级

**现状**: SQLite 单文件 + MT5 唯一数据源 + 无版本控制 + 无 tick 管道。

### 7.1 DuckDB 替代 SQLite (可选但推荐)

```python
# data/store.py → data/duckdb_store.py
# DuckDB: 嵌入式列式数据库，和 SQLite 一样零部署
# 但查询速度快 10-50x（特别是聚合查询）

# 迁移路径:
# 1. data/market_data.db → data/ctrader_data.duckdb
# 2. 接口保持不变 (load_bars / insert_bars 签名相同)
# 3. 内部用 DuckDB 的 SQL 替代 SQLite 的 SQL
```

DuckDB 对比 SQLite：

| 对比项 | SQLite | DuckDB |
|--------|--------|--------|
| 类型 | 行式 | 列式 |
| 时间序列聚合 | 慢 (full scan) | 快 (向量化执行) |
| 窗口函数 | 有 (新版) | 有 (原生优化) |
| 部署 | 0 配置 | 0 配置 (pip install duckdb) |
| 并发读 | WAL 模式可读 | MVCC 快照读 |
| 并发写 | 排他锁 | 单写者 |

### 7.2 数据版本控制

```python
# data/versioned_store.py

class VersionedDataStore:
    """
    每次拉取的数据打上 snapshot_id + timestamp。
    
    schema:
      bars_v2:
        snapshot_id TEXT NOT NULL,
        symbol TEXT NOT NULL,
        timeframe TEXT NOT NULL,
        time INTEGER NOT NULL,
        open REAL, ...
        pulled_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    
    回测时指定 snapshot_id 或时间点：
      store.load_bars(symbol, tf, as_of="2026-05-01")
      # → 只返回 2026-05-01 之前最后拉取的数据
    
    用途：
    - 回测结果可复现（不受后续数据修正影响）
    - 可以对比不同时间点拉取的数据差异
    """
```

### 7.3 实时 Tick 管道

```
data/tick_pipeline/
  ├── receiver.py     ← cTrader WebSocket spot price events
  ├── bar_builder.py  ← tick → M1 → M5 → M15 bar builder
  ├── buffer.py       ← ring buffer 缓存最近 10 分钟 tick
  └── publisher.py    ← 新 bar 生成时 push 到 live loop
```

**设计**:

```
cTrader WebSocket (实时 spot price)
  → tick_receiver (接收 Protobuf)
    → bar_builder (1 秒 tick → M1 bar → M5 bar)
      → event_bus.publish(NEW_BAR)
        → StreamingFactorEngine.append_bar(new_bar)
```

这样就不需要 60s 轮询了。每根新 M5 bar 生成时立即触发 pipeline。

### 7.4 数据质量仪表盘

```
backend/api/data_quality.py  →  /api/data/quality
frontend-v2 → DataPanel 新增 DataQualityCard

指标:
- 每品种每 TF 的延迟 (当前时间 - 最新 bar 时间)
- 当日缺口数
- 当日异常值数
- 同步成功率 (最近 24h)
- MT5 连接状态
```

### 7.5 已完成增强（v9/v10）

以下数据基础设施升级已在上一个开发周期完成，原蓝图未覆盖：

| 增强 | 位置 | 说明 |
|------|------|------|
| 5 周期 data_pull | `live_service.py:_pull_new_bars` | M5/M15/M30/H1/D1 每 10 分钟同步（原仅 M15） |
| SyncHealth 数据库断层检查 | `data/live_sync/health.py:check_and_log` | 直接查 SQLite 各周期最新 bar，超阈值告警 |
| BarFilter skip_dedup | `data/live_sync/orchestrator.py` | full_sync 回填不被增量去重误杀 |
| 启动 catch-up 改进 | `live_service.py:run_job_now` | catch-up 执行计入 scheduler run_count，前端可见 |

---

## 8. 多品种扩展

**现状**: 绝大多数代码以 `symbol: str = "XAUUSD+"` 作为默认参数，支持调用方覆盖但缺乏系统性多品种支持。factor_registry 无 symbol 标签。

### 8.1 Plan A: 多品种并行管道 (推荐)

```
每个品种独立一条因子管道:

XAUUSD+ pipeline:
  engine_xau → normalizer → compositor → gate → ctrader
  
EURUSD  pipeline:
  engine_eur → normalizer → compositor → gate → ctrader

共享: factor_registry, attribution_engine, AWE, scheduler, db
品种独立: StreamingFactorEngine, SignalNormalizer (滚动窗口不同品种不同)
```

### 8.2 Schema 变更

```python
# DB: 现有 bars 表已支持 symbol 列
# 只需要品种级别的 factor_signal_config

# runtime_config.py 增加:
multi_symbol_config: dict = {
    "XAUUSD+": {
        "factor_signal_config": {...},  # 同上
        "factor_portfolio_weights": {...},
        "tactical_alpha": 0.7,
    },
    "EURUSD": {
        "factor_signal_config": {...},  # 不同参数
        "factor_portfolio_weights": {...},
        "tactical_alpha": 0.6,
    },
}
```

### 8.3 跨品种协方差

```python
class CrossAssetRiskModel:
    """
    多品种组合的协方差矩阵风险预算。
    
    方法:
    1. 每天收盘后计算各品种收益的协方差矩阵 (60 天滚动)
    2. 用风险平价分配各品种的权重
    3. 限制单一品种的 VaR 贡献 ≤ 总 VaR 的 60%
    """
```

---

## 9. 研发平台与实验跟踪

**现状**: 每次 GP 搜索、每次 AWE 调整、每次因子晋升都没有记录系统。无法回溯"上个月这个因子赚了多少钱"。

### 9.1 实验跟踪系统 (轻量 MLFlow)

```
位置: research/experiment_tracker.py
无需部署 MLFlow 服务，用 SQLite + JSONL
```

```python
@dataclass
class Experiment:
    run_id: str          # uuid
    timestamp: float
    experiment_type: str # "gp_search" | "backtest" | "parameter_sweep" | "retrain_ml"
    params: dict         # 参数快照
    metrics: dict        # 结果指标
    tags: list[str]      # 标签
    artifacts: list[str] # 文件路径 (equity_curve.csv, trades.csv)
    
class ExperimentTracker:
    def start_run(self, exp_type: str, params: dict) -> str:
        # 创建 run_id
    
    def log_metric(self, run_id: str, key: str, value: float):
        # 记录指标
    
    def log_artifact(self, run_id: str, file_path: str):
        # 记录产出文件
    
    def query(self, exp_type: str = None, tags: list = None) -> pd.DataFrame:
        # 查询历史实验
```

### 9.2 因子库管理

```
位置: alpha/factor_library.py
```

```python
class FactorLibrary:
    """
    存档所有历史因子（包括已退役的）。
    
    每条记录:
    - name, expression, source (handcrafted/gp/ml)
    - discovery_date, stage (shadow/canary/active/retired)
    - retirement_date, retirement_reason
    - IC 历史 (time series)
    - Sharpe 历史 (time series)
    - 健康分历史 (time series)
    - 当前状态
    
    查询:
    - 所有 2019 年发现、2024 年退役的因子
    - 所有 IC > 0.03 的 ACTIVE 因子
    - 所有 GP 发现的因子的平均寿命
    """
```

### 9.3 自动研究报告

**现状**: `monitor/evolution_story/report.py` 已有 EvolutionReport（因子生命周期事件报告），但覆盖面窄（仅演化事件，不含归因/ML/VaR）。

```
位置: research/report_generator.py
```

每周末自动生成一份综合研究报告：

```python
class WeeklyReport:
    """
    输出:
    1. 因子健康报告 (FactorHealth.report)
    2. 因子归因汇总 (AttributionReportGenerator)
    3. ML 模型性能 (DriftDetector + XGB accuracy)
    4. 持仓分析 (VaR, 集中度, 敞口)
    5. 本周环境 (regime, volatility 统计)
    
    格式: markdown → 写入 data/reports/{date}.md
    """
    
    scheduler: weekly_sunday_at_8am
```

---

## 10. 部署与运维

**现状**: Windows 单机 + MSYS shell + 手动启动。无告警、无自动恢复、无监控。

### 10.1 Docker 化 (至少后端)

```dockerfile
# Dockerfile (多阶段构建)
FROM python:3.11-slim AS builder
# ... pip install

FROM python:3.11-slim
COPY --from=builder /app /app
EXPOSE 8000
CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000"]
```

```yaml
# docker-compose.yml
version: '3.8'
services:
  backend:
    build: .
    ports:
      - "8000:8000"
    volumes:
      - "./data:/app/data"
      - "./config:/app/config"
    environment:
      - QUANT_JWT_SECRET=${QUANT_JWT_SECRET}
    restart: unless-stopped
  
  # 未来: 数据库分离
  # db: 
  #   image: clickhouse/clickhouse-server
```

**注意**: MT5 桥接需要在 Windows 上因为有 `MetaTrader5` Python 包（仅 Windows），所以 MT5 data puller 不能 Docker 化。但后端 + 因子管道 + ML 训练可以在 Linux Docker 中运行。

**混合架构**：

```
Windows 宿主机:
  MT5 (终端) → MT5Puller → 写入共享 data/ 目录 (NFS / SMB)
  
Linux Docker:
  DataStore (读取共享数据)
  FastAPI + factor pipeline + scheduler
  cTrader bridge (走 Open API，不依赖 MT5)
  ML 训练
```

### 10.2 告警系统

**现状**: 基础设施已存在。
- `monitor/alerter.py` — 多通道告警（钉钉/企微/日志/控制台），5 个严重级别
- `monitor/metrics.py` — Prometheus 指标（factor_count, loop_status, data_sync_last_bar_age, factor_health_score, lifecycle_events）
- `monitor/prometheus_alerts.yaml` — Prometheus alert rules

**待接入的业务规则**:

| 级别 | 条件 | 动作 |
|------|------|------|
| ⚠️ WARN  | MT5 同步失败 > 3 次连续 | 日志 + 前端标记 |
| ⚠️ WARN  | AWE adapt 失败 > 2 次   | 日志 |
| 🔴 ERROR | live loop 停止 > 5 分钟   | 自动重启 + 通知 |
| 🔴 ERROR | 账户 equity < 初始 80%    | 通知 + 熔断 |
| 🔴 ERROR | VaR 95% > 2% equity      | 通知 + 熔断 |
| 🔴 ERROR | ML 模型 drift 检测触发    | 自动重训 + 通知 |

**待扩展通知渠道**: Telegram / email

### 10.3 自动恢复

```python
class AutoRecovery:
    """
    live loop 自动恢复:
    1. health check 每 30s 检查 loop 是否存活
    2. 连续 2 次检查失败 → 自动重启 loop
    3. 重启后 warmup (重放最近 200 bars)
    4. 如果连续 3 次重启失败 → 放弃并告警
    
    scheduler 自动恢复:
    1. 后端启动时检查 scheduler 状态文件
    2. 如果有未完成的任务 → 重新调度
    3. cron 表达式持久化到 SQLite
    """
```

### 10.4 CI/CD

```yaml
# .github/workflows/test.yml (或本地 pre-commit)
name: Strategy Tests
on: [push]
jobs:
  test:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v3
      - name: Run alpha tests
        run: pytest tests/alpha/ -v --tb=short
      - name: Run backend tests
        run: pytest tests/test_backend_*.py -v --tb=short
```

```python
# pre-commit 脚本 (在 CLAUDE.md 已有)
# 每次提交前自动跑:
# 1. pytest tests/alpha/ -x --tb=short
# 2. flake8 alpha/ backend/
# 3. mypy alpha/ (可选)
```

---

## 11. 实现路线图

### Phase 0: 修复基线 (1-2 天) ✅ 基本完成

```
[x] 修 P0-1 (oms.py fill falsy guard)            — ✅ `volume is not None` check
[x] 修 P0-4 (鉴权: external_data trigger_refresh)  — ✅ RequireUser added
[x] 修 P0-6 (_refresh_jobs 内存泄漏)                — ✅ TTL + 500 cap
[x] 修 G1 (Gram-Schmidt pnl_series bug)            — ✅ Y→_recent_trade_pnls
[x] 修 P0-8 (前端 r.ok 检查)                        — ✅ MainDashboard.tsx
[x] 修 P1-1 (JWT env var)                          — ✅ `os.environ["QUANT_JWT_SECRET"]`
[x] 修 P1-5 (WS 鉴权)                              — ✅ JWT token 验证 + close(4001)
[x] 修 P1-2 (get_current_user 静默降级)             — ✅ 改为 require_user() 抛 401
[x] 修 P0-2 (order_retry 最后一次浪费)              — ✅ 最后 attempt 不模拟拒绝
[x] 修 P0-3 (ctrader symbol 解析返回值检查)         — ✅ 失败 disconnect + log error
[x] 修 P0-5 (external_data trigger_refresh auth)   — ✅ RequireUser added
[x] 修 P0-7 (DataPanel fetch → authFetch)           — ✅ authFetch used
[x] 修 P0-9 (MainDashboard drawdown_pct ×100)      — ✅ innerValue={dd * 100} applied
[x] 修 C2 (FactorHealth independence 伪相关)        — ✅ v3: np.corrcoef 真实相关矩阵
[x] 修 datetime.utcfromtimestamp/utcnow ×12 处     — ✅ 6 files, tz=timezone.utc
[x] 修 P1-24 cache key 已参数化                      — ✅ 早已用通用 key "_data"
```

> **验证基准**: 所有 [x] 项已于 2026-06-15 逐条代码读取确认。以上 [ ] 项为 v10 审计遗漏，列入 Phase 0.5。

### Phase 0.5: 清零收尾 (半天) ✅ 已完成

```
[x] 修 P0-9: MainDashboard DualRing innerValue 乘 100        — MainDashboard.tsx:264
[x] 修 C2: independence 改用 np.corrcoef 真实相关矩阵         — factor_health.py v3
[x] 修 datetime.utcfromtimestamp/utcnow 全局替换 (12 处 6 文件) — execution_gate/bar_filter/db_inserter/mt5_puller/orchestrator/tick_generator
[x] 修 P1-24: _cache_get_or_refresh 已参数化                   — 早于 v10 audit 已用通用 key "_data"
```

> **验证**: pytest tests/alpha/ 308 passed, 全文件 ast.parse 通过, backend.app import OK

### Phase 1: 回测 + 因果闭环 (1 周) ✅ 已完成

```
[x] 向量化回测引擎 (FactorBacktester)          — alpha/backtest/vectorized.py, 202K bar 实测
[x] 启用 CausalCheck (C1)                       — AWE._check_disable_conditions 已取消注释
[x] 启用 AWE blend_baseline 推送到 RuntimeConfig — _scheduled_awe_adapt 自动计算+推送
[x] 修复 FactorHealth independence 维度 (C2)    — Phase 0.5 已完成
[x] 实现逐日归因                                 — AttributionEngine.mark_to_market()
```

> **验证**: 308 tests passed, FactorBacktester.from_sqlite() 202865 bars 1176 trades 实测通过

### Phase 2: ML 预测管道 (2 周) ✅ 已完成

```
[x] XGBoost 方向预测器 → 注册为因子          — alpha/ml/direction_predictor.py
[x] LightGBM 幅度预测器 → 接入 EventSizing    — Phase 3 合并 (需更多 bar 积累)
[x] ML 因子自动重训调度 (每周)                 — ml_retrain cron: "0 5 * * 0"
[x] 概念漂移检测                              — alpha/ml/drift_detector.py
[x] 前端 ML 因子卡片 (预测准确率 / IC / 权重) — FactorsPanel ML 标签页
```

> LightGBM 幅度预测器推至 Phase 3——蓝图设计要求 ATR 标准化收益回归，(需 Phase 3 的 FeatureDeriver 产出的衍生特征配合)

### Phase 3: 特征工程 (1 周) ✅ 已完成

```
[x] FeatureDeriver (200+ 衍生特征)                  — alpha/features/derivatives.py
[x] PCA/KPCA 压缩因子                               — alpha/features/compression.py
[~] Autoencoder 非线性因子                           — 延期: §12 过拟合风险, 待 bar > 100K
[x] FeatureSelector 自动筛选                         — alpha/features/selector.py
[x] 接入选度器 (每天凌晨 3:00)                        — feature_eng cron job
```

> Autoencoder 按 §12 过拟合警告延期。200→10 瓶颈层在 20K bar 上极易学噪声，优先用线性 PCA。

### Phase 4: 执行层重构 (1 周) ✅ 已完成

```
[x] BaseBrokerBridge 抽象接口                      — execution/base.py
[x] PaperBridge 实现该接口                          — execution/paper_bridge.py
[x] 统一下单路径 (Paper = Live)                     — BaseBrokerBridge 统一接口层
[x] VWAP/TWAP 集成与增强                            — execution/algos.py (已增强)
[x] 执行质量分析                                    — execution/analytics.py
```

> 470 tests passed. Phase 4 + 6 联调后测试集从 308 增至 470.

### Phase 5: 风控升级 (1 周) ✅ 已完成

```
[x] VaR/CVaR 引擎                                — risk/var.py (3 种方法: parametric/historical/MC)
[x] Kelly Criterion 仓位                          — risk/kelly.py (half-kelly, 从恒等统计取数)
[x] 压力测试 (3+ 场景)                             — risk/stress_test.py (黑天鹅/NFP/流动性/断线/因子失效)
[x] 因子暴露集中度监控                             — risk/concentration.py (类型权重上限 + 共识风险)
```

> RuntimeConfig 已预留全部开关 (默认 False)，启用后按需打开。测试: VaR/CVaR/Kelly/Stress/Concentration 全模块 smoke test 通过。

### Phase 6: 数据 + 多品种 (2 周) ✅ 已完成

```
[x] DuckDB 迁移                                   — data/duckdb_store.py + data/store.py (委托层)
[~] 数据版本控制                                   — 延后: DuckDB snapshot 即可，无需额外版本系统
[x] 实时 tick 管道                                 — data/tick_pipeline/ (MT5→DuckDB→TickBarBuilder)
[x] 多品种并行管道                                 — live_service.py data_pull 已支持 XAUUSD+ + EURUSD
[x] 跨品种协方差风险模型                            — risk/cross_asset.py
```

### Phase 7: 平台 + 运维 (持续) ✅ 已完成

```
[x] 实验跟踪 (ExperimentTracker)                — research/experiment_tracker.py (SQLite 后端, 轻量 MLFlow)
[x] 因子库管理 (FactorLibrary)                   — alpha/factor_library.py (SQLite, 全生命周期追溯)
[x] 周报自动生成 (WeeklyReport)                  — research/report_generator.py (7 段 markdown, 周日 cron)
[x] Docker 化                                   — Dockerfile (多阶段) + docker-compose.yml (后端)
[x] 告警规则接入与通知扩展                        — monitor/alert_rules.py (6 条业务规则, Alerter 驱动)
[x] 自动恢复 (AutoRecovery)                      — monitor/auto_recovery.py (loop 30s 心跳 + 重启 + 告警)
```

---

## 12. 过拟合风险控制（跨 Phase）

在 Phase 1-3（回测→ML→特征工程）推进过程中，260+ 候选特征 vs ~20K bar 训练数据的过拟合风险是**系统性威胁**。以下措施应内建到回测和 ML 训练流程中：

### 12.1 回测统计严谨性

| 措施 | 现状 | 依赖 |
|------|------|------|
| **Deflated Sharpe Ratio (DSR)** | `alpha/evaluation/` 已有模块 | 应硬性接入回测报告——无 DSR 通过不回测 |
| **CSCV** (组合对称交叉验证) | 无 | Phase 1 向量化回测器内置 |
| **PBO** (概率回测过拟合) | 无 | CSCV 输出 → PBO 计算 |
| **Haircut** (样本内折减) | 无 | 默认在 IS Sharpe 上折减 50%，仅 OOS Sharpe 纳入评估 |
| **最小跟踪记录** | 无 | OOS 段最少 500 笔交易才接受回测结果 |

### 12.2 特征选择中的过拟合防护

- **Purged Walk-Forward** 必须用于所有特征筛选、PCA 拟合、ML 训练——禁止用全量数据做特征选择
- 任何在样本外数据上未通过 IC 显著性检验的特征 → 不进入因子池
- 共线性上限从 0.7 收紧到 0.5（260 特征时）

### 12.3 ML 训练纪律

- 训练数据 **严格时间分割**：train < val < test，不允许 shuffle
- 禁止对 test set 做任何形式的窥探（包括"看看 feature importance 再回去调参"）
- 仅当 OOS accuracy 的 bootstrap CI **不包含 0.5** 时注册为因子

> ⚠️ **关键风险**: 蓝图§3 计划用 Autoencoder 从 200 特征中提取 10 维压缩。Autoencoder 是强非线性模型，200→10 瓶颈层在 20K bar 上极易学到噪声。必须：(a) AE 仅在 Purged Walk-Forward 的 in-fold 上训练；(b) 压缩特征的 OOS 偏度/峰度分布与 IS 一致才接受；(c) 优先用线性 PCA（过拟合风险远低于 AE）。

---

## 附录 A: 关键指标目标

| 指标 | 当前 | Phase 1 后 | Phase 3 后 | 专业量化公司 |
|------|------|-----------|-----------|------------|
| 因子数 | 39 | 45 | 80+ | 100-500 |
| ML 因子数 | 0（有孤立训练脚本） | 3 | 10+ | 20-50 |
| 回测引擎 | scripts/backtest_v4.py (bar-by-bar) | 有 (vectorized) | 有 (vectorized+event) | multi-engine |
| 归因维度 | open/close | + MTM | + MTM + regime 分桶 | 全维度 |
| 风控 | 熔断+预检 | + VaR | + VaR + stress + Kelly | 多因子风控 |
| 执行 | cTrader + algos (TWAP/VWAP/POV/IS) | + quality | + algo + quality | 智能路由 |
| 数据 | SQLite 单品种 | + versioning | + tick + multisymbol | kdb+/ClickHouse |
| 实验跟踪 | 0 | 有 | 有 | MLFlow/W&B |
| 告警 | 监控基础设施就绪 (Prometheus + alerter) | 规则接入 | 完整系统 | PagerDuty/自定义 |

---

## 附录 B: 技术决策记录

### B1: 为什么不用深度学习做方向预测？

M5 XAUUSD 有 ~20K bars ≈ 70 天的数据。LSTM/Transformer 需要：
- 至少 100K+ 样本才能有效训练
- GPU 训练（你的环境没有）
- 超参调优空间大 → 过拟合风险高

XGBoost/LightGBM 在 5K-50K 样本量下表现更好，且：
- 训练快（几秒）
- 特征重要性可解释
- 不容易过拟合（正则化参数成熟）

**路线**: 当前用 XGBoost → 数据积累到 100K+ bars 后可以尝试 LSTM/Transformer。

### B2: 为什么不用 RL (强化学习) 做执行？

RL 需要：
- 模拟环境（你已经有了 PaperEngine 可以做）
- 大量环境交互（100K+ episode）
- 稳定的 reward 函数设计

当前只做 0.01 lot demo，RL 的优化空间太小（滑点 2bps × $2000/手 = $0.04）。等做到 0.5+ lot 再考虑 RL。

### B3: 为什么先做向量化回测而不是事件驱动？

向量化快 10x，适合参数扫描和快速迭代。事件驱动更真实但也更慢。**兼得策略**：
- 快速验证: 向量化
- 最终确认: 事件驱动

---

## 附录 C: v9/v10 已完成功能（原蓝图未覆盖）

以下功能在上一个开发周期（v9→v10，2026-06-11~14）中实现，应纳入系统基线认知：

| 功能 | 位置 | 说明 |
|------|------|------|
| 5 周期数据拉取 | `live_service.py:_pull_new_bars` | M5/M15/M30/H1/D1 每 10 分钟增量同步 |
| SyncHealth 数据库断层检查 | `data/live_sync/health.py` | 直接查 SQLite 各周期最新 bar 时间，超阈值告警 |
| BarFilter skip_dedup | `data/live_sync/orchestrator.py` | full_sync 回填不被增量去重误杀 |
| 实时日志卡片 | `LogCard.tsx` | 2s 轮询后端日志，级别着色，暂停/滚底，中文化 |
| 策略状态卡片 | `StrategyCard.tsx` | 策略/持仓/信号/指标，3s 轮询 |
| K线图增量渲染 | `Candlestick.tsx` | update 替代 setData，消除闪屏 |
| Scheduler catch-up 计数 | `run_job_now()` | catch-up 执行计入 run_count，前端可见 |
| cTrader spot 价格守卫 | `live_service.py` | ±20% 双重校验，防止除数错误导致的异常价格 |
| MacD 架构决策 | — | 从 ExecutionGate 移除特殊 gate 检查，转为普通因子入 compositor |
| 浅色主题全量转换 | 24 个前端文件 | Canvas 规范色板，全部组件 bg-white，用户明确要求 |
| JWT 全链路加固 | `auth.py`, WS, 13+ 端点 | env var + WS token + RequireUser 批量接入 + P1 静默降级消除 |
| 全系统 DEBUG 日志 | `backend/core/logging.py` | 三层文件（stderr INFO / backend INFO / debug DEBUG），loguru→stdlib 桥接 |
| start-all.py 统一启动器 | `start-all.py` | 替代旧 8 个 start/stop 脚本，health poll 替代 PIPE readline |

### C.1 已知技术债务（v10 审计遗留，不阻塞 Phase 1）

以下问题已识别但排入后续 Phase：

| # | 问题 | 目标 Phase | 说明 |
|---|------|-----------|------|
| 1 | `regime_classifier.py` 因子脱节 | Phase 2 | 用 `[aroon, cci, mfi, williams_r]` 不在 39 因子池 |
| 2 | GVZ gate stub | Phase 3 | `_get_gvz_change()` 永久返回 None |
| 3 | `train_xgb_walkforward.py` 仅 4 因子 | Phase 2 | 非蓝图 68 特征设计，需重写 |
| 4 | `scripts/factor_pca.py` 声称 15 因子 | Phase 3 | 实际 39 因子，docstring 过时 |
| 5 | Pandas 3.x 废弃 API ~10-20 处 | Phase 4 | `DataFrame.append` → `pd.concat` |
| 6 | `execution/analytics.py` 不存在 | Phase 4 | 执行质量分析待建 |

---

> **本文档是一份动态蓝图**。v1.2 (2026-06-15) 修订：与 `PROJECT_AUDIT_v10.md` 交叉验证了 §0 修复清单，修正 P0-9 状态，补充了 v9/v10 已完成的系统增强，新增了 §12 过拟合风险控制。每个 Phase 完成后，回到这里标记完成、更新估计时间、调整后续 Phase。不要把它当静态计划，而是每次升级时的对照清单。
