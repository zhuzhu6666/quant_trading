"""
core/app.py — Application Context (dependency injection container).

ARCH-2 (audit 2026-06-21): 引入 DI 容器, 消除模块级全局单例.

用法:
    # 获取默认实例 (向后兼容, 替代 from core.state import state)
    ctx = AppContext.shared()

    # 测试中重置
    AppContext.reset()

    # 获取各组件
    bus = ctx.event_bus
    state = ctx.state
    clock = ctx.clock
    store = ctx.data_store
    factor_registry = ctx.factor_registry

设计原则:
  - 模块级全局变量 (state / bus / clock 等) 现在从 AppContext 延迟读取,
    因此测试可以 reset() 后获得全新实例.
  - 生产代码保持不变: `from core.state import state` 仍然可用.
"""

from __future__ import annotations

import threading

from core.event_bus import EventBus
from core.state import StateContainer
from core.clock import Clock


class AppContext:
    """应用上下文 — 持有所有可注入的核心组件."""

    _shared: AppContext | None = None
    _lock: threading.Lock = threading.Lock()

    def __init__(self):
        self._event_bus: EventBus | None = None
        self._state: StateContainer | None = None
        self._clock: Clock | None = None
        # DataStore 是延迟初始化的单例 (data/store.py), 不在此处创建

    @classmethod
    def shared(cls) -> AppContext:
        """返回全局共享的 AppContext 实例 (线程安全)."""
        if cls._shared is None:
            with cls._lock:
                if cls._shared is None:
                    cls._shared = cls()
        return cls._shared

    @classmethod
    def reset(cls):
        """仅供测试使用: 重置全局 AppContext + 所有组件.

        注意: DataStore 的单例也需要手动重置 (DataStore._instance = None).
        """
        with cls._lock:
            old = cls._shared
            cls._shared = None
            if old is not None:
                old._event_bus = None
                old._state = None
                old._clock = None

    # ── 组件访问 (懒初始化) ──

    @property
    def event_bus(self) -> EventBus:
        if self._event_bus is None:
            self._event_bus = EventBus()
        return self._event_bus

    @property
    def state(self) -> StateContainer:
        if self._state is None:
            self._state = StateContainer()
        return self._state

    @property
    def clock(self) -> Clock:
        if self._clock is None:
            self._clock = Clock()
        return self._clock

    @property
    def data_store(self):
        """DataStore — 从 data.store 懒加载 (自身是单例)."""
        from data.store import DataStore
        return DataStore()

    @property
    def factor_registry(self):
        """FactorRegistry — 从 alpha.registry 懒加载."""
        from alpha.registry import factor_registry
        return factor_registry

    @property
    def strategy_registry(self):
        """StrategyRegistry — 从 strategy.registry 懒加载."""
        from strategy.registry import strategy_registry
        return strategy_registry
