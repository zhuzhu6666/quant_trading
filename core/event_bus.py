"""
Event Bus — 架构核心通信层

发布/订阅模式，所有模块通过事件总线通信，松耦合。
事件类型包括：Tick、Bar、Signal、Order、Fill、RiskAlert 等。
"""

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)


class EventType(Enum):
    """事件类型枚举"""
    TICK = auto()              # Tick数据
    BAR = auto()               # K线完成
    SIGNAL = auto()            # 策略信号
    ORDER_NEW = auto()         # 新订单
    ORDER_FILLED = auto()      # 订单成交
    ORDER_CANCELLED = auto()   # 订单取消
    ORDER_REJECTED = auto()    # 订单拒绝
    RISK_ALERT = auto()        # 风控告警
    CIRCUIT_BREAK = auto()     # 熔断
    POSITION_UPDATE = auto()   # 持仓更新
    EQUITY_UPDATE = auto()     # 权益更新
    SYSTEM = auto()            # 系统事件


@dataclass(frozen=True, slots=True)
class Event:
    """事件数据类"""
    type: EventType
    data: Any = None
    timestamp: float = 0.0
    source: str = ""


class EventBus:
    """
    异步事件总线
    
    特性：
    - 按事件类型订阅
    - 支持优先级（数字越小越先执行）
    - 异常隔离：一个handler异常不影响其他handler
    - 内置事件计数统计
    """

    def __init__(self):
        self._subscribers: dict[EventType, list[tuple[int, Callable]]] = defaultdict(list)
        self._stats: dict[EventType, int] = defaultdict(int)

    def subscribe(self, event_type: EventType, handler: Callable, priority: int = 50):
        """订阅事件。priority越小越先执行。"""
        self._subscribers[event_type].append((priority, handler))
        self._subscribers[event_type].sort(key=lambda x: x[0])

    def unsubscribe(self, event_type: EventType, handler: Callable):
        """取消订阅"""
        self._subscribers[event_type] = [
            (p, h) for p, h in self._subscribers[event_type] if h != handler
        ]

    async def publish(self, event: Event):
        """发布事件到所有订阅者"""
        handlers = self._subscribers.get(event.type, [])
        self._stats[event.type] += 1

        for priority, handler in handlers:
            try:
                result = handler(event)
                if asyncio.iscoroutine(result) or asyncio.isfuture(result):
                    await result
            except Exception:
                logger.exception(
                    f"Handler {handler.__name__} failed for {event.type.name}"
                )

    def publish_sync(self, event: Event):
        """同步发布（回测模式用）"""
        handlers = self._subscribers.get(event.type, [])
        self._stats[event.type] += 1

        for priority, handler in handlers:
            try:
                result = handler(event)
                if asyncio.iscoroutine(result):
                    logger.warning(
                        f"Handler {handler.__name__} is async, skipped in sync mode"
                    )
            except Exception:
                logger.exception(
                    f"Handler {handler.__name__} failed for {event.type.name}"
                )

    @property
    def stats(self) -> dict:
        return dict(self._stats)


# 全局单例
bus = EventBus()
