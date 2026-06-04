"""
tests/test_p16_bug3_oms_partial_fill.py — BUG-3 fix

引自 framework_audit_20260604.md BUG-3:
execution/oms.py partial_fill() 只 transition 不 _archive,
订单永远停在 _orders dict 泄漏。修复: 累计 filled_volume,
== volume 时自动 transition FILLED + 归档。
"""
import pytest

from execution.oms import OrderManager, Order, OrderStatus


def test_bug3_partial_fill_accumulates_filled_volume():
    """BUG-3: 多次 partial_fill 累加 filled_volume"""
    oms = OrderManager()
    o = oms.create("XAUUSD+", 1, "market", volume=1.0, price=2000.0)
    oms.submit(o.ticket)
    oms.partial_fill(o.ticket, fill_price=2000.5, filled_vol=0.3)
    oms.partial_fill(o.ticket, fill_price=2001.0, filled_vol=0.3)
    oms.partial_fill(o.ticket, fill_price=2001.5, filled_vol=0.4)
    # 累计 1.0, 应当 transition FILLED + 归档
    order = oms.get(o.ticket)
    assert order is None, (
        f"BUG-3 复发: order 还在 _orders, 应已归档"
    )
    assert len(oms.history) == 1
    assert oms.history[0].status == OrderStatus.FILLED
    assert oms.history[0].filled_volume == 1.0


def test_bug3_partial_fill_keeps_active_when_not_full():
    """BUG-3: 未到 full 之前订单仍 active, status=PARTIAL_FILLED"""
    oms = OrderManager()
    o = oms.create("XAUUSD+", 1, "market", volume=1.0, price=2000.0)
    oms.submit(o.ticket)
    oms.partial_fill(o.ticket, fill_price=2000.5, filled_vol=0.5)
    order = oms.get(o.ticket)
    assert order is not None  # 仍 active
    assert order.status == OrderStatus.PARTIAL_FILLED
    assert order.filled_volume == 0.5


def test_bug3_order_has_filled_volume_field():
    """BUG-3: Order 应当有 filled_volume 字段"""
    o = Order()
    assert hasattr(o, "filled_volume")
    assert o.filled_volume == 0.0
