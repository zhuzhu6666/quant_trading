"""
Tick Receiver — MT5实时Tick接收

从MT5订阅实时tick数据流，推送到EventBus。
支持自动重连和断线缓存。
"""

import asyncio
import logging
from collections import deque
from datetime import datetime, timezone

from core.event_bus import bus, Event, EventType
from core.clock import clock

logger = logging.getLogger(__name__)

# 条件导入 — MT5可能未安装
try:
    import MetaTrader5 as mt5
    HAS_MT5 = True
except ImportError:
    HAS_MT5 = False
    logger.warning("MetaTrader5 not installed — tick streaming disabled")


class TickReceiver:
    """
    MT5 Tick 实时接收器
    
    特性：
    - 异步tick接收
    - 环形缓冲区（防内存溢出）
    - 自动重连
    - 推送到EventBus
    """

    def __init__(self, symbol: str, buffer_size: int = 10000):
        self.symbol = symbol
        self.buffer = deque(maxlen=buffer_size)
        self._running = False
        self._last_tick_time: float = 0.0

    async def start(self):
        """启动tick接收"""
        if not HAS_MT5:
            logger.error("Cannot start: MetaTrader5 not available")
            return

        if not mt5.initialize():
            logger.error(f"MT5 init failed: {mt5.last_error()}")
            return

        self._running = True
        logger.info(f"Tick receiver started for {self.symbol}")

        while self._running:
            try:
                tick = mt5.symbol_info_tick(self.symbol)
                if tick is None:
                    await asyncio.sleep(0.01)
                    continue

                ts = tick.time_msc / 1000.0 if tick.time_msc else clock.now
                tick_data = {
                    "symbol": self.symbol,
                    "bid": tick.bid,
                    "ask": tick.ask,
                    "last": tick.last,
                    "volume": tick.volume,
                    "time": ts,
                }
                self.buffer.append(tick_data)
                self._last_tick_time = ts

                # 推送到EventBus
                await bus.publish(Event(
                    type=EventType.TICK,
                    data=tick_data,
                    timestamp=ts,
                    source="tick_receiver",
                ))

                await asyncio.sleep(0.01)  # ~100Hz max

            except Exception:
                logger.exception("Tick receive error")
                await asyncio.sleep(1)

    def stop(self):
        """停止tick接收"""
        self._running = False
        if HAS_MT5:
            mt5.shutdown()
        logger.info("Tick receiver stopped")

    @property
    def latest(self) -> dict | None:
        return self.buffer[-1] if self.buffer else None

    @property
    def spread(self) -> float:
        t = self.latest
        return t["ask"] - t["bid"] if t else 0.0
