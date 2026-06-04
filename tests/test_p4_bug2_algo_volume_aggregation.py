"""
tests/test_p4_bug2_algo_volume_aggregation.py — P4 fix: algo 子单 volume 累加

引自 framework_audit_20260604.md BUG-2:
execution/router.py:143-155 on_fill 直写 state.position.volume =
order.volume, 0.5 lot algo 拆 10 笔 0.05 lot 子单时, position.volume
在 0.05↔0.10 之间跳变, 永不累加到 0.50。

修复: on_fill 根据当前 state.position.direction 分支:
  - 0 (首笔): 初始化
  - 同向: VWAP 加权 entry_price, 累加 volume
  - 反向: 本期 warn + skip (留 TODO)

本文件 4 case:
  - 首笔 fill 初始化 position
  - 同向子单累加 volume
  - 多笔不同 fill_price, entry_price 是 VWAP
  - SL/TP 跟到最新一笔
"""
from unittest.mock import MagicMock

import pytest

from core.state import state
from execution.router import ExecutionRouter
from execution.oms import Order


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


def _make_router():
    oms = MagicMock()
    portfolio = MagicMock()
    pre_trade = MagicMock()
    return ExecutionRouter(oms=oms, portfolio=portfolio, pre_trade=pre_trade)


def _make_order(ticket: int, direction: int, volume: float,
                sl: float = 1900.0, tp: float = 2100.0) -> Order:
    return Order(ticket=ticket, symbol="XAUUSD+", direction=direction,
                 volume=volume, sl=sl, tp=tp)


def test_on_fill_first_child_initializes_position():
    """P4: 首笔 fill 应当初始化 position, 不累加"""
    router = _make_router()
    o = _make_order(ticket=1, direction=1, volume=0.05)
    router.on_fill(o, fill_price=2000.0)

    assert state.position.direction == 1
    assert state.position.volume == 0.05
    assert state.position.entry_price == 2000.0


def test_on_fill_subsequent_child_accumulates_volume():
    """P4: 后续同向 fill 应当累加 volume, 不覆盖

    buggy 行为: 0.05 → 0.05 → 0.05 (永远 0.05)
    修复后: 0.05 → 0.10 → 0.15
    """
    router = _make_router()
    for ticket in range(1, 4):
        o = _make_order(ticket=ticket, direction=1, volume=0.05)
        router.on_fill(o, fill_price=2000.0)

    # 3 笔 0.05 应累加到 0.15 (用 approx 防浮点误差)
    assert state.position.volume == pytest.approx(0.15, abs=1e-9), (
        f"BUG-2 复发: volume={state.position.volume}, 应为 0.15"
    )


def test_on_fill_vwap_entry_price():
    """P4: 多笔不同 fill_price, entry_price 应当是 VWAP

    笔 1: 0.05 @ 2000 → contribution 100
    笔 2: 0.05 @ 2010 → contribution 100.5
    笔 3: 0.10 @ 2005 → contribution 200.5
    总 volume = 0.20, 总 contribution = 401
    VWAP = 401 / 0.20 = 2005.0
    """
    router = _make_router()
    router.on_fill(_make_order(1, 1, 0.05), fill_price=2000.0)
    router.on_fill(_make_order(2, 1, 0.05), fill_price=2010.0)
    router.on_fill(_make_order(3, 1, 0.10), fill_price=2005.0)

    assert abs(state.position.volume - 0.20) < 1e-9
    assert abs(state.position.entry_price - 2005.0) < 0.01, (
        f"VWAP 错: entry_price={state.position.entry_price}, 应为 2005.0"
    )


def test_on_fill_sl_tp_tracks_latest():
    """P4: SL/TP 应当跟到最新一笔 fill"""
    router = _make_router()
    router.on_fill(_make_order(1, 1, 0.05, sl=1900.0, tp=2100.0),
                   fill_price=2000.0)
    # 后续 fill 用不同的 sl/tp
    router.on_fill(_make_order(2, 1, 0.05, sl=1950.0, tp=2050.0),
                   fill_price=2010.0)

    # SL/TP 跟到第 2 笔
    assert state.position.sl_price == 1950.0
    assert state.position.tp_price == 2050.0


def test_on_fill_opposite_direction_warns_and_skips():
    """P4: 反向 fill (减仓/翻仓) 本期不处理, 留 TODO"""
    router = _make_router()
    # 先建长仓
    router.on_fill(_make_order(1, 1, 0.10), fill_price=2000.0)
    # 反向 fill (平仓/翻仓)
    router.on_fill(_make_order(2, -1, 0.10), fill_price=2010.0)

    # 修复后: 看到 logger.warning, 但 position.volume 保持 0.10 (没改)
    # 这是 "本期不处理" 的明确语义, 后续 PR 加翻仓逻辑
    assert state.position.volume == pytest.approx(0.10, abs=1e-9)
    assert state.position.direction == 1
