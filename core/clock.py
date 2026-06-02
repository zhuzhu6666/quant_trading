"""
Clock — 时钟模块

支持两种模式：
1. RealtimeClock: 真实时间驱动（实盘模式）
2. SimulatedClock: 模拟时间驱动（回测模式）
"""

import time
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class Clock:
    """统一时钟接口"""
    mode: str = "realtime"  # realtime | simulated

    @property
    def now(self) -> float:
        """返回当前 Unix 时间戳"""
        return time.time() if self.mode == "realtime" else self._sim_time

    @property
    def utcnow(self) -> datetime:
        """返回当前 UTC datetime"""
        return datetime.now(timezone.utc) if self.mode == "realtime" \
            else datetime.fromtimestamp(self._sim_time, tz=timezone.utc)

    def timestamp(self) -> int:
        return int(self.now)

    # --- Simulated mode ---
    _sim_time: float = 0.0

    def advance(self, seconds: float):
        """回测模式：推进模拟时钟"""
        if self.mode != "simulated":
            raise RuntimeError("Cannot advance in realtime mode")
        self._sim_time += seconds

    def set(self, ts: float):
        """回测模式：设置模拟时间"""
        self._sim_time = ts


# 全局单例
clock = Clock()
