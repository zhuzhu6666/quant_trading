"""
tests/test_p17_bug4_oms_cancel.py — BUG-4 fix

引自 framework_audit_20260604.md BUG-4:
execution/oms.py cancel() 调 _transition 失败时仍 _archive,
history 出现 'FILLED 状态被 cancel 成功的' 假记录。
修复: _transition 返回 False 时 raise, 不再静默 archive 错状态。
"""
import pytest

from execution.oms import OrderManager, OrderStatus


def test_bug4_cancel_raises_on_invalid_transition():
    """BUG-4: FILLED 状态 cancel 应当 raise (不能 cancel 已成交订单)"""
    oms = OrderManager()
    o = oms.create("XAUUSD+", 1, "market", volume=0.1, price=2000.0)
    oms.submit(o.ticket)
    oms.fill(o.ticket, fill_price=2000.0)
    # 此时 order 状态 FILLED, 不能 cancel
    with pytest.raises(RuntimeError, match="Cannot cancel"):
        oms.cancel(o.ticket)


def test_bug4_cancel_succeeds_on_valid_transition():
    """BUG-4: NEW/SUBMITTED 状态 cancel 应当成功"""
    oms = OrderManager()
    o = oms.create("XAUUSD+", 1, "market", volume=0.1, price=2000.0)
    oms.submit(o.ticket)
    oms.cancel(o.ticket)
    # history 应当有这条 cancel
    assert any(h.status == OrderStatus.CANCELLED for h in oms.history)


def test_bug4_cancel_after_invalid_does_not_corrupt_history():
    """BUG-4: 失败 cancel 不污染 history"""
    oms = OrderManager()
    o = oms.create("XAUUSD+", 1, "market", volume=0.1, price=2000.0)
    oms.submit(o.ticket)
    oms.fill(o.ticket, fill_price=2000.0)
    # 这次 cancel 应当 raise, history 不会多一条
    history_len_before = len(oms.history)
    with pytest.raises(RuntimeError):
        oms.cancel(o.ticket)
    assert len(oms.history) == history_len_before, (
        "BUG-4 复发: cancel 失败后 history 长度变化, 审计轨迹被污染"
    )
