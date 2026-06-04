"""
tests/test_p2_bug6_trailing_sl.py — P2 fix: trailing SL 公式反向

引自 framework_audit_20260604.md BUG-6:
risk/position.py:62-65 长仓分支用 (sl-entry)/abs(sl-entry) = -1 当系数,
导致 trail_sl = _trail_high - trail_atr_mult * (-1) = _trail_high + trail_atr_mult,
新 SL 推到历史最高点**之上**, 下一根 bar 立刻被打掉。

本文件 4 个 case:
  - 长仓 trailing SL 必 <= _trail_high - mult
  - 短仓 trailing SL 必 >= _trail_low + mult
  - reset() 同时清两个 tracker, 不污染新仓位
  - reset() on flat (direction=0) 不污染 tracker
"""
import pytest

# conftest 已把 PROJECT_ROOT 加 sys.path
from core.state import state
from risk.position import PositionMonitor


@pytest.fixture(autouse=True)
def _reset_state():
    """每个 test 前后清 state, 避免污染"""
    state.balance = 1000.0
    state.equity = 1000.0
    state.position.symbol = "XAUUSD+"
    state.position.direction = 0
    state.position.volume = 0.0
    state.position.entry_price = 0.0
    state.position.sl_price = 0.0
    state.position.tp_price = 0.0
    state.is_circuit_breaker = False
    state.circuit_reason = ""
    yield
    state.position.direction = 0
    state.position.volume = 0.0


def _make_tick_event(bid: float, ask: float):
    from core.event_bus import Event, EventType
    return Event(type=EventType.TICK, data={"bid": bid, "ask": ask})


def test_long_trailing_sl_below_peak():
    """P2: 长仓 SL 永远不能推到 _trail_high 之上"""
    state.position.direction = 1
    state.position.entry_price = 1900.0
    state.position.sl_price = 0.0       # SL 设 0: bid=1908 > 0 不会 hit
    state.position.tp_price = 99999.0   # TP 设极高: bid=1908 < 99999 不会 hit
    state.position.volume = 0.1
    pm = PositionMonitor(enable_trailing_stop=True, trail_atr_mult=2.0)
    pm._trail_high = 1910.0  # 已见到 1910

    # tick bid=1908, _trail_high 应更新到 1910, trail_sl = 1910 - 2.0 = 1908
    pm.on_tick(_make_tick_event(bid=1908.0, ask=1908.5))

    # 关键断言: 新 SL 必 <= 1908 (1908 = _trail_high - mult)
    assert state.position.sl_price <= 1908.0, (
        f"BUG-6 复发: sl_price={state.position.sl_price} 推到 _trail_high 之上"
    )
    # 进一步: SL 应只升不降 (trailing 性质)
    assert state.position.sl_price >= 0.0  # 原 SL, 不应被改低


def test_short_trailing_sl_above_trough():
    """P2: 短仓 buggy 公式会把 SL 推到 _trail_low 之下, 修复后 SL 不变松

    设 sl_price=99999 (极高, ask=1892 < 99999 不会 hit SL, 走不到 trailing).
    不行, ask=1892 也不 hit TP (tp=-99999), 走 trailing:
    buggy: trail_sl = 1890 + (99999-1900) = 99989, 99989 < 99999? 不, 不更新
    修复后: trail_sl = 1890 + 2.0 = 1892, 1892 < 99999? 是, 更新到 1892
    -> 修复后 SL 会变成 1892, buggy SL 不变 = 99999

    关键: buggy 公式用 (sl-entry) 系数, 大 sl 算出来也大, 不更新.
    修复后用固定 mult, 算出来 1892, < 99999, 更新.
    所以断言: 修复后 SL == 1892, buggy SL == 99999.
    """
    state.position.direction = -1
    state.position.entry_price = 1900.0
    state.position.sl_price = 99999.0   # 极高, 不会 hit SL
    state.position.tp_price = -99999.0  # 极低, 不会 hit TP
    state.position.volume = 0.1
    pm = PositionMonitor(enable_trailing_stop=True, trail_atr_mult=2.0)
    pm._trail_low = 1890.0  # 已见到 1890

    pm.on_tick(_make_tick_event(bid=1891.5, ask=1892.0))

    # 修复后: SL 应更新到 1892 (= _trail_low + mult)
    # buggy: SL 应保留 99999 (公式算出来 99989 < 99999 不更新? 不, 99989 < 99999 是, 更新! 错)
    # 等等, 重新算 buggy: 1890 + (99999-1900) = 99989, if 99989 < 99999: True, 错地把 SL 改成 99989
    # 然后再 on_tick 第二次: bug 又跑一次, 99989 同样 < 99999? 不, 99989 < 99999, 又更新
    # OK 反正 buggy 至少改成 99989, 修复后改成 1892.
    # 简化: 修复后 SL == 1892, buggy SL != 1892 (是 99989)
    assert state.position.sl_price == 1892.0, (
        f"BUG-6 修复未生效 (short): sl_price={state.position.sl_price}, 应为 1892"
    )


def test_reset_clears_both_trackers_on_new_position():
    """P2: reset() 应同时清两个 tracker, 不让旧仓位数据污染新仓位"""
    # 模拟: 先有长仓, _trail_high=1910; 然后 reset 准备开空仓
    pm = PositionMonitor(enable_trailing_stop=True, trail_atr_mult=2.0)
    pm._trail_high = 1910.0
    pm._trail_low = 1880.0  # 同时也有"低"数据, 不论实际
    # 注意: _trail_low 在 PositionMonitor 里只在空仓时更新, 残留是 reset bug

    # 现在准备开空仓
    state.position.direction = -1
    state.position.entry_price = 1900.0
    pm.reset()

    # 修复后: _trail_high 应重置为 0 (不影响新空仓的 _trail_low 逻辑)
    assert pm._trail_high == 0.0
    # _trail_low 应设为 entry (新空仓的 baseline)
    assert pm._trail_low == 1900.0


def test_reset_on_flat_does_not_corrupt_trackers():
    """P2: reset() 在 direction=0 (flat) 时不应把 _trail_low 设成 0

    如果 flat 时走 else, _trail_low = pos.entry_price = 0,
    下次开多仓时 _trail_low = 0, min(_trail_low=0, ask) 永远 = 0,
    短仓 trailing 逻辑死掉。
    """
    pm = PositionMonitor(enable_trailing_stop=True, trail_atr_mult=2.0)
    pm._trail_high = 1910.0
    pm._trail_low = 1880.0

    # flat 状态
    state.position.direction = 0
    state.position.entry_price = 0.0
    pm.reset()

    # 修复后: flat 时两个 tracker 都应是 sentinel
    # 长仓 sentinel = 0.0, 短仓 sentinel = inf
    # 这里 direction=0, 任何一边都不该 active
    assert pm._trail_high == 0.0
    assert pm._trail_low == float("inf")
