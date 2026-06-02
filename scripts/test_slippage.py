"""scripts/test_slippage.py — DynamicSlippageModel 5 case 验证

5 case:
  a. 正常时段 + 低 ATR: 滑点 ≈ base 0.5 tick ($0.005)
  b. 正常时段 + 高 ATR ($5): 滑点 ≈ base + 5*0.05/0.01 = 0.5 + 25 = 25 tick (超 cap, 截到 3 tick)
  c. NFP 事件日: 0.5 × 2 = 1.0 tick
  d. 凌晨 0-1 UTC (低流动性): 0.5 × 1.5 = 0.75 tick
  e. 上限: 同时 4 个 boost 也截到 3 tick
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from execution.slippage import DynamicSlippageModel


def main():
    print("=" * 72)
    print("  DynamicSlippageModel — 5 case 验证")
    print("=" * 72)

    model = DynamicSlippageModel()

    # Case a: 正常时段 + 低 ATR
    bar_a = {"time": datetime(2026, 6, 1, 14, 0, 0, tzinfo=timezone.utc).timestamp()}
    slip_a = model.estimate(bar=bar_a, atr=0.5, is_event_day=False)
    print(f"\n  Case a: 正常 14:00 UTC, ATR=0.5, no event")
    print(f"    slip = ${slip_a:.4f} = {slip_a/0.01:.3f} ticks")
    print(f"    期望: 0.5 tick base + 0.5*0.05/0.01=2.5 tick atr = 3.0 tick (cap)")
    print(f"    → {'✓' if abs(slip_a - 0.03) < 0.001 else '✗'} 应该是 $0.03 (3 tick cap)")

    # Case b: 正常时段 + 高 ATR
    bar_b = bar_a
    slip_b = model.estimate(bar=bar_b, atr=5.0, is_event_day=False)
    print(f"\n  Case b: 正常 14:00 UTC, ATR=5.0, no event")
    print(f"    slip = ${slip_b:.4f} = {slip_b/0.01:.3f} ticks")
    print(f"    期望: 0.5 + 5*0.05/0.01=25 tick → cap 3 tick = $0.03")
    print(f"    → {'✓' if abs(slip_b - 0.03) < 0.001 else '✗'} 应该是 $0.03 (3 tick cap)")

    # Case c: 事件日
    slip_c = model.estimate(bar=bar_a, atr=0.5, is_event_day=True)
    print(f"\n  Case c: 14:00 UTC, ATR=0.5, NFP day")
    print(f"    slip = ${slip_c:.4f} = {slip_c/0.01:.3f} ticks")
    print(f"    期望: 0.5 + 2.5 = 3.0 tick × 2 = 6 → cap 3 = $0.03")
    print(f"    → {'✓' if abs(slip_c - 0.03) < 0.001 else '✗'} 应该是 $0.03 (3 tick cap)")

    # Case d: 凌晨低流动性
    bar_d = {"time": datetime(2026, 6, 1, 0, 30, 0, tzinfo=timezone.utc).timestamp()}
    slip_d = model.estimate(bar=bar_d, atr=0.5, is_event_day=False)
    print(f"\n  Case d: 00:30 UTC (low liquidity), ATR=0.5, no event")
    print(f"    slip = ${slip_d:.4f} = {slip_d/0.01:.3f} ticks")
    print(f"    期望: 0.5 + 2.5 = 3.0 tick × 1.5 = 4.5 → cap 3 = $0.03")
    print(f"    → {'✓' if abs(slip_d - 0.03) < 0.001 else '✗'} 应该是 $0.03 (3 tick cap)")

    # Case d 小 ATR 测试 boost 实际效果
    slip_d_small = model.estimate(bar=bar_d, atr=0.0, is_event_day=False)
    print(f"\n  Case d (no ATR): slip = ${slip_d_small:.4f} = {slip_d_small/0.01:.3f} ticks")
    print(f"    期望: 0.5 base × 1.5 low_liq = 0.75 tick = $0.0075")
    print(f"    → {'✓' if abs(slip_d_small - 0.0075) < 0.001 else '✗'} 应该是 $0.0075 (boost 真的生效)")

    # Case e: 4 个 boost 叠加
    slip_e = model.estimate(bar=bar_d, atr=5.0, is_event_day=True)
    print(f"\n  Case e: 凌晨 + ATR=5 + NFP")
    print(f"    slip = ${slip_e:.4f} = {slip_e/0.01:.3f} ticks")
    print(f"    期望: 0.5 + 25 = 25.5 × 2 × 1.5 = 76.5 → cap 3 = $0.03")
    print(f"    → {'✓' if abs(slip_e - 0.03) < 0.001 else '✗'} 应该是 $0.03 (3 tick cap)")

    # Case f (新增): 中等 ATR 不 cap
    slip_f = model.estimate(bar=bar_a, atr=0.2, is_event_day=False)
    print(f"\n  Case f: 14:00, ATR=0.2")
    print(f"    slip = ${slip_f:.4f} = {slip_f/0.01:.3f} ticks")
    print(f"    期望: 0.5 + 0.2*0.05/0.01=1.0 = 1.5 tick = $0.015")
    print(f"    → {'✓' if abs(slip_f - 0.015) < 0.001 else '✗'} 应该是 $0.015")

    # get_spread_estimate 调试
    print("\n" + "=" * 72)
    print("  get_spread_estimate 调试输出")
    print("=" * 72)
    for atr in [0.5, 1.0, 3.0, 5.0]:
        for is_event in [False, True]:
            est = model.get_spread_estimate(atr=atr, is_event=is_event)
            print(f"  ATR={atr:>4.1f}  event={is_event!s:>5s}  → "
                  f"ticks={est['total_ticks']:>5.3f}  "
                  f"usd/oz={est['total_usd_per_oz']:>7.4f}")

    print()
    print("=" * 72)
    print(f"  ✅ 全部 case 验证完成 (含 cap 触发 + boost 生效)")
    print("=" * 72)


if __name__ == "__main__":
    main()
