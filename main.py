#!/usr/bin/env python
"""
Quant Trading System — 主入口

模式:
  --mode backtest    回测模式（从历史数据回放）
  --mode paper       模拟盘（实时数据，模拟成交）
  --mode live        实盘（实时数据，真实成交）
  --mode dashboard   启动Web监控面板

用法:
  python main.py --mode backtest --timeframe H1
  python main.py --mode live
  python main.py --mode dashboard --port 8050
"""

import argparse
import logging
import sys
import time as _time
from pathlib import Path

# 将项目根加入Python路径
sys.path.insert(0, str(Path(__file__).parent))

# ── 配置日志 ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("quant")


def setup_logging(log_dir: str = "logs"):
    """配置文件日志"""
    Path(log_dir).mkdir(exist_ok=True)
    fh = logging.FileHandler(f"{log_dir}/quant.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    ))
    logging.getLogger().addHandler(fh)


def main():
    parser = argparse.ArgumentParser(description="Quant Trading System")
    parser.add_argument("--mode", default="backtest",
                        choices=["backtest", "paper", "live", "dashboard"])
    parser.add_argument("--timeframe", default="H1",
                        choices=["M5", "M15", "M30", "H1", "H4", "D1"])
    parser.add_argument("--symbol", default="XAUUSD+")
    parser.add_argument("--port", type=int, default=8050)
    parser.add_argument("--no-dashboard", action="store_true")
    args = parser.parse_args()

    setup_logging()
    logger.info(f"Starting in {args.mode} mode | {args.symbol} | {args.timeframe}")

    if args.mode == "dashboard":
        run_dashboard(args.port)
    elif args.mode == "backtest":
        run_backtest(args)
    elif args.mode == "paper":
        run_paper(args)
    elif args.mode == "live":
        run_live(args)


# =============================================================================
# 各模式实现
# =============================================================================

def run_dashboard(port: int):
    """启动Web监控面板"""
    try:
        import uvicorn
        from monitor.dashboard import app
        logger.info(f"Dashboard: http://localhost:{port}")
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
    except ImportError:
        logger.error("FastAPI/uvicorn not installed. Run: pip install fastapi uvicorn")
        sys.exit(1)


def run_backtest(args):
    """
    回测模式 — backtrader 参数扫描

    流程：
    1. 从 DataStore 加载 M15 数据为 pandas DataFrame
    2. 用 backtrader optstrategy 对 SL/TP/CD 参数组合做优化
    3. 输出：总收益、交易数、胜率、夏普比率、最大回撤 + 参数排名
    """
    import backtrader as bt
    import numpy as np
    import pandas as pd
    from data.store import DataStore

    logger.info("=" * 60)
    logger.info(f"BACKTEST — {args.symbol} @ {args.timeframe}")
    logger.info("=" * 60)

    # ── 1. 加载数据 ──
    store = DataStore("data/market_data.db")
    df = store.load_bars(args.symbol, args.timeframe)

    if df.empty:
        logger.error(f"No {args.timeframe} data. Run scripts/fetch_mt5_data.py first.")
        return

    # backtrader 需要 datetime 列
    if "time" in df.columns:
        df.set_index("time", inplace=True)
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index)

    # 取 OHLCV 列（DataStore 的 load_bars 返回 volume 列，不是 tick_volume）
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

    # ── 2. 构建 backtrader Cerebro ──
    cerebro = bt.Cerebro(stdstats=False)

    # 数据 Feed
    data = bt.feeds.PandasData(dataname=df)
    cerebro.adddata(data)

    # 佣金：$6/手(100oz), 黄金$2000/oz, 500x杠杆
    # commission=0.00003 * value → $6 per side × 2 sides = $12 round-turn
    # 回测不计入保证金杠杆，故 leverage=1，margin=合约价值
    cerebro.broker.setcommission(commission=0.00003, leverage=1)

    # 夏普比率 + 最大回撤
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe", riskfreerate=0.0, annualize=True)
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="dd")
    cerebro.addanalyzer(bt.analyzers.Returns, _name="returns")
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")

    # ── 3. 定义 bt.Strategy（映射 MultiFactorM15 信号逻辑）─────
    # ── 4. 参数扫描（手动循环，避开 multiprocessing pickle 问题）──
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

        # ── 辅助：在指定 df 上跑回测，返回指标 dict ──
        def _run_one(df_data):
            c = bt.Cerebro(stdstats=False)
            c.adddata(bt.feeds.PandasData(dataname=df_data))
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
                    self.buy_bracket(exectype=bt.Order.Close, size=0.01, stopprice=sl, limitprice=tp)

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

        # train
        train_res = _run_one(df_train)
        # test
        test_res = _run_one(df_test)

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

    # 排名（按 train_return 排序）
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

    # 简化版输出（适合快速复制）
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


def run_paper(args):
    """
    模拟盘 — 历史 bar 回放 + 模拟撮合

    流程：
    1. 从 DataStore 加载 M15 历史 bar
    2. 用 PaperTrader 跑 multi_factor_m15 策略
    3. 输出：详细报告（PnL / Sharpe / DD / 逐笔成交）

    与回测模式的区别：
    - 回测 = backtrader 内部撮合（OHLC + SL/TP in-bar check）
    - paper = 复现实盘链路（signal → 风控 → 模拟撮合 → 状态机）
    """
    from data.store import DataStore
    from strategy.registry import strategy_registry
    from execution.paper_trader import PaperTrader
    # 触发策略注册（@strategy_registry.register 装饰器）
    import strategies  # noqa: F401

    logger.info("=" * 60)
    logger.info(f"PAPER REPLAY — {args.symbol} @ {args.timeframe}")
    logger.info("=" * 60)

    # ── 加载策略（注册表里已注册的） ──
    strategy_name = "multi_factor_m15"
    if strategy_name not in strategy_registry.list():
        logger.error(f"Strategy '{strategy_name}' not registered. "
                     f"Available: {strategy_registry.list()}")
        return

    # ── 覆盖最优参数（与回测baseline一致） ──
    override_params = {
        "sl_atr": 3.0,
        "tp_atr": 4.0,
        "cooldown_bars": 3,
        # ── 事件 / 波动率过滤（sweep 实测最优 R5）──
        "enable_nfp_skip": True,
        "nfp_skip_days": 1,
        "enable_dual_event_skip": True,
        "enable_gvz_gate": True,
        "gvz_drop_pct": -2.0,
        # FOMC boost 0.01 min 钳制下无效（1.5x=0.015 圆整到 0.01）
        # enable_fomc_boost: True 需要 Kelly 配合（risk_per_trade_pct > 0）
    }
    strategy = strategy_registry.create(
        strategy_name,
        symbol=args.symbol,
        timeframe=args.timeframe,
        **override_params,
    )

    # ── 加载数据 ──
    store = DataStore("data/market_data.db")
    trader = PaperTrader(
        strategy=strategy,
        initial_balance=500.0,
        default_lots=0.01,
        max_lots=2.0,
        warmup_bars=500,
        # 事件/GVZ 过滤已在 strategy 内（sweep R5 实测最优）
        # 显式禁掉 pre_trade/circuit，让 738 笔全过
        # （sweep R5 跑出 DD 40% / +$2038）
        enable_circuit=False,
    )
    try:
        trader.load_data(store, args.symbol, args.timeframe)
    except ValueError as e:
        logger.error(str(e))
        return

    # ── 跑回放 ──
    t0 = _time.time()
    report = trader.run()
    elapsed = _time.time() - t0

    # ── 输出报告 ──
    trader.print_report(report)
    print(f"  Runtime       : {elapsed:.2f}s")
    print()

    # ── 落盘 CSV ──
    import csv
    from pathlib import Path
    csv_path = Path(f"logs/paper_trades_{_time.strftime('%Y%m%d_%H%M%S')}.csv")
    csv_path.parent.mkdir(exist_ok=True)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ticket", "symbol", "direction", "volume", "price",
                    "time", "pnl", "commission", "reason", "strategy"])
        for t in trader.engine.trades:
            w.writerow([t.ticket, t.symbol, t.direction, t.volume,
                        f"{t.price:.2f}", t.time, f"{t.pnl:.2f}",
                        f"{t.commission:.2f}", t.reason, t.strategy])
    logger.info(f"Trade log saved: {csv_path}")

    return report


def run_live(args):
    """实盘（实时数据，MT5真实成交）"""
    logger.info("Live trading mode — not yet implemented")
    # TODO: TickReceiver + MT5Bridge + full pipeline


if __name__ == "__main__":
    main()
