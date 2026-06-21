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
# ARCH-2 (audit 2026-06-21): 通过 _LazyClockProxy 委托给 AppContext.
class _LazyClockProxy:
    _local: Clock | None = None

    def _target(self) -> Clock:
        try:
            from core.app import AppContext
            if AppContext._shared is not None:
                return AppContext._shared.clock
        except ImportError:
            pass
        local = object.__getattribute__(self, "_local")
        if local is None:
            local = Clock()
            object.__setattr__(self, "_local", local)
        return local

    def __getattr__(self, name):
        return getattr(self._target(), name)

    def __setattr__(self, name, value):
        if name == "_local":
            object.__setattr__(self, name, value)
        else:
            setattr(self._target(), name, value)

clock: _LazyClockProxy = _LazyClockProxy()
