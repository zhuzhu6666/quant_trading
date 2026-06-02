"""
Trend Following Strategy — Paper test on XAUUSD+ M15 (50000 bars)

目标:
  - 加载 50000 根 M15 bar (XAUUSD+)
  - 跑 PaperTrader
  - 打印 trades / WR / net / DD / Sharpe
  - 至少要能跑完不出错

用法:
  cd C:\\Users\\zhu\\quant_trading
  python strategies/_test_trend.py
"""

import sys
import os
import time

# 让 python 能找到项目根目录下的包
ROOT = r'C:\Users\zhu\quant_trading'
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

import logging
logging.basicConfig(
    level=logging.WARNING,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
)
# 关键模块降噪, 只看 INFO
logging.getLogger('strategies.trend_following').setLevel(logging.WARNING)
logging.getLogger('execution.paper_engine').setLevel(logging.WARNING)

from data.store import DataStore
from strategy.registry import strategy_registry
import strategies  # noqa: F401  触发注册
from execution.paper_trader import PaperTrader


def main():
    t0 = time.time()

    # ── 0. 校验注册表 ──
    print('=' * 72)
    print('Trend Following Strategy — Paper test')
    print('=' * 72)
    print(f'Registered strategies: {strategy_registry.list()}')
    assert 'trend_following' in strategy_registry.list(), \
        "trend_following not registered!"
    print()

    # ── 1. 加载数据 ──
    store = DataStore('data/market_data.db')
    df = store.load_bars('XAUUSD+', 'M15')
    print(f'Loaded XAUUSD+ M15 bars: {len(df)}')
    if len(df) < 50000:
        print(f'  [!] 只有 {len(df)} 根 (需求 50000), 仍继续跑')
    print(f'  Period : {df.index[0]} → {df.index[-1]}')
    print()

    # ── 2. 构造策略 + 注入 engine 引用 (让 signal_flip 可平仓) ──
    strategy = strategy_registry.create(
        'trend_following',
        symbol='XAUUSD+',
        timeframe='M15',
    )
    print(f'Strategy: {strategy}')
    print(f'  params: {strategy.params}')
    print(f'  min_bars: {strategy._min_bars}')
    print()

    # ── 3. 构造 PaperTrader (启用风控, 跟其它测试一致) ──
    trader = PaperTrader(
        strategy=strategy,
        initial_balance=500.0,
        default_lots=0.01,
        max_lots=0.5,
        warmup_bars=300,           # EMA200 + ADX warmup 给足
        max_daily_loss_pct=5.0,
        max_consecutive_loss=5,
        max_trades_per_day=20,
        single_risk_usd=35.0,
        volatility_mult=3.0,
        risk_per_trade_pct=0.0,
        enable_circuit=True,
    )
    # 关键: 让策略拿到 engine, signal_flip 时可主动平仓
    strategy.engine = trader.engine

    # ── 4. 加载数据并跑回放 ──
    print('Loading data into trader...')
    trader.load_data(store, 'XAUUSD+', 'M15')
    print(f'  bars loaded: {len(trader._bars)}')
    print()

    print('Running paper replay...')
    t_run = time.time()
    report = trader.run()
    dt = time.time() - t_run
    print(f'  done in {dt:.1f}s ({dt/len(trader._bars)*1000:.2f} ms/bar)')
    print()

    # ── 5. 打印报告 (用 PaperTrader 自带的格式) ──
    trader.print_report(report)
    print()

    # ── 6. 关键指标再单拎出来打印一遍 (需求) ──
    print('=' * 72)
    print('KEY METRICS (需求项)')
    print('=' * 72)
    print(f'  trades = {report.total_trades}')
    print(f'  WR     = {report.win_rate:.2f}%   ({report.wins}W / {report.losses}L)')
    print(f'  net    = ${report.net_pnl:+.2f}  '
          f'({report.total_return_pct:+.2f}%)  '
          f'final=${report.final_balance:.2f}')
    print(f'  DD     = {report.max_drawdown_pct:.2f}%')
    print(f'  Sharpe = {report.sharpe:.3f}')
    print(f'  PF     = {report.profit_factor:.2f}')
    print('=' * 72)
    print()

    # ── 7. 信号数 / 平仓原因分布 (debug 价值) ──
    closes = [t for t in trader.engine.trades if t.direction in (2, -2)]
    reasons: dict[str, int] = {}
    for t in closes:
        reasons[t.reason] = reasons.get(t.reason, 0) + 1
    print('Close reasons:')
    for r, n in sorted(reasons.items(), key=lambda x: -x[1]):
        print(f'  {r:20s}: {n}')
    print()

    opens = [t for t in trader.engine.trades if t.direction in (1, -1)]
    print(f'Total open trades: {len(opens)}')
    if opens:
        longs = sum(1 for t in opens if t.direction == 1)
        shorts = sum(1 for t in opens if t.direction == -1)
        print(f'  long={longs}  short={shorts}')
    print()

    print(f'Total wall time: {time.time() - t0:.1f}s')
    print('OK ✅')


if __name__ == '__main__':
    main()
