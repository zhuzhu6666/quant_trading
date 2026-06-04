"""
tests/test_p8_bug1_tp_sl_spread.py — P8 fix: 长仓 TP 滑点不对称

引自 framework_audit_2026-04 BUG-1:
execution/paper_engine.py:463 长仓 TP 用 bid_high = bar.high - spread
来判断 tp_hit, 但 bar.high 已经是 ask-extreme high (MT5 约定),
减 spread 反而把 TP 触发推后 ~spread 距离。

修复: 长仓 TP 改用 h >= tp (ask-extreme), 不再减 spread。

注: _check_exit(bar) 只接 bar, 从 self.position 读 pos。
本文件 4 case:
  - 长仓 TP 在 bar.high == tp 触发 (buggy 不会)
  - 长仓 SL 仍正确
  - 短仓 TP/SL 不变
  - 既没 TP 也没 SL 不触发
"""
import pytest

from execution.paper_engine import PaperExecutionEngine
from core.state import state


@pytest.fixture(autouse=True)
def _reset_state():
    state.balance = 1000.0
    state.equity = 1000.0
    state.position.symbol = "XAUUSD+"
    state.position.direction = 0
    state.position.volume = 0.0
    state.position.entry_price = 0.0
    state.position.sl_price = 0.0
    state.position.tp_price = 0.0
    yield
    state.position.direction = 0
    state.position.volume = 0.0


def _set_pos(eng, direction, entry, sl, tp, volume=0.1):
    """直接设 eng.position (PaperExecutionEngine 实例属性, 不是 state)"""
    from execution.paper_engine import Position
    eng.position = Position(
        symbol="XAUUSD+",
        direction=direction,
        entry_price=entry,
        sl_price=sl,
        tp_price=tp,
        volume=volume,
        entry_time=None,
    )


def test_long_tp_triggers_at_ask_extreme_not_bid_extreme(monkeypatch):
    """P8: 长仓 TP 在 bar.high == tp 时触发

    buggy: tp_hit = (2010 - 0.5) = 2009.5 >= 2010? False, 不触发
    修复后: tp_hit = 2010 >= 2010, True, 触发
    """
    eng = PaperExecutionEngine()
    _set_pos(eng, direction=1, entry=2000.0, sl=1990.0, tp=2010.0)
    bar = {"high": 2010.0, "low": 2005.0, "close": 2005.0, "open": 2005.0,
           "time": 1234567890, "spread": 50}
    closed = []
    monkeypatch.setattr(eng, "_close",
                        lambda price, reason, bar_time=None: closed.append((price, reason)) or None)
    eng._check_exit(bar)
    assert any(r == "tp" for _, r in closed), (
        f"BUG-1 复发: 长仓 TP 没在 bar.high=tp 触发, closed={closed}"
    )


def test_long_sl_still_triggers(monkeypatch):
    """P8 修复不影响 SL 路径"""
    eng = PaperExecutionEngine()
    _set_pos(eng, direction=1, entry=2000.0, sl=1990.0, tp=2100.0)
    bar = {"high": 2005.0, "low": 1989.0, "close": 1990.0, "open": 2000.0,
           "time": 1234567890, "spread": 50}
    closed = []
    monkeypatch.setattr(eng, "_close",
                        lambda price, reason, bar_time=None: closed.append((price, reason)) or None)
    eng._check_exit(bar)
    assert any(r == "sl" for _, r in closed)


def test_short_tp_sl_unchanged(monkeypatch):
    """P8 修复不影响短仓路径"""
    eng = PaperExecutionEngine()
    _set_pos(eng, direction=-1, entry=2000.0, sl=2010.0, tp=1990.0)
    bar = {"high": 2011.0, "low": 1989.0, "close": 2000.0, "open": 2000.0,
           "time": 1234567890, "spread": 50}
    closed = []
    monkeypatch.setattr(eng, "_close",
                        lambda price, reason, bar_time=None: closed.append((price, reason)) or None)
    eng._check_exit(bar)
    # 短仓 SL: ask-high (h) >= 2010, 2011 >= 2010, 触发
    assert any(r == "sl" for _, r in closed), (
        f"短仓 SL 没触发, closed={closed}"
    )


def test_long_no_exit_when_neither_hit(monkeypatch):
    """P8: 既没 TP 也没 SL 时, _close 不被调"""
    eng = PaperExecutionEngine()
    _set_pos(eng, direction=1, entry=2000.0, sl=1990.0, tp=2100.0)
    bar = {"high": 2005.0, "low": 1995.0, "close": 2000.0, "open": 2000.0,
           "time": 1234567890, "spread": 50}
    closed = []
    monkeypatch.setattr(eng, "_close",
                        lambda price, reason, bar_time=None: closed.append((price, reason)) or None)
    result = eng._check_exit(bar)
    assert result is None
    assert closed == []
