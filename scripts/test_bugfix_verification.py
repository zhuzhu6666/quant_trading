"""BUGFIX verification 2026-06-03
- BUG-1: daily_loss_pct 在大盈利日不返回正值 (不再误熔断)
- BUG-2: circuit.reset() 不再覆写 daily.peak_equity
- BUG-3: factor_engine IC 多周期 forward_periods 实现
- BUG-5: 零净利交易 break-even 单独计
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.state
from core.state import state, DailyStats
from risk.circuit import CircuitBreaker


def test_bug1_no_mis_trip_on_profit():
    """BUG-1: 大盈利日 daily_loss_pct 应为 0, 不应触发熔断"""
    state.daily = DailyStats()
    state.balance = 1000.0
    state.daily.net_pnl = +100.0  # 赚 100 (10% 盈利)
    state.daily.peak_equity = 1100.0
    state.equity = 1100.0

    # 旧版: abs(100)/1000*100 = 10% → 触发熔断
    # 新版: max(0, -100)/1000*100 = 0% → 不触发
    assert state.daily_loss_pct == 0.0, \
        f"BUG-1 FAIL: 盈利日 daily_loss_pct 应为 0, 实际 {state.daily_loss_pct}"
    print("  ✓ test_bug1_no_mis_trip_on_profit")


def test_bug1_loss_day_unchanged():
    """BUG-1: 亏损日 daily_loss_pct 应正常返回正百分比"""
    state.daily = DailyStats()
    state.balance = 1000.0
    state.daily.net_pnl = -50.0
    state.equity = 950.0

    expected = 50.0 / 1000 * 100  # 5%
    assert abs(state.daily_loss_pct - expected) < 0.001, \
        f"BUG-1 FAIL: 亏损日 daily_loss_pct 应为 {expected}%, 实际 {state.daily_loss_pct}"
    print("  ✓ test_bug1_loss_day_unchanged")


def test_bug2_peak_equity_preserved():
    """BUG-2: circuit.reset() 不再清 daily.peak_equity"""
    state.daily = DailyStats()
    state.balance = 1000.0
    state.daily.peak_equity = 1500.0  # 模拟当日峰值
    state.daily.net_pnl = 500.0
    state.daily.gross_pnl = 500.0
    state.daily.winning_trades = 5

    cb = CircuitBreaker()
    cb.reset()

    # 旧版: state.reset_daily() 会把 peak_equity 清成 0
    # 新版: circuit.reset() 只清自己的 ATR/slip, 不动 daily
    assert state.daily.peak_equity == 1500.0, \
        f"BUG-2 FAIL: peak_equity 应保留 1500, 实际 {state.daily.peak_equity}"
    assert state.daily.winning_trades == 5, \
        f"BUG-2 FAIL: winning_trades 应保留 5, 实际 {state.daily.winning_trades}"
    assert state.is_circuit_breaker == False, "circuit 应解除"
    print("  ✓ test_bug2_peak_equity_preserved")


def test_bug2_circuit_state_cleared():
    """BUG-2: circuit.reset() 应正确清自己的状态"""
    state.daily = DailyStats()
    state.daily.peak_equity = 1500.0  # 保留
    state.is_circuit_breaker = True
    state.circuit_reason = "test"

    cb = CircuitBreaker()
    cb._atr_history.append(1.5)
    cb._slippage_sum = 10.0
    cb._slippage_count = 5
    cb.reset()

    assert state.is_circuit_breaker == False
    assert state.circuit_reason == ""
    assert len(cb._atr_history) == 0
    assert cb._slippage_sum == 0.0
    assert cb._slippage_count == 0
    assert state.daily.peak_equity == 1500.0  # 关键: peak 不被清
    print("  ✓ test_bug2_circuit_state_cleared")


def test_bug5_break_even_counted():
    """BUG-5: 零净利交易 break_even 单独计"""
    state.daily = DailyStats()
    state.record_trade(pnl=10.0, commission=0.0)  # 赢
    state.record_trade(pnl=-5.0, commission=0.0)   # 亏
    state.record_trade(pnl=0.0, commission=0.0)    # 零净利
    state.record_trade(pnl=0.0, commission=0.0)    # 零净利

    assert state.daily.winning_trades == 1, f"wins=1, got {state.daily.winning_trades}"
    assert state.daily.losing_trades == 1, f"losses=1, got {state.daily.losing_trades}"
    assert state.daily.break_even_trades == 2, \
        f"break_even=2, got {state.daily.break_even_trades}"
    # zero-PnL 不重置 consecutive_losses (跟赢不同), 维持 1
    assert state.daily.consecutive_losses == 1, \
        f"0 净利不重置 consecutive_losses, 应保持 1, got {state.daily.consecutive_losses}"
    assert state.daily.total_trades == 4
    print("  ✓ test_bug5_break_even_counted")


def test_bug3_multi_period_ic():
    """BUG-3: IC 多周期输出 (验证列存在 + 1/5/10/20 列)"""
    import numpy as np
    import pandas as pd
    from alpha.factor_engine import FactorEngine
    import strategies  # noqa

    # 真实数据驱动, 不 mock 随机数据
    from data.store import DataStore
    store = DataStore("data/market_data.db")
    df = store.load_bars("XAUUSD+", "M15")
    df = df.tail(5000).reset_index(drop=True)

    eng = FactorEngine(df)
    eng.compute_all()  # 22 因子真实计算

    ic = eng.ic_analysis(forward_periods=[1, 5, 10, 20])
    assert "ic_1" in ic.columns, "BUG-3 FAIL: ic_1 列缺失"
    assert "ic_5" in ic.columns, "BUG-3 FAIL: ic_5 列缺失"
    assert "ic_10" in ic.columns, "BUG-3 FAIL: ic_10 列缺失"
    assert "ic_20" in ic.columns, "BUG-3 FAIL: ic_20 列缺失"
    assert "ic_mean" in ic.columns, "BUG-3 FAIL: ic_mean 列缺失"

    # 关键: 多周期 IC 之间应不一致 (如果都一致, 说明 BUG 还在硬编码 1-bar)
    ic1 = ic["ic_1"].values
    ic5 = ic["ic_5"].values
    # 至少 60% 因子 ic_1 跟 ic_5 不完全相等
    diff_pct = np.mean(ic1 != ic5) * 100
    assert diff_pct >= 60, \
        f"BUG-3 FAIL: 只有 {diff_pct:.0f}% 因子 ic_1 跟 ic_5 不同 — 多周期可能未生效"
    print(f"  ✓ test_bug3_multi_period_ic ({len(ic)} 因子, {diff_pct:.0f}% ic_1≠ic_5, 说明多周期生效)")
    print(f"    强因子 (ic_mean top3):")
    top3 = ic.sort_values("ic_mean", ascending=False).head(3)
    for _, r in top3.iterrows():
        print(f"      {r['factor']:20s} ic_1={r['ic_1']:+.4f} ic_5={r['ic_5']:+.4f} ic_10={r['ic_10']:+.4f} ic_20={r['ic_20']:+.4f} mean={r['ic_mean']:+.4f}")


if __name__ == "__main__":
    print("BUGFIX verification (2026-06-03):")
    print("=" * 60)
    test_bug1_no_mis_trip_on_profit()
    test_bug1_loss_day_unchanged()
    test_bug2_peak_equity_preserved()
    test_bug2_circuit_state_cleared()
    test_bug5_break_even_counted()
    test_bug3_multi_period_ic()
    print("=" * 60)
    print("✓ 全部 6 个测试通过")
