"""Smoke tests for AccountState — balance/equity/position updates."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from core.state import AccountState, Position


def test_initial_state():
    acct = AccountState(name="test", initial_balance=500.0)
    assert acct.balance == 500.0
    assert acct.equity == 500.0
    assert acct.has_position is False
    assert acct.win_rate == 0.0
    assert acct.is_circuit_breaker is False


def test_equity_update_long():
    """Long position: equity = balance + (current - entry) * volume * contract."""
    acct = AccountState(name="test", initial_balance=500.0)
    acct.position = Position(
        symbol="XAUUSD",
        direction=1,
        volume=0.01,
        entry_price=2000.0,
        contract_size=100,
    )

    acct.update_equity(current_price=2010.0)
    # PnL = (2010 - 2000) * 0.01 * 100 = 10.0
    assert acct.equity == pytest.approx(510.0, abs=0.01)
    assert acct.position.unrealized_pnl == pytest.approx(10.0, abs=0.01)
    assert acct.position.current_price == 2010.0


def test_equity_update_short():
    """Short position: equity = balance + (entry - current) * volume * contract."""
    acct = AccountState(name="test", initial_balance=500.0)
    acct.position = Position(
        symbol="XAUUSD",
        direction=-1,
        volume=0.01,
        entry_price=2000.0,
        contract_size=100,
    )

    acct.update_equity(current_price=1990.0)
    # PnL = (2000 - 1990) * 0.01 * 100 = 10.0
    assert acct.equity == pytest.approx(510.0, abs=0.01)
    assert acct.position.unrealized_pnl == pytest.approx(10.0, abs=0.01)


def test_equity_flat():
    """Flat position: equity = balance."""
    acct = AccountState(name="test", initial_balance=500.0)
    acct.update_equity(current_price=2000.0)
    assert acct.equity == 500.0


def test_record_trade_updates_stats():
    """record_trade() should update balance, daily stats, and streaks."""
    acct = AccountState(name="test", initial_balance=500.0)

    # Win
    acct.record_trade(pnl=10.0, commission=1.0)
    assert acct.balance == 510.0
    assert acct.daily.total_trades == 1
    assert acct.daily.winning_trades == 1
    assert acct.daily.losing_trades == 0
    assert acct.daily.consecutive_losses == 0
    assert acct.daily.net_pnl == 10.0
    assert acct.daily.commission == 1.0
    assert acct.daily.gross_pnl == 11.0  # net + commission

    # Loss
    acct.record_trade(pnl=-5.0, commission=1.0)
    assert acct.balance == 505.0  # 510 - 5
    assert acct.daily.total_trades == 2
    assert acct.daily.winning_trades == 1
    assert acct.daily.losing_trades == 1
    assert acct.daily.consecutive_losses == 1
    assert acct.daily.net_pnl == 5.0  # 10 - 5

    # Another loss → streak = 2
    acct.record_trade(pnl=-3.0)
    assert acct.daily.consecutive_losses == 2
    assert acct.win_rate == pytest.approx(1.0 / 3.0 * 100, abs=0.1)


def test_reset_daily_preserves_peak():
    acct = AccountState(name="test", initial_balance=500.0)
    acct.daily.peak_equity = 550.0
    acct.daily.consecutive_losses = 3
    acct.record_trade(pnl=-10.0)

    acct.reset_daily(preserve_peak=True)
    assert acct.daily.peak_equity == 550.0
    assert acct.daily.total_trades == 0
    assert acct.daily.consecutive_losses == 0
