"""execution/match_replay.py — 撮合回放 (历史 bar 重建 book)

用历史 OHLCV bar 拆 tick 序列 + 随机游走, 构造委托簿, 然后按 size/level 撮合.

设计 (简化版):
  1. tick 序列: open → ... → close, 中间走 [low, high] 区间 (Brownian bridge)
  2. book: 最后一 tick 为 mid, 上下 spread 各 5 档, 各档 size = volume / depth
  3. 撮合: side=1 吃 ask (从低到高), side=-1 吃 bid (从高到低), 不够 partial fill

依赖: numpy (已有)
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


class MatchReplayEngine:
    """单 bar 撮合回放"""

    DEFAULT_CONFIG = {
        "n_ticks": 100,
        "seed": 42,
        "book_depth": 5,
    }

    def __init__(self, bar: dict, config: Optional[dict] = None):
        self.bar = bar
        self.config = {**self.DEFAULT_CONFIG, **(config or {})}
        self._rng = np.random.default_rng(self.config["seed"])
        self._ticks: list[dict] = []
        self._book: dict = {"bids": [], "asks": []}

    def _generate_ticks(self) -> list[dict]:
        """OHLCV → N 个 tick (Brownian bridge: open → close, 走 low~high)"""
        n = self.config["n_ticks"]
        o = float(self.bar["open"])
        c = float(self.bar["close"])
        h = float(self.bar["high"])
        l = float(self.bar["low"])
        v = float(self.bar.get("volume", 0))
        bar_time = float(self.bar.get("time", 0))
        dt = 1.0 / n  # 每 tick 占 bar 的 1/n 时间

        # 基础线 (open → close 线性插值)
        base = np.linspace(o, c, n)
        # 残差: 把 max/min 偏差 (h - max(o,c), l - min(o,c)) 加在中间
        # 用 sin 形的扰动 (峰 = mid bar)
        env_top = max(h, max(o, c))
        env_bot = min(l, min(o, c))
        # 取每个 tick 到 base 的偏差方向 + 大小
        # 简化: 整个序列按 min/max envelope 缩放
        # 让 min(ticks) = l, max(ticks) = h
        ticks = base.copy()
        ticks_min = ticks.min()
        ticks_max = ticks.max()
        # 缩放到 [env_bot, env_top] (如果范围 < 0 跳过)
        if ticks_max - ticks_min > 1e-12:
            ticks = env_bot + (ticks - ticks_min) / (ticks_max - ticks_min) * (env_top - env_bot)
        # 加少量随机扰动 (基于 tick_size 0.01)
        noise = self._rng.normal(0, 0.01, size=n)
        ticks = np.round(ticks + noise, 2)

        bar_volume_per_tick = v / n if v > 0 else 0
        return [
            {"time": bar_time + i * dt, "price": float(t), "volume": bar_volume_per_tick}
            for i, t in enumerate(ticks)
        ]

    def _build_book(self) -> dict:
        """从 tick 序列建 book: 最后一个 tick = mid, 上下各 depth 档"""
        if not self._ticks:
            self._ticks = self._generate_ticks()
        last_price = self._ticks[-1]["price"]
        # spread = 0.5 tick (黄金 1 tick = 0.01, spread = 0.005)
        spread = 0.01 / 2
        depth = self.config["book_depth"]
        # 总量 = last tick 的 volume
        total_volume = self._ticks[-1]["volume"] or 0.01
        size_per_level = total_volume / depth

        # 真实黄金 1 手 = 100 oz, 但 book_size 抽象: 用 volume / depth 直接做 level size
        # 这样大单量超过 total_volume 会 partial
        bids = []
        asks = []
        for i in range(1, depth + 1):
            bids.append((round(last_price - i * spread, 2), size_per_level))
            asks.append((round(last_price + i * spread, 2), size_per_level))
        return {"bids": bids, "asks": asks, "mid": last_price}

    def match_order(self, side: int, size: float = 0.01) -> dict:
        """
        side=1 (buy) 吃 ask, side=-1 (sell) 吃 bid. size 单位: 手.
        返回 {filled_price, filled_size, level, slippage_ticks, partial}.
        """
        if not self._book["bids"] and not self._book["asks"]:
            self._book = self._build_book()

        if side == 1:
            # buy: 从最低 ask 开始吃
            levels = sorted(self._book["asks"], key=lambda x: x[0])
        else:
            # sell: 从最高 bid 开始吃
            levels = sorted(self._book["bids"], key=lambda x: -x[0])

        filled_size = 0.0
        filled_value = 0.0
        max_level = 0
        mid = self._book["mid"]
        for i, (price, level_size) in enumerate(levels, 1):
            if filled_size >= size:
                break
            take = min(size - filled_size, level_size)
            filled_size += take
            filled_value += take * price
            max_level = i
        partial = filled_size < size
        avg_price = filled_value / filled_size if filled_size > 0 else 0.0
        # 滑点 (tick): |avg - mid| / 0.01
        slippage_ticks = abs(avg_price - mid) / 0.01 if filled_size > 0 else 0.0
        return {
            "filled_price": round(avg_price, 2),
            "filled_size": round(filled_size, 4),
            "level": max_level,
            "slippage_ticks": round(slippage_ticks, 2),
            "partial": partial,
            "mid": mid,
        }

    def replay(self, side: int, size: float = 0.01) -> dict:
        """一次完整回放: 生成 ticks → 建 book → 撮合"""
        self._ticks = self._generate_ticks()
        self._book = self._build_book()
        result = self.match_order(side, size)
        result["tick_count"] = len(self._ticks)
        result["book_snapshot"] = {
            "mid": self._book["mid"],
            "bid_top": self._book["bids"][0] if self._book["bids"] else None,
            "ask_top": self._book["asks"][0] if self._book["asks"] else None,
            "spread_ticks": 1.0,  # 固定 0.5 tick per side
        }
        return result
