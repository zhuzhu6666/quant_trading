#!/usr/bin/env python
"""
Quant Trading System — 主入口

模式:
  --mode backtest    回测模式（从历史数据回放）
  --mode paper       模拟盘（实时数据，模拟成交）
  --mode live        实盘（实时数据，真实成交）

用法:
  python main.py --mode backtest --timeframe H1
  python main.py --mode live
"""

import argparse
import logging
import sys
from pathlib import Path

# 将项目根加入Python路径
sys.path.insert(0, str(Path(__file__).parent))

# 配置日志
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
                        choices=["backtest", "paper", "live"])
    parser.add_argument("--timeframe", default="H1",
                        choices=["M5", "M15", "M30", "H1", "H4", "D1"])
    parser.add_argument("--symbol", default="XAUUSD+")

    # 自学习层开关
    parser.add_argument("--use-router", action="store_true")
    parser.add_argument("--use-scheduler", action="store_true")
    parser.add_argument("--use-scorer", action="store_true")
    parser.add_argument("--use-calibrator", action="store_true")
    parser.add_argument("--calibrator-path", type=str,
                        default="data/charts/calibrator_bucket.json")
    parser.add_argument("--calibrator-save", action="store_true")
    parser.add_argument("--use-drift-research", action="store_true")
    parser.add_argument("--drift-n-bars", type=int, default=5000)
    parser.add_argument("--drift-pop", type=int, default=50)
    parser.add_argument("--drift-gen", type=int, default=30)
    parser.add_argument("--use-meta-monitor", action="store_true")
    parser.add_argument("--use-factor-monitor", action="store_true")
    parser.add_argument("--use-alerter", action="store_true")
    parser.add_argument("--enable-circuit", action="store_true")
    parser.add_argument("--risk-per-trade-pct", type=float, default=None)
    parser.add_argument("--use-retrain", action="store_true")
    parser.add_argument("--retrain-every-n", type=int, default=200)
    parser.add_argument("--use-event-filter", action="store_true")
    parser.add_argument("--no-event-filter", action="store_true")
    parser.add_argument("--use-event-sizing", action="store_true", default=True)
    parser.add_argument("--no-event-sizing", action="store_true")
    parser.add_argument("--factor-health-report", action="store_true")
    parser.add_argument("--factor-health-data", type=str, default=None)
    parser.add_argument("--router-seed", type=int, default=42)
    parser.add_argument("--router-arms", nargs="+",
                        default=["multi_factor_m15", "trend_following", "mean_reversion", "breakout"])
    parser.add_argument("--include-shadow-factors", action="store_true")
    parser.add_argument("--shadow-top-k", type=int, default=3)
    parser.add_argument("--shadow-vote-weight", type=float, default=None)
    parser.add_argument("--runtime-config-path", type=str, default=None)

    # 风险参数 CLI 透传
    parser.add_argument("--max-daily-loss-pct", type=float, default=None)
    parser.add_argument("--max-consecutive-loss", type=int, default=None)
    parser.add_argument("--single-risk-usd", type=float, default=None)
    parser.add_argument("--volatility-mult", type=float, default=None)

    args = parser.parse_args()

    setup_logging()
    logger.info(f"Starting in {args.mode} mode | {args.symbol} | {args.timeframe}")

    # RuntimeConfig 初始化
    try:
        from config.runtime_config import RuntimeConfig, replace as rc_replace
        yaml_path = args.runtime_config_path or "config/settings.yaml"
        yaml_cfg = load_config(yaml_path)
        rc = RuntimeConfig.from_yaml(yaml_cfg)
        if args.shadow_vote_weight is not None:
            rc.shadow_vote_weight = float(args.shadow_vote_weight)
        rc_replace(rc)
        logger.info(f"RuntimeConfig loaded from {yaml_path}")
    except Exception as _rc_err:
        logger.warning(f"RuntimeConfig load skipped: {_rc_err!r}")

    # 模式分发
    if args.mode == "backtest":
        from cli.backtest import run_backtest
        run_backtest(args)
    elif args.mode == "paper":
        from cli.paper import run_paper
        run_paper(args)
    elif args.mode == "live":
        from cli.live import run_live
        run_live(args)


if __name__ == "__main__":
    main()
