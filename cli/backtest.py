"""
cli/backtest.py — 回测模式: backtrader 参数扫描.

Split from main.py (ARCH-1 audit 2026-06-21).
"""

import logging
import numpy as np
import pandas as pd

from backend.services.research_evidence import enforce_legacy_backtest_contract

logger = logging.getLogger("quant")


def run_backtest(args):
    """
    回测模式 — backtrader 参数扫描

    流程：
    1. 从 DataStore 加载 M15 数据为 pandas DataFrame
    2. 用 backtrader optstrategy 对 SL/TP/CD 参数组合做优化
    3. 输出：总收益、交易数、胜率、夏普比率、最大回撤 + 参数排名
    """
    import backtrader as bt
    from data.store import DataStore

    logger.info("=" * 60)
    logger.info(f"BACKTEST — {args.symbol} @ {args.timeframe}")
    provenance = enforce_legacy_backtest_contract()
    logger.warning(
        "LEGACY DIAGNOSTIC ONLY — engine=%s live_parity=%s governance_eligible=%s",
        provenance["engine"],
        provenance["live_parity"],
        provenance["governance_eligible"],
    )
    logger.info("=" * 60)

    # ── 1. 加载数据 ──
    store = DataStore("data/ctrader_data.duckdb")
    df = store.load_bars(args.symbol, args.timeframe)

    if df.empty:
        logger.error(f"No {args.timeframe} data. Run scripts/fetch_mt5_data.py first.")
        return enforce_legacy_backtest_contract(
            {
                "status": "no_data",
                "symbol": args.symbol,
                "timeframe": args.timeframe,
                "rows": [],
            }
        )

    # backtrader 需要 datetime 列
    if "time" in df.columns:
        df.set_index("time", inplace=True)
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index)

    # 取 OHLCV 列
    keep_cols = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
    df = df[keep_cols].copy()
    df["volume"] = df.get("volume", 0).fillna(0)

    n = len(df)
    logger.info(f"Loaded {n} bars, {df.index[0]} → {df.index[-1]}")

    # ── 分割 train / test ──
    split_idx = int(n * 0.7)
    df_train = df.iloc[:split_idx].copy()
    df_test  = df.iloc[split_idx:].copy()
    logger.info(
        f"Split: train={len(df_train)} bars [{df_train.index[0]} → {df_train.index[-1]}]"
        f" | test={len(df_test)} bars [{df_test.index[0]} → {df_test.index[-1]}]"
    )

    INITIAL_BALANCE = 500.0

    # ── 预构建 PandasData feed (PERF-2 audit 2026-06-21) ──
    feed_train = bt.feeds.PandasData(dataname=df_train)
    feed_test = bt.feeds.PandasData(dataname=df_test)

    param_combinations = [
        {"sl_atr": sl, "tp_atr": tp, "cooldown_bars": cd}
        for sl in [2.0, 2.5, 3.0]
        for tp in [3.0, 4.0]
        for cd in [3, 5]
    ]
    rows = []
    total_runs = len(param_combinations)

    for idx, params in enumerate(param_combinations, 1):
        sl_atr = params["sl_atr"]
        tp_atr = params["tp_atr"]
        cooldown_bars = params["cooldown_bars"]

        risk_pct = getattr(args, 'risk_per_trade_pct', None)
        if risk_pct is not None and risk_pct > 0:
            logger.info(f"[REFACTOR-4] backtest 走 Kelly 仓位: {risk_pct}% 单笔风险, "
                        f"对比 paper 路径默认 2.0%")

        # 辅助：在指定 feed 上跑回测，返回指标 dict
        def _run_one(feed):
            c = bt.Cerebro(stdstats=False)
            c.adddata(feed)
            c.broker.setcommission(commission=0.00003, leverage=1)
            c.broker.setcash(INITIAL_BALANCE)
            c.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe", riskfreerate=0.0, annualize=True)
            c.addanalyzer(bt.analyzers.DrawDown, _name="dd")
            c.addanalyzer(bt.analyzers.Returns, _name="returns")
            c.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")

            class _ScanStrategy(bt.Strategy):
                params = (
                    ("_sl_atr", sl_atr),
                    ("_tp_atr", tp_atr),
                    ("_cooldown_bars", cooldown_bars),
                    ("warmup_bars", 500),
                    ("_contract_size", 100),
                )

                di_period = 14; rsi_period = 14; stoch_period = 14
                macd_fast = 12; macd_slow = 26; macd_signal = 9
                bb_period = 20; bb_std = 2.0; atr_period = 14

                def __init__(self):
                    self._cooldown = 0
                    self._bb_widths = bt.LineBuffer()
                    self._rsi = bt.ind.RSI(self.data.close, period=self.rsi_period)
                    self._atr = bt.ind.ATR(self.data, period=self.atr_period)
                    self._pdi = bt.ind.PlusDI(self.data, period=self.di_period)
                    self._ndi = bt.ind.MinusDI(self.data, period=self.di_period)
                    self._stoch = bt.ind.Stochastic(self.data, period=self.stoch_period, period_dfast=3, period_dslow=3)
                    self._macd = bt.ind.MACD(self.data.close, period_me1=self.macd_fast, period_me2=self.macd_slow, period_signal=self.macd_signal)
                    self._bb = bt.ind.BollingerBands(self.data.close, period=self.bb_period, devfactor=self.bb_std)

                def _di_spread(self): return self._pdi[0] - self._ndi[0]
                def _stoch_k(self): return self._stoch.lines.percK[0]
                def _bb_width(self): return self._bb.lines.top[0] - self._bb.lines.bot[0]
                def _hist(self): return self._macd.lines.macd[0] - self._macd.lines.signal[0]

                def next(self):
                    p = self.params
                    if len(self) <= p.warmup_bars: return
                    if self._cooldown > 0: self._cooldown -= 1; return
                    a=self._atr[0]; r=self._rsi[0]; d=self._di_spread(); s=self._stoch_k(); bw=self._bb_width(); h=self._hist()
                    if a is None or r is None or d is None or s is None or bw is None or h is None: return
                    self._bb_widths.extend([bw])
                    if len(self._bb_widths) >= 20:
                        thresh = float(np.percentile(list(self._bb_widths), 80))
                        if bw >= thresh: return
                    vl = vs = 0
                    if d > 0: vl+=1
                    elif d < 0: vs+=1
                    if r > 50: vl+=1
                    elif r < 50: vs+=1
                    if s > 50: vl+=1
                    elif s < 50: vs+=1
                    if vl < 2 and vs < 2: return
                    direction = 1 if vl >= 2 else -1
                    if direction == 1 and h > 0: return
                    if direction == -1 and h < 0: return
                    self._cooldown = p._cooldown_bars
                    entry = self.data.close[0]
                    sl = entry - p._sl_atr*a if direction==1 else entry + p._sl_atr*a
                    tp = entry + p._tp_atr*a if direction==1 else entry - p._tp_atr*a
                    if risk_pct and risk_pct > 0:
                        sl_dist = abs(entry - sl)
                        if sl_dist > 0:
                            risk_dollars = self.broker.getvalue() * (risk_pct / 100.0)
                            size = risk_dollars / (sl_dist * p._contract_size)
                        else:
                            size = 0.01
                    else:
                        size = 0.01
                    self.buy_bracket(exectype=bt.Order.Close, size=size, stopprice=sl, limitprice=tp)

            c.addstrategy(_ScanStrategy)
            results = c.run(runonce=True)
            strat = results[0]

            ta = strat.analyzers.trades.get_analysis()
            dd = strat.analyzers.dd.get_analysis()
            sh = strat.analyzers.sharpe.get_analysis()
            final_value = strat.broker.getvalue()
            net_pnl = final_value - INITIAL_BALANCE
            total_return = net_pnl / INITIAL_BALANCE * 100
            sharpe = sh.get("sharperatio", None) or 0.0
            max_dd = dd.get("max", {}).get("drawdown", 0.0) or 0.0
            total_t = ta.get("total", {}).get("total", 0) or 0
            won_t = ta.get("won", {}).get("total", 0) or 0
            win_rate = (won_t / total_t * 100) if total_t > 0 else 0.0
            return {
                "trades": total_t, "win_rate": win_rate,
                "net_pnl": net_pnl, "total_return": total_return,
                "sharpe": sharpe, "max_drawdown": max_dd,
            }

        train_res = _run_one(feed_train)
        test_res = _run_one(feed_test)

        decay = (test_res["total_return"] / train_res["total_return"]) if train_res["total_return"] != 0 else float("-inf")

        rows.append({
            "sl_atr": sl_atr, "tp_atr": tp_atr, "cooldown_bars": cooldown_bars,
            **train_res,
            "total_return_test": test_res["total_return"],
            "trades_test": test_res["trades"],
            "decay": decay,
        })

        mark = "✓" if (decay > 0.5 or (decay >= 0 and train_res["total_return"] > 0)) else "✗"
        print(
            f"  [{idx}/{total_runs}] SL={sl_atr} TP={tp_atr} CD={cooldown_bars} | "
            f"train: ret={train_res['total_return']:+.1f}%({train_res['trades']}t) "
            f"test: ret={test_res['total_return']:+.1f}%({test_res['trades']}t) "
            f"decay={decay:.0%} {mark}"
        )

    # 排名
    rows.sort(key=lambda x: x["total_return"], reverse=True)

    print()
    print("=" * 90)
    print(f"{'Rank':<5} {'SL':>4} {'TP':>4} {'CD':>3}  "
          f"{'Trn Ret':>8} {'Trn Trd':>7}  "
          f"{'Tst Ret':>8} {'Tst Trd':>7}  "
          f"{'Decay':>7}  {'Sharpe':>7}  {'DD%':>6}")
    print("=" * 90)
    for i, r in enumerate(rows, 1):
        mark = "✓" if (r["decay"] > 0.5 or (r["decay"] >= 0 and r["total_return"] > 0)) else "✗"
        print(
            f"#{i:<4} {r['sl_atr']:>4.1f} {r['tp_atr']:>4.1f} {r['cooldown_bars']:>3}  "
            f"{r['total_return']:>+8.1f}% {r['trades']:>7}  "
            f"{r.get('total_return_test', r['total_return']):>+8.1f}% {r.get('trades_test', r['trades']):>7}  "
            f"{r['decay']:>7.0%}  {r['sharpe']:>7.3f}  {r['max_drawdown']:>6.2f}% {mark}"
        )
    print("=" * 90)

    print()
    print("--- train/test 对比汇总 ---")
    print(f"Train: {len(df_train)} bars [{df_train.index[0]} → {df_train.index[-1]}]")
    print(f"Test:  {len(df_test)} bars [{df_test.index[0]} → {df_test.index[-1]}]")
    print()
    best = rows[0] if rows else {}
    if best:
        print(f"Best: SL={best['sl_atr']} TP={best['tp_atr']} CD={best['cooldown_bars']}")
        print(f"  train ret={best['total_return']:+.2f}%({best['trades']}t) "
              f"test ret={best.get('total_return_test', best['total_return']):+.2f}%({best.get('trades_test', best['trades'])}t)")
        print(f"  decay={best['decay']:.0%}  sharpe={best['sharpe']:.3f}  dd={best['max_drawdown']:.2f}%")

    return enforce_legacy_backtest_contract(
        {
            "status": "completed",
            "symbol": args.symbol,
            "timeframe": args.timeframe,
            "rows": rows,
            "best": best,
            "total_runs": total_runs,
        }
    )
