"""deployment/risk_rebalancer.py — 因子集变更仓位重平衡器 (Phase 2.3, 2026-06-12)

当因子集发生变更 (晋升/回滚/淘汰) 时, 重新计算各标的的仓位大小.
默认使用 RiskBudgeting (等风险贡献) 算法.

工作流:
1. 从 new_factor_set 获取当前活跃因子列表
2. 计算各因子的风险估计 (波动率 / VaR)
3. 用 RiskBudgeting 分配目标仓位权重
4. 结合 current_positions 和 config (总资金, 杠杆上限) 输出调整后仓位
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np

logger = logging.getLogger(__name__)

# ── 默认配置 ──────────────────────────────────────────────────────────
DEFAULT_TOTAL_CAPITAL = 100_000.0
DEFAULT_MAX_LEVERAGE = 1.0            # 无杠杆
DEFAULT_RISK_TARGET = 0.15            # 年化波动率目标 15%
MIN_POSITION_PCT = 0.01               # 最低仓位比例 1%
MAX_POSITION_PCT = 0.40               # 单标的上限 40%


@dataclass
class Position:
    """单个标的的当前持仓"""
    symbol: str
    size: float                       # 当前持仓量
    factor_scores: dict[str, float] = field(default_factory=dict)
    # ^ 因子对该标的的参与度 (z-score / signal), 用于风险分解

    @property
    def notional(self) -> float:
        """名义价值 (size 已按价格折算)"""
        return self.size


@dataclass
class FactorSet:
    """新的因子集"""
    names: list[str]                   # 活跃因子列表
    risk_contributions: dict[str, float] | None = None
    # ^ 各因子的风险预算比例 (None=均匀分配)


@dataclass
class RebalanceConfig:
    """重平衡配置"""
    total_capital: float = DEFAULT_TOTAL_CAPITAL
    max_leverage: float = DEFAULT_MAX_LEVERAGE
    risk_target: float = DEFAULT_RISK_TARGET
    min_position_pct: float = MIN_POSITION_PCT
    max_position_pct: float = MAX_POSITION_PCT
    algorithm: str = "risk_budgeting"  # risk_budgeting | equal_weight | volatility_parity


class RiskRebalancer:
    """
    因子集变更仓位重平衡器.

    用法:
        rb = RiskRebalancer()
        current_positions = [
            Position(symbol="XAUUSD", size=1.0),
            Position(symbol="XAGUSD", size=2.0),
        ]
        new_set = FactorSet(names=["factor_a", "factor_b"])
        config = RebalanceConfig(total_capital=100000)
        adjusted = rb.rebalance(current_positions, new_set, config)
        # -> [{"symbol": "XAUUSD", "size": 1.5}, ...]
    """

    def __init__(self):
        self._algorithms: dict[str, Any] = {
            "risk_budgeting": _risk_budgeting,
            "equal_weight": _equal_weight,
            "volatility_parity": _volatility_parity,
        }

    # ── 主入口 ─────────────────────────────────────────────────────

    def rebalance(
        self,
        current_positions: list[Position],
        new_factor_set: FactorSet,
        config: RebalanceConfig | None = None,
    ) -> list[dict]:
        """
        重新计算仓位大小.

        Args:
            current_positions: 当前持仓列表
            new_factor_set: 新的因子集 (含风险预算)
            config: 重平衡配置

        Returns:
            [{"symbol": str, "size": float, "weight_pct": float, "method": str}, ...]
        """
        cfg = config or RebalanceConfig()
        if not current_positions:
            return []

        # 1. 计算目标权重 (算法层)
        algo_fn = self._algorithms.get(cfg.algorithm, _risk_budgeting)
        n = len(current_positions)
        target_weights = algo_fn(
            n,
            new_factor_set,
            current_positions,
            cfg,
        )

        # 2. 上下界钳制
        clamped = np.clip(target_weights,
                          cfg.min_position_pct,
                          cfg.max_position_pct)

        # 3. 归一化至总预算 (含杠杆)
        total_budget = cfg.total_capital * cfg.max_leverage
        total_w = float(np.sum(clamped))
        if total_w > 0:
            clamped = clamped / total_w  # 归一化至 [0,1]

        # 4. 转为绝对仓位大小
        result = []
        for pos, weight in zip(current_positions, clamped):
            size = total_budget * weight
            result.append({
                "symbol": pos.symbol,
                "size": round(size, 6),
                "weight_pct": round(weight * 100, 4),
                "method": cfg.algorithm,
            })

        logger.info(
            f"[RiskRebalancer] rebalanced {len(current_positions)} positions "
            f"(algo={cfg.algorithm}, capital={cfg.total_capital:.0f}, "
            f"leverage={cfg.max_leverage:.2f})"
        )
        return result

    def register_algorithm(self, name: str,
                           fn: Any) -> None:
        """注册自定义重平衡算法"""
        if name in self._algorithms:
            logger.warning(f"[RiskRebalancer] 算法 '{name}' 已存在, 覆盖")
        self._algorithms[name] = fn


# ── 算法实现 ────────────────────────────────────────────────────────


def _risk_budgeting(
    n: int,
    factor_set: FactorSet,
    positions: list[Position],
    config: RebalanceConfig,
) -> np.ndarray:
    """
    RiskBudgeting (等风险贡献) — 默认算法.

    各标的获得均等风险预算, 最终权重 ≈ 1/n.
    如果提供了 factor_set.risk_contributions, 则按该比例分配.

    注: 完整版需要标的协方差矩阵. 这里简化成均等风险,
    假设各标的波动率一致.
    """
    if n == 0:
        return np.array([], dtype=np.float64)

    if factor_set.risk_contributions:
        # 按因子风险预算分配
        total = sum(factor_set.risk_contributions.values())
        weights = np.array([
            factor_set.risk_contributions.get(p.symbol, 1.0 / n)
            for p in positions
        ], dtype=np.float64)
        if total > 0:
            weights = weights / total
        return weights

    # 默认: 均等分配
    return np.full(n, 1.0 / n)


def _equal_weight(
    n: int,
    factor_set: FactorSet,
    positions: list[Position],
    config: RebalanceConfig,
) -> np.ndarray:
    """等权分配"""
    if n == 0:
        return np.array([], dtype=np.float64)
    return np.full(n, 1.0 / n)


def _volatility_parity(
    n: int,
    factor_set: FactorSet,
    positions: list[Position],
    config: RebalanceConfig,
) -> np.ndarray:
    """
    波动率平价 — 用标的的参与度分数近似波动率.
    分数高  → 降低权重 (波动越大权重越小).
    完整版需要真波动率估计.
    """
    if n == 0:
        return np.array([], dtype=np.float64)

    # 用各标的 factor_scores 的均值作为"活跃度"近似
    # active = avg 参与度, 越高波动越大 → 权重越低
    volatilities = []
    for p in positions:
        if p.factor_scores:
            avg = float(np.mean(list(p.factor_scores.values())))
        else:
            avg = 1.0
        vol = max(abs(avg), 0.01)  # 避免除 0
        volatilities.append(1.0 / vol)  # 逆波动率权重

    weights = np.array(volatilities, dtype=np.float64)
    total = float(np.sum(weights))
    if total > 0:
        weights = weights / total
    return weights
