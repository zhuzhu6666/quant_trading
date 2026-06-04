"""scripts/daily_paper_dryrun.py - 日终 paper dryrun 评估 (T16.8 配套, 2026-06-03)

设计:
- 跑最近 1000 M15 bar (~17 小时) 的 paper trade, 出 PnL / Sharpe / DD 报告
- 与昨日 dryrun 对比, delta > 5% 触发告警
- 落盘 data/charts/daily_dryrun_YYYYMMDD.json + 报告 .txt
- cron: hermes every 1d 02:00

CLI:
  python scripts/daily_paper_dryrun.py --days 1
  python scripts/daily_paper_dryrun.py --days 3 --timeframe M15
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time as _time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger('daily_dryrun')

import numpy as np
import pandas as pd


def load_recent_bars(symbol: str, timeframe: str, n_bars: int) -> pd.DataFrame:
    import sqlite3
    db = PROJECT_ROOT / 'data' / 'market_data.db'
    con = sqlite3.connect(str(db))
    try:
        df = pd.read_sql_query(
            f"SELECT time, open, high, low, close, volume "
            f"FROM bars WHERE symbol=? AND timeframe=? "
            f"ORDER BY time DESC LIMIT ?",
            con, params=(symbol, timeframe, n_bars), index_col='time', parse_dates=['time']
        )
    finally:
        con.close()
    return df.sort_index()


def run_paper_dryrun(n_bars: int, timeframe: str, symbol: str) -> dict:
    """
    简化版 paper 跑: 用 multi_factor_m15 单策略 baseline 跑 n_bars
    复用 main.py paper 模式的核心循环, 但只输出 PnL 报告
    """
    from strategies.multi_factor_m15 import MultiFactorM15Strategy as MultiFactorM15
    from execution.paper_engine import PaperExecutionEngine
    from execution.event_sizing import EventSizing
    from alpha.probability_calibrator import ProbabilityCalibrator

    logger.info(f'Loading last {n_bars} {timeframe} bars for {symbol}...')
    bars = load_recent_bars(symbol, timeframe, n_bars)
    if len(bars) < 100:
        raise ValueError(f'Not enough bars: {len(bars)} < 100')

    logger.info(f'Bars: {len(bars)}, range {bars.index[0]} -> {bars.index[-1]}')

    # 简化 paper 引擎
    strategy = MultiFactorM15(name="multi_factor_m15", symbol=symbol, timeframe=timeframe)
    event_sizing = EventSizing(db_path=str(PROJECT_ROOT / 'data' / 'market_data.db'))
    engine = PaperExecutionEngine(
        initial_balance=500.0,
        default_lots=0.01,
        max_position_lots=0.1,
        risk_per_trade_pct=2.0,
        enable_swap=True,
        swap_long_per_lot_per_day=-1.0,
        swap_short_per_lot_per_day=0.0,
        event_sizing=event_sizing,
    )

    t0 = _time.time()
    n_signals = 0
    for i, (ts, row) in enumerate(bars.iterrows()):
        # 时间统一为 epoch 秒 (utcfromtimestamp 期望数字)
        ts_epoch = int(ts.timestamp()) if hasattr(ts, "timestamp") else int(ts)
        bar = {'time': ts_epoch, 'open': float(row['open']), 'high': float(row['high']),
               'low': float(row['low']), 'close': float(row['close']),
               'volume': float(row.get('volume', 0))}
        signal = strategy.on_bar(bar)
        if signal is not None:
            n_signals += 1
            engine.on_bar(bar, signal)
        else:
            engine.on_bar(bar, None)
    elapsed = _time.time() - t0

    all_trades = engine.trades
    closed = [t for t in all_trades if t.direction in (2, -2)]
    pnl = sum(t.pnl for t in closed)
    swap_total = sum(t.swap for t in closed)
    comm = sum(getattr(t, "commission", 0.0) for t in closed)
    net = pnl - comm + swap_total
    n_trades = len(closed)
    wr = sum(1 for t in closed if t.pnl > 0) / max(1, n_trades)

    # DD: 走 balance 的 cummax
    bal = engine.balance_history if hasattr(engine, "balance_history") else []
    if not bal:
        # 退化: 用每个 trade close 后的 balance
        bal = [500.0]
        running = 500.0
        for t in closed:
            running += t.pnl
            bal.append(running)
    eq = pd.Series(bal)
    peak = eq.cummax()
    dd_series = peak - eq
    dd = float(dd_series.max())
    dd_pct = float(dd / peak.iloc[0] * 100) if peak.iloc[0] > 0 else 0.0

    # Sharpe-like
    if n_trades > 5:
        rets = [t.pnl / 500.0 for t in closed]
        sharpe = float(np.mean(rets) / (np.std(rets) + 1e-9) * np.sqrt(n_trades))
    else:
        sharpe = 0.0

    return {
        'n_bars': int(len(bars)),
        'n_signals': int(n_signals),
        'n_trades': int(n_trades),
        'gross_pnl': round(float(pnl), 2),
        'swap': round(float(swap_total), 2),
        'net_pnl': round(float(net), 2),
        'return_pct': round(float(net / 500.0 * 100), 2),
        'win_rate': round(float(wr), 4),
        'sharpe': round(sharpe, 3),
        'max_dd_usd': round(float(dd), 2),
        'max_dd_pct': round(float(dd_pct), 2),
        'elapsed_sec': round(elapsed, 1),
        'timeframe': timeframe,
        'symbol': symbol,
        'bar_range': [str(bars.index[0]), str(bars.index[-1])],
        'ts_utc': datetime.now(timezone.utc).isoformat(),
    }


def compare_with_previous(result: dict, prev_path: Path) -> dict:
    """与昨日 dryrun 对比, delta 超阈值返回 WARNING."""
    if not prev_path.exists():
        return {'prev': None, 'delta_return_pct': None, 'alert': None}
    try:
        prev = json.loads(prev_path.read_text(encoding='utf-8'))
        delta = result['return_pct'] - prev.get('return_pct', 0.0)
        alert = None
        if delta < -5.0:
            alert = f'WARNING: PnL dropped {delta:+.2f}% vs prev'
        elif delta > 5.0:
            alert = f'GOOD: PnL improved {delta:+.2f}% vs prev'
        return {'prev': prev.get('return_pct'), 'delta_return_pct': round(delta, 2),
                'alert': alert, 'prev_date': prev.get('bar_range', [None, None])[1]}
    except Exception as e:
        return {'prev': None, 'error': str(e), 'alert': None}


def main():
    parser = argparse.ArgumentParser(description='Daily paper dryrun')
    parser.add_argument('--days', type=float, default=1.0,
                        help='回放天数 (1.0 = ~17h, 1.5 = 1.5 day, default 1)')
    parser.add_argument('--timeframe', default='M15')
    parser.add_argument('--symbol', default='XAUUSD+')
    parser.add_argument('--out-dir', default='data/charts')
    args = parser.parse_args()

    # M15 = 96 bar/day
    bars_per_day = {'M15': 96, 'M5': 288, 'H1': 24, 'D1': 1}
    n_bars = int(args.days * bars_per_day.get(args.timeframe, 96))
    n_bars = max(200, n_bars)

    logger.info(f'=== Daily paper dryrun: {args.days} days = {n_bars} {args.timeframe} bars ===')
    result = run_paper_dryrun(n_bars, args.timeframe, args.symbol)
    logger.info(f'  n_trades={result["n_trades"]}, net=${result["net_pnl"]}, '
                f'return={result["return_pct"]:+.2f}%, sharpe={result["sharpe"]:.2f}, '
                f'DD={result["max_dd_pct"]:.1f}%')

    # 找上一次 dryrun (除了今天的)
    out_dir = PROJECT_ROOT / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime('%Y%m%d')
    today_path = out_dir / f'daily_dryrun_{today}.json'
    # 找上一次
    others = sorted([p for p in out_dir.glob('daily_dryrun_*.json') if p != today_path],
                    key=lambda p: p.stat().st_mtime, reverse=True)
    prev_path = others[0] if others else None
    cmp = compare_with_previous(result, prev_path) if prev_path else {'prev': None, 'alert': None}
    if cmp.get('alert'):
        logger.warning(cmp['alert'])

    # 落盘
    result['comparison'] = cmp
    today_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')
    logger.info(f'  -> {today_path}')

    # 报告 txt
    report_path = out_dir / f'daily_dryrun_{today}.txt'
    with report_path.open('w', encoding='utf-8') as f:
        f.write(f'=== Daily Paper Dryrun ({today}) ===\n')
        f.write(f'Range: {result["bar_range"][0]} -> {result["bar_range"][1]}\n')
        f.write(f'n_bars={result["n_bars"]}, n_signals={result["n_signals"]}, n_trades={result["n_trades"]}\n\n')
        f.write(f'Gross PnL:    ${result["gross_pnl"]:+.2f}\n')
        f.write(f'Swap total:   ${result["swap"]:+.2f}\n')
        f.write(f'Net PnL:      ${result["net_pnl"]:+.2f}\n')
        f.write(f'Return:       {result["return_pct"]:+.2f}%\n')
        f.write(f'Win rate:     {result["win_rate"]*100:.1f}%\n')
        f.write(f'Sharpe:       {result["sharpe"]:.3f}\n')
        f.write(f'Max DD:       ${result["max_dd_usd"]:.2f} ({result["max_dd_pct"]:.2f}%)\n')
        f.write(f'Elapsed:      {result["elapsed_sec"]:.1f}s\n\n')
        f.write(f'Comparison vs prev:\n')
        if cmp.get('prev') is not None:
            f.write(f'  prev return:  {cmp["prev"]:+.2f}%\n')
            f.write(f'  delta:        {cmp["delta_return_pct"]:+.2f}%\n')
            if cmp.get('alert'):
                f.write(f'  ALERT:        {cmp["alert"]}\n')
        else:
            f.write('  (no previous dryrun for comparison)\n')
    logger.info(f'  -> {report_path}')

    # 控制台摘要
    print()
    print('=' * 60)
    print(f'DAILY DRYRUN ({today})')
    print('=' * 60)
    print(f'  bars={result["n_bars"]}  trades={result["n_trades"]}  '
          f'net=${result["net_pnl"]:+.2f}  return={result["return_pct"]:+.2f}%')
    print(f'  sharpe={result["sharpe"]:.2f}  DD={result["max_dd_pct"]:.1f}%  '
          f'WR={result["win_rate"]*100:.0f}%')
    if cmp.get('alert'):
        print(f'  ALERT: {cmp["alert"]}')
    print('=' * 60)


if __name__ == '__main__':
    main()