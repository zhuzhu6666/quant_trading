"""execution/market_impact.py — Almgren-Chriss 市场冲击模型

Almgren & Chriss (1999) 经典模型: 估算最优执行滑点.
给定 (订单大小 Q, 日波动率 σ, 日均成交量 V_avg) → 市场冲击成本.

Key formulas:
  Temporary impact (bps):  η × (Q_slice / V_avg) × 10000
  Permanent impact (bps):  γ × (Q_total / V_avg) × 10000
  总滑点 = temp + permanent (bps)
  Cost USD/oz = total_bps / 10000 × price

集成: 供 PaperExecutionEngine 后续接入.
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class AlmgrenChrissModel:
    """Almgren-Chriss 1999 市场冲击模型 (黄金).

    参数说明:
        sigma:         日波动率 (%)
        daily_volume_oz: 黄金日均成交量 (oz)
        eta:           临时冲击系数
        gamma:         永久冲击系数
        lambda_risk:   风险厌恶系数 (用于最优执行计划)
        tick_size:     黄金 tick (USD/oz)
    """

    DEFAULT_PARAMS: dict = {
        "sigma": 1.5,              # 日波动率 (%, 黄金 ~1.5%)
        "daily_volume_oz": 200000, # 黄金日均成交量 (oz)
        "eta": 2.5e-6,             # 临时冲击系数 (Almgren paper: 2.5e-6)
        "gamma": 2.5e-6,           # 永久冲击系数
        "lambda_risk": 1e-6,       # 风险厌恶系数
        "tick_size": 0.01,         # 黄金 tick (USD/oz)
    }

    def __init__(self, params: Optional[dict] = None):
        """初始化 Almgren-Chriss 模型.

        Args:
            params: 可覆盖默认参数的字典
        """
        self.params = {**self.DEFAULT_PARAMS, **(params or {})}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def optimal_schedule(self, total_qty: float, n_slices: int = 10) -> list[float]:
        """返回 n_slices 个手数, 总和 = total_qty.

        Almgren 最优解 (无约束):
            x_k = sinh(κ(T-t_k)) / sinh(κT) × Q
            κ = √(λσ²/η)

        简化版: 平均分配 (用于 cost 估算).

        Args:
            total_qty: 总数量 (oz)
            n_slices:  切片数

        Returns:
            list[float]: 每片数量
        """
        if n_slices <= 0 or total_qty <= 0:
            return []
        slice_size = total_qty / n_slices
        return [slice_size] * n_slices

    def expected_cost(
        self, total_qty: float, n_slices: int = 10, side: int = 1
    ) -> dict:
        """估算 Almgren-Chriss 市场冲击成本.

        Args:
            total_qty: 总数量 (oz)
            n_slices:  切片数
            side:      交易方向 (1=买入, -1=卖出, 不影响成本幅度)

        Returns:
            dict:
                temporary_impact_bps  临时冲击 (bps)
                permanent_impact_bps  永久冲击 (bps)
                total_slippage_bps    总滑点 (bps)
                optimal_slices        切片数
                total_cost_usd_per_oz 每盎司成本 (USD)
        """
        p = self.params
        V_avg = p["daily_volume_oz"]
        eta = p["eta"]
        gamma = p["gamma"]

        # 每片大小
        slice_size = total_qty / n_slices if n_slices > 0 else total_qty

        # 临时冲击 (bps): 每片冲击, 与片大小成正比
        temp_bps = eta * (slice_size / V_avg) * 10_000

        # 永久冲击 (bps): 总量冲击, 与总大小成正比
        perm_bps = gamma * (total_qty / V_avg) * 10_000

        total_bps = temp_bps + perm_bps

        # 成本 USD/oz = total_bps / 10000 × price
        # price = 1.0 作为标量 (不在黄金即时价格上计价)
        cost_usd_per_oz = total_bps / 10_000 * 1.0

        return {
            "temporary_impact_bps": temp_bps,
            "permanent_impact_bps": perm_bps,
            "total_slippage_bps": total_bps,
            "optimal_slices": n_slices,
            "total_cost_usd_per_oz": cost_usd_per_oz,
        }

    def compare_strategies(self, total_qty: float) -> pd.DataFrame:
        """对比不同切片数的 cost 结构.

        遍历 n_slices = 1, 5, 10, 20, 50, 100 的场景,
        输出 DataFrame 帮交易员选最优切片数.

        Args:
            total_qty: 总数量 (oz)

        Returns:
            pd.DataFrame:
                slices | temporary_bps | permanent_bps | total_bps | cost_usd_per_oz
        """
        rows = []
        for n in (1, 5, 10, 20, 50, 100):
            cost = self.expected_cost(total_qty, n_slices=n)
            rows.append(
                {
                    "slices": n,
                    "temporary_bps": cost["temporary_impact_bps"],
                    "permanent_bps": cost["permanent_impact_bps"],
                    "total_bps": cost["total_slippage_bps"],
                    "cost_usd_per_oz": cost["total_cost_usd_per_oz"],
                }
            )
        return pd.DataFrame(rows)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _default_params() -> dict:
        """返回默认参数字典."""
        return dict(AlmgrenChrissModel.DEFAULT_PARAMS)
