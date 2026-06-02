"""
Strategy Registry — 策略注册+热加载

特性：
- 注册制：装饰器注册策略类
- 多时间框架：一个策略可注册到多个TF
- 动态启用/禁用
"""

import logging

logger = logging.getLogger(__name__)


class StrategyRegistry:
    """策略注册表"""

    def __init__(self):
        self._strategies: dict[str, type] = {}  # name → class
        self._active: set[str] = set()          # 当前启用

    def register(self, name: str, timeframes: list[str] | None = None):
        """装饰器：注册策略类"""
        def decorator(cls):
            cls._reg_name = name
            cls._reg_timeframes = timeframes or ["H1"]
            self._strategies[name] = cls
            logger.debug(f"Registered strategy: {name}")
            return cls
        return decorator

    def create(self, name: str, symbol: str, timeframe: str, **kwargs):
        """创建策略实例"""
        cls = self._strategies.get(name)
        if cls is None:
            raise KeyError(f"Strategy '{name}' not registered")

        # 合并类参数和实例参数
        params = {**getattr(cls, "params", {}), **kwargs}
        instance = cls(name=name, symbol=symbol, timeframe=timeframe)
        instance.params = params
        return instance

    def enable(self, name: str):
        self._active.add(name)

    def disable(self, name: str):
        self._active.discard(name)

    def list(self) -> list[str]:
        return list(self._strategies.keys())

    def is_active(self, name: str) -> bool:
        return name in self._active


# 全局注册表
strategy_registry = StrategyRegistry()
