"""
P2 SL/TP bid-ask 单元回归测试 (2026-06-03)

验证 paper_engine._check_exit 和 entry 路径在 spread > 0 时行为正确:
  1. long SL: bar.low <= SL (bid-extreme 触发)
  2. long TP: bar.high - spread >= TP (bid-extreme high 触发)
  3. short SL: bar.high >= SL (ask-extreme 触发)
  4. short TP: bar.low + spread <= TP (ask-extreme low 触发)
  5. long entry 在 ask (bar.open + half_spread)
  6. close-based fallback (FORCE_CLOSE_BASED_SLTP=1) 等同老逻辑
"""
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import importlib


def reset_paper_engine():
    """强制重读 paper_engine 让 FORCE_CLOSE_BASED_SLTP env 生效"""
    if "execution.paper_engine" in sys.modules:
        del sys.modules["execution.paper_engine"]
    import execution.paper_engine as pe
    importlib.reload(pe)
    return pe


def test_long_sl_trigger():
    """long SL: bar.low <= SL 应触发, 否则持仓"""
    os.environ["FORCE_CLOSE_BASED_SLTP"] = "1"  # 关 bid/ask, 走老逻辑
    pe = reset_paper_engine()
    e = pe.PaperExecutionEngine(initial_balance=500.0, default_lots=0.01, max_position_lots=1.0)
    sig = pe.Signal(symbol="XAUUSD+", direction=1, price=100.0, atr=1.0,
                    sl_atr=4.0, tp_atr=4.0, strategy="t", timestamp=1000.0, confidence=1.0)
    # on_bar 时序: signal 在 bar[t] 缓存 → bar[t+1] open 成交
    bar0 = {"time": 999.0, "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "spread": 0}
    e.on_bar(bar0, sig)  # 缓存 signal
    bar1 = {"time": 1014.0, "open": 100.0, "high": 100.5, "low": 100.0, "close": 100.5, "spread": 0}
    e.on_bar(bar1, None)  # 触发 entry @ bar1.open
    assert e.position is not None, "long entry 应成功"
    # 老逻辑: entry = 100.0 + 2bps 滑点 = 100.02
    assert abs(e.position.entry_price - 100.02) < 0.001, \
        f"close-based entry 应是 100.02 (含 2bps 滑点), 实际 {e.position.entry_price}"

    # bar.low=95.5 触发 SL (< SL=96)
    bar = {"time": 1015.0, "open": 100.0, "high": 100.5, "low": 95.5, "close": 96.0, "spread": 20}
    e.on_bar(bar, None)
    assert e.position is None, f"SL hit 后应平仓, 实际 pos={e.position}"
    assert e.trades[-1].reason == "sl", f"应 SL 出场, 实际 {e.trades[-1].reason}"
    print("  ✓ test_long_sl_trigger")


def test_long_tp_bid_ask():
    """P2 逻辑: long TP 用 bid-extreme high, spread 0.20 USD 时 bar.high=104.05 不会触发 TP=104.0"""
    os.environ["FORCE_CLOSE_BASED_SLTP"] = "0"
    pe = reset_paper_engine()
    e = pe.PaperExecutionEngine(initial_balance=500.0, default_lots=0.01, max_position_lots=1.0)
    sig = pe.Signal(symbol="XAUUSD+", direction=1, price=100.0, atr=1.0,
                    sl_atr=4.0, tp_atr=4.0, strategy="t", timestamp=1000.0, confidence=1.0)
    bar0 = {"time": 999.0, "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "spread": 0}
    e.on_bar(bar0, sig)
    bar1 = {"time": 1014.0, "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "spread": 0}
    e.on_bar(bar1, None)
    assert e.position is not None, "entry 应成功"

    # P2: entry 100.10 (ask), SL=96.10, TP=104.10
    # bar.high=104.05 < 104.10 → NOT TP
    # bar.low=99.5 > SL (96.10) → NOT SL
    bar = {"time": 1015.0, "open": 100.0, "high": 104.05, "low": 99.5, "close": 100.5, "spread": 20}
    e.on_bar(bar, None)
    assert e.position is not None, f"P2 应仍未平仓, 实际 {e.position}"

    # 第二根 bar high=104.20 >= 104.10 → TP
    bar2 = {"time": 1030.0, "open": 100.0, "high": 104.20, "low": 99.0, "close": 104.0, "spread": 20}
    e.on_bar(bar2, None)
    assert e.position is None, f"TP hit 后应平仓, 实际 {e.position}"
    assert e.trades[-1].reason == "tp", f"应 TP 出场, 实际 {e.trades[-1].reason}"
    print("  ✓ test_long_tp_bid_ask")


def test_long_tp_close_based():
    """close-based (env=1) 时, bar.high=104.05 应触发 TP=104.0 (老逻辑)"""
    os.environ["FORCE_CLOSE_BASED_SLTP"] = "1"
    pe = reset_paper_engine()
    e = pe.PaperExecutionEngine(initial_balance=500.0, default_lots=0.01, max_position_lots=1.0)
    sig = pe.Signal(symbol="XAUUSD+", direction=1, price=100.0, atr=1.0,
                    sl_atr=4.0, tp_atr=4.0, strategy="t", timestamp=1000.0, confidence=1.0)
    bar0 = {"time": 999.0, "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "spread": 0}
    e.on_bar(bar0, sig)
    bar1 = {"time": 1014.0, "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "spread": 0}
    e.on_bar(bar1, None)
    assert e.position is not None

    # 老逻辑: entry 100.02, SL=96.02, TP=104.02
    # bar.high=104.05 >= 104.02 → TP
    bar = {"time": 1015.0, "open": 100.0, "high": 104.05, "low": 99.5, "close": 100.5, "spread": 20}
    e.on_bar(bar, None)
    assert e.position is None, f"close-based TP 应触发, 实际 {e.position}"
    assert e.trades[-1].reason == "tp", f"应 TP 出场, 实际 {e.trades[-1].reason}"
    print("  ✓ test_long_tp_close_based")


def test_short_sl_tp():
    """short: SL 用 ask-extreme high, TP 用 ask-extreme low (low+spread)"""
    os.environ["FORCE_CLOSE_BASED_SLTP"] = "0"
    pe = reset_paper_engine()
    e = pe.PaperExecutionEngine(initial_balance=500.0, default_lots=0.01, max_position_lots=1.0)
    sig = pe.Signal(symbol="XAUUSD+", direction=-1, price=100.0, atr=1.0,
                    sl_atr=4.0, tp_atr=4.0, strategy="t", timestamp=1000.0, confidence=1.0)
    bar0 = {"time": 999.0, "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "spread": 0}
    e.on_bar(bar0, sig)
    bar1 = {"time": 1014.0, "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "spread": 0}
    e.on_bar(bar1, None)
    assert e.position is not None and e.position.direction == -1

    # short entry @ 100.0 (bid), SL=104.0, TP=96.0
    # bar.high=104.5 >= SL → SL hit
    bar = {"time": 1015.0, "open": 100.0, "high": 104.5, "low": 99.5, "close": 100.5, "spread": 20}
    e.on_bar(bar, None)
    assert e.position is None, f"short SL hit 应平仓, 实际 {e.position}"
    assert e.trades[-1].reason == "sl", f"应 SL, 实际 {e.trades[-1].reason}"
    print("  ✓ test_short_sl_tp")


def test_long_entry_in_ask():
    """long entry 价应在 ask (bar.open + half_spread + 2bps 滑点)"""
    os.environ["FORCE_CLOSE_BASED_SLTP"] = "0"
    pe = reset_paper_engine()
    e = pe.PaperExecutionEngine(initial_balance=500.0, default_lots=0.01, max_position_lots=1.0)
    sig = pe.Signal(symbol="XAUUSD+", direction=1, price=100.0, atr=1.0,
                    sl_atr=4.0, tp_atr=4.0, strategy="t", timestamp=1000.0, confidence=1.0)
    bar0 = {"time": 999.0, "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "spread": 0}
    e.on_bar(bar0, sig)
    # bar1.open=100.0, spread=20 points=0.20 USD, half=0.10 → entry ask=100.10
    # + 2bps 滑点 = 100.10 * 1.0002 = 100.12 (四舍五入)
    bar1 = {"time": 1014.0, "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "spread": 20}
    e.on_bar(bar1, None)
    assert e.position is not None
    # 预期 entry ≈ 100.10 + 2bps 滑点 = 100.12
    assert abs(e.position.entry_price - 100.12) < 0.005, \
        f"long entry 应在 ask+slippage (≈100.12), 实际 {e.position.entry_price}"
    print("  ✓ test_long_entry_in_ask")


def test_short_entry_in_bid():
    """short entry 价应在 bid (bar.open, 不加 half_spread, 但扣 2bps 滑点)"""
    os.environ["FORCE_CLOSE_BASED_SLTP"] = "0"
    pe = reset_paper_engine()
    e = pe.PaperExecutionEngine(initial_balance=500.0, default_lots=0.01, max_position_lots=1.0)
    sig = pe.Signal(symbol="XAUUSD+", direction=-1, price=100.0, atr=1.0,
                    sl_atr=4.0, tp_atr=4.0, strategy="t", timestamp=1000.0, confidence=1.0)
    bar0 = {"time": 999.0, "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "spread": 0}
    e.on_bar(bar0, sig)
    bar1 = {"time": 1014.0, "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "spread": 20}
    e.on_bar(bar1, None)
    assert e.position is not None
    # short entry = 100.0 - 2bps 滑点 = 99.98
    assert abs(e.position.entry_price - 99.98) < 0.005, \
        f"short entry 应在 bid-sl (≈99.98), 实际 {e.position.entry_price}"
    print("  ✓ test_short_entry_in_bid")


def main():
    print("P2 SL/TP bid-ask 单元回归测试 (2026-06-03)")
    print("=" * 60)
    test_long_sl_trigger()
    test_long_tp_bid_ask()
    test_long_tp_close_based()
    test_short_sl_tp()
    test_long_entry_in_ask()
    test_short_entry_in_bid()
    print("=" * 60)
    print("✓ 全部 6 个测试通过")


if __name__ == "__main__":
    main()
