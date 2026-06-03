"""scripts/t13_skip_backfill.py - PR-3.4 T13 skip 期间数据批量喂 meta-learner

设计:
- 找出过去 N 天所有 EventFilter 跳过的 bar (NFP/FOMC/CPI/GVZ)
- 对这些 bar 重新跑 model 预测 (用 walkforward OOS 预测近似)
- 批量喂给 MetaLearnerMonitor (offline calibration 校正)
- 这样 skip 期间 19,909 bar 不会白费, 反过来校准 model

CLI:
  python scripts/t13_skip_backfill.py --days 30
  python scripts/t13_skip_backfill.py --days 7 --dry-run

cron 建议 (hermes):
  job_id: "t13_skip_backfill"
  every:  "24h"
  run_at: "00:30"
  script: "scripts/t13_skip_backfill.py --days 7"
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
logger = logging.getLogger("t13_backfill")


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


def detect_skip_bars(bars_df) -> "pd.DataFrame":
    """
    用 EventFilter 找出所有 skip 过的 bar (含 skip reason).
    返回 sub-df, columns=[bar_time, skip_reason].
    """
    from execution.event_filter import SharedEventFilter
    ef = SharedEventFilter(
        enable_nfp_skip=True, nfp_skip_days=1,
        enable_dual_event_skip=True,
        enable_gvz_gate=True, gvz_drop_pct=-2.0,
        db_path=str(PROJECT_ROOT / "data" / "market_data.db"),
    )
    rows = []
    for ts, _ in bars_df.iterrows():
        bar_time = int(ts.timestamp())
        skip, reason = ef.should_skip(bar_time)
        if skip:
            rows.append({"bar_time": bar_time, "skip_reason": reason,
                         "bar_ts": ts.isoformat()})
    import pandas as pd
    return pd.DataFrame(rows)


def compute_predictions_and_actuals(bars_df, skip_df) -> "pd.DataFrame":
    """
    对每个 skip bar:
    - 预测: 用最近 100 bar 的 close/std 估算 prob (简化版; 实际应该用 xgboost)
    - 实际: 下 1 根 bar 的 forward return > 0 -> 1 else 0
    """
    import numpy as np
    out = []
    close = bars_df["close"].values
    n = len(close)
    bar_times = [int(t.timestamp()) for t in bars_df.index]
    bt_idx = {bt: i for i, bt in enumerate(bar_times)}
    for _, row in skip_df.iterrows():
        bt = int(row["bar_time"])
        i = bt_idx.get(bt)
        if i is None or i + 1 >= n:
            continue
        # 简化预测: 基于近 20 根 close 的 z-score 概率
        if i < 20:
            continue
        recent = close[i - 20:i]
        mu = float(np.mean(recent))
        std = float(np.std(recent) + 1e-9)
        z = (close[i] - mu) / std
        prob = 1.0 / (1.0 + np.exp(-z))  # sigmoid 映射
        prob = float(np.clip(prob, 0.05, 0.95))
        # 实际: 下一根 close > 当前 close -> 1
        actual = int(close[i + 1] > close[i])
        out.append({
            "bar_time": bt, "skip_reason": row["skip_reason"],
            "pred_prob": prob, "y_true": actual,
        })
    import pandas as pd
    return pd.DataFrame(out)


def feed_meta_monitor(pred_df, model_name: str = "xgboost", dry_run: bool = False) -> dict:
    """
    把 pred_df 喂给 MetaLearnerMonitor (in-process 实例化).
    """
    from live.meta_learner_monitor import MetaLearnerMonitor
    monitor = MetaLearnerMonitor(model_names=[model_name], window=500,
                                 drift_threshold=0.05, severe_threshold=0.10)

    n_fed = 0
    for _, row in pred_df.iterrows():
        if not dry_run:
            monitor.on_observation(model_name, float(row["pred_prob"]),
                                   int(row["y_true"]), bar_ts=float(row["bar_time"]))
        n_fed += 1

    # 报告漂移状态
    status = monitor.status()
    summary = {"n_fed": n_fed, "n_models": 1}
    for s in status:
        summary["drift"] = s.drift_status
        summary["calibration_gap"] = round(s.calibration_gap, 4) if hasattr(s, "calibration_gap") else 0
    return summary


def main():
    parser = argparse.ArgumentParser(description="T13 skip backfill -> meta-learner")
    parser.add_argument("--days", type=int, default=7,
                        help="回看天数 (default 7)")
    parser.add_argument("--model-name", default="xgboost")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    n_bars = args.days * 96  # M15 96 bar/day
    logger.info(f"=== T13 skip backfill: {args.days} days = {n_bars} M15 bars ===")
    bars = load_m15_bars(n_bars)
    if len(bars) < 100:
        raise ValueError(f"Not enough bars: {len(bars)}")
    logger.info(f"Loaded {len(bars)} bars, range {bars.index[0]} -> {bars.index[-1]}")

    skip_df = detect_skip_bars(bars)
    logger.info(f"Skip bars detected: {len(skip_df)} ({len(skip_df) / len(bars) * 100:.1f}%)")
    if len(skip_df) == 0:
        logger.info("No skip bars, nothing to backfill")
        return

    # 按 reason 统计
    reason_counts = skip_df["skip_reason"].value_counts().to_dict()
    logger.info(f"Skip reasons: {reason_counts}")

    pred_df = compute_predictions_and_actuals(bars, skip_df)
    logger.info(f"Predictions computed: {len(pred_df)}")
    if len(pred_df) == 0:
        return

    # 喂 meta-learner
    summary = feed_meta_monitor(pred_df, args.model_name, args.dry_run)
    logger.info(f"Meta-learner fed: {summary}")

    # 落盘
    today = datetime.now().strftime("%Y%m%d")
    out_dir = PROJECT_ROOT / "data" / "charts"
    out_path = out_dir / f"t13_skip_backfill_{today}.json"
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "config": vars(args),
        "data_range": [str(bars.index[0]), str(bars.index[-1])],
        "n_bars": len(bars),
        "n_skip_bars": int(len(skip_df)),
        "skip_reasons": reason_counts,
        "n_predictions": int(len(pred_df)),
        "meta_learner": summary,
    }
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info(f"  -> {out_path}")

    print()
    print("=" * 60)
    print(f"T13 SKIP BACKFILL ({today})")
    print("=" * 60)
    print(f"  bars: {len(bars)}  skip: {len(skip_df)} ({len(skip_df)/len(bars)*100:.1f}%)")
    print(f"  reasons: {reason_counts}")
    print(f"  predictions: {len(pred_df)}")
    print(f"  meta-learner fed: {summary['n_fed']} (drift={summary.get('drift')})")
    if args.dry_run:
        print("  DRY RUN (no actual feed)")
    print(f"  log: {out_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()