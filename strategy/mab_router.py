"""
MAB Thompson Sampling Router — 多臂老虎机策略路由器

用 Beta 分布建模每个策略在每个 regime 下的胜率，
通过 Thompson Sampling 选择最优策略。

核心思想:
  - 每个 (regime, strategy) 对维护 Beta(alpha, beta) 后验
  - alpha = wins + 1, beta = losses + 1 (贝叶斯先验 +1 平滑)
  - select(): 从每个候选策略的 Beta 采样一次, 取最大值
  - update(): 根据 trade outcome 更新对应后验
  - regime: 用 EMA50/200 + ATR 百分位实时分类

依赖: numpy + pandas (无外部 MAB 库)
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ── Regime 常量 ────────────────────────────────────
REGIMES = ["TRENDING_UP", "TRENDING_DOWN", "RANGING", "HIGH_VOL", "LOW_VOL"]

# EMA / ATR 参数
EMA_FAST = 50
EMA_SLOW = 200
ATR_PERIOD = 14
ATR_WINDOW = 200          # 百分位统计窗口
TREND_THRESHOLD = 0.001   # EMA 比例阈值 (0.1%)
VOL_HIGH_PCTILE = 70.0    # ATR 百分位 > 此值 → HIGH_VOL
VOL_LOW_PCTILE = 30.0     # ATR 百分位 < 此值 → LOW_VOL


# ── 指标工具 ──────────────────────────────────────

def _ema(arr: np.ndarray, period: int) -> np.ndarray:
    """指数移动平均 (O(n), 就地计算)"""
    out = np.empty_like(arr, dtype=float)
    out[:period] = np.nan
    out[period - 1] = np.nanmean(arr[:period])
    alpha = 2.0 / (period + 1)
    for i in range(period, len(arr)):
        out[i] = (arr[i] - out[i - 1]) * alpha + out[i - 1]
    return out


def _atr(high: np.ndarray, low: np.ndarray, close: np.ndarray,
         period: int = 14) -> np.ndarray:
    """ATR (Average True Range, SMA 变体)"""
    tr = np.maximum(
        high[1:] - low[1:],
        np.maximum(
            np.abs(high[1:] - close[:-1]),
            np.abs(low[1:] - close[:-1]),
        ),
    )
    out = np.empty_like(close, dtype=float)
    out[:period] = np.nan
    out[period] = np.nanmean(tr[:period])
    for i in range(period + 1, len(close)):
        out[i] = (out[i - 1] * (period - 1) + tr[i - 1]) / period
    return out


# ── Regime 分类器 ─────────────────────────────────

def classify_regime(closes: np.ndarray,
                    highs: Optional[np.ndarray] = None,
                    lows: Optional[np.ndarray] = None,
                    ema50: Optional[np.ndarray] = None,
                    ema200: Optional[np.ndarray] = None,
                    atr: Optional[np.ndarray] = None) -> str:
    """
    用 EMA50/200 + ATR 百分位把当前市场状态归入 5 类 regime 之一。

    Parameters
    ----------
    closes : np.ndarray
        close 价序列 (至少 200 根, 最新在末尾).
    highs, lows : np.ndarray, optional
        用于计算 ATR. 不传时只靠 EMA 做趋势分类 (vol 相关 regime 不可用).
    ema50, ema200, atr : np.ndarray, optional
        预计算的指标, 避免重复计算.

    Returns
    -------
    str
        REGIMES 之一 (默认 RANGING).
    """
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    if n < EMA_SLOW:
        return "RANGING"

    # ── 计算 EMA (如果未预计算) ──
    if ema50 is None:
        ema50 = _ema(closes, EMA_FAST)
    if ema200 is None:
        ema200 = _ema(closes, EMA_SLOW)

    if np.isnan(ema50[-1]) or np.isnan(ema200[-1]):
        return "RANGING"

    ratio = ema50[-1] / ema200[-1] - 1.0  # EMA50 相对 EMA200 偏离

    # ── ATR 百分位 (仅当有 high/low 时) ──
    if highs is not None and lows is not None:
        highs = np.asarray(highs, dtype=float)
        lows = np.asarray(lows, dtype=float)
        if atr is None:
            atr = _atr(highs, lows, closes, ATR_PERIOD)

        if not np.isnan(atr[-1]) and len(atr) >= ATR_WINDOW:
            recent_atr = atr[-ATR_WINDOW:]
            current = atr[-1]
            rank_pct = float(np.sum(recent_atr <= current)) / len(recent_atr) * 100.0

            if rank_pct >= VOL_HIGH_PCTILE:
                return "HIGH_VOL"
            if rank_pct <= VOL_LOW_PCTILE:
                return "LOW_VOL"

    # ── 趋势分类 ──
    if ratio > TREND_THRESHOLD:
        return "TRENDING_UP"
    if ratio < -TREND_THRESHOLD:
        return "TRENDING_DOWN"
    return "RANGING"


# ── MAB Router ────────────────────────────────────

class MABRouter:
    """
    Multi-Armed Bandit — Thompson Sampling 策略路由器

    为每个 (regime, strategy) 维护独立的 Beta 后验分布,
    每次 ``select(regime)`` 采样最大值决定当前最优策略。

    用法::

        router = MABRouter(strategies, baseline=baseline_dict)
        chosen = router.select("RANGING")
        router.update(chosen, "RANGING", win=True)
        df = router.stats()
    """

    def __init__(self, strategies: list[str],
                 baseline: Optional[dict[str, tuple[float, float]]] = None,
                 seed: int | None = None):
        """
        Parameters
        ----------
        strategies : list[str]
            候选策略名称列表.
        baseline : dict[str, tuple[float, float]] | None
            冷启动基线: {策略名: (历史胜场, 历史负场)}.
            不传则全部初始化为 Beta(1, 1).
        seed : int | None
            (P1-D) 随机种子, 用于 Thompson sampling 的可复现.
            None = 用全局 np.random (默认).
        """
        self.strategies = list(strategies)
        self._rng = np.random.default_rng(seed) if seed is not None else np.random
        self._alpha: dict[str, dict[str, float]] = {}  # regime → strategy → α
        self._beta: dict[str, dict[str, float]] = {}   # regime → strategy → β

        for regime in REGIMES:
            self._alpha[regime] = {}
            self._beta[regime] = {}
            for s in self.strategies:
                if baseline and s in baseline:
                    w, l = baseline[s]
                    self._alpha[regime][s] = float(w) + 1.0
                    self._beta[regime][s] = float(l) + 1.0
                else:
                    self._alpha[regime][s] = 1.0
                    self._beta[regime][s] = 1.0

    # ── 公共接口 ──────────────────────────────────

    def select(self, regime: str) -> str:
        """
        Thompson Sampling 选择策略.

        对当前 regime 下每个策略从 Beta(α, β) 独立采样,
        返回采样值最大的策略.

        Parameters
        ----------
        regime : str
            当前市场 regime (REGIMES 之一). 不合法时回落 RANGING.

        Returns
        -------
        str
            选中的策略名.
        """
        if regime not in REGIMES:
            regime = "RANGING"

        best_strategy = self.strategies[0]
        best_sample = -1.0
        for s in self.strategies:
            a = self._alpha[regime].get(s, 1.0)
            b = self._beta[regime].get(s, 1.0)
            sample = float(self._rng.beta(a, b))
            if sample > best_sample:
                best_sample = sample
                best_strategy = s
        return best_strategy

    def update(self, strategy: str, regime: str, win: bool):
        """
        根据交易结果更新后验.

        Parameters
        ----------
        strategy : str
            实际执行的策略名.
        regime : str
            执行时的 regime.
        win : bool
            True = 盈利, False = 亏损.
        """
        if regime not in REGIMES:
            regime = "RANGING"
        if strategy not in self.strategies:
            logger.warning("update() 收到未知策略 '%s'", strategy)
            return

        # 确保 key 存在 (防御)
        if strategy not in self._alpha.setdefault(regime, {}):
            self._alpha[regime][strategy] = 1.0
            self._beta[regime][strategy] = 1.0

        if win:
            self._alpha[regime][strategy] += 1.0
        else:
            self._beta[regime][strategy] += 1.0

    def stats(self) -> pd.DataFrame:
        """
        返回每个 (regime, strategy) 的 alpha / beta / 期望胜率.

        Returns
        -------
        pd.DataFrame
            Columns: [regime, strategy, alpha, beta, expected_win_rate]
        """
        rows: list[dict] = []
        for regime in REGIMES:
            for s in self.strategies:
                a = self._alpha[regime].get(s, 1.0)
                b = self._beta[regime].get(s, 1.0)
                ev = a / (a + b) if (a + b) > 0 else 0.5
                rows.append({
                    "regime": regime,
                    "strategy": s,
                    "alpha": round(a, 2),
                    "beta": round(b, 2),
                    "expected_win_rate": round(ev, 6),
                })
        return pd.DataFrame(rows)

    def strategy_summary(self) -> pd.DataFrame:
        """
        按策略聚合, 跨 regime 求和 alpha/beta 并计算综合期望胜率.

        Returns
        -------
        pd.DataFrame
            Columns: [strategy, total_alpha, total_beta, expected_win_rate]
        """
        rows: list[dict] = []
        for s in self.strategies:
            total_a = sum(self._alpha[r].get(s, 1.0) for r in REGIMES)
            total_b = sum(self._beta[r].get(s, 1.0) for r in REGIMES)
            ev = total_a / (total_a + total_b) if (total_a + total_b) > 0 else 0.5
            rows.append({
                "strategy": s,
                "total_alpha": round(total_a, 2),
                "total_beta": round(total_b, 2),
                "expected_win_rate": round(ev, 6),
            })
        return pd.DataFrame(rows).sort_values("expected_win_rate", ascending=False)
