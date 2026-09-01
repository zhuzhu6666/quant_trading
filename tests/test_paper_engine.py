"""Smoke tests for PaperExecutionEngine — public on_bar() interface.

IMPORTANT: on_bar() defers signal execution by 1 bar (anti-future-leak).
  - bar[t]: signal arrives → stored as _pending_signal
  - bar[t+1]: _pending_signal executed at bar[t+1].open

Tests must call on_bar() twice: once to generate the signal, once to execute it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from execution.paper_execution import PaperExecutionEngine
from strategy.base import Signal
from core.state import state as global_state


@pytest.fixture(autouse=True)
def reset_global_state():
    """Reset global state before each test."""
    default = global_state.accounts.get("default")
    if default:
        default.balance = 500.0
        default.equity = 500.0
        default.position.direction = 0
        default.position.volume = 0.0
        default.is_circuit_breaker = False
        default.circuit_reason = ""
        default.reset_daily(preserve_peak=False)


def _bar(o, h, l, c, t=1_000_000):
    return {"open": o, "high": h, "low": l, "close": c,
            "time": t, "timeframe": "M15", "spread": 0, "volume": 100}


def _long_signal(atr=5.0, price=2000.0, ts=1_000_000):
    return Signal(strategy="test", symbol="XAUUSD", direction=1,
                  strength=1.0, sl_atr=2.0, tp_atr=3.0, atr=atr,
                  price=price, timestamp=ts)


def _short_signal(atr=5.0, price=2000.0, ts=1_000_000):
    return Signal(strategy="test", symbol="XAUUSD", direction=-1,
                  strength=1.0, sl_atr=2.0, tp_atr=3.0, atr=atr,
                  price=price, timestamp=ts)


def _engine():
    return PaperExecutionEngine(
        initial_balance=500.0, default_volume=0.01,
        pre_trade=None, circuit_breaker=None, risk_per_trade_pct=None,
    )


def test_open_long_on_signal():
    """bar[0] generates signal; bar[1] opens position at bar[1].open."""
    eng = _engine()
    t0, t1 = 1_000_000, 1_000_900

    # bar[0]: generate signal (stored, not yet executed)
    eng.on_bar(_bar(2000, 2001, 1999, 2000.5, t0), _long_signal(price=2000.0, ts=t0))
    assert eng.position is None  # not executed yet

    # bar[1]: execute pending signal at bar[1].open
    eng.on_bar(_bar(2001, 2003, 1998, 2002.0, t1), None)
    assert eng.position is not None
    assert eng.position.direction == 1
    assert eng.position.volume > 0


def test_open_then_sl_hit():
    """Open long; next bar low hits SL → position closed."""
    eng = _engine()

    # bar0: generate signal
    eng.on_bar(_bar(2000, 2001, 1999, 2000.5, 1_000_000), _long_signal())
    # bar1: execute signal (open at 2001)
    eng.on_bar(_bar(2001, 2003, 1998, 2002.0, 1_000_900), None)
    assert eng.position is not None

    # bar2: SL=2001-2*5=1991, low=1989 → SL triggered
    eng.on_bar(_bar(1995, 1996, 1989, 1993, 1_001_800), None)
    assert eng.position is None


def test_open_then_tp_hit():
    """Open long; next bar high hits TP → position closed."""
    eng = _engine()

    eng.on_bar(_bar(2000, 2001, 1999, 2000.5, 1_000_000), _long_signal())
    eng.on_bar(_bar(2001, 2003, 1998, 2002.0, 1_000_900), None)
    assert eng.position is not None

    # TP=2001+3*5=2016, high=2017 → TP triggered
    eng.on_bar(_bar(2008, 2017, 2007, 2014, 1_001_800), None)
    assert eng.position is None


def test_neither_hit_hold():
    """Narrow bar — neither SL nor TP hit → position held."""
    eng = _engine()

    eng.on_bar(_bar(2000, 2001, 1999, 2000.5, 1_000_000), _long_signal())
    eng.on_bar(_bar(2001, 2003, 1998, 2002.0, 1_000_900), None)
    assert eng.position is not None

    # Tight bar: SL=1991, TP=2016, low=1995, high=2005 → no trigger
    eng.on_bar(_bar(2002, 2005, 1995, 2003, 1_001_800), None)
    assert eng.position is not None


def test_flip_long_to_short():
    """To flip, first close position, then open opposite direction.

    The engine does NOT auto-flip: pending signals are dropped when a
    position is open. Strategy must explicitly close before re-entering.
    """
    eng = _engine()

    # Open long
    eng.on_bar(_bar(2000, 2001, 1999, 2000.5, 1_000_000), _long_signal())
    eng.on_bar(_bar(2001, 2003, 1998, 2002.0, 1_000_900), None)
    assert eng.position.direction == 1

    # Close via SL: low=1988 hits SL=1991
    eng.on_bar(_bar(1993, 1994, 1988, 1991, 1_001_800), None)
    assert eng.position is None  # closed

    # Now generate short signal (stored)
    eng.on_bar(_bar(1991, 1993, 1990, 1992.0, 1_002_700),
               _short_signal(price=1991.0))
    # Executed next bar
    eng.on_bar(_bar(1992, 1995, 1990, 1994.0, 1_003_600), None)

    assert eng.position is not None
    assert eng.position.direction == -1
