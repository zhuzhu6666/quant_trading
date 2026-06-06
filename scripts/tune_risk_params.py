"""scripts/tune_risk_params.py — 梯度调参 verify-2

3 轮测试:
1. risk=1.0%, CB=15%  (保守)
2. risk=1.5%, CB=20%  (平衡)
3. risk=2.0%, CB=25%  (激进)

每轮跑 50396 M15 bar, 输出 PnL / trades / MaxDD / Sharpe / CB 触发次数.
"""
import sys
import os
import time
import logging

# 抑制大量 INFO log, 只保留 WARNING+
logging.basicConfig(level=logging.WARNING)
# 但保留 quant logger 的 WARNING
quant_logger = logging.getLogger("quant")
quant_logger.setLevel(logging.WARNING)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import load_config, cfg_get
from data.store import DataStore
from strategy.registry import strategy_registry
from execution.paper_trader import PaperTrader
from execution.event_sizing import EventSizing
import strategies  # noqa: F401 — 触发策略注册

CFG = load_config()
store = DataStore("data/market_data.db")

# 3 轮调参
TUNES = [
    {"risk_pct": 1.0, "cb_pct": 15.0, "label": "保守 (1% Kelly + 15% CB)"},
    {"risk_pct": 1.5, "cb_pct": 20.0, "label": "平衡 (1.5% Kelly + 20% CB)"},
    {"risk_pct": 2.0, "cb_pct": 25.0, "label": "激进 (2% Kelly + 25% CB)"},
]

results = []

for tune in TUNES:
    risk_pct = tune["risk_pct"]
    cb_pct = tune["cb_pct"]
    label = tune["label"]

    print(f"\n{'='*60}")
    print(f"  轮次: {label}")
    print(f"  risk_per_trade_pct={risk_pct}%, max_daily_loss_pct={cb_pct}%")
    print(f"{'='*60}")

    # 每轮重新建策略 (避免状态残留)
    override_params = {
        "sl_atr": 3.0, "tp_atr": 4.0, "cooldown_bars": 3,
        "enable_nfp_skip": True, "nfp_skip_days": 1,
        "enable_dual_event_skip": True,
        "enable_gvz_gate": True, "gvz_drop_pct": -2.0,
    }
    strategy = strategy_registry.create(
        "multi_factor_m15", symbol="XAUUSD+", timeframe="M15",
        **override_params,
    )

    # event_sizing (每轮重建)
    try:
        event_sizing = EventSizing(
            db_path=cfg_get(CFG, "event_sizing", "db_path", default="data/market_data.db"),
            enabled=True,
        )
    except Exception:
        event_sizing = None

    trader = PaperTrader(
        strategy=strategy,
        initial_balance=500.0,
        default_lots=0.01,
        max_lots=0.1,
        warmup_bars=500,
        risk_per_trade_pct=risk_pct,
        enable_circuit=True,
        max_daily_loss_pct=cb_pct,
        event_sizing=event_sizing,
    )

    try:
        trader.load_data(store, "XAUUSD+", "M15")
    except ValueError as e:
        print(f"  ✗ 加载失败: {e}")
        continue

    t0 = time.time()
    report = trader.run()
    elapsed = time.time() - t0

    # PaperReport 是 dataclass, 直接访问属性
    n_trades = report.total_trades
    n_win = report.wins
    n_loss = report.losses
    wr = report.win_rate
    net_pnl = report.net_pnl
    net_pnl_pct = report.total_return_pct
    max_dd = report.max_drawdown_pct
    sharpe = report.sharpe
    pf = report.profit_factor

    result = {
        "label": label,
        "risk_pct": risk_pct,
        "cb_pct": cb_pct,
        "trades": n_trades,
        "win": n_win,
        "loss": n_loss,
        "wr": wr,
        "pnl": net_pnl,
        "pnl_pct": net_pnl_pct,
        "max_dd": max_dd,
        "sharpe": sharpe,
        "pf": pf,
        "runtime": elapsed,
    }
    results.append(result)

    print(f"\n  Trades    : {n_trades}  (W:{n_win} / L:{n_loss}  WR={wr:.1f}%)")
    print(f"  Net PnL   : ${net_pnl:.2f}  ({net_pnl_pct:+.2f}%)")
    print(f"  Max DD    : {max_dd:.2f}%")
    print(f"  Sharpe    : {sharpe:.3f}")
    print(f"  PF        : {pf:.2f}")
    print(f"  Runtime   : {elapsed:.1f}s")

# ── 汇总 ──
print(f"\n\n{'='*70}")
print(f"  调参汇总")
print(f"{'='*70}")
print(f"{'轮次':<25} {'Trades':>6} {'WR%':>6} {'PnL%':>8} {'MaxDD%':>8} {'Sharpe':>8} {'PF':>6}")
print(f"{'-'*70}")
for r in results:
    print(f"{r['label']:<25} {r['trades']:>6} {r['wr']:>5.1f}% {r['pnl_pct']:>+7.2f}% {r['max_dd']:>7.2f}% {r['sharpe']:>8.3f} {r['pf']:>5.2f}")
print(f"{'-'*70}")

# 推荐
best = max(results, key=lambda r: r["pnl_pct"]) if results else None
if best:
    print(f"\n  推荐: {best['label']}  (PnL={best['pnl_pct']:+.2f}%, DD={best['max_dd']:.1f}%)")
