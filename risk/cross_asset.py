"""
risk/cross_asset.py — 跨品种协方差风险模型 (Phase 6)

60 天滚动协方差矩阵 + 风险平价权重分配。
用于多品种并行管道中的仓位预算管理。
"""
from __future__ import annotations

import logging
from collections import deque
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class CrossAssetCovariance:
    """跨品种协方差 + 风险预算

    维护滚动窗口的协方差/相关矩阵，提供风险平价权重。
    接入: live_service 中每个品种下单前调用 adjust_position() 调整仓位上限。

    用法:
        cac = CrossAssetCovariance(["XAUUSD+", "EURUSD"], window=60)
        cac.update(multi_close_df)  # columns: (XAUUSD+, close), (EURUSD, close)
        weights = cac.risk_parity_weights()  # {"XAUUSD+": 0.6, "EURUSD": 0.4}
        limits = cac.position_limits(total_volume=0.5)  # {"XAUUSD+": 0.3, "EURUSD": 0.2}
    """

    def __init__(self, symbols: list[str], window: int = 60):
        self.symbols = list(symbols)
        self.window = max(10, window)

        # 滚动窗口：存每根 bar 的各品种 close 价
        self._price_history: dict[str, deque[float]] = {
            sym: deque(maxlen=self.window + 1) for sym in symbols
        }

        self._cov: Optional[np.ndarray] = None
        self._corr: Optional[np.ndarray] = None
        self._n_updates: int = 0

    @property
    def cov_matrix(self) -> Optional[np.ndarray]:
        return self._cov

    @property
    def corr_matrix(self) -> Optional[np.ndarray]:
        return self._corr

    # ── 数据更新 ──

    def append(self, prices: dict[str, float]):
        """追加一根 bar 的各品种收盘价"""
        for sym in self.symbols:
            if sym in prices and prices[sym] is not None and prices[sym] > 0:
                self._price_history[sym].append(prices[sym])

    def update(self, df: pd.DataFrame) -> Optional[np.ndarray]:
        """从 MultiIndex columns DataFrame 批量更新

        df 格式: columns=MultiIndex.from_tuples([("XAUUSD+", "close"), ("EURUSD", "close"), ...])
        或普通 DataFrame, columns=["XAUUSD+", "EURUSD"]
        """
        # 尝试 MultiIndex
        if isinstance(df.columns, pd.MultiIndex):
            closes = {}
            for sym in self.symbols:
                try:
                    col = df.xs("close", level=1, axis=1).get(sym)
                    if col is not None and len(col) > 0:
                        closes[sym] = col
                except (KeyError, AttributeError):
                    pass
        else:
            # 普通 columns
            for sym in self.symbols:
                if sym in df.columns:
                    closes[sym] = df[sym]

        if not closes:
            return None

        # 用最新的 close 价格追加
        for sym, col in closes.items():
            if len(col) > 0:
                val = float(col.iloc[-1])
                if val > 0:
                    self._price_history[sym].append(val)

        return self._compute()

    def _compute(self) -> Optional[np.ndarray]:
        """从 _price_history 滚动窗口重算协方差矩阵"""
        # 构建 (n_bars, n_symbols) 价格矩阵
        n_syms = len(self.symbols)
        price_lists = []
        for sym in self.symbols:
            prices = list(self._price_history[sym])
            if len(prices) < 10:
                return None  # 至少 10 根 bar
            price_lists.append(prices)

        # 对齐长度（取最短）
        min_len = min(len(p) for p in price_lists)
        if min_len < 10:
            return None

        aligned = np.array([p[-min_len:] for p in price_lists]).T  # (n_bars, n_symbols)

        # 对数收益率
        log_returns = np.diff(np.log(aligned), axis=0)

        if len(log_returns) < 5:
            return None

        # 协方差矩阵 (年化: ×252 交易日 × 每日bar数)
        # M5: ~288 bars/day, M15: ~96 bars/day
        bars_per_day = 288  # M5 default
        annual_factor = 252 * bars_per_day

        self._cov = np.cov(log_returns, rowvar=False) * annual_factor
        self._corr = np.corrcoef(log_returns, rowvar=False)
        self._n_updates += 1

        vol_strs = [f"{float(np.sqrt(self._cov[i,i])):.4f}" for i in range(n_syms)]
        corr_strs = [f"{float(self._corr[0,i]):.3f}" for i in range(1, n_syms)] if n_syms > 1 else ["N/A"]
        logger.debug(
            f"[CrossAsset] cov update #{self._n_updates}: "
            f"vols={vol_strs} corr={corr_strs}"
        )
        return self._cov

    # ── 风险预算 ──

    def risk_parity_weights(self) -> dict[str, float]:
        """风险平价: 每个品种的仓位权重 ∝ 1/σ"""
        if self._cov is None:
            n = len(self.symbols)
            w = 1.0 / n
            return {sym: w for sym in self.symbols}

        vols = np.sqrt(np.diag(self._cov))
        # 防止零波动率
        vols = np.maximum(vols, 1e-10)
        inv_vols = 1.0 / vols
        weights = inv_vols / inv_vols.sum()

        return {sym: float(w) for sym, w in zip(self.symbols, weights)}

    def equal_risk_contribution_weights(self, max_iter: int = 50) -> dict[str, float]:
        """等风险贡献 (ERC): 每个品种的边际风险贡献相等

        用简单的迭代算法: w_i = 1/σ_i / sum(1/σ_j) 作为初始，然后迭代
        使 σ_i * w_i ≈ 常数 (每个品种贡献相同风险)
        """
        if self._cov is None:
            n = len(self.symbols)
            w = 1.0 / n
            return {sym: w for sym in self.symbols}

        n = len(self.symbols)
        # 初始: 风险平价
        vols = np.sqrt(np.diag(self._cov))
        vols = np.maximum(vols, 1e-10)
        w = (1.0 / vols)
        w = w / w.sum()

        for _ in range(max_iter):
            # 组合波动率
            sigma_p = np.sqrt(w.T @ self._cov @ w)
            # 边际风险贡献
            mrc = self._cov @ w / sigma_p
            # 风险贡献 = w * mrc
            rc = w * mrc
            # 目标: 每个 rc 相等 = sigma_p / n
            target = sigma_p / n
            # 更新: w *= target / rc
            rc = np.maximum(rc, 1e-10)
            w = w * (target / rc)
            w = w / w.sum()

        return {sym: float(w) for sym, w in zip(self.symbols, w)}

    def position_limits(self, total_volume: float = 0.5,
                        max_single_pct: float = 0.60) -> dict[str, float]:
        """返回各品种的仓位上限

        Args:
            total_volume: 所有品种总 volume 上限
            max_single_pct: 单一品种最大占比 (默认 60%)
        """
        weights = self.risk_parity_weights()
        limits = {}
        for sym in self.symbols:
            w = weights.get(sym, 1.0 / len(self.symbols))
            limits[sym] = min(w * total_volume, max_single_pct * total_volume)
        return limits

    def correlation_warning(self, threshold: float = 0.7) -> list[str]:
        """返回高相关品种对 (>threshold)，用于告警"""
        if self._corr is None:
            return []
        warnings = []
        n = len(self.symbols)
        for i in range(n):
            for j in range(i + 1, n):
                if abs(self._corr[i, j]) > threshold:
                    warnings.append(
                        f"{self.symbols[i]}↔{self.symbols[j]} corr={self._corr[i,j]:.2f}"
                    )
        return warnings

    def summary(self) -> dict:
        """返回协方差矩阵摘要 (供前端展示)"""
        if self._cov is None:
            return {"status": "insufficient_data"}
        n = len(self.symbols)
        vols = np.sqrt(np.diag(self._cov))
        return {
            "symbols": self.symbols,
            "annualized_vol": {sym: float(v) for sym, v in zip(self.symbols, vols)},
            "correlation_matrix": {
                self.symbols[i]: {
                    self.symbols[j]: float(self._corr[i, j])
                    for j in range(n)
                }
                for i in range(n)
            },
            "risk_parity_weights": self.risk_parity_weights(),
            "n_updates": self._n_updates,
        }
