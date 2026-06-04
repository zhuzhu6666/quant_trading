"""
tests/test_p5b_batch_risk_core.py — Batch risk/core 剩 4 条 fix

引自 framework_audit_20260604.md:
  BUG-11: record_trade pnl 模糊 (gross/net)
  ARCH-4: State.reset_daily 跟 CircuitBreaker.reset 合约不一致
  ARCH-6: EventBus._subscribers / _stats 无锁
  FOOTGUN-8: alerter.py LEVEL_ORDER.get(level, 0) 静默接受拼写错误

本文件 8 case:
  - BUG-11: net pnl 传 net, balance += net (不双扣 commission)
  - BUG-11: 显式 docstring / 约定
  - ARCH-4: reset_daily(preserve_peak=True) 保留 peak_equity
  - ARCH-4: reset_daily(preserve_peak=False) 清 peak
  - ARCH-6: 多线程 subscribe + publish 不抛 RuntimeError
  - ARCH-6: publish 期间 handler 可安全 subscribe 新 handler
  - FOOTGUN-8: 未知 level 抛 ValueError
  - FOOTGUN-8: 未知 min_level 抛 ValueError
"""
import threading

import pytest

from core.state import state, State, DailyStats
from core.event_bus import bus, EventType, Event


@pytest.fixture(autouse=True)
def _reset_state():
    state.is_circuit_breaker = False
    state.circuit_reason = ""
    state.balance = 1000.0
    state.daily = DailyStats()
    yield
    state.is_circuit_breaker = False
    state.circuit_reason = ""
    state.balance = 1000.0
    state.daily = DailyStats()


# ── BUG-11 ─────────────────────────────────────────────────────────────

def test_bug11_record_trade_net_pnl_does_not_double_deduct_commission():
    """BUG-11: 传 net pnl, balance 应当 +net (不双扣 commission)"""
    state.record_trade(pnl=50.0, commission=2.0)  # pnl 是 NET
    # net = 50, commission=2 应已含在 pnl 里
    # 修复后: balance += 50 (不 -2)
    assert state.balance == 1050.0, (
        f"BUG-11 复发: balance={state.balance}, 应为 1050 (没双扣 commission)"
    )
    assert state.daily.net_pnl == 50.0
    assert state.daily.commission == 2.0


def test_bug11_record_trade_documented_pnl_contract():
    """BUG-11: record_trade 应当有 docstring 说明 pnl 是 net"""
    import inspect
    src = inspect.getdoc(state.record_trade) or ""
    assert "net" in src.lower(), (
        f"BUG-11: record_trade 缺 pnl 约定 docstring, got: {src[:200]}"
    )


# ── ARCH-4 ────────────────────────────────────────────────────────────

def test_arch4_reset_daily_preserves_peak_by_default():
    """ARCH-4: State.reset_daily() 默认保留 peak_equity (跟 CircuitBreaker.reset 合约一致)"""
    state.daily.peak_equity = 1500.0
    state.daily.net_pnl = 100.0

    state.reset_daily()

    # 修复后: 默认 preserve_peak=True, peak 保留
    assert state.daily.peak_equity == 1500.0, (
        f"ARCH-4 复发: reset_daily 默认清 peak={state.daily.peak_equity}, 应保留 1500"
    )
    # net_pnl 等其他字段应重置
    assert state.daily.net_pnl == 0.0


def test_arch4_reset_daily_can_drop_peak_explicitly():
    """ARCH-4: reset_daily(preserve_peak=False) 显式清 peak"""
    state.daily.peak_equity = 1500.0
    state.reset_daily(preserve_peak=False)
    assert state.daily.peak_equity == 0.0


# ── ARCH-6 ────────────────────────────────────────────────────────────

def test_arch6_eventbus_subscribe_during_publish_does_not_raise():
    """ARCH-6: publish_sync 期间 handler 内 subscribe 不抛 RuntimeError"""
    received = []

    def handler_a(e):
        received.append(e)
        # 关键: 在 handler 内 subscribe 新 handler
        bus.subscribe(EventType.ORDER_FILLED, handler_b)

    def handler_b(e):
        received.append(("b", e))

    bus.subscribe(EventType.ORDER_FILLED, handler_a)
    try:
        # publish 应当不抛
        bus.publish_sync(Event(type=EventType.ORDER_FILLED, data={"x": 1}))
        assert len(received) >= 1
        assert received[0].data["x"] == 1
    finally:
        bus._subscribers[EventType.ORDER_FILLED] = []


def test_arch6_eventbus_thread_safe_concurrent_publish():
    """ARCH-6: 多线程并发 publish, 不抛 RuntimeError"""
    received = []

    def handler(e):
        received.append(e)

    bus.subscribe(EventType.ORDER_FILLED, handler)
    barrier = threading.Barrier(10)

    def publisher(i):
        barrier.wait()
        for _ in range(50):
            bus.publish_sync(Event(type=EventType.ORDER_FILLED, data={"i": i}))

    threads = [threading.Thread(target=publisher, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 10 线程 x 50 次 = 500 次 publish
    assert len(received) == 500
    bus._subscribers[EventType.ORDER_FILLED] = []


# ── FOOTGUN-8 ────────────────────────────────────────────────────────

def test_footgun8_alerter_raises_on_unknown_level():
    """FOOTGUN-8: 未知 level 应当 raise ValueError, 不再静默"""
    from monitor.alerter import Alerter
    alerter = Alerter({"log_file": "logs/_test_alerter.log", "min_level": "INFO"})
    with pytest.raises(ValueError, match="unknown level"):
        alerter.send("INOF", "typo test", "msg")  # 拼写错


def test_footgun8_alerter_raises_on_unknown_min_level():
    """FOOTGUN-8: 未知 min_level 也应当 raise"""
    from monitor.alerter import Alerter
    with pytest.raises(ValueError, match="unknown min_level"):
        Alerter({"log_file": "logs/_test_alerter.log", "min_level": "WRANING"})
