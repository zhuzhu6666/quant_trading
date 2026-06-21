"""Smoke tests for OrderManager —订单状态机转换."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from execution.oms import OrderManager, Order, OrderStatus


def test_create_order_initial_state():
    oms = OrderManager()
    order = oms.create(symbol="XAUUSD", direction=1, order_type="market",
                       volume=0.01, price=2000.0, sl=1995.0, tp=2010.0)

    assert order.ticket >= 1000
    assert order.symbol == "XAUUSD"
    assert order.direction == 1
    assert order.status == OrderStatus.NEW


def test_full_lifecycle_new_to_filled():
    """Complete lifecycle: create → submit → fill."""
    oms = OrderManager()
    order = oms.create(symbol="XAUUSD", direction=1, order_type="market",
                       volume=0.01, price=2000.0)

    assert oms.submit(order.ticket) is True
    assert order.status == OrderStatus.SUBMITTED

    oms.fill(order.ticket, fill_price=2000.5)
    # After fill, order is archived
    assert order.status == OrderStatus.FILLED


def test_rejection_from_new():
    """reject() should retry first (NEW→PENDING) then reject."""
    oms = OrderManager()
    order = oms.create(symbol="XAUUSD", direction=1, order_type="market",
                       volume=0.01, price=2000.0)

    # First rejection: it'll retry (NEW → PENDING for retry)
    oms.reject(order.ticket, reason="test rejection")
    # After first rejection, it goes to PENDING for retry
    assert order.status == OrderStatus.PENDING

    # exhaust retries: submit → reject again → permanent rejection
    order.max_retries = 0  # Disable retry
    oms.reject(order.ticket, reason="permanent")
    assert order.status == OrderStatus.REJECTED


def test_cancel_from_pending():
    """cancel() from PENDING should transition to CANCELLED."""
    oms = OrderManager()
    order = oms.create(symbol="XAUUSD", direction=1, order_type="market",
                       volume=0.01, price=2000.0)

    oms.submit(order.ticket)  # NEW→SUBMITTED

    # SUBMITTED→CANCELLED is valid
    oms.cancel(order.ticket)
    assert order.status == OrderStatus.CANCELLED


def test_cancel_after_filled_raises():
    """cancel() on already FILLED order should raise RuntimeError."""
    oms = OrderManager()
    order = oms.create(symbol="XAUUSD", direction=1, order_type="market",
                       volume=0.01, price=2000.0)

    oms.submit(order.ticket)
    oms.fill(order.ticket, fill_price=2000.5)

    # FILLED→CANCELLED is invalid → RuntimeError
    with pytest.raises(RuntimeError, match="cancel"):
        oms.cancel(order.ticket)


def test_order_not_found_submit():
    """Submit on non-existent ticket returns False."""
    oms = OrderManager()
    assert oms.submit(99999) is False


def test_partial_fill_accumulates():
    """Partial fill can accumulate volume across multiple calls."""
    oms = OrderManager()
    order = oms.create(symbol="XAUUSD", direction=1, order_type="market",
                       volume=0.05, price=2000.0)

    oms.submit(order.ticket)

    # First partial fill
    oms.partial_fill(order.ticket, fill_price=2000.0, filled_vol=0.02)
    assert order.status == OrderStatus.PARTIAL_FILLED
    assert order.filled_volume == 0.02

    # Second partial fill
    oms.partial_fill(order.ticket, fill_price=2001.0, filled_vol=0.02)
    assert order.status == OrderStatus.PARTIAL_FILLED
    assert order.filled_volume == 0.04

    # Third partial fill → reaches total volume → auto FILLED
    oms.partial_fill(order.ticket, fill_price=2002.0, filled_vol=0.01)
    assert order.status == OrderStatus.FILLED
    assert order.filled_volume == 0.05
