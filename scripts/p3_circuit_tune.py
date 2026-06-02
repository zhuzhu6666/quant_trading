"""
scripts/p3_circuit_tune.py
============================

P3 风险调优 — circuit breaker 阈值 sweep:
  测试 4 档: 5.0 (原) / 8.0 / 10.0 / 12.0 (日损)
           2.5 (原) / 3.0 / 4.0 (波动率乘数)

每档跑 50K bar, 测:
  - Final PnL
  - Trades 数
  - DD
  - 触发次数 (从 CIRCUIT BREAKER TRIPPED 计数)

目标: 找 circuit 既能阻止真崩盘, 又不过度限制 PnL 的甜蜜点
"""
import logging
import sys
import time as _time
from pathlib import Path
from io import StringIO

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import strategies  # noqa: F401
from execution.paper_trader import PaperTrader
from strategy.registry import strategy_registry
from data.store import DataStore

logging.basicConfig(level=logging.WARNING, format="%(message)s")
logger = logging.getLogger("p3_tune")
logger.setLevel(logging.WARNING)


# (max_daily_loss_pct, volatility_mult, label)
CONFIGS = [
    (5.0, 3.0, "原配置 5%/3x (触发频繁)"),
    (8.0, 3.0, "日损 8% (宽松)"),
    (10.0, 3.0, "日损 10% (更宽松)"),
    (8.0, 4.0, "日损 8% + 波动 4x (最宽松)"),
]


def count_circuit_trips(paper_output: str) -> int:
    """从 paper 输出里数 CIRCUIT BREAKER TRIPPED 次数"""
    return paper_output.count("CIRCUIT BREAKER TRIPPED")


def run_one(max_daily_loss_pct: float, volatility_mult: float,
           strat, bars_df) -> dict:
    """跑一次, 捕获 circuit 触发次数"""
    store = DataStore("data/market_data.db")

    # 用 StringIO 截 logger 输出
    log_stream = StringIO()
    handler = logging.StreamHandler(log_stream)
    handler.setLevel(logging.WARNING)
    logger_obj = logging.getLogger("quant")
    logger_obj.addHandler(handler)

    trader = PaperTrader(
        strategy=strat, initial_balance=500.0,
        default_lots=0.01, max_lots=0.5,
        warmup_bars=500,
        max_daily_loss_pct=max_daily_loss_pct,
        volatility_mult=volatility_mult,
        enable_circuit=True,
    )
    trader.load_data(store, "XAUUSD+", "M15")
    t0 = _time.time()
    report = trader.run()
    elapsed = _time.time() - t0

    # 移除 handler
    logger_obj.removeHandler(handler)
    trips = count_circuit_trips(log_stream.getvalue())

    return {
        "max_daily_loss_pct": max_daily_loss_pct,
        "volatility_mult": volatility_mult,
        "PnL": report.net_pnl,
        "PnL_pct": report.total_return_pct,
        "Trades": report.total_trades,
        "WR": report.win_rate,
        "DD": report.max_drawdown_pct,
        "Sharpe": report.sharpe,
        "Final": report.final_balance,
        "trips": trips,
        "elapsed": elapsed,
    }


def main():
    print("=" * 78)
    print("  P3 风险调优 — circuit breaker 阈值 sweep (M15 50K bar)")
    print("=" * 78)

    # 单次创建策略
    strat = strategy_registry.create(
        "multi_factor_m15", symbol="XAUUSD+", timeframe="M15",
        sl_atr=3.0, tp_atr=4.0, cooldown_bars=3,
        enable_nfp_skip=True, nfp_skip_days=1,
        enable_dual_event_skip=True,
        enable_gvz_gate=True, gvz_drop_pct=-2.0,
    )

    store = DataStore("data/market_data.db")
    bars = store.load_bars("XAUUSD+", "M15")
    print(f"  Loaded {len(bars)} bars")

    results = []
    for max_dl, vol_mult, label in CONFIGS:
        print(f"\n  [{label}]  max_daily_loss={max_dl}%, vol_mult={vol_mult}x")
        # 每次重建 strategy (circuit 状态独立)
        strat = strategy_registry.create(
            "multi_factor_m15", symbol="XAUUSD+", timeframe="M15",
            sl_atr=3.0, tp_atr=4.0, cooldown_bars=3,
            enable_nfp_skip=True, nfp_skip_days=1,
            enable_dual_event_skip=True,
            enable_gvz_gate=True, gvz_drop_pct=-2.0,
        )
        r = run_one(max_dl, vol_mult, strat, bars)
        r["label"] = label
        results.append(r)
        print(f"  done in {r['elapsed']:.0f}s  PnL={r['PnL_pct']:+.2f}%  "
              f"Trips={r['trips']}  DD={r['DD']:.1f}%  Sharpe={r['Sharpe']:.2f}")

    # 报告
    print("\n" + "=" * 78)
    print("  P3 Circuit 阈值 sweep 报告")
    print("=" * 78)
    print(f"  {'配置':<32s}  {'PnL%':>8s}  {'Trades':>7s}  {'Trips':>5s}  "
          f"{'DD%':>5s}  {'Sharpe':>6s}  {'Final':>8s}")
    print("-" * 95)
    for r in results:
        print(f"  {r['label']:<32s}  {r['PnL_pct']:>+8.2f}  {r['Trades']:>7d}  "
              f"{r['trips']:>5d}  {r['DD']:>5.1f}  {r['Sharpe']:>6.3f}  "
              f"{r['Final']:>8.2f}")

    # 找最优 (Sharpe 最大, 同时 trips < 5, PnL > 0)
    valid = [r for r in results if r["PnL_pct"] > 0 and r["trips"] < 5]
    if valid:
        best = max(valid, key=lambda r: r["Sharpe"])
        print(f"\n  最优 (Sharpe 最大 + PnL>0 + Trips<5):")
        print(f"    {best['label']}  Sharpe={best['Sharpe']:.3f}  "
              f"PnL={best['PnL_pct']:+.2f}%  Trips={best['trips']}")
    else:
        print("\n  ⚠ 没有满足 PnL>0 + Trips<5 的配置")

    # 落盘
    out_path = PROJECT_ROOT / "data" / "charts" / "p3_circuit_tune_report.txt"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("P3 Circuit Breaker 阈值调优报告\n\n")
        f.write(f"Bar 范围: {len(bars)} (M15, multi_factor_m15, enable_circuit=True)\n\n")
        for r in results:
            f.write(f"{r['label']}\n")
            f.write(f"  max_daily_loss_pct={r['max_daily_loss_pct']}  "
                    f"volatility_mult={r['volatility_mult']}\n")
            f.write(f"  PnL: {r['PnL_pct']:+.2f}%  Trades: {r['Trades']}  "
                    f"Trips: {r['trips']}  DD: {r['DD']:.1f}%  Sharpe: {r['Sharpe']:.3f}\n")
            f.write(f"  Final: {r['Final']:.2f}\n\n")
        if valid:
            f.write(f"最优: {best['label']} (PnL={best['PnL_pct']:+.2f}%, "
                    f"Sharpe={best['Sharpe']:.3f}, Trips={best['trips']})\n")
    print(f"\n→ 落盘: {out_path}")
    print("=" * 78)


if __name__ == "__main__":
    main()
