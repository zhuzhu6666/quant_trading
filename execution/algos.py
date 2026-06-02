"""
execution/algos.py — 智能下单算法 (T1.1 智能路由)

P1-B 任务: 实现 4 个经典执行算法:
  - TWAP (Time-Weighted Average Price): 等时间切片
  - VWAP (Volume-Weighted Average Price): 按历史成交量分布切片
  - POV  (Percentage of Volume): 按市场实时成交量的固定比例跟单
  - IS   (Implementation Shortfall): urgency-based, 越急越偏激进

每个算法接收 ParentOrder, 返回 List[ChildOrder] (含子单的价格/数量/时间).

无 broker 也能单测 — 算法本身只算切片, 不实际下单.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np

logger = logging.getLogger(__name__)


# ── 数据结构 ─────────────────────────────────────────

@dataclass
class ChildOrder:
    """子单: 算法输出, 实际下单单位"""
    sequence: int           # 1, 2, 3, ... 切片序号
    target_time: datetime   # 目标执行时刻
    volume: float           # 本切片手数
    price_hint: float       # 价格提示 (limit 单用), 0 = 用市价
    order_type: str = "market"  # 'market' | 'limit'
    limit_offset: float = 0.0   # 限价相对 mid 的偏移 (ticks)


@dataclass
class ParentOrder:
    """父单: 路由输入"""
    symbol: str
    direction: int          # 1=buy, -1=sell
    total_volume: float     # 总手数
    start_time: datetime
    end_time: datetime
    current_price: float = 0.0   # 决策时刻 mid price
    urgency: float = 0.5          # 0=不急 (用 IS 算法), 1=极急 (immediate)

    def duration_seconds(self) -> float:
        return (self.end_time - self.start_time).total_seconds()


# ── 抽象基类 ─────────────────────────────────────────

class ExecutionAlgorithm(ABC):
    """所有执行算法的接口"""

    name: str = "BASE"

    @abstractmethod
    def slice(self, parent: ParentOrder) -> list[ChildOrder]:
        """输入父单, 输出子单列表"""
        raise NotImplementedError

    @staticmethod
    def _n_slices(duration_s: float, min_slice: int = 3, max_slice: int = 60) -> int:
        """根据持续时间决定切片数 (1-3min/片)"""
        if duration_s <= 0:
            return 1
        n = int(duration_s / 120)  # 2 分钟一片
        return max(min_slice, min(max_slice, n))


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
            # U-shape: 开盘 + 收盘高, 中午低
            # U(0)=1, U(π/2)=0, U(π)=1 → 1 - sin(x)  再归一化
            # 96 根 M15 bar / 天
            n = 96
            x = np.linspace(0, np.pi, n)
            profile = 1.0 - np.sin(x)  # 1.0 → 0.0 → 1.0, 真的 U-shape
            # 避免 0 (中午恰好 0 也不行, 略微抬高)
            profile = profile + 0.2
            self.profile = profile
        else:
            self.profile = historical_volume_profile / historical_volume_profile.sum()

    def slice(self, parent: ParentOrder) -> list[ChildOrder]:
        n_bars = min(len(self.profile), self._n_slices(parent.duration_seconds()))
        # 用 profile 的前 n_bars 个 bin
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
        self.estimated_market_volume = estimated_market_volume  # 单位: 手/总周期

    def slice(self, parent: ParentOrder) -> list[ChildOrder]:
        n = self._n_slices(parent.duration_seconds())
        # 总市场量 × participation_rate = 我们总该下的量
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
        if actual_total < parent.total_volume:
            logger.warning(f"POV cap: 只下了 {actual_total}/{parent.total_volume} 手 "
                          f"(participation {self.participation_rate:.0%} × market {self.estimated_market_volume})")
        return children


# ── IS: Implementation Shortfall ──────────────────

class ISAlgorithm(ExecutionAlgorithm):
    """Implementation Shortfall: urgency-driven.

    高 urgency → 切片多但更激进 (市价 + deviation 大)
    低 urgency → 切片少 + 限价 (passive, 等更好的价格)
    """
    name = "IS"

    def __init__(self, base_ticks: int = 2):
        self.base_ticks = base_ticks

    def slice(self, parent: ParentOrder) -> list[ChildOrder]:
        u = max(0.0, min(1.0, parent.urgency))
        # urgency 高 → 切片更多 (市价冲击大, 分摊), 限价偏移更大
        n = int(self._n_slices(parent.duration_seconds()) * (1 + u))
        n = max(2, n)
        vol_per_slice = parent.total_volume / n
        interval = parent.duration_seconds() / n

        # 限价偏移: 越不急越靠近 mid (passive), 越急越偏离
        tick_offset = self.base_ticks * (1 + u * 4)  # 2 ticks → 最多 10 ticks
        # buy 单用 ask offset (向上) 等更激进, sell 用 bid offset (向下)
        sign = 1 if parent.direction == 1 else -1

        children = []
        for i in range(n):
            # 离散时间进度 → 渐进 urgency 上升 (越临近 end_time 越急)
            progress = (i + 1) / n
            local_u = u * (0.5 + progress * 0.5)  # 0.5*u → 1.0*u
            local_offset = self.base_ticks * (1 + local_u * 4) * sign

            if u >= 0.7:
                # 高 urgency → 市价
                children.append(ChildOrder(
                    sequence=i + 1,
                    target_time=parent.start_time + timedelta(seconds=interval * (i + 0.5)),
                    volume=round(vol_per_slice, 4),
                    price_hint=parent.current_price,
                    order_type="market",
                ))
            else:
                # 低 urgency → 限价 (相对 current_price 偏移)
                limit_price = parent.current_price + local_offset * 0.01  # 假设 1 tick = 0.01
                children.append(ChildOrder(
                    sequence=i + 1,
                    target_time=parent.start_time + timedelta(seconds=interval * (i + 0.5)),
                    volume=round(vol_per_slice, 4),
                    price_hint=round(limit_price, 2),
                    order_type="limit",
                    limit_offset=local_offset,
                ))
        return children


# ── 调度器 ────────────────────────────────────────

class AlgoDispatcher:
    """根据父单特征选择算法 + 输出子单.

    选择规则 (简单启发式):
      - 总手数 < 0.05: 直接市价 (无算法必要)
      - urgency >= 0.7: IS
      - 时长 < 5 分钟 + 大单: VWAP
      - 时长 >= 5 分钟 + 默认: TWAP
      - 跟市: POV
    """

    def __init__(self, vwap_profile: np.ndarray | None = None):
        self.twap = TWAPAlgorithm()
        self.vwap = VWAPAlgorithm(vwap_profile)
        self.pov = POVAlgorithm()
        self.is_algo = ISAlgorithm()

    def dispatch(self, parent: ParentOrder, algo: str | None = None) -> list[ChildOrder]:
        if algo is None:
            algo = self._choose_algo(parent)
        if algo == "MARKET":
            # 直接市价一单
            return [ChildOrder(
                sequence=1,
                target_time=parent.start_time,
                volume=parent.total_volume,
                price_hint=parent.current_price,
                order_type="market",
            )]
        algo_obj = {
            "TWAP": self.twap,
            "VWAP": self.vwap,
            "POV": self.pov,
            "IS": self.is_algo,
        }[algo]
        return algo_obj.slice(parent)

    def _choose_algo(self, parent: ParentOrder) -> str:
        if parent.total_volume < 0.05:
            return "MARKET"
        if parent.urgency >= 0.7:
            return "IS"
        if parent.duration_seconds() < 300 and parent.total_volume > 0.5:
            return "VWAP"
        if parent.total_volume > 1.0:
            return "POV"
        return "TWAP"
