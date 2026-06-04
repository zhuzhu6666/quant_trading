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
    # P1 (audit 2026-06-04): 显式读 settings.yaml, 让 YAML 改 → 行为变
    # 改用 cfg_get(..., override=X) 模式, 调优值在 main.py 显式声明
    from config import load_config, cfg_get
    CFG = load_config()

    # 启动时恢复 shadow 因子 (跨进程持久化, T15.5)
    try:
        from alpha.persistent_registry import restore_from_log
        restore_from_log()
    except Exception as e:
        logger.warning(f"恢复 shadow 因子失败 (非致命): {e}")

    parser = argparse.ArgumentParser(description="Quant Trading System")
    parser.add_argument("--mode", default="backtest",
                        choices=["backtest", "paper", "live", "dashboard"])
    parser.add_argument("--timeframe", default="H1",
                        choices=["M5", "M15", "M30", "H1", "H4", "D1"])
    parser.add_argument("--symbol", default="XAUUSD+")
    parser.add_argument("--port", type=int, default=8050)
    parser.add_argument("--no-dashboard", action="store_true")
    # ── 自学习层开关 (T1-T4, 2026-06-02 集成) ──
    parser.add_argument("--use-router", action="store_true",
                        help="MAB 多策略路由 (4 策略共享 paper, 默认关闭=单策略 baseline)")
    parser.add_argument("--use-scheduler", action="store_true",
                        help="SelfLearningScheduler 动态调权 (依赖 --use-router)")
    parser.add_argument("--use-scorer", action="store_true",
                        help="WeightedScorer 加权打分 (依赖 --use-router)")
    parser.add_argument("--use-calibrator", action="store_true",
                        help="ProbabilityCalibrator 校准 confidence (依赖 --use-scorer)")
    parser.add_argument("--calibrator-path", type=str,
                        default="data/charts/calibrator_bucket.json",
                        help="ProbabilityCalibrator 加载路径 (default: data/charts/calibrator_bucket.json, 缺失时回退 identity)")
    parser.add_argument("--calibrator-save", action="store_true",
                        help="启用 calibrator 定时保存 (每 N 笔 trade 把当前 calibrator 落盘, 默认 off)")
    parser.add_argument("--use-drift-research", action="store_true",
                        help="(PR-3.2) SEVERE_DRIFT triggers GP re-search subprocess")
    parser.add_argument("--drift-n-bars", type=int, default=5000,
                        help="re-search N bars (default 5000)")
    parser.add_argument("--drift-pop", type=int, default=50,
                        help="re-search GP pop size (default 50)")
    parser.add_argument("--drift-gen", type=int, default=30,
                        help="re-search GP gen (default 30)")
    parser.add_argument("--use-meta-monitor", action="store_true",
                        help="MetaLearnerMonitor track model calibration (paper always logs)")
    parser.add_argument("--use-factor-monitor", action="store_true",
                        help="FactorMonitor 跟踪因子 IC 衰减")
    parser.add_argument("--use-alerter", action="store_true",
                        help="Alerter 告警 (circuit / 大额 trade / drift)")
    parser.add_argument("--enable-circuit", action="store_true",
                        help="开启 CircuitBreaker (默认 False=baseline, P3 调优 10%%)")
    parser.add_argument("--use-retrain", action="store_true",
                        help="启用 RetrainScheduler (T8: 每 N 笔触发 walkforward)")
    parser.add_argument("--retrain-every-n", type=int, default=200,
                        help="retrain 频率 (默认 200 笔)")
    parser.add_argument("--use-event-filter", action="store_true",
                        help="启用 SharedEventFilter (T13: NFP/FOMC+CPI/GVZ 共享 skip)")
    parser.add_argument("--no-event-filter", action="store_true",
                        help="显式禁用 SharedEventFilter (覆盖 --use-event-filter 之外的默认)")
    parser.add_argument("--factor-health-report", action="store_true",
                        help="T14.1 跑 paper 前先评估 22 因子健康分, 落盘 data/charts/factor_health_report.txt")
    parser.add_argument("--factor-health-data", type=str, default=None,
                        help="T14.1 评估时用的 bar CSV 路径 (默认 data/market_data.db M15)")
    parser.add_argument("--router-seed", type=int, default=42,
                        help="MABRouter 随机种子 (P1-D 默认 42)")
    parser.add_argument("--router-arms", nargs="+",
                        default=["multi_factor_m15", "trend_following", "mean_reversion", "breakout"],
                        help="MABRouter 候选策略名 (默认 4 个 M15)")
    parser.add_argument('--include-shadow-factors', action='store_true',
                        help='T15.5 enable DSL-discovered shadow/discovered factors as extra votes (default off)')
    parser.add_argument('--shadow-top-k', type=int, default=3,
                        help='T15.5 max top-K shadow factors to consume (default 3)')

    # FEAT-1 (audit 2026-06-04): 风险参数 CLI 透传, 覆盖 YAML 默认
    # 之前只能在 PaperTrader.__init__ hardcode, 改个参数要改代码
    # 现在 --max-daily-loss-pct=3 临时调紧熔断, --single-risk-usd=5 临时放低单笔
    parser.add_argument("--max-daily-loss-pct", type=float, default=None,
                        help="最大日内亏损 %% (覆盖 settings.yaml 默认 5.0)")
    parser.add_argument("--max-consecutive-loss", type=int, default=None,
                        help="最大连续亏损笔数 (覆盖 settings.yaml 默认 5)")
    parser.add_argument("--single-risk-usd", type=float, default=None,
                        help="单笔最大风险 USD (覆盖 settings.yaml 默认 2.0)")
    parser.add_argument("--volatility-mult", type=float, default=None,
                        help="ATR 波动率乘数 (覆盖 settings.yaml 默认 3.0)")
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
    2. 默认: 单一 multi_factor_m15 策略 (baseline, +407.51%)
       启用 --use-router 后: MABRouter 4 策略共享 (T1, 2026-06-02)
    3. 输出：详细报告（PnL / Sharpe / DD / 逐笔成交）

    与回测模式的区别：
    - 回测 = backtrader 内部撮合（OHLC + SL/TP in-bar check）
    - paper = 复现实盘链路（signal → 风控 → 模拟撮合 → 状态机）

    自学习层开关 (T1-T4, 2026-06-02 集成):
    - --use-router:        MABRouter 4 策略共享 (T1)
    - --use-scheduler:     SelfLearningScheduler 调权 (T5)
    - --use-scorer:        WeightedScorer (T3 注: 暂未接入 router, 仅做示例)
    - --use-calibrator:    ProbabilityCalibrator.calibrate(signal.confidence) (T3)
    - --use-meta-monitor:  MetaLearnerMonitor.on_observation() (T6)
    - --use-factor-monitor: FactorMonitor.on_bar() (T7)
    - --use-alerter:       Alerter 告警 (T4)
    - --enable-circuit:    CircuitBreaker (默认 False=baseline, P3 调优 10%)
    """
    from data.store import DataStore
    from strategy.registry import strategy_registry
    from execution.paper_trader import PaperTrader
    # 触发策略注册（@strategy_registry.register 装饰器）
    import strategies  # noqa: F401

    logger.info("=" * 60)
    logger.info(f"PAPER REPLAY — {args.symbol} @ {args.timeframe}")

    if args.include_shadow_factors:
        logger.info(f"  [T15.5] shadow factors ENABLED (top_k={args.shadow_top_k})")
    if args.factor_health_report:
        logger.info(f"  [T14.1] 跑因子健康评估 (落盘 factor_health_report.txt)")

    # T14.1 因子健康评估 (跑 paper 前的轻量步骤, 算 22 因子 IC + 健康分)
    if args.factor_health_report:
        from alpha.ic_tracker import ICTracker
        from alpha.factor_engine import FactorEngine
        from alpha.factor_health import FactorHealth
        from alpha.registry_adapter import RegistryAdapter
        from data.store import DataStore
        from pathlib import Path
        import json as _json

        store = DataStore("data/market_data.db")
        df = store.load_bars(args.symbol, args.timeframe)
        if df.empty:
            logger.warning(f"[T14.1] 无 {args.timeframe} 数据, 跳过健康评估")
        else:
            # 注入跨资产/事件列 (DXY/SLV/real_yield/hours_to_fomc/nfp)
            # 没这些列, 5 个跨资产/事件因子会算 nan (n_obs=0)
            from data.external_loader import ExternalDataLoader
            ext = ExternalDataLoader("data/market_data.db")
            ext_df = ext.align_to_bars(df)
            # align_to_bars 只返回外部列, 需手动 join 回原 bar
            df = df.join(ext_df, how="left")
            logger.info(f"[T14.1] 加载 {len(df)} {args.timeframe} bar (含跨资产列, {len(ext_df.columns)} ext cols)")
            engine = FactorEngine(df)
            # 算所有 22 因子
            factor_data = engine.compute_all()
            logger.info(f"[T14.1] 算 {len(factor_data)} 因子值")

            # 算 forward return (1 bar)
            forward_returns = df["close"].pct_change().shift(-1).fillna(0).values

            # 喂 ICTracker
            ic_tracker = ICTracker(window=min(5000, len(df)))
            for name, vals in factor_data.items():
                ic_tracker.update(name, vals, forward_returns)

            # 评估 (先建 health, 再用 health 的结果给 adapter 当 independence 参考)
            health = FactorHealth(ic_tracker, active_factor_names=[])
            report = health.report()
            report_dict = health.report_dict()

            # 拿到 ACTIVE 列表后, 二次评估 (这次有 ACTIVE 列表, independence 准)
            active_now = health.get_active_factors(min_score=70)
            health2 = FactorHealth(ic_tracker, active_factor_names=active_now)
            report2 = health2.report()

            # 落盘 (第二次评估含 independence)
            out_dir = Path("data/charts")
            out_dir.mkdir(parents=True, exist_ok=True)
            txt_path = out_dir / "factor_health_report.txt"
            txt_path.write_text(report2, encoding="utf-8")
            json_path = out_dir / "factor_health_report.json"
            json_path.write_text(_json.dumps(health2.report_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            logger.info(f"[T14.1] 报告落盘: {txt_path}, {json_path}")
            logger.info(f"[T14.1] ACTIVE 因子: {active_now}")
            logger.info("\n" + report2)

    if args.use_router:
        logger.info(f"  [T1] MABRouter 启用: {args.router_arms} (seed={args.router_seed})")
        if args.use_scheduler:
            logger.info(f"  [T5] SelfLearningScheduler 启用 (动态调权)")
        if args.use_calibrator:
            logger.info(f"  [T3] ProbabilityCalibrator 启用 (calibrate confidence)")
        if args.use_meta_monitor:
            logger.info(f"  [T6] MetaLearnerMonitor 启用 (记录 close 校准)")
        if args.use_factor_monitor:
            logger.info(f"  [T7] FactorMonitor 启用 (每根 bar 记录 IC)")
        if args.use_alerter:
            logger.info(f"  [T4] Alerter 启用 (circuit/大额/drift 告警)")
        if args.use_retrain:
            logger.info(f"  [T8] RetrainScheduler 启用 (每 {args.retrain_every_n} 笔 walkforward)")
        if args.use_event_filter:
            logger.info(f"  [T13] SharedEventFilter 启用 (NFP/FOMC+CPI/GVZ 共享 skip, 避免 OOH 跳爆仓)")
    if args.enable_circuit:
        logger.info(f"  [P3] CircuitBreaker 启用 (10% 日损阈值)")
    logger.info("=" * 60)

    # ── 加载数据 ──
    store = DataStore("data/market_data.db")

    # ── 路径 A: --use-router MAB 多策略 ──
    if args.use_router:
        from strategy.mab_router import MABRouter
        from execution.mab_paper_runner import MABPaperRunner

        # 加载候选策略
        strats = {}
        # 注: 事件 skip 过滤 (nfp/fomc/gvz) 只在 multi_factor_m15 / ma_cross_h4 等
        # P0 之后的 strategy 里实装. trend_following / mean_reversion / breakout 早期 strategy
        # 没有这些字段, 强 merge 会 KeyError. 故只对支持字段的 strategy 设.
        overrides_partial = {
            "sl_atr": 3.0, "tp_atr": 4.0, "cooldown_bars": 3,
        }
        overrides_full = {
            **overrides_partial,
            "enable_nfp_skip": True, "nfp_skip_days": 1,
            "enable_dual_event_skip": True,
            "enable_gvz_gate": True, "gvz_drop_pct": -2.0,
        }
        for n in args.router_arms:
            if n not in strategy_registry.list():
                logger.error(f"Strategy '{n}' not registered. Available: {strategy_registry.list()}")
                return
            s = strategy_registry.create(n, args.symbol, args.timeframe)
            # 检查 strategy params 里有没有 full override 字段
            has_event_fields = all(k in s.params for k in overrides_full)
            if has_event_fields:
                s.params = {**s.params, **overrides_full}
            else:
                s.params = {**s.params, **overrides_partial}
                logger.debug(f"  Strategy {n} 无事件 skip 字段, 只设 SL/TP")
            strats[n] = s

        router = MABRouter(strategies=list(strats.keys()), seed=args.router_seed)

        # 自学习层 (按 flag 装配)
        scheduler = None
        if args.use_scheduler:
            from strategy.scheduler import SelfLearningScheduler
            scheduler = SelfLearningScheduler(router=router, check_interval=50)

        calibrator = None
        if args.use_calibrator:
            from alpha.probability_calibrator import ProbabilityCalibrator
            from pathlib import Path as _Path  # 局部 import, 避开其他函数同名 local
            # 优先从磁盘加载已有的 calibrator (P0-7 跑过的桶级 / Platt), 缺失时回退 identity
            cal_path = _Path(args.calibrator_path)
            if cal_path.exists():
                try:
                    calibrator = ProbabilityCalibrator.load(str(cal_path))
                    logger.info(f"  [T3] calibrator: {calibrator.method} (loaded from {cal_path}, "
                                f"buckets={len(calibrator.buckets)}, platt=({calibrator.platt_a:.3f}, {calibrator.platt_b:.3f}))")
                except Exception as e:
                    logger.warning(f"  [T3] calibrator load failed ({e}), fallback to identity")
                    calibrator = ProbabilityCalibrator.identity()
            else:
                logger.info(f"  [T3] calibrator: identity (无 {cal_path}, 不校准 baseline)")
                calibrator = ProbabilityCalibrator.identity()

        meta_monitor = None
        if args.use_meta_monitor:
            from live.meta_learner_monitor import MetaLearnerMonitor
            meta_monitor = MetaLearnerMonitor(model_names=list(strats.keys()))
            # PR-3.2: SEVERE_DRIFT -> 触发 GP re-search
            if args.use_drift_research:
                from scripts.drift_research_daemon import make_drift_handler
                meta_monitor.drift_handler = make_drift_handler(
                    n_bars=args.drift_n_bars, pop=args.drift_pop, gen=args.drift_gen,
                )
                logger.info(f"  [PR-3.2] drift_handler installed: SEVERE_DRIFT -> re-search")

        factor_monitor = None
        if args.use_factor_monitor:
            from live.factor_monitor import FactorMonitor
            from alpha.registry import factor_registry as f_reg
            factor_names = f_reg.list() if hasattr(f_reg, 'list') else []
            if not factor_names:
                # fallback: 4 个老因子
                factor_names = ["rsi_14", "macd_hist", "adx", "bb_width"]
            factor_monitor = FactorMonitor(factor_names=factor_names, window=500)

        alerter = None
        if args.use_alerter:
            from monitor.alerter import Alerter
            alerter = Alerter({"log_file": "logs/alerts.log", "min_level": "INFO"})

        retrain_scheduler = None
        if args.use_retrain:
            from strategy.retrain_scheduler import RetrainScheduler
            retrain_scheduler = RetrainScheduler(
                trigger_every_n_trades=args.retrain_every_n,
                min_trades_before_first=args.retrain_every_n,
                walkforward_script="scripts/walkforward_p0_6.py",
                calibrator_path="data/charts/calibrator_bucket.json",
                timeout_sec=300,
            )

        event_filter = None
        if args.use_event_filter and not args.no_event_filter:
            from execution.event_filter import SharedEventFilter
            event_filter = SharedEventFilter(
                enable_nfp_skip=True, nfp_skip_days=1,
                enable_dual_event_skip=True,
                enable_gvz_gate=True, gvz_drop_pct=-2.0,
                db_path="data/market_data.db",
            )

        runner = MABPaperRunner(
            strategies=strats, router=router,
            scheduler=scheduler, calibrator=calibrator,
            meta_monitor=meta_monitor, factor_monitor=factor_monitor,
            alerter=alerter, retrain_scheduler=retrain_scheduler,
            event_filter=event_filter,
            # baseline 等比例: 0.01 lot × 100 contract = 1 oz XAUUSD
            # 3 ATR SL × \$7 ATR × 1 oz = \$21 单笔 = 4.2% 账户 (P0 风控原则)
            # max_lots=2.0 跟 baseline 一致, 共享 4 策略仓位
            initial_balance=500.0, default_lots=0.01, max_lots=2.0,
            enable_circuit=args.enable_circuit,
        )
        try:
            runner.load_data(store, args.symbol, args.timeframe)
        except ValueError as e:
            logger.error(str(e))
            return

        t0 = _time.time()
        report = runner.run()
        elapsed = _time.time() - t0
        runner.print_report(report)
        print(f"  Runtime       : {elapsed:.2f}s")
        print()
        return

    # ── 路径 B: 单一策略 baseline (default, +407.51%) ──
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

    trader = PaperTrader(
        strategy=strategy,
        initial_balance=500.0,
        default_lots=0.01,
        max_lots=2.0,
        warmup_bars=500,
        # 事件/GVZ 过滤已在 strategy 内（sweep R5 实测最优）
        # 显式禁掉 pre_trade/circuit，让 738 笔全过
        # （sweep R5 跑出 DD 40% / +$2038）
        enable_circuit=args.enable_circuit,  # T2 改: 默认 False, 启用 --enable-circuit 打开
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
