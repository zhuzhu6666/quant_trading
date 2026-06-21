"""scripts/baseline_all_strategies.py — 7 策略 50K bar baseline 排名

跑 paper baseline, 复用 PaperTrader, 输出 ret/WR/DD/PF/trades 排名表.
"""
import sys
import time as _time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy.registry import strategy_registry
from data.store import DataStore
from execution.paper_trader import PaperTrader

# TF 优先级: 策略 timeframes 里按这个顺序选
TF_PREF = ["M15", "H1", "H4", "M30", "M5", "D1"]


def main():
    print("=" * 78)
    print("  7 策略 50K bar baseline — XAUUSD+")
    print("=" * 78)
    print()

    store = DataStore("data/ctrader_data.duckdb")
    results = []
    for name in strategy_registry.list():
        strat_cls = strategy_registry._strategies[name]
        # 选 TF
        supported = getattr(strat_cls, "_reg_timeframes", ["M15"])
        tf = next((t for t in TF_PREF if t in supported), supported[0])

        # 检查数据
        df = store.load_bars("XAUUSD+", tf)
        if df.empty:
            print(f"  {name:20s} ({tf})  skip — no data")
            continue

        # 跑
        # run_paper 验证用的 5 个事件/波动率过滤 (仅对 multi_factor / macd_bb 有效)
        # ma_cross_h4 / gold_momentum 忽略这些参数 (它们 params 字典不认)
        kwargs = {}
        if name in ("multi_factor_m15", "macd_bb"):
            kwargs = dict(sl_atr=3.0, tp_atr=4.0, cooldown_bars=3,
                          enable_nfp_skip=True, nfp_skip_days=1,
                          enable_dual_event_skip=True,
                          enable_gvz_gate=True, gvz_drop_pct=-2.0)
        strat = strategy_registry.create(name, symbol="XAUUSD+", timeframe=tf, **kwargs)
        trader = PaperTrader(
            strategy=strat, initial_balance=500.0, default_lots=0.01,
            max_lots=2.0, warmup_bars=500, enable_circuit=False,
        )
        trader.load_data(store, "XAUUSD+", tf)
        t0 = _time.time()
        try:
            report = trader.run()
        except Exception as e:
            print(f"  {name:20s} ({tf})  ERROR: {type(e).__name__}: {e}")
            continue
        dt = _time.time() - t0

        results.append({
            "name": name, "tf": tf, "ret": report.total_return_pct,
            "wr": report.win_rate, "dd": report.max_drawdown_pct,
            "pf": report.profit_factor, "trades": report.total_trades,
            "rt": dt,
        })
        print(f"  {name:20s} ({tf})  ret={report.total_return_pct:+8.2f}%  "
              f"WR={report.win_rate:5.1f}%  DD={report.max_drawdown_pct:6.2f}%  "
              f"PF={report.profit_factor:5.2f}  trades={report.total_trades:5d}  "
              f"[{dt:5.1f}s]")

    # 排名
    print()
    print("=" * 78)
    print("  排名 (按 ret 降序)")
    print("=" * 78)
    results.sort(key=lambda r: r["ret"], reverse=True)
    print(f"  {'Rank':<5} {'Strategy':<20} {'TF':<4}  "
          f"{'ret%':>8} {'WR%':>6} {'DD%':>7} {'PF':>5}  {'trades':>7}  {'sec':>6}")
    print(f"  {'-'*5} {'-'*20} {'-'*4}  "
          f"{'-'*8} {'-'*6} {'-'*7} {'-'*5}  {'-'*7}  {'-'*6}")
    for i, r in enumerate(results, 1):
        print(f"  {i:<5} {r['name']:<20} {r['tf']:<4}  "
              f"{r['ret']:>+8.2f} {r['wr']:>6.1f} {r['dd']:>7.2f} {r['pf']:>5.2f}  "
              f"{r['trades']:>7d}  {r['rt']:>6.1f}")

    # 落盘
    out_path = Path("data/charts/baseline_all_strategies.txt")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("7 策略 50K bar baseline — XAUUSD+\n")
        f.write(f"{'Rank':<5} {'Strategy':<20} {'TF':<4}  "
                f"{'ret%':>8} {'WR%':>6} {'DD%':>7} {'PF':>5}  {'trades':>7}  {'sec':>6}\n")
        for i, r in enumerate(results, 1):
            f.write(f"{i:<5} {r['name']:<20} {r['tf']:<4}  "
                    f"{r['ret']:>+8.2f} {r['wr']:>6.1f} {r['dd']:>7.2f} {r['pf']:>5.2f}  "
                    f"{r['trades']:>7d}  {r['rt']:>6.1f}\n")
    print()
    print(f"  → {out_path}")
    print("=" * 78)


if __name__ == "__main__":
    main()
