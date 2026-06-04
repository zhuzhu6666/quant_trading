"""
tests/test_p5a_state_lock_helpers.py — P5a fix: state mutation 走 helper

引自 framework_audit_20260604.md ARCH-3 + BUG-10:
core/state.py:51 有 _lock, 但只有 3 个方法 acquire, 其余
直读/写 (circuit.py / pre_trade.py / router.py) 全裸写, 多线程下
torn state (direction=1 + volume=0).

修复:
  - State.mark_breaker(tripped, reason) helper (持锁 + 发 CIRCUIT_BREAK event)
  - State.set_sl_price(price) helper (持锁)
  - circuit.trip / circuit.reset / pre_trade 直写 全部改走 helper
  - router.on_fill 的 sl_price 改走 helper

本文件 4 case:
  - mark_breaker 持锁, 多线程并发不 torn
  - mark_breaker(True, reason) 发 EventType.CIRCUIT_BREAK event
  - set_sl_price 持锁, 写后读一致
  - circuit.trip 改走 helper 后, 发 event (验证 BUG-10 修复)
"""
import threading

import pytest

from core.state import state
from core.event_bus import bus, EventType


@pytest.fixture(autouse=True)
def _reset_breaker():
    """每个 test 前后清 state.circuit_breaker"""
    state.is_circuit_breaker = False
    state.circuit_reason = ""
    yield
    state.is_circuit_breaker = False
    state.circuit_reason = ""


def test_mark_breaker_sets_fields_atomically():
    """P5a: mark_breaker(True, reason) 同时设 is_circuit_breaker 和 reason"""
    state.mark_breaker(True, "test reason")
    assert state.is_circuit_breaker is True
    assert state.circuit_reason == "test reason"


def test_mark_breaker_publishes_circuit_break_event():
    """P5a + BUG-10: mark_breaker(True, ...) 发 EventType.CIRCUIT_BREAK event"""
    received = []
    bus.subscribe(EventType.CIRCUIT_BREAK,
                  lambda e: received.append(e))
    try:
        state.mark_breaker(True, "test trip")
        assert len(received) == 1, (
            f"BUG-10 复发: mark_breaker 没发 event, received={received}"
        )
        assert received[0].data["reason"] == "test trip"
    finally:
        # 清理 subscription (没 unsubscribe API, 用 list 改写)
        bus._subscribers[EventType.CIRCUIT_BREAK] = [
            s for s in bus._subscribers.get(EventType.CIRCUIT_BREAK, [])
            if s not in (lambda e: received.append(e),)
        ]


def test_mark_breaker_reset_does_not_publish_event():
    """P5a: mark_breaker(False) 是 reset, 不发 event (reset 单独发 RESET event)"""
    received = []
    bus.subscribe(EventType.CIRCUIT_BREAK,
                  lambda e: received.append(e))
    try:
        state.mark_breaker(True, "trip first")
        received.clear()
        state.mark_breaker(False, "")
        # reset 不发 CIRCUIT_BREAK (避免误报)
        assert len(received) == 0
    finally:
        bus._subscribers[EventType.CIRCUIT_BREAK] = []


def test_mark_breaker_thread_safe_no_torn_state():
    """P5a: 多线程并发 mark_breaker, 不出现 torn state (is_circuit=True 但 reason="")"""
    barrier = threading.Barrier(10)

    def tripper(reason: str):
        barrier.wait()
        for _ in range(100):
            state.mark_breaker(True, reason)

    threads = [threading.Thread(target=tripper, args=(f"reason-{i}",))
               for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 1000 次写后, 应当是 (is_circuit=True, reason=某 reason-N)
    # torn = (is_circuit=True, reason="") 是 bug
    assert state.is_circuit_breaker is True
    assert state.circuit_reason != "", (
        f"P5a 复发: torn state, is_circuit=True 但 reason=空"
    )


def test_set_sl_price_thread_safe():
    """P5a: set_sl_price 持锁, 多线程写不丢更新"""
    state.position.sl_price = 0.0
    barrier = threading.Barrier(10)

    def writer(price: float):
        barrier.wait()
        for _ in range(100):
            state.set_sl_price(price)

    threads = [threading.Thread(target=writer, args=(1900.0 + i,))
               for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 1000 次写后, sl_price 应当是某个 1900+i (i in 0..9)
    assert 1900.0 <= state.position.sl_price <= 1909.0


def test_circuit_trip_routes_through_mark_breaker():
    """P5a + BUG-10: CircuitBreaker.trip 应当触发 EventType.CIRCUIT_BREAK event

    修复前: pre_trade.py 直接 state.is_circuit_breaker = True, 不发 event
    修复后: 都走 state.mark_breaker(), 自动发 event
    """
    from risk.circuit import CircuitBreaker
    received = []
    bus.subscribe(EventType.CIRCUIT_BREAK,
                  lambda e: received.append(e))
    try:
        cb = CircuitBreaker()
        cb.trip("test from circuit")
        assert len(received) == 1, (
            f"BUG-10 复发: CircuitBreaker.trip 没发 event, received={received}"
        )
        assert received[0].data["reason"] == "test from circuit"
    finally:
        bus._subscribers[EventType.CIRCUIT_BREAK] = []


def test_pre_trade_trip_routes_through_mark_breaker():
    """P5a + BUG-10: PreTradeChecker 触发的 trip 也发 event (通过 mark_breaker)"""
    from risk.pre_trade import PreTradeChecker
    received = []
    bus.subscribe(EventType.CIRCUIT_BREAK,
                  lambda e: received.append(e))
    try:
        # 设 state 触发 daily loss > 0
        state.balance = 1000.0
        state.daily.net_pnl = -100.0  # 10% 亏损
        ptc = PreTradeChecker(max_daily_loss_pct=5.0)
        ptc.check(entry_price=2000.0, sl_price=1990.0, size=0.01)
        assert len(received) == 1, (
            f"BUG-10 复发: PreTradeChecker 没发 event, received={received}"
        )
    finally:
        bus._subscribers[EventType.CIRCUIT_BREAK] = []
        state.daily.net_pnl = 0.0
        state.daily.consecutive_losses = 0
