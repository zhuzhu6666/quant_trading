"""scripts/promote_shadow_to_active.py - PR-2.5 shadow -> DISCOVERED 升级

设计:
- 跑最近 N bar (默认 4000 ~= 42 day M15)
- 对所有 SOURCE_SHADOW 因子评估 FactorHealth
- 健康分 >= healthy_threshold (默认 70) -> promote to DISCOVERED (ACTIVE)
- 健康分 < watch_threshold (默认 40) -> unregister
- watch 区间 -> 保持 shadow, 等待下次评估
- 落盘 data/charts/shadow_promotion_YYYYMMDD.json

cron 建议 (hermes):
  job_id: "shadow_promote_daily"
  every:  "24h"
  run_at: "01:30"  (在 auto_discover 之后)
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
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("shadow_promote")


def load_m15_bars(n_bars: int, symbol: str = "XAUUSD+"):
    import sqlite3
    import pandas as pd
    db = PROJECT_ROOT / "data" / "market_data.db"
    con = sqlite3.connect(str(db))
    try:
        df = pd.read_sql_query(
            f"SELECT time, open, high, low, close, volume FROM bars "
            f"WHERE symbol=? AND timeframe=\"M15\" ORDER BY time DESC LIMIT ?",
            con, params=(symbol, n_bars), index_col="time", parse_dates=["time"],
        )
    finally:
        con.close()
    return df.sort_index()


def main():
    parser = argparse.ArgumentParser(description="Shadow -> DISCOVERED 升级")
    parser.add_argument("--n-bars", type=int, default=4000)
    parser.add_argument("--healthy-threshold", type=float, default=70.0)
    parser.add_argument("--watch-threshold", type=float, default=40.0)
    parser.add_argument("--min-age-days", type=float, default=1.0,
                        help="shadow 至少存活 N 天才能升级 (避免过早升 ACTIVE)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logger.info(f"=== Shadow -> DISCOVERED promotion check ===")
    df = load_m15_bars(args.n_bars)
    if len(df) < 500:
        raise ValueError(f"Not enough bars: {len(df)} < 500")
    logger.info(f"Loaded {len(df)} M15 bars, range {df.index[0]} -> {df.index[-1]}")

    from alpha.registry_adapter import RegistryAdapter, SOURCE_SHADOW, SOURCE_DISCOVERED
    from alpha.factor_score_evaluator import FactorScoreEvaluator
    from alpha.registry import factor_registry
    from alpha.persistent_registry import restore_from_log

    adapter = RegistryAdapter()
    n_restored = restore_from_log("data/charts/factor_lifecycle_log.jsonl", adapter=adapter)
    logger.info(f"Persistent restore: {n_restored} factors from lifecycle log")
    evaluator = FactorScoreEvaluator(df, forward_period=1)
    shadows = adapter.list_by_source(SOURCE_SHADOW)
    if not shadows:
        logger.info("No shadow factors found. Nothing to do.")
        return

    logger.info(f"Found {len(shadows)} shadow factors: {shadows}")

    # 评估健康分 (evaluator 已构造, 见上)
    now = _time.time()
    promoted, watched, removed = [], [], []
    decisions = []

    for name in shadows:
        meta = adapter.get_meta(name)
        age_days = (now - meta.get("register_time", now)) / 86400.0
        if name not in factor_registry._factors:
            logger.warning(f"  {name}: not in factor_registry, skip")
            continue
        func = factor_registry._factors[name]
        desc = meta.get("description", "")

        # 用 FactorScoreEvaluator 算分 (基于 IC + 稳定性 + decay)
        try:
            scores = evaluator.score_expression(desc) if desc else None
            if scores is None or scores.status == "UNKNOWN":
                logger.warning(f"  {name}: expression evaluate failed")
                continue
            # 构造一个 status-like 对象给后续代码用
            from types import SimpleNamespace
            status = SimpleNamespace(
                score=scores.score, status=scores.status,
                abs_ic_mean=scores.abs_ic_mean, n_obs=scores.n_obs,
            )
        except Exception as e:
            logger.warning(f"  {name}: evaluate error: {e}")
            continue

        score = status.score
        decision = "watch"
        if score >= args.healthy_threshold and age_days >= args.min_age_days:
            decision = "promote"
        elif score < args.watch_threshold:
            decision = "remove"

        decisions.append({
            "name": name, "age_days": round(age_days, 2),
            "health_score": round(score, 2), "status": status.status,
            "decision": decision, "abs_ic": round(status.abs_ic_mean, 4),
        })
        logger.info(f"  {name}: age={age_days:.1f}d, score={score:.1f} "
                    f"({status.status}) -> {decision}")

        if args.dry_run:
            continue
        if decision == "promote":
            ok = adapter.promote(name, SOURCE_DISCOVERED,
                                 reason=f"score={score:.1f} >= {args.healthy_threshold}, age={age_days:.1f}d")
            if ok:
                promoted.append(name)
        elif decision == "remove":
            ok = adapter.unregister(name, reason=f"score={score:.1f} < {args.watch_threshold}")
            if ok:
                removed.append(name)
        else:
            watched.append(name)

    # 落盘
    today = datetime.now().strftime("%Y%m%d")
    out_path = PROJECT_ROOT / "data" / "charts" / f"shadow_promotion_{today}.json"
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "config": vars(args),
        "data_range": [str(df.index[0]), str(df.index[-1])],
        "decisions": decisions,
        "summary": {"promoted": promoted, "watched": watched, "removed": removed},
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info(f"  -> {out_path}")

    print()
    print("=" * 60)
    print(f"SHADOW -> DISCOVERED ({today})")
    print("=" * 60)
    print(f"  shadows checked: {len(shadows)}")
    print(f"  promoted: {len(promoted)}  watch: {len(watched)}  removed: {len(removed)}")
    if promoted:
        print(f"  + ACTIVE: {promoted}")
    if removed:
        print(f"  - REMOVED: {removed}")
    if args.dry_run:
        print(f"  DRY RUN (no change)")
    print(f"  log: {out_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()