"""scripts/test_gp_search.py - GP 因子搜索 A/B 验证 (T15.3 v2, 2026-06-03)

对比 random search vs GP search 在同一数据/同一评估器下的 top-k 表现.
目标: GP top-1 score >= random search top-1 score.
"""
from __future__ import annotations

import logging
import os
import sys
import time as _time
from pathlib import Path

# 路径 + UTF-8
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger('test_gp_search')

import numpy as np
import pandas as pd

from alpha.factor_score_evaluator import FactorScoreEvaluator
from alpha.factor_search import FactorSearch
from alpha.factor_search_gp import FactorSearchGP, save_gp_result


N_GEN = 20
POP_SIZE = 50


def load_m15_data():
    import sqlite3
    db = Path('data/market_data.db')
    if not db.exists():
        raise FileNotFoundError(f'{db} not found')
    con = sqlite3.connect(str(db))
    try:
        df = pd.read_sql_query(
            "SELECT time, open, high, low, close, volume "
            "FROM bars WHERE symbol='XAUUSD+' AND timeframe='M15' "
            "ORDER BY time ASC LIMIT 5000",
            con, index_col='time', parse_dates=['time'],
        )
    finally:
        con.close()
    df = df.dropna()
    logger.info(f'loaded {len(df)} M15 bars, range {df.index[0]} -> {df.index[-1]}')
    return df


def main():
    logger.info('=== T15.3 v2: GP vs Random factor search A/B ===')
    df = load_m15_data()
    evaluator = FactorScoreEvaluator(df, forward_period=1)

    # --- A: Random search (1000 candidates, baseline)
    logger.info('--- A: Random search (1000 candidates) ---')
    t0 = _time.time()
    rs = FactorSearch(evaluator)
    rand_result = rs.random_search(n_candidates=1000, top_k=20, max_depth=4, seed=42, verbose=False)
    rand_t = _time.time() - t0
    rand_top1 = rand_result.top_k[0] if rand_result.top_k else None
    rand_top1_score = rand_top1.score if rand_top1 else 0.0
    rand_top1_expr = rand_top1.expression if rand_top1 else '<empty>'
    rand_top5_scores = [s.score for s in rand_result.top_k[:5]]
    logger.info(f'A done in {rand_t:.1f}s | top1={rand_top1_score:.2f} | expr={rand_top1_expr!r}')

    # --- B: GP search
    logger.info('--- B: GP search (pop=50, gen=20) ---')
    t0 = _time.time()
    gp = FactorSearchGP(evaluator)
    gp_result = gp.run(pop_size=POP_SIZE, n_generations=N_GEN, top_k=20, init_max_depth=4, seed=42, verbose=False)
    gp_t = _time.time() - t0
    gp_top1 = gp_result.best[0] if gp_result.best else None
    gp_top1_score = gp_top1.score if gp_top1 else 0.0
    gp_top1_expr = gp_top1.expression if gp_top1 else '<empty>'
    gp_top5_scores = [s.score for s in gp_result.best[:5]]
    logger.info(f'B done in {gp_t:.1f}s | top1={gp_top1_score:.2f} | expr={gp_top1_expr!r}')

    # 落盘 GP 结果
    out_dir = Path('data/charts/factor_discovery')
    out_path = save_gp_result(gp_result, out_dir)

    # 报告
    report_path = Path('data/charts/gp_search_report.txt')
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open('w', encoding='utf-8') as f:
        f.write('=== T15.3 v2: GP vs Random factor search A/B ===\n')
        f.write(f'Data: {len(df)} M15 bars, {df.index[0]} -> {df.index[-1]}\n\n')
        f.write('--- A: Random search (1000 candidates, max_depth=4) ---\n')
        f.write(f'  elapsed:         {rand_t:.1f}s\n')
        f.write(f'  n_valid:         {rand_result.n_valid}/{rand_result.n_total}\n')
        f.write(f'  top-1 score:     {rand_top1_score:.2f}\n')
        f.write(f'  top-1 expr:      {rand_top1_expr}\n')
        f.write(f'  top-5 scores:    {[round(s,1) for s in rand_top5_scores]}\n\n')
        f.write('--- B: GP search (pop=50, gen=20, max_depth=4) ---\n')
        f.write(f'  elapsed:         {gp_t:.1f}s ({gp_result.total_evaluated} evals)\n')
        f.write(f'  top-1 score:     {gp_top1_score:.2f}\n')
        f.write(f'  top-1 expr:      {gp_top1_expr}\n')
        f.write(f'  top-5 scores:    {[round(s,1) for s in gp_top5_scores]}\n')
        f.write(f'  best_history:    {[round(x,1) for x in gp_result.best_score_history]}\n')
        f.write(f'  avg_history:     {[round(x,1) for x in gp_result.avg_score_history]}\n\n')
        f.write('--- SUMMARY ---\n')
        delta_score = gp_top1_score - rand_top1_score
        delta_pct = (delta_score / rand_top1_score * 100) if rand_top1_score > 0 else 0
        f.write(f'  random top1:   {rand_top1_score:.2f}\n')
        f.write(f'  GP top1:       {gp_top1_score:.2f}\n')
        f.write(f'  delta:         {delta_score:+.2f} ({delta_pct:+.1f}%)\n')
        f.write(f'  random top5:   {[round(s,1) for s in rand_top5_scores]}\n')
        f.write(f'  GP top5:       {[round(s,1) for s in gp_top5_scores]}\n')
        f.write(f'  time ratio:    GP/random = {gp_t/rand_t:.2f}x\n')
        f.write(f'  GP saved to:   {out_path}\n')
    logger.info(f'report -> {report_path}')

    # 控制台摘要
    print()
    print('=' * 70)
    print('SUMMARY: T15.3 v2 GP vs Random')
    print('=' * 70)
    print(f'  data:         {len(df)} M15 bars')
    print(f'  random top1:  {rand_top1_score:6.2f}  ({rand_t:5.1f}s)')
    print(f'  GP top1:      {gp_top1_score:6.2f}  ({gp_t:5.1f}s)')
    print(f'  delta score:  {delta_score:+6.2f}  ({delta_pct:+.1f}%)')
    print(f'  GP > random:  {"YES" if gp_top1_score > rand_top1_score else "NO"}')
    print(f'  report:       {report_path}')
    print('=' * 70)


if __name__ == '__main__':
    main()