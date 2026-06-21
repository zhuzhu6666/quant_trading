"""Smoke tests for CircuitBreaker —熔断状态触发和恢复."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from core.state import state as global_state


@pytest.fixture(autouse=True)
def reset_global_state():
    """Reset global state before each test to avoid cross-test pollution."""
    global_state.reset_daily(preserve_peak=False)
    # Also reset the default account
    default = global_state.accounts.get("default")
    if default:
        default.balance = 500.0
        default.equity = 500.0
        default.is_circuit_breaker = False
        default.circuit_reason = ""
        default.daily.peak_equity = 500.0
        default.position.direction = 0
        default.position.volume = 0.0


def test_no_trigger_on_small_loss():
    """Small loss (<5%) should NOT trigger circuit breaker."""
    from risk.circuit import CircuitBreaker
    cb = CircuitBreaker(max_daily_loss_pct=5.0, max_consecutive_loss=5)

    global_state.balance = 500.0
    global_state.record_trade(pnl=-10.0)  # -2% of 500

    tripped, _reason = cb.check_all()
    assert not tripped
    assert not global_state.is_circuit_breaker


def test_trigger_on_daily_loss_exceeded():
    """Daily loss > 5% should trigger circuit breaker."""
    from risk.circuit import CircuitBreaker
    cb = CircuitBreaker(max_daily_loss_pct=5.0, max_consecutive_loss=999)

    global_state.balance = 500.0
    global_state.daily.peak_equity = 500.0

    # Record loss and sync equity (no position → equity = balance)
    global_state.record_trade(pnl=-30.0)
    global_state.equity = global_state.balance  # = 470.0

    tripped, reason = cb.check_all()
    assert tripped, f"Expected tripped, got: {reason} (bal={global_state.balance}, eq={global_state.equity})"
    # Reason should contain the trigger explanation (中文或英文)
    assert reason != "OK"


def test_trigger_on_consecutive_losses():
    """5 consecutive losses should trigger circuit breaker."""
    from risk.circuit import CircuitBreaker
    cb = CircuitBreaker(max_daily_loss_pct=50.0, max_consecutive_loss=5)

    global_state.balance = 500.0
    global_state.equity = 500.0
    global_state.daily.peak_equity = 500.0
    for _ in range(5):
        global_state.record_trade(pnl=-0.10)

    assert global_state.daily.consecutive_losses >= 5
    tripped, _reason = cb.check_all()
    assert tripped


def test_reset_clears_breaker():
    """Reset should clear circuit breaker, preserve peak equity."""
    global_state.mark_breaker(tripped=True, reason="test")
    global_state.daily.peak_equity = 480.0

    global_state.reset_daily(preserve_peak=True)

    assert global_state.is_circuit_breaker is False
    assert global_state.circuit_reason == ""
    assert global_state.daily.peak_equity == 480.0
    assert global_state.daily.total_trades == 0


def test_daily_loss_pct_only_counts_losses():
    """BUG fix: daily_loss_pct should NOT trigger on positive PnL days."""
    global_state.balance = 500.0
    global_state.daily.net_pnl = 100.0
    global_state.equity = 600.0

    loss_pct = global_state.daily_loss_pct
    assert loss_pct == 0.0
