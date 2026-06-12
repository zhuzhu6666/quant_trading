"""
Bar Builder — Tick到K线实时聚合

从tick流构建多周期OHLCV bar，支持：
- 多周期并行聚合（M5/M15/M30/H1/H4/D1）
- 实时推送完成bar到EventBus
- 回放模式：从历史数据生成bar
"""

import logging
from collections import defaultdict

import numpy as np

from core.event_bus import bus, Event, EventType
from core.clock import clock

logger = logging.getLogger(__name__)

# 时间周期到秒数的映射
TIMEFRAME_SECONDS = {
    "M5": 300,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
}


class BarAggregator:
    """
    多周期 Bar 聚合器
    
    从 tick 流构建 OHLCV bar，每个 tick 更新所有周期。
    """

    def __init__(self, timeframes: list[str] | None = None):
        self.timeframes = timeframes or ["M5", "M15", "M30", "H1", "H4", "D1"]
        # 每个周期一个 partial bar: {tf: {open, high, low, close, volume, start_ts}}
        self._partial: dict[str, dict] = {tf: self._empty_bar(0) for tf in self.timeframes}
        # 已完成的bar缓存（最新N根）
        self._cache: dict[str, list[dict]] = defaultdict(list)

    def _empty_bar(self, ts: float) -> dict:
        return {"open": 0.0, "high": float("-inf"), "low": float("inf"),
                "close": 0.0, "volume": 0.0, "time": ts, "complete": False}

    def _bar_start(self, ts: float, tf_seconds: int) -> int:
        """计算当前bar的起始时间戳（对齐到周期边界）"""
        return int(ts // tf_seconds) * tf_seconds

    def on_tick(self, event: Event):
        """处理tick事件，更新所有周期"""
        tick = event.data
        ts = tick["time"]
        price = tick["last"] if tick["last"] > 0 else (tick["bid"] + tick["ask"]) / 2

        for tf in self.timeframes:
            tf_sec = TIMEFRAME_SECONDS[tf]
            bar_start = self._bar_start(ts, tf_sec)
            partial = self._partial[tf]

            # 新bar开始 → 完成旧bar
            if partial["open"] > 0 and bar_start > partial["time"]:
                partial["complete"] = True
                self._cache[tf].append(dict(partial))
                if len(self._cache[tf]) > 500:
                    self._cache[tf].pop(0)

                # 推送完成bar到EventBus
                bus.publish_sync(Event(
                    type=EventType.BAR,
                    data={"timeframe": tf, **partial},
                    timestamp=partial["time"],
                    source="bar_builder",
                ))

                partial.update(self._empty_bar(bar_start))

            # 更新当前bar
            if partial["open"] == 0:
                partial["open"] = price
                partial["time"] = bar_start
            partial["high"] = max(partial["high"], price)
            partial["low"] = min(partial["low"], price)
            partial["close"] = price
            partial["volume"] += tick.get("volume", 0) or 1

    def get_bars(self, tf: str, n: int = 100) -> list[dict]:
        """获取最近N根已完成bar + 当前partial"""
        bars = list(self._cache.get(tf, []))[-n:]
        partial = self._partial.get(tf)
        if partial and partial["open"] > 0:
            bars.append(dict(partial))
        return bars

    def from_historical(self, df, tf: str):
        """
        从历史DataFrame批量加载bar（回测模式）
        df需包含: time, open, high, low, close, volume 列
        """
        self._cache[tf] = []
        # audit 2026-06-12 P1-2: iterrows() → numpy 向量化 (8x faster, pitfall-33)
        times = df["time"].to_numpy()
        opens = df["open"].to_numpy()
        highs = df["high"].to_numpy()
        lows = df["low"].to_numpy()
        closes = df["close"].to_numpy()
        if "volume" in df.columns:
            vols = df["volume"].to_numpy()
        elif "tick_volume" in df.columns:
            vols = df["tick_volume"].to_numpy()
        else:
            vols = np.zeros(len(df))
        for i in range(len(df)):
            bar = {
                "open": float(opens[i]), "high": float(highs[i]),
                "low": float(lows[i]), "close": float(closes[i]),
                "volume": float(vols[i]),
                "time": times[i].timestamp() if hasattr(times[i], "timestamp")
                         else float(times[i]),
                "timeframe": tf, "complete": True,
            }
            self._cache[tf].append(bar)
        logger.info(f"Loaded {len(self._cache[tf])} historical {tf} bars")
