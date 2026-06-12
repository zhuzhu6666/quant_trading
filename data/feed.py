"""
Data Feed — 历史数据回放

回测模式：从DataStore读取历史bar，按时间顺序推送到EventBus。
支持多品种、多周期数据对齐。
"""

import logging

import numpy as np
import pandas as pd

from core.event_bus import bus, Event, EventType
from core.clock import clock
from data.store import DataStore

logger = logging.getLogger(__name__)


class DataFeed:
    """
    历史数据回放引擎

    用法:
        feed = DataFeed(store, symbol="XAUUSD+", timeframes=["M5","H1"])
        feed.load()
        for bar in feed.stream():
            strategy.on_bar(bar)
    """

    def __init__(self, store: DataStore, symbol: str,
                 timeframes: list[str] | None = None):
        self.store = store
        self.symbol = symbol
        self.timeframes = timeframes or ["M5", "H1"]

        # 每个周期一个有序bar列表
        self._bars: dict[str, list[dict]] = {}
        # 每个周期的当前指针
        self._cursor: dict[str, int] = {}

    def load(self, start: str | None = None, end: str | None = None):
        """从存储加载历史数据"""
        for tf in self.timeframes:
            df = self.store.load_bars(self.symbol, tf, start=start, end=end)
            if df.empty:
                logger.warning(f"No {tf} data for {self.symbol}")
                self._bars[tf] = []
                self._cursor[tf] = 0
                continue

            bars = []
            # audit 2026-06-12 P1-2: iterrows() → numpy 向量化 (8x faster, pitfall-33)
            times = df.index.to_numpy()
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
                bars.append({
                    "timeframe": tf,
                    "time": float(times[i].timestamp()) if hasattr(times[i], "timestamp") else float(times[i]),
                    "open": float(opens[i]),
                    "high": float(highs[i]),
                    "low": float(lows[i]),
                    "close": float(closes[i]),
                    "volume": float(vols[i]),
                    "complete": True,
                })
            self._bars[tf] = bars
            self._cursor[tf] = 0
            logger.info(f"Loaded {len(bars)} {tf} bars for {self.symbol}")

    def stream(self):
        """生成器：按时间顺序yield所有bar（所有周期混合）"""
        # 收集所有周期bar并按时间排序
        all_bars = []
        for tf in self.timeframes:
            for bar in self._bars.get(tf, []):
                all_bars.append(bar)

        all_bars.sort(key=lambda b: b["time"])

        # 预热期：前500根bar仅更新指标，不产生信号
        warmup = 500
        for i, bar in enumerate(all_bars):
            bar["_warmup"] = i < warmup
            yield bar

    @property
    def n_bars(self) -> dict[str, int]:
        return {tf: len(bars) for tf, bars in self._bars.items()}

    @property
    def date_range(self) -> tuple | None:
        all_times = []
        for bars in self._bars.values():
            all_times.extend(b["time"] for b in bars)
        if not all_times:
            return None
        return (min(all_times), max(all_times))
