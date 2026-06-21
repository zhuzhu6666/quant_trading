"""
Event Bus — 架构核心通信层

发布/订阅模式，所有模块通过事件总线通信，松耦合。
事件类型包括：Tick、Bar、Signal、Order、Fill、RiskAlert 等。
"""

import asyncio
import logging
import threading
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
    - ARCH-6 (audit 2026-06-04): 线程安全, subscribe/unsubscribe/publish_sync 持 RLock
    """

    def __init__(self):
        self._subscribers: dict[EventType, list[tuple[int, Callable]]] = defaultdict(list)
        self._stats: dict[EventType, int] = defaultdict(int)
        # ARCH-6: RLock 让 publish 期间 handler 内 subscribe/unsubscribe 不抛
        # RuntimeError, 也能让多线程并发 publish / subscribe 安全
        self._lock = threading.RLock()
        # OPT-3 (audit 2026-06-06): publish_async_fire_and_forget 后台 loop
        # 解决: 同步 caller (paper path) 想异步跑 async handler 不阻塞
        # 设计: daemon 线程跑 asyncio loop, 永不退出 (跟程序同寿)
        # 调用方: bus.publish_async_ff(event) 立即返回, handler 在后台跑
        self._async_loop: asyncio.AbstractEventLoop | None = None
        self._async_thread: threading.Thread | None = None
        self._async_started = threading.Event()  # 启动完成信号
        self._start_async_loop()

    def _start_async_loop(self):
        """启动 daemon 线程跑 asyncio event loop, fire-and-forget 异步派发用.
        幂等: 多次调用只启动 1 个 loop.
        """
        if self._async_thread is not None and self._async_thread.is_alive():
            return
        def _runner():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._async_loop = loop
            self._async_started.set()
            loop.run_forever()
        t = threading.Thread(target=_runner, name="EventBusAsyncLoop", daemon=True)
        t.start()
        # 最多等 1s 等 loop 起来 (通常 < 10ms)
        self._async_started.wait(timeout=1.0)

    def subscribe(self, event_type: EventType, handler: Callable, priority: int = 50):
        """订阅事件。priority越小越先执行。"""
        with self._lock:
            self._subscribers[event_type].append((priority, handler))
            self._subscribers[event_type].sort(key=lambda x: x[0])

    def unsubscribe(self, event_type: EventType, handler: Callable):
        """取消订阅"""
        with self._lock:
            self._subscribers[event_type] = [
                (p, h) for p, h in self._subscribers[event_type] if h != handler
            ]

    async def publish(self, event: Event):
        """发布事件到所有订阅者"""
        # ARCH-6: snapshot handlers 在锁内, 实际派发在锁外
        with self._lock:
            handlers = list(self._subscribers.get(event.type, []))
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
        # ARCH-6: snapshot 后再迭代, 期间 handler 内修改 _subscribers 不会炸
        with self._lock:
            handlers = list(self._subscribers.get(event.type, []))
            self._stats[event.type] += 1

        for priority, handler in handlers:
            try:
                result = handler(event)
                if asyncio.iscoroutine(result):
                    logger.warning(
                        f"Handler {handler.__name__} is async, skipped in sync mode "
                        f"(use bus.publish_async_ff() for non-blocking fire-and-forget)"
                    )
            except Exception:
                logger.exception(
                    f"Handler {handler.__name__} failed for {event.type.name}"
                )

    def publish_async_ff(self, event: Event):
        """OPT-3 (audit 2026-06-06): 异步 fire-and-forget 发布.
        ──────────────────────────────────────────────────
        跟 publish_sync 同调用签名 (同步 caller, 立即返回), 但内部把
        async handler 链投到后台 asyncio loop, 主线程不阻塞.

        旧路径: paper / live 同步循环调 publish_sync, 遇到 async handler 报警告 + 跳过
        新路径: 调 publish_async_ff, async handler 真在后台跑, 主线程继续 trade loop

        跟 publish (async 方法) 的区别:
        - publish(event) 是 coroutine, caller 必须 await, 阻塞到所有 handler 完
        - publish_async_ff(event) 立即返回, 不等 handler

        用法:
            # 旧: 同步 caller 想跑 async handler (会被警告 + 跳过)
            bus.publish_sync(event)

            # 新: 同步 caller 真跑 async handler (后台执行, 不阻塞)
            bus.publish_async_ff(event)
        """
        if self._async_loop is None or self._async_loop.is_closed():
            logger.warning("[OPT-3] async loop 未就绪, fallback 到 publish_sync")
            return self.publish_sync(event)
        # 投到后台 loop
        try:
            asyncio.run_coroutine_threadsafe(self.publish(event), self._async_loop)
        except RuntimeError as e:
            logger.warning(f"[OPT-3] async loop 已关闭, fallback: {e}")
            self.publish_sync(event)

    @property
    def stats(self) -> dict:
        return dict(self._stats)

    @property
    def async_loop_running(self) -> bool:
        """OPT-3: 后台 asyncio loop 是否在跑 (publish_async_ff 可用前提)"""
        return self._async_loop is not None and not self._async_loop.is_closed()


# 全局单例
# ARCH-2 (audit 2026-06-21): 通过 _LazyBusProxy 委托给 AppContext.
class _LazyBusProxy:
    _local: EventBus | None = None

    def _target(self) -> EventBus:
        try:
            from core.app import AppContext
            if AppContext._shared is not None:
                return AppContext._shared.event_bus
        except ImportError:
            pass
        local = object.__getattribute__(self, "_local")
        if local is None:
            local = EventBus()
            object.__setattr__(self, "_local", local)
        return local

    def __getattr__(self, name):
        return getattr(self._target(), name)

    def __setattr__(self, name, value):
        if name == "_local":
            object.__setattr__(self, name, value)
        else:
            setattr(self._target(), name, value)

bus: _LazyBusProxy = _LazyBusProxy()
