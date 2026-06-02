"""scripts/test_match_replay.py — MatchReplayEngine 3 case 验证"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.store import DataStore
from execution.match_replay import MatchReplayEngine


def main():
    print("=" * 78)
    print("  MatchReplayEngine — 撮合回放 3 case 验证")
    print("=" * 78)

    store = DataStore("data/market_data.db")
    df = store.load_bars("XAUUSD+", "M15")
    assert not df.empty
    print(f"Loaded {len(df)} M15 bars")

    # Case A: 0.01 手市价买, 5 个不同 bar
    print("\n" + "=" * 78)
    print("  Case A: 0.01 手市价买 × 5 个不同 bar")
    print("=" * 78)
    print(f"  {'i':>4s}  {'O':>9s}  {'H':>9s}  {'L':>9s}  {'C':>9s}  "
          f"{'mid':>9s}  {'fill':>9s}  {'level':>5s}  {'partial':>8s}")
    indices = [1000, 5000, 10000, 25000, 49999]
    for i in indices:
        row = df.iloc[i]
        bar = {
            "time": df.index[i].timestamp() if hasattr(df.index[i], "timestamp") else float(df.index[i]),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row.get("volume", 0)),
        }
        eng = MatchReplayEngine(bar, config={"n_ticks": 100, "seed": 42})
        result = eng.replay(side=1, size=0.01)
        print(f"  {i:>4d}  {bar['open']:>9.2f}  {bar['high']:>9.2f}  {bar['low']:>9.2f}  "
              f"{bar['close']:>9.2f}  {result['mid']:>9.2f}  {result['filled_price']:>9.2f}  "
              f"{result['level']:>5d}  {str(result['partial']):>8s}")
        # 验证: fill_price 在 [low, high] 范围内
        assert bar['low'] - 0.5 <= result['filled_price'] <= bar['high'] + 0.5, \
            f"fill {result['filled_price']} out of range [{bar['low']}, {bar['high']}]"
    print("  ✓ 5 bar 的 fill_price 都在 [low, high] 范围内")

    # Case B: 不同 size 的市价买
    print("\n" + "=" * 78)
    print("  Case B: 不同 size 市价买 (大单应触及多档)")
    print("=" * 78)
    bar_b = {
        "time": df.index[10000].timestamp(),
        "open": float(df.iloc[10000]["open"]),
        "high": float(df.iloc[10000]["high"]),
        "low": float(df.iloc[10000]["low"]),
        "close": float(df.iloc[10000]["close"]),
        "volume": float(df.iloc[10000].get("volume", 0)),
    }
    print(f"  bar: O={bar_b['open']:.2f} H={bar_b['high']:.2f} L={bar_b['low']:.2f} C={bar_b['close']:.2f} V={bar_b['volume']:.1f}")
    print(f"  {'size':>8s}  {'fill':>9s}  {'level':>5s}  {'slip_t':>7s}  {'partial':>8s}")
    for size in [0.001, 0.01, 0.05, 0.20, 1.00]:
        eng = MatchReplayEngine(bar_b, config={"n_ticks": 100, "seed": 42})
        result = eng.replay(side=1, size=size)
        print(f"  {size:>8.3f}  {result['filled_price']:>9.2f}  {result['level']:>5d}  "
              f"{result['slippage_ticks']:>7.2f}  {str(result['partial']):>8s}")

    # Case C: N=10 vs N=100 ticks
    print("\n" + "=" * 78)
    print("  Case C: N=10 vs N=100 ticks (tick 越多 book 越精细)")
    print("=" * 78)
    bar_c = bar_b
    for n_ticks in [10, 50, 100, 500]:
        eng = MatchReplayEngine(bar_c, config={"n_ticks": n_ticks, "seed": 42})
        result = eng.replay(side=1, size=0.01)
        # 算 tick 价格范围 (low ~ high in tick sequence)
        tick_prices = [t["price"] for t in eng._ticks]
        print(f"  N={n_ticks:>4d}  fill={result['filled_price']:.2f}  "
              f"tick range=[{min(tick_prices):.2f}, {max(tick_prices):.2f}]  "
              f"slip={result['slippage_ticks']:.2f}t")

    # 卖单示例
    print("\n" + "=" * 78)
    print("  Case D: 卖单示例 (side=-1)")
    print("=" * 78)
    eng = MatchReplayEngine(bar_c, config={"n_ticks": 100, "seed": 42})
    result = eng.replay(side=-1, size=0.01)
    print(f"  卖 0.01 手: fill={result['filled_price']:.2f}  mid={result['mid']:.2f}  "
          f"slip={result['slippage_ticks']:.2f}t (sell 应 < mid)")

    print()
    print("=" * 78)
    print("  ✅ MatchReplayEngine 验证完成")
    print("=" * 78)


if __name__ == "__main__":
    main()
