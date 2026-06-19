"""Execution algorithms: TWAP / VWAP / POV / IS."""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import timedelta
from typing import Any

import numpy as np

from execution.base import OrderResult, PositionInfo, AccountInfo

logger = logging.getLogger(__name__)


# ── 数据结构 ──────────────────────────────────────


class ParentOrder:
    """父单: 一个大单需要怎么切片"""

    def __init__(self, symbol: str, total_volume: float,
                 start_time: Any, end_time: Any,
                 current_price: float, direction: int = 1,
                 urgency: float = 0.5):
        self.symbol = symbol
        self.total_volume = total_volume
        self.start_time = start_time
        self.end_time = end_time
        self.current_price = current_price
        self.direction = direction  # 1=买, -1=卖
        self.urgency = urgency  # 0.0=不急, 1.0=急

    def duration_seconds(self) -> float:
        return (self.end_time - self.start_time).total_seconds()


class ChildOrder:
    """子单: 算法输出的一片"""

    def __init__(self, sequence: int, target_time: Any,
                 volume: float, price_hint: float,
                 order_type: str = "market",
                 limit_offset: int = 0):
        self.sequence = sequence
        self.target_time = target_time
        self.volume = volume
        self.price_hint = price_hint
        self.order_type = order_type
        self.limit_offset = limit_offset


# ── 算法基类 ──────────────────────────────────────


class ExecutionAlgorithm(ABC):
    """所有执行算法的接口"""

    name: str = "BASE"

    @abstractmethod
    def slice(self, parent: ParentOrder) -> list[ChildOrder]:
        raise NotImplementedError

    @staticmethod
    def _n_slices(duration_s: float, min_slice: int = 3, max_slice: int = 60) -> int:
        if duration_s <= 0:
            return 1
        n = int(duration_s / 120)
        n = max(min_slice, min(max_slice, n))
        return n

    @staticmethod
    def _reconcile(parent_vol: float, children: list[ChildOrder]) -> None:
        if children:
            total = sum(ch.volume for ch in children)
            diff = parent_vol - total
            if abs(diff) > 1e-10:
                children[-1].volume = round(children[-1].volume + diff, 4)


# ── TWAP: 等时间切片 ────────────────────────────────

class TWAPAlgorithm(ExecutionAlgorithm):
    """Time-Weighted Average Price: 时间均分, 每片等量"""
    name = "TWAP"

    def slice(self, parent: ParentOrder) -> list[ChildOrder]:
        n = self._n_slices(parent.duration_seconds())
        vol_per_slice = parent.total_volume / n
        interval = parent.duration_seconds() / n

        children = []
        for i in range(n):
            children.append(ChildOrder(
                sequence=i + 1,
                target_time=parent.start_time + timedelta(seconds=interval * (i + 0.5)),
                volume=round(vol_per_slice, 4),
                price_hint=parent.current_price,
                order_type="market",
            ))
        self._reconcile(parent.total_volume, children)
        return children


# ── VWAP: 按历史成交量分布切片 ─────────────────────

class VWAPAlgorithm(ExecutionAlgorithm):
    """Volume-Weighted Average Price: 按历史 volume profile 切片.

    需要 historical_volume_profile: 每根 bar 的成交量数组 (相对值, 总和=1).
    profile 来自 execution/volume_profile.py (本模块内置 fallback: U-shape).
    """
    name = "VWAP"

    def __init__(self, historical_volume_profile: np.ndarray | None = None):
        if historical_volume_profile is None:
            n = 96
            x = np.linspace(0, np.pi, n)
            profile = 1.0 - np.sin(x)
            profile = profile + 0.2
            self.profile = profile
        else:
            self.profile = historical_volume_profile / historical_volume_profile.sum()

    def slice(self, parent: ParentOrder) -> list[ChildOrder]:
        n_bars = min(len(self.profile), self._n_slices(parent.duration_seconds()))
        weights = self.profile[:n_bars]
        weights = weights / weights.sum()
        interval = parent.duration_seconds() / n_bars

        children = []
        for i in range(n_bars):
            vol = parent.total_volume * weights[i]
            children.append(ChildOrder(
                sequence=i + 1,
                target_time=parent.start_time + timedelta(seconds=interval * (i + 0.5)),
                volume=round(vol, 4),
                price_hint=parent.current_price,
                order_type="market",
            ))
        self._reconcile(parent.total_volume, children)
        return children


# ── POV: 按市场实时成交量的固定比例 ────────────────

class POVAlgorithm(ExecutionAlgorithm):
    """Percentage of Volume: 跟踪市场实时成交量的 participation_rate.

    传入 estimated_market_volume_per_sec (broker 给的), 算法按比例分配.
    participation_rate: 0.05 = 5% (保守), 0.30 = 30% (激进)
    """
    name = "POV"

    def __init__(self, participation_rate: float = 0.10,
                 estimated_market_volume: float = 100.0):
        self.participation_rate = participation_rate
        self.estimated_market_volume = estimated_market_volume

    def slice(self, parent: ParentOrder) -> list[ChildOrder]:
        n = self._n_slices(parent.duration_seconds())
        cap = self.estimated_market_volume * self.participation_rate
        actual_total = min(parent.total_volume, cap)
        vol_per_slice = actual_total / n
        interval = parent.duration_seconds() / n

        children = []
        for i in range(n):
            children.append(ChildOrder(
                sequence=i + 1,
                target_time=parent.start_time + timedelta(seconds=interval * (i + 0.5)),
                volume=round(vol_per_slice, 4),
                price_hint=parent.current_price,
                order_type="market",
            ))
        self._reconcile(actual_total, children)
        if actual_total < parent.total_volume:
            logger.warning(f"POV cap: 只下了 {actual_total}/{parent.total_volume} 手 "
                          f"(participation {self.participation_rate:.0%} x market {self.estimated_market_volume})")
        return children


# ── IS: Implementation Shortfall ──────────────────

class ISAlgorithm(ExecutionAlgorithm):
    """Implementation Shortfall: urgency-driven.

    高 urgency -> 切片多但更激进 (市价 + deviation 大)
    低 urgency -> 切片少 + 限价 (passive, 等更好的价格)
    """
    name = "IS"

    def __init__(self, base_ticks: int = 2):
        self.base_ticks = base_ticks

    def slice(self, parent: ParentOrder) -> list[ChildOrder]:
        u = max(0.0, min(1.0, parent.urgency))
        n = int(self._n_slices(parent.duration_seconds()) * (1 + u))
        n = max(2, n)
        vol_per_slice = parent.total_volume / n
        interval = parent.duration_seconds() / n

        tick_offset = self.base_ticks * (1 + u * 4)
        sign = 1 if parent.direction == 1 else -1

        children = []
        for i in range(n):
            progress = (i + 1) / n
            local_u = u * (0.5 + progress * 0.5)
            local_offset = self.base_ticks * (1 + local_u * 4) * sign

            if u >= 0.7:
                children.append(ChildOrder(
                    sequence=i + 1,
                    target_time=parent.start_time + timedelta(seconds=interval * (i + 0.5)),
                    volume=round(vol_per_slice, 4),
                    price_hint=parent.current_price,
                    order_type="market",
                ))
            else:
                limit_price = parent.current_price + local_offset * 0.01
                children.append(ChildOrder(
                    sequence=i + 1,
                    target_time=parent.start_time + timedelta(seconds=interval * (i + 0.5)),
                    volume=round(vol_per_slice, 4),
                    price_hint=round(limit_price, 2),
                    order_type="limit",
                    limit_offset=local_offset,
                ))
        self._reconcile(parent.total_volume, children)
        return children


# ── 调度器 ────────────────────────────────────────

class AlgoDispatcher:
    """根据父单特征选择算法 + 输出子单.

    lookup:
      - 大单 + 高 urgency -> IS
      - 大单 + 低 urgency -> VWAP
      - 小单 + 任何 urgency -> TWAP
      - 特殊 (volume cap) -> POV
    """

    def __init__(self, volume_threshold: float = 50.0,
                 urgency_threshold: float = 0.5):
        self.volume_threshold = volume_threshold
        self.urgency_threshold = urgency_threshold

    def dispatch(self, parent: ParentOrder) -> list[ChildOrder]:
        if parent.total_volume > self.volume_threshold * 2 and parent.urgency >= 0.7:
            algo = ISAlgorithm()
        elif parent.total_volume > self.volume_threshold:
            algo = VWAPAlgorithm()
        else:
            algo = TWAPAlgorithm()
        logger.info(f"[AlgoDispatcher] {algo.name} -> vol={parent.total_volume}, "
                    f"urgency={parent.urgency:.2f}")
        return algo.slice(parent)
