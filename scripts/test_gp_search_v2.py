"""scripts/test_gp_search_v2.py - GP 改进验证 (100 pop x 10 gen vs 1000 random)
"""
import io, os, sys, time as _time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('gp_v2')

from pathlib import Path
import sqlite3, pandas as pd
from alpha.factor_score_evaluator import FactorScoreEvaluator
from alpha.factor_search import FactorSearch
from alpha.factor_search_gp import FactorSearchGP

con = sqlite3.connect('data/market_data.db')
df = pd.read_sql_query(
    "SELECT time, open, high, low, close, volume FROM bars "
    "WHERE symbol=\"XAUUSD+\" AND timeframe=\"M15\" ORDER BY time ASC LIMIT 5000",
    con, index_col='time', parse_dates=['time'])
con.close()
df = df.dropna()
log.info(f'loaded {len(df)} M15 bars')
ev = FactorScoreEvaluator(df, forward_period=1)

# A: 1000 random (baseline)
log.info('--- A: 1000 random ---')
t0 = _time.time()
ra = FactorSearch(ev).random_search(n_candidates=1000, top_k=10, max_depth=4, seed=42, verbose=False)
ta = _time.time() - t0
log.info(f'A: top1={ra.top_k[0].score:.2f} in {ta:.1f}s, expr={ra.top_k[0].expression!r}')

# B: GP 100 pop x 10 gen
log.info('--- B: GP 100x10 ---')
t0 = _time.time()
gb = FactorSearchGP(ev).run(pop_size=100, n_generations=10, top_k=10, init_max_depth=4, seed=42, verbose=False)
tb = _time.time() - t0
log.info(f'B: top1={gb.best[0].score:.2f} in {tb:.1f}s, expr={gb.best[0].expression!r}')
log.info(f'B best_history: {[round(x,1) for x in gb.best_score_history]}')

# C: GP 50 pop x 30 gen
log.info('--- C: GP 50x30 ---')
t0 = _time.time()
gc = FactorSearchGP(ev).run(pop_size=50, n_generations=30, top_k=10, init_max_depth=4, seed=42, verbose=False)
tc = _time.time() - t0
log.info(f'C: top1={gc.best[0].score:.2f} in {tc:.1f}s, expr={gc.best[0].expression!r}')
log.info(f'C best_history: {[round(x,1) for x in gc.best_score_history]}')

print()
print('=' * 70)
print('GP variants comparison')
print('=' * 70)
print(f'  A random 1000c:  {ra.top_k[0].score:6.2f}  ({ta:5.1f}s)  expr={ra.top_k[0].expression!r}')
print(f'  B GP    100x10: {gb.best[0].score:6.2f}  ({tb:5.1f}s)  expr={gb.best[0].expression!r}')
print(f'  C GP    50x30:  {gc.best[0].score:6.2f}  ({tc:5.1f}s)  expr={gc.best[0].expression!r}')
print('=' * 70)