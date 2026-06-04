"""
Order Management System — 订单状态机

订单生命周期:
  NEW → PENDING → SUBMITTED → PARTIAL_FILLED → FILLED → DONE
                            → REJECTED
                            → CANCELLED

特性：
- 完整状态机转换
- 异常自动重试
- 订单簿记录
"""

import logging
import time
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class OrderStatus(Enum):
    NEW = "new"
    PENDING = "pending"
    SUBMITTED = "submitted"
    PARTIAL_FILLED = "partial_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass
class Order:
    """订单数据类"""
    ticket: int = 0
    symbol: str = ""
    direction: int = 0          # 1=buy, -1=sell
    order_type: str = "market"  # market | limit | stop
    volume: float = 0.0
    price: float = 0.0          # 请求价
    sl: float = 0.0
    tp: float = 0.0
    fill_price: float = 0.0     # 实际成交价
    filled_volume: float = 0.0  # BUG-3 (audit 2026-06-04): 累计已成交量
    status: OrderStatus = OrderStatus.NEW
    create_time: float = 0.0
    update_time: float = 0.0
    retry_count: int = 0
    max_retries: int = 3
    comment: str = ""
    meta: dict = field(default_factory=dict)


class OrderManager:
    """
    订单状态机

    管理所有活跃订单的全生命周期。
    """

    VALID_TRANSITIONS = {
        OrderStatus.NEW: [OrderStatus.PENDING, OrderStatus.SUBMITTED,
                          OrderStatus.REJECTED],
        OrderStatus.PENDING: [OrderStatus.SUBMITTED, OrderStatus.CANCELLED, OrderStatus.REJECTED],
        OrderStatus.SUBMITTED: [OrderStatus.PARTIAL_FILLED, OrderStatus.FILLED,
                                 OrderStatus.CANCELLED, OrderStatus.REJECTED],
        # P16: PARTIAL→PARTIAL 允许 (累加 partial_fill), 仍可去 FILLED / CANCELLED
        OrderStatus.PARTIAL_FILLED: [OrderStatus.PARTIAL_FILLED, OrderStatus.FILLED,
                                     OrderStatus.CANCELLED],
        # Terminal states
        OrderStatus.FILLED: [],
        OrderStatus.CANCELLED: [],
        OrderStatus.REJECTED: [],
        OrderStatus.EXPIRED: [],
    }

    def __init__(self):
        self._orders: dict[int, Order] = {}       # ticket → Order
        self._history: list[Order] = []            # 已完成订单
        self._next_ticket = 1000

    # ── 状态转换 ──

    def create(self, symbol: str, direction: int, order_type: str,
               volume: float, price: float, sl: float = 0.0, tp: float = 0.0,
               **meta) -> Order:
        """创建新订单"""
        ticket = self._next_ticket
        self._next_ticket += 1

        order = Order(
            ticket=ticket, symbol=symbol, direction=direction,
            order_type=order_type, volume=volume, price=price,
            sl=sl, tp=tp, create_time=time.time(), update_time=time.time(),
            meta=meta,
        )
        # P16 (BUG-3 修复期间发现): create 应当直接设 status=NEW,
        # 不应 _transition(NEW, NEW) (那会 invalid)
        order.status = OrderStatus.NEW
        self._orders[ticket] = order
        return order

    def submit(self, ticket: int) -> bool:
        return self._transition(ticket, OrderStatus.SUBMITTED)

    def fill(self, ticket: int, fill_price: float, volume: float | None = None):
        order = self._orders.get(ticket)
        if not order:
            return
        order.fill_price = fill_price
        if volume:
            order.volume = volume
        order.filled_volume = volume if volume else order.volume  # BUG-3
        self._transition(order, OrderStatus.FILLED)
        self._archive(order)

    def partial_fill(self, ticket: int, fill_price: float, filled_vol: float):
        """BUG-3 (audit 2026-06-04) 修复:
        累计 filled_volume, == volume 时自动 transition 到 FILLED + 归档。
        原版不归档, 订单永远停在 PARTIAL_FILLED, 泄漏内存 + ticket_number 单调递增。
        """
        order = self._orders.get(ticket)
        if not order:
            return
        order.fill_price = fill_price
        order.filled_volume = (order.filled_volume or 0.0) + filled_vol
        if order.filled_volume >= order.volume - 1e-9:
            # 全成, transition FILLED + 归档
            order.filled_volume = order.volume
            self._transition(order, OrderStatus.FILLED)
            self._archive(order)
            logger.info(f"Order {ticket} partial fills aggregate to full, archived")
        else:
            # 部分成, 仍保持活跃
            self._transition(order, OrderStatus.PARTIAL_FILLED)

    def cancel(self, ticket: int):
        self._transition(ticket, OrderStatus.CANCELLED)
        self._archive(ticket)

    def reject(self, ticket: int, reason: str = ""):
        order = self._orders.get(ticket)
        if not order:
            return
        order.comment = reason

        # 重试
        if order.retry_count < order.max_retries:
            order.retry_count += 1
            order.status = OrderStatus.PENDING
            order.update_time = time.time()
            logger.warning(f"Order {ticket} rejected, retry {order.retry_count}/{order.max_retries}: {reason}")
        else:
            self._transition(order, OrderStatus.REJECTED)
            self._archive(order)
            logger.error(f"Order {ticket} rejected after {order.max_retries} retries: {reason}")

    def _transition(self, ticket_or_order, target: OrderStatus) -> bool:
        """执行状态转换"""
        if isinstance(ticket_or_order, int):
            order = self._orders.get(ticket_or_order)
        else:
            order = ticket_or_order

        if not order:
            return False

        if target not in self.VALID_TRANSITIONS.get(order.status, []):
            logger.warning(f"Invalid transition: {order.status.value} → {target.value} (ticket={order.ticket})")
            return False

        order.status = target
        order.update_time = time.time()
        return True

    def _archive(self, ticket_or_order):
        # P16 (BUG-3 修复): 真正从 _orders 弹出, 不只是 append 到 _history
        # 原 bug: 之前只 append, _orders 永远不释放, 内存泄漏
        if isinstance(ticket_or_order, Order):
            order = ticket_or_order
            self._orders.pop(order.ticket, None)
        else:
            order = self._orders.pop(ticket_or_order, None)
        if order:
            self._history.append(order)

    # ── 查询 ──

    def get(self, ticket: int) -> Order | None:
        return self._orders.get(ticket)

    def active_orders(self) -> list[Order]:
        return [o for o in self._orders.values()
                if o.status not in (OrderStatus.FILLED, OrderStatus.CANCELLED,
                                    OrderStatus.REJECTED, OrderStatus.EXPIRED)]

    def pending_count(self) -> int:
        return sum(1 for o in self._orders.values() if o.status == OrderStatus.PENDING)

    @property
    def history(self) -> list[Order]:
        return self._history[-100:]  # 最近100笔
