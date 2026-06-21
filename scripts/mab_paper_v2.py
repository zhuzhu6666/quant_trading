"""scripts/mab_paper_v2.py — MAB router 驱动的多策略 paper 跑回放 (v2, 现行)

设计:
  1. 加载 50K M15 bar
  2. 维护所有候选策略实例 (only M15-compatible: multi_factor_m15, trend_following, mean_reversion, breakout)
     (ma_cross_h4 是 H4 跳过, gold_momentum / macd_bb 待 baseline 测了再加)
  3. 维护 1 个 PaperEngine 共享 (单仓)
  4. 每根 bar:
     a. 算当前 regime (classify_regime)
     b. router.select(regime) → chosen
     c. 只调 chosen.on_bar(bar) → signal
     d. engine.on_bar(bar, signal) → 处理开/平
     e. 如果本 bar 触发了 close, router.update(chosen, regime, win=...)
  5. 跑完输出:
     - MAB portfolio ret/WR/DD/PF/trades
     - 每策略被选次数 + 实际胜场
     - per-regime 策略偏好

依赖: PaperEngine (单仓), MABRouter, classify_regime
"""
import logging
import sys
import time as _time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy.registry import strategy_registry
from strategy.mab_router import MABRouter, classify_regime, REGIMES
from data.store import DataStore
from execution.paper_engine import PaperExecutionEngine
from db.store import DecisionLogStore
from strategy.scheduler import SelfLearningScheduler
from strategy.scorer import WeightedScorer
from execution.paper_trader import PaperReport  # 只复用 dataclass

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("mab_paper")
logger.setLevel(logging.WARNING)


# ── 仅跑 M15 兼容的策略 ──
M15_STRATEGIES = ["multi_factor_m15", "trend_following", "mean_reversion", "breakout"]


def main():
    print("=" * 78)
    print("  MAB-driven Paper — XAUUSD+ M15, 50K bar")
    print(f"  候选策略: {M15_STRATEGIES}")
    print("=" * 78)
    print()

    # 1. 加载数据
    store = DataStore("data/ctrader_data.duckdb")
    df = store.load_bars("XAUUSD+", "M15")
    assert not df.empty, "No M15 data"
    bars = []
    for idx, row in df.iterrows():
        bars.append({
            "time": idx.timestamp() if hasattr(idx, "timestamp") else float(idx),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row.get("volume", 0)),
            "complete": True,
        })
    closes = np.array([b["close"] for b in bars])
    highs = np.array([b["high"] for b in bars])
    lows = np.array([b["low"] for b in bars])
    n = len(bars)
    print(f"Loaded {n} bars, {datetime.fromtimestamp(bars[0]['time'], tz=timezone.utc)} → "
          f"{datetime.fromtimestamp(bars[-1]['time'], tz=timezone.utc)}")

    # ── 1.5 batch 算 regime 标签 (避免主循环 O(n²)) ──
    # EMA 来自 trend_following (公开 staticmethod), ATR/ADX 来自 scripts.equity_by_regime
    import importlib
    er = importlib.import_module("scripts.equity_by_regime")
    tf_mod = importlib.import_module("strategies.trend_following")
    print("预计算 regime 标签 (batch)...")
    t_batch = _time.time()
    ema50_full = tf_mod.TrendFollowingStrategy._vector_ema(closes, 50)
    ema200_full = tf_mod.TrendFollowingStrategy._vector_ema(closes, 200)
    atr_full = er._vector_atr(highs, lows, closes, 14)  # noqa: SLF001
    print(f"  done [{_time.time()-t_batch:.1f}s]")

    # 2. 策略实例 + MAB router (冷启动: 用单策略 baseline 的胜率)
    # 4 策略 baseline (50K paper, 仅这 4 个, M15):
    #   multi_factor_m15: (376, 362) WR=50.9%
    #   trend_following:  (18, 30)   WR=37.5%
    #   mean_reversion:   (457, 786) WR=36.8%
    #   breakout:         (530, 792) WR=40.1%
    baseline = {
        "multi_factor_m15": (376, 362),
        "trend_following":  (18, 30),
        "mean_reversion":   (457, 786),
        "breakout":         (530, 792),
    }
    strategy_objs = {
        name: strategy_registry.create(name, symbol="XAUUSD+", timeframe="M15",
                                        sl_atr=3.0, tp_atr=4.0, cooldown_bars=3,
                                        enable_nfp_skip=True, nfp_skip_days=1,
                                        enable_dual_event_skip=True,
                                        enable_gvz_gate=True, gvz_drop_pct=-2.0)
        for name in M15_STRATEGIES
    }
    for s in strategy_objs.values():
        s.on_init()
    router = MABRouter(M15_STRATEGIES, baseline=baseline)
    print(f"MAB router 冷启动: {baseline}")
    print()

    # 3. 共享 PaperEngine (单仓)
    def _atr_source(bar):
        atr = None
        for s in strategy_objs.values():
            if s.last_atr is not None and s.last_atr > 0:
                atr = float(s.last_atr)
                break
        return atr

    engine = PaperExecutionEngine(
        initial_balance=500.0,
        default_lots=0.01,
        max_position_lots=2.0,
        risk_per_trade_pct=0.0,
        pre_trade=None,
        circuit_breaker=None,
        atr_source=_atr_source,
    )

    # 4. 主循环
    warmup = 500
    last_close_trade_count = 0
    trade_records = []   # (idx, regime, chosen, win)
    equity_curve = []
    closes_seq = []      # 用于 classify_regime

    t0 = _time.time()
    # batch 算 per-bar regime (5 类字符串) — 比每根 bar 调 classify_regime 快 1000x
    batch_regimes = np.array(
        [classify_regime(closes[max(0, i-200):i+1], ema50=ema50_full[max(0, i-200):i+1],
                         ema200=ema200_full[max(0, i-200):i+1], atr=atr_full[max(0, i-200):i+1])
         for i in range(n)],
        dtype=object,
    )
    print(f"batch regime 算完 [{_time.time()-t0:.1f}s]")

    t0 = _time.time()
    last_trade_total = 0  # engine.trades 累计长度
    last_close_count = 0  # 已 close 的 trade 计数
    for i, bar in enumerate(bars):
        # 转 dict + time 字段 (P1-E bug 修复)
        if isinstance(bar, pd.Series):
            bar_dict = bar.to_dict()
            bar_dict["time"] = bar.name if hasattr(bar, "name") else 0
            if isinstance(bar_dict["time"], pd.Timestamp):
                bar_dict["time"] = int(bar_dict["time"].timestamp())
        else:
            bar_dict = dict(bar) if not isinstance(bar, dict) else bar

        # a. 直接用预计算的 regime (O(1))
        if i < warmup or i < 200:
            engine.on_bar(bar_dict, None)
            equity_curve.append((bar_dict["time"], engine.equity))
            continue

        regime = batch_regimes[i]

        # b. 选策略
        chosen = router.select(regime)

        signal = None
        if chosen is not None:
            signal = strategy_objs[chosen].on_bar(bar_dict)

        # 撮合
        engine.on_bar(bar_dict, signal)

        # 增量检测新 close 事件 (只遍历 engine.trades 新追加部分)
        cur_trade_total = len(engine.trades)
        if cur_trade_total > last_trade_total:
            for t in engine.trades[last_trade_total:]:
                if t.direction in (2, -2):
                    win = t.pnl > 0
                    if chosen is not None:
                        router.update(chosen, regime, win)
                    trade_records.append({
                        "idx": i, "time": t.time, "regime": regime, "chosen": chosen,
                        "pnl": t.pnl, "win": win, "reason": t.reason,
                    })
                    last_close_count += 1
            last_trade_total = cur_trade_total

        equity_curve.append((bar["time"], engine.equity))

    elapsed = _time.time() - t0

    # 5. 报告
    final_bal = engine.balance
    initial = engine.initial_balance
    net_pnl = final_bal - initial
    ret = net_pnl / initial * 100
    closes_list = [t for t in engine.trades if t.direction in (2, -2)]
    wins = sum(1 for t in closes_list if t.pnl > 0)
    losses = len(closes_list) - wins
    wr = wins / len(closes_list) * 100 if closes_list else 0.0
    gp = sum(t.pnl for t in closes_list if t.pnl > 0)
    gl = abs(sum(t.pnl for t in closes_list if t.pnl <= 0))
    pf = gp / gl if gl > 1e-9 else float("inf")
    # DD
    eq = np.array([e for _, e in equity_curve])
    peak = np.maximum.accumulate(eq)
    dd = float(((peak - eq) / peak).max() * 100) if len(eq) > 0 else 0.0
    # Sharpe
    rets = np.diff(eq) / eq[:-1]
    rets = rets[np.isfinite(rets)]
    bars_per_year = 252 * 24 * 4
    sharpe = float(rets.mean() / rets.std() * np.sqrt(bars_per_year)) if rets.std() > 1e-12 else 0.0

    print()
    print("=" * 78)
    print(f"  MAB-driven Paper Report ({elapsed:.1f}s)")
    print("=" * 78)
    print(f"  Period       : {datetime.fromtimestamp(bars[0]['time'], tz=timezone.utc)} → "
          f"{datetime.fromtimestamp(bars[-1]['time'], tz=timezone.utc)}  ({n} bars)")
    print(f"  Initial      : ${initial:.2f}")
    print(f"  Final        : ${final_bal:.2f}")
    print(f"  Net PnL      : ${net_pnl:+.2f}  ({ret:+.2f}%)")
    print(f"  Trades       : {len(closes_list)}  (W:{wins} / L:{losses}  WR={wr:.1f}%)")
    print(f"  Avg W/L      : ${gp/max(wins,1):+.2f} / ${-gl/max(losses,1):.2f}  (PF={pf:.2f})")
    print(f"  Max Drawdown : {dd:.2f}%")
    print(f"  Sharpe (ann.): {sharpe:.3f}")

    # per-strategy 统计
    print()
    print("  策略被选次数 + 实际胜场:")
    chosen_count = {s: 0 for s in M15_STRATEGIES}
    chosen_wins = {s: 0 for s in M15_STRATEGIES}
    for r in trade_records:
        if r["chosen"] is not None:
            chosen_count[r["chosen"]] += 1
            if r["win"]:
                chosen_wins[r["chosen"]] += 1
    for s in M15_STRATEGIES:
        c = chosen_count[s]
        w = chosen_wins[s]
        wr_s = w / c * 100 if c > 0 else 0.0
        print(f"    {s:<20s}  selected={c:5d}  wins={w:4d}  WR={wr_s:5.1f}%")

    # per-regime 偏好
    print()
    print("  Per-Regime 策略偏好 (router 后验 E[WR]):")
    df_stats = router.strategy_summary()
    print(df_stats.to_string(index=False))

    # 对比基线
    print()
    print("  对比单策略 baseline (M15 only):")
    baselines = [
        ("multi_factor_m15", "+407.51%", "50.9%", "39.77", "1.29", 738),
        ("trend_following",   "-22.76%", "37.5%", "35.69", "0.68",  48),
        ("mean_reversion",    "-714.69%", "36.8%", "695.98", "0.71", 1243),
        ("breakout",          "-199.48%", "40.1%", "231.58", "0.93", 1322),
    ]
    print(f"    {'Strategy':<20s}  {'ret%':>9}  {'WR%':>6}  {'DD%':>7}  {'PF':>5}  {'trades':>7}")
    for name, r, w, d, p, t in baselines:
        print(f"    {name:<20s}  {r:>9}  {w:>6}  {d:>7}  {p:>5}  {t:>7}")
    print(f"    {'MAB-portfolio':<20s}  {ret:>+8.2f}%  {wr:>5.1f}%  {dd:>7.2f}  {pf:>4.2f}  {len(closes_list):>7d}")

    # 落盘
    out_path = Path("data/charts/mab_paper_report.txt")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("MAB-driven Paper — XAUUSD+ M15, 50K bar\n\n")
        f.write(f"Final: ${final_bal:.2f}  ret={ret:+.2f}%  WR={wr:.1f}%  DD={dd:.2f}%  PF={pf:.2f}  "
                f"trades={len(closes_list)}  sharpe={sharpe:.3f}\n\n")
        f.write("Per-Strategy selected/wins:\n")
        for s in M15_STRATEGIES:
            c = chosen_count[s]
            w = chosen_wins[s]
            f.write(f"  {s:<20s}  selected={c:5d}  wins={w:4d}  WR={w/c*100 if c else 0:5.1f}%\n")
        f.write(f"\nPer-Strategy router 后验:\n{df_stats.to_string(index=False)}\n")
    print()
    print(f"  → {out_path}")
    print("=" * 78)


if __name__ == "__main__":
    main()
