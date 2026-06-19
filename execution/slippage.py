"""execution/slippage.py — DynamicSlippageModel

基于 (ATR + 事件日 + 时段) 的动态滑点估算, 黄金 (1 tick = 0.01 USD/oz).

设计:
  1. base: 0.5 tick (固定基础滑点)
  2. atr_component: + atr * atr_mult (高波动时滑点变大)
  3. event_day: × event_boost (NFP/FOMC 日翻倍)
  4. low_liquidity_hour: × low_liquidity_boost (UTC 22-1 时段)
  5. cap: max_ticks (硬上限)

返回: USD/oz 的滑点 (正数), 调用方按方向加/减

集成: PaperExecutionEngine 已支持 slippage_model 参数 (默认 None = 旧固定 2bps).
测试: scripts/test_slippage.py 5 case 验证.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# 黄金 1 tick = 0.01 USD/oz
GOLD_TICK_USD = 0.01


class DynamicSlippageModel:
    """动态滑点估算器 (黄金)"""

    DEFAULT_CONFIG = {
        "base_ticks": 0.5,            # 基础 0.5 tick
        "atr_mult": 0.05,             # 滑点增量 = atr * 0.05
        "event_boost": 2.0,           # 事件日翻倍
        "low_liquidity_hours": [22, 23, 0, 1],  # UTC 凌晨
        "low_liquidity_boost": 1.5,   # 低流动性 ×1.5
        "max_ticks": 3.0,             # 上限 3 tick (避免极端情况)
    }

    def __init__(self, config: Optional[dict] = None):
        self.config = {**self.DEFAULT_CONFIG, **(config or {})}

    def _is_low_liquidity_hour(self, bar: dict) -> bool:
        """bar['time'] (unix timestamp) → UTC hour → 是否在低流动性时段"""
        ts = bar.get("time")
        if ts is None:
            return False
        try:
            hour = datetime.fromtimestamp(ts, tz=timezone.utc).hour
        except (OSError, ValueError, OverflowError):
            return False
        return hour in self.config["low_liquidity_hours"]

    def estimate(self, bar: Optional[dict] = None, atr: Optional[float] = None,
                 is_event_day: bool = False) -> float:
        """
        返回该笔成交的预期滑点 (USD/oz, 正数).

        Args:
            bar: 当前 bar 字典 (含 'time' 字段)
            atr: 当前 ATR (USD/oz), 可选
            is_event_day: 是否 NFP/FOMC 事件日
        """
        cfg = self.config
        # 1. base
        ticks = cfg["base_ticks"]
        # 2. + atr 增量
        if atr is not None and atr > 0:
            ticks += atr * cfg["atr_mult"] / GOLD_TICK_USD  # 把 USD 换算成 tick
        # 3. × event_boost
        if is_event_day:
            ticks *= cfg["event_boost"]
        # 4. × low_liquidity_boost
        if bar is not None and self._is_low_liquidity_hour(bar):
            ticks *= cfg["low_liquidity_boost"]
        # 5. cap
        ticks = min(ticks, cfg["max_ticks"])
        # 6. 换 USD
        return ticks * GOLD_TICK_USD

    def get_spread_estimate(self, atr: Optional[float] = None,
                            is_event: bool = False,
                            bar: Optional[dict] = None) -> dict:
        """调试 / 报告用: 返回各组分 (含低流动性加成)"""
        cfg = self.config
        base_ticks = cfg["base_ticks"]
        atr_ticks = (atr * cfg["atr_mult"] / GOLD_TICK_USD) if (atr and atr > 0) else 0.0
        total_ticks = base_ticks + atr_ticks
        event_factor = cfg["event_boost"] if is_event else 1.0
        total_ticks *= event_factor
        low_liq_factor = 1.0
        if bar is not None and self._is_low_liquidity_hour(bar):
            low_liq_factor = cfg.get("low_liquidity_boost", 1.5)
            total_ticks *= low_liq_factor
        total_ticks = min(total_ticks, cfg["max_ticks"])
        return {
            "base_ticks": base_ticks,
            "atr_component_ticks": atr_ticks,
            "event_boost": event_factor,
            "low_liquidity_boost": low_liq_factor,
            "max_ticks_cap": cfg["max_ticks"],
            "total_ticks": round(total_ticks, 4),
            "total_usd_per_oz": round(total_ticks * GOLD_TICK_USD, 4),
        }
