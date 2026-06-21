"""
cli/paper.py — 模拟盘模式: 历史 bar 回放 + 模拟撮合.

Split from main.py (ARCH-1 audit 2026-06-21).
"""

import csv
import logging
import time as _time
from pathlib import Path

logger = logging.getLogger("quant")


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
    from config import load_config, cfg_get
    CFG = load_config()

    logger.info("=" * 60)
    logger.info(f"PAPER REPLAY — {args.symbol} @ {args.timeframe}")

    if args.include_shadow_factors:
        logger.info(f"  [T15.5] shadow factors ENABLED (top_k={args.shadow_top_k})")
    if args.factor_health_report:
        logger.info(f"  [T14.1] 跑因子健康评估 (落盘 factor_health_report.txt)")

    # T14.1 因子健康评估
    if args.factor_health_report:
        _run_factor_health_report(args)

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

    # 加载数据
    store = DataStore("data/ctrader_data.duckdb")

    # 事件感知仓位 (默认开启)
    event_sizing = None
    if args.use_event_sizing and not args.no_event_sizing:
        from execution.event_sizing import EventSizing
        event_sizing = EventSizing(
            db_path=cfg_get(CFG, "event_sizing", "db_path", default="data/events.duckdb"),
            enabled=True,
        )
        logger.info(f"  [EventSizing] 启用: {event_sizing.stats()}")

    # 路径 A: --use-router MAB 多策略
    if args.use_router:
        _run_paper_with_router(args, store, event_sizing)
        return

    # 路径 B: 单一策略 baseline (default)
    strategy_name = "multi_factor_m15"
    if strategy_name not in strategy_registry.list():
        logger.error(f"Strategy '{strategy_name}' not registered. "
                     f"Available: {strategy_registry.list()}")
        return

    override_params = {
        "sl_atr": 3.0,
        "tp_atr": 4.0,
        "cooldown_bars": 3,
        "enable_nfp_skip": True,
        "nfp_skip_days": 1,
        "enable_dual_event_skip": True,
        "enable_gvz_gate": True,
        "gvz_drop_pct": -2.0,
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
        max_lots=0.1,
        warmup_bars=500,
        risk_per_trade_pct=1.0,
        max_daily_loss_pct=15.0,
        enable_circuit=args.enable_circuit,
        event_sizing=event_sizing,
    )
    try:
        trader.load_data(store, args.symbol, args.timeframe)
    except ValueError as e:
        logger.error(str(e))
        return

    t0 = _time.time()
    report = trader.run()
    elapsed = _time.time() - t0

    trader.print_report(report)
    print(f"  Runtime       : {elapsed:.2f}s")
    print()

    # 落盘 CSV
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


def _run_factor_health_report(args):
    """T14.1: 跑 paper 前的因子健康评估."""
    import json as _json
    from alpha.ic_tracker import ICTracker
    from alpha.factor_engine import FactorEngine  # batch-only, offline analysis
    from alpha.factor_health import FactorHealth
    from data.store import DataStore

    store = DataStore("data/ctrader_data.duckdb")
    df = store.load_bars(args.symbol, args.timeframe)
    if df.empty:
        logger.warning(f"[T14.1] 无 {args.timeframe} 数据, 跳过健康评估")
        return

    from data.external_loader import ExternalDataLoader
    ext = ExternalDataLoader("data/ctrader_data.duckdb")
    ext_df = ext.align_to_bars(df)
    df = df.join(ext_df, how="left")
    logger.info(f"[T14.1] 加载 {len(df)} {args.timeframe} bar (含跨资产列, {len(ext_df.columns)} ext cols)")

    engine = FactorEngine(df)
    factor_data = engine.compute_all()
    logger.info(f"[T14.1] 算 {len(factor_data)} 因子值")

    forward_returns = df["close"].pct_change().shift(-1).fillna(0).values
    ic_tracker = ICTracker(window=min(5000, len(df)))
    for name, vals in factor_data.items():
        ic_tracker.update(name, vals, forward_returns)

    health = FactorHealth(ic_tracker, active_factor_names=[])
    report2 = health.report()

    active_now = health.get_active_factors(min_score=70)
    health2 = FactorHealth(ic_tracker, active_factor_names=active_now)
    report2 = health2.report()

    out_dir = Path("data/charts")
    out_dir.mkdir(parents=True, exist_ok=True)
    txt_path = out_dir / "factor_health_report.txt"
    txt_path.write_text(report2, encoding="utf-8")
    json_path = out_dir / "factor_health_report.json"
    json_path.write_text(_json.dumps(health2.report_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"[T14.1] 报告落盘: {txt_path}, {json_path}")
    logger.info(f"[T14.1] ACTIVE 因子: {active_now}")
    logger.info("\n" + report2)


def _run_paper_with_router(args, store, event_sizing):
    """路径 A: MABRouter 多策略 paper."""
    from strategy.mab_router import MABRouter
    from execution.mab_paper_runner import MABPaperRunner
    from strategy.registry import strategy_registry

    strats = {}
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
            continue
        use_full = n in ("multi_factor_m15",)
        ov = overrides_full if use_full else overrides_partial
        strats[n] = strategy_registry.create(n, symbol=args.symbol, timeframe=args.timeframe, **ov)
        strats[n].on_init()

    if not strats:
        logger.error("No valid strategies to run")
        return

    router = MABRouter(list(strats.keys()), seed=args.router_seed, warmup_trades=50)
    runner = MABPaperRunner(
        strategies=strats, router=router,
        initial_balance=500.0, default_lots=0.01, max_lots=0.1,
        risk_per_trade_pct=1.0,
        max_daily_loss_pct=15.0,
        enable_circuit=args.enable_circuit,
        event_sizing=event_sizing,
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
