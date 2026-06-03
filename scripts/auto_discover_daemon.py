"""scripts/auto_discover_daemon.py - PR-2.1 自主因子发现 (L2 GP cron 化)

设计:
- 每日从 db 拉最近 N 根 M15 bar (默认 8000 ~= 83 天)
- 跑 GP 50x30 (推荐配置, 已验证赢 random +2.36)
- top-K 进 auto_register, 注册到 RegistryAdapter (SOURCE_SHADOW)
- 落盘 data/charts/factor_discovery/auto_run_YYYYMMDD.json
- 0 守卫: 已有同名 shadow factor 不重复注册

CLI:
  python scripts/auto_discover_daemon.py             # 默认 8000 bar
  python scripts/auto_discover_daemon.py --n-bars 5000 --pop 30 --gen 20
  python scripts/auto_discover_daemon.py --dry-run   # 只跑 GP, 不 register

cron 建议 (hermes):
  job_id: "auto_discover_daily"
  every:  "24h"
  run_at: "01:00"
  script: "scripts/auto_discover_daemon.py"
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
logger = logging.getLogger('auto_discover')


def load_m15_bars(n_bars: int, symbol: str = "XAUUSD+") -> "pd.DataFrame":
    import sqlite3
    import pandas as pd
    db = PROJECT_ROOT / "data" / "market_data.db"
    con = sqlite3.connect(str(db))
    try:
        df = pd.read_sql_query(
            f"SELECT time, open, high, low, close, volume FROM bars "
            f"WHERE symbol=? AND timeframe='M15' ORDER BY time DESC LIMIT ?",
            con, params=(symbol, n_bars), index_col="time", parse_dates=["time"],
        )
    finally:
        con.close()
    df = df.sort_index()
    return df


def main():
    parser = argparse.ArgumentParser(description="Auto GP factor discovery (L2 cron)")
    parser.add_argument("--n-bars", type=int, default=8000,
                        help="回看 bar 数 (default 8000 ~= 83 day M15)")
    parser.add_argument("--pop", type=int, default=50)
    parser.add_argument("--gen", type=int, default=30)
    parser.add_argument("--top-k", type=int, default=10,
                        help="取前 K 名 register 为 shadow")
    parser.add_argument("--score-threshold", type=float, default=50.0,
                        help="score 低于此不进 shadow (避免垃圾因子)")
    parser.add_argument("--dry-run", action="store_true",
                        help="只跑 GP, 不 register")
    parser.add_argument("--out-dir", default="data/charts/factor_discovery")
    args = parser.parse_args()

    logger.info(f"=== Auto GP discover: {args.n_bars} bar, pop={args.pop}, gen={args.gen} ===")
    df = load_m15_bars(args.n_bars)
    if len(df) < 500:
        raise ValueError(f"Not enough bars: {len(df)} < 500")
    logger.info(f"Loaded {len(df)} M15 bars, range {df.index[0]} -> {df.index[-1]}")

    from alpha.factor_score_evaluator import FactorScoreEvaluator
    from alpha.factor_search_gp import FactorSearchGP, save_gp_result

    evaluator = FactorScoreEvaluator(df, forward_period=1)
    gp = FactorSearchGP(evaluator)
    t0 = _time.time()
    result = gp.run(
        pop_size=args.pop, n_generations=args.gen, top_k=args.top_k,
        init_max_depth=4, seed=42, verbose=True,
    )
    elapsed = _time.time() - t0
    logger.info(f"GP done in {elapsed:.1f}s, top-1 score={result.best[0].score:.2f}")

    # 过滤
    kept = [s for s in result.best if s.score >= args.score_threshold and s.status != "UNKNOWN"]
    logger.info(f"After score >= {args.score_threshold} filter: {len(kept)}/{len(result.best)} kept")

    # 注册 (shadow)
    promoted, skipped = [], []
    if not args.dry_run and kept:
        from alpha.registry_adapter import RegistryAdapter, SOURCE_SHADOW
        from alpha.factor_dsl import evaluate_dsl
        from alpha.registry import factor_registry
        from alpha.persistent_registry import restore_from_log

        n_restored = restore_from_log("data/charts/factor_lifecycle_log.jsonl")
        logger.info(f"Persistent restore: {n_restored} factors from lifecycle log")
        adapter = RegistryAdapter()
        for i, s in enumerate(kept):
            expr = s.expression
            expr_hash = str(abs(hash(expr)))[:8]
            name = f"dsl_auto_{expr_hash}_{i:03d}"
            # 守卫: 已有同名跳过
            if name in factor_registry._factors or name in adapter._meta:
                skipped.append(name)
                continue
            func = lambda df_, e=expr: evaluate_dsl(e, df_)
            ok = adapter.register_runtime(
                name=name, func=func, source=SOURCE_SHADOW, description=expr,
            )
            if ok:
                promoted.append(name)

        logger.info(f"Auto-register: promoted={len(promoted)}, skipped={len(skipped)}")
        for n in promoted[:5]:
            logger.info(f"  + {n}")

    # 落盘
    out_dir = PROJECT_ROOT / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")
    out_path = out_dir / f"auto_run_{today}.json"
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "config": {
            "n_bars": args.n_bars, "pop": args.pop, "gen": args.gen,
            "top_k": args.top_k, "score_threshold": args.score_threshold,
            "dry_run": args.dry_run,
        },
        "data_range": [str(df.index[0]), str(df.index[-1])],
        "gp_result": {
            "elapsed_sec": round(elapsed, 1),
            "best_score": result.best[0].score if result.best else 0,
            "best_expr": result.best[0].expression if result.best else "",
            "best_history": [round(x, 1) for x in result.best_score_history],
        },
        "kept": [{"expr": s.expression, "score": s.score, "abs_ic": s.abs_ic_mean,
                  "status": s.status} for s in kept],
        "promoted": promoted,
        "skipped": skipped,
    }
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info(f"  -> {out_path}")

    # 控制台摘要
    print()
    print("=" * 60)
    print(f"AUTO DISCOVER ({today})")
    print("=" * 60)
    print(f"  bars={len(df)}  pop={args.pop}  gen={args.gen}  elapsed={elapsed:.1f}s")
    print(f"  top-1 score: {result.best[0].score:.2f}")
    print(f"  kept (score>={args.score_threshold}): {len(kept)}")
    if not args.dry_run:
        print(f"  promoted (shadow): {len(promoted)}  skipped: {len(skipped)}")
    else:
        print(f"  DRY RUN (no register)")
    print(f"  log: {out_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()