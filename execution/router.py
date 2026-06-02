"""
Execution Router — 智能下单路由

职责：
1. 接收融合后的Signal → 创建Order
2. 通过PreTrade风控检查
3. 调用MT5执行
4. 记录成交信息

P1-B 增强: 集成智能路由算法 (TWAP / VWAP / POV / IS)
  - 大单 (> 0.05 手) 按 algos 拆成 child orders
  - 小单仍走市价
"""

import logging
from datetime import datetime, timedelta

from core.state import state
from execution.oms import OrderManager, Order, OrderStatus
from execution.algos import AlgoDispatcher, ParentOrder
from strategy.base import Signal
from strategy.portfolio import PortfolioManager
from risk.pre_trade import PreTradeChecker
from risk.circuit import CircuitBreaker
from risk.position import PositionMonitor

logger = logging.getLogger(__name__)


class ExecutionRouter:
    """
    下单路由器

    连接 Signal → Risk → OMS → MT5 的完整链路。
    P1-B: 大单走智能路由 (algos), 小单直接市价。
    """

    def __init__(self, oms: OrderManager, portfolio: PortfolioManager,
                 pre_trade: PreTradeChecker,
                 circuit_breaker: CircuitBreaker | None = None,
                 algo_dispatcher: AlgoDispatcher | None = None,
                 algo_threshold: float = 0.05):
        self.oms = oms
        self.portfolio = portfolio
        self.pre_trade = pre_trade
        self.circuit_breaker = circuit_breaker
        self.algo_dispatcher = algo_dispatcher or AlgoDispatcher()
        self.algo_threshold = algo_threshold  # < 这个手数直接市价

    def route(self, signal: Signal, urgency: float = 0.5,
              algo: str | None = None) -> Order | list[Order] | None:
        """
        处理信号，生成订单

        流程: signal → compute_size → pre_trade_check → circuit_check
              → [algo split if 大单] → create child orders → submit

        Args:
            signal: 融合后的信号
            urgency: 0-1, 给 IS 算法用
            algo: 强制指定算法 ('TWAP' / 'VWAP' / 'POV' / 'IS' / None=auto)

        Returns:
            - None: 风控拒绝
            - Order: 单笔 (小单)
            - list[Order]: 多笔 (大单, algo split)
        """
        # 1. 计算仓位
        entry_price = signal.price
        sl_price = entry_price - signal.atr * signal.sl_atr if signal.direction == 1 \
              else entry_price + signal.atr * signal.sl_atr

        size = self.portfolio.compute_size(entry_price, sl_price, signal.atr)

        # 2. 前置风控
        passed, reason = self.pre_trade.check(entry_price, sl_price, size)
        if not passed:
            logger.warning(f"Pre-trade check failed for {signal.strategy}: {reason}")
            return None

        # 3. 熔断器
        if self.circuit_breaker is not None:
            tripped, cb_reason = self.circuit_breaker.check_all()
            if tripped:
                logger.warning(
                    f"Circuit breaker blocks order for {signal.strategy}: {cb_reason}"
                )
                return None

        # 4. TP 计算
        tp_price = entry_price + signal.atr * signal.tp_atr if signal.direction == 1 \
                   else entry_price - signal.atr * signal.tp_atr

        # 5. 算法路由 (P1-B)
        if size < self.algo_threshold:
            # 小单: 直接市价
            return self._create_single_order(signal, entry_price, sl_price, tp_price, size)

        # 大单: algo 切片
        # 默认 30 分钟执行窗口 (生产环境可调)
        end_time = datetime.now() + timedelta(minutes=30)
        parent = ParentOrder(
            symbol=signal.symbol,
            direction=signal.direction,
            total_volume=size,
            start_time=datetime.now(),
            end_time=end_time,
            current_price=entry_price,
            urgency=urgency,
        )
        children = self.algo_dispatcher.dispatch(parent, algo=algo)
        logger.info(f"[ALGO {algo or 'AUTO'}] {signal.strategy} {size}手 → {len(children)} child orders")
        return self._create_child_orders(signal, children, sl_price, tp_price)

    def _create_single_order(self, signal: Signal, entry_price: float,
                              sl_price: float, tp_price: float, size: float) -> Order:
        order = self.oms.create(
            symbol=signal.symbol, direction=signal.direction,
            order_type="market", volume=size, price=entry_price,
            sl=sl_price, tp=tp_price, strategy=signal.strategy,
        )
        self.oms.submit(order.ticket)
        logger.info(f"Order created: ticket={order.ticket} {signal.symbol} "
                    f"{'BUY' if signal.direction==1 else 'SELL'} "
                    f"size={size} price={entry_price:.2f} sl={sl_price:.2f}")
        return order

    def _create_child_orders(self, signal: Signal, children: list,
                              sl_price: float, tp_price: float) -> list[Order]:
        orders = []
        for child in children:
            order = self.oms.create(
                symbol=signal.symbol, direction=signal.direction,
                order_type=child.order_type, volume=child.volume,
                price=child.price_hint if child.order_type == "limit" else 0.0,
                sl=sl_price, tp=tp_price, strategy=signal.strategy,
                **{"algo_sequence": child.sequence, "algo_type": child.order_type},
            )
            self.oms.submit(order.ticket)
            orders.append(order)
        return orders

    def on_fill(self, order: Order, fill_price: float):
        """成交回调"""
        self.oms.fill(order.ticket, fill_price)

        # 更新持仓状态
        state.position.direction = order.direction
        state.position.volume = order.volume
        state.position.entry_price = fill_price
        state.position.sl_price = order.sl
        state.position.tp_price = order.tp

        logger.info(f"FILLED: ticket={order.ticket} price={fill_price:.2f} "
                    f"size={order.volume}")

    def on_reject(self, order: Order, reason: str):
        """拒绝回调"""
        self.oms.reject(order.ticket, reason)
