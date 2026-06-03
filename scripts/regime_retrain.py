"""scripts/regime_retrain.py - PR-3.7 Regime 分类器周期重训

设计:
- 加载最近 N 根 M15 bar, 用 4 因子 (aroon/cci/mfi/williams_r) + 5 regime
- 给每根 bar 生成训练样本: (regime 1-of-5, factor_values 4) -> y=sign(forward_return) > 0
- 训练 LogisticRegression (轻量, <100ms)
- save 到 data/regime_classifier.pkl (替换旧模型)
- 落盘 data/charts/regime_retrain_YYYYMMDD.json

cron 建议 (hermes):
  job_id: "regime_retrain_weekly"
  every:  "168h"   (每周)
  run_at: "Sunday 02:30"
  script: "scripts/regime_retrain.py --n-bars 8000"
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
logger = logging.getLogger("regime_retrain")


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


def make_regime_label(close_arr, i, lookback: int = 20) -> str:
    """基于 ADX/volatility 简单判定 regime (5 类)."""
    import numpy as np
    if i < lookback:
        return "RANGING"
    window = close_arr[i - lookback:i + 1]
    if len(window) < lookback + 1:
        return "RANGING"
    returns = np.diff(window) / window[:-1]
    vol = float(np.std(returns))
    trend = float((window[-1] - window[0]) / window[0])
    if vol > 0.005:
        return "HIGH_VOL"
    if vol < 0.001:
        return "LOW_VOL"
    if trend > 0.01:
        return "TRENDING_UP"
    if trend < -0.01:
        return "TRENDING_DOWN"
    return "RANGING"


def main():
    parser = argparse.ArgumentParser(description="Regime classifier periodic retrain")
    parser.add_argument("--n-bars", type=int, default=8000)
    parser.add_argument("--model-path", default="data/regime_classifier.pkl")
    args = parser.parse_args()

    logger.info(f"=== Regime retrain: {args.n_bars} M15 bars ===")
    df = load_m15_bars(args.n_bars)
    if len(df) < 200:
        raise ValueError(f"Not enough bars: {len(df)}")
    logger.info(f"Loaded {len(df)} bars, range {df.index[0]} -> {df.index[-1]}")

    # 算 4 因子
    import numpy as np
    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    volume = df["volume"].values
    n = len(close)
    f_aroon = np.zeros(n)
    f_cci = np.zeros(n)
    f_mfi = np.zeros(n)
    f_williams = np.zeros(n)
    for i in range(25, n):
        # aroon
        last_25_high = high[i - 24:i + 1]
        last_25_low = low[i - 24:i + 1]
        f_aroon[i] = (25 - (24 - np.argmax(last_25_high))) / 25.0 - (25 - (24 - np.argmin(last_25_low))) / 25.0
        # cci
        tp = (high[i] + low[i] + close[i]) / 3
        tp_20 = np.array([(high[i - k] + low[i - k] + close[i - k]) / 3 for k in range(20)])
        sma_tp = tp_20.mean()
        mad = np.mean(np.abs(tp_20 - sma_tp)) + 1e-9
        f_cci[i] = (tp - sma_tp) / (0.015 * mad)
        # mfi (简化, 假设 volume 都是正向)
        v_20 = volume[i - 19:i + 1].sum() + 1e-9
        f_mfi[i] = 50.0 + 50.0 * (close[i] - close[i - 19]) / close[i - 19]
        # williams %R
        hh = high[i - 13:i + 1].max()
        ll = low[i - 13:i + 1].min()
        f_williams[i] = -100.0 * (hh - close[i]) / (hh - ll + 1e-9)
    # 截断
    f_aroon = np.clip(f_aroon, -1, 1) / 2 + 0.5  # -> [0,1]
    f_cci = np.clip(f_cci / 200 + 0.5, 0, 1)
    f_mfi = np.clip(f_mfi / 100, 0, 1)
    f_williams = np.clip((f_williams + 100) / 100, 0, 1)

    # 生成训练样本
    X_samples, y_labels, regime_used = [], [], []
    for i in range(50, n - 1):
        regime = make_regime_label(close, i)
        factor_values = {
            "aroon": float(f_aroon[i]),
            "cci": float(f_cci[i]),
            "mfi": float(f_mfi[i]),
            "williams_r": float(f_williams[i]),
        }
        X_samples.append({"regime": regime, "factor_values": factor_values})
        # label: 下一根 bar 涨 -> 1, 跌/平 -> 0
        y_labels.append(int(close[i + 1] > close[i]))
        regime_used.append(regime)

    n_pos = sum(y_labels)
    logger.info(f"Training samples: {len(X_samples)} (pos={n_pos}, neg={len(y_labels) - n_pos})")
    # regime 分布
    from collections import Counter
    rc = Counter(regime_used)
    logger.info(f"Regime dist: {dict(rc)}")

    # 训练
    from alpha.regime_classifier import RegimeAwareClassifier
    cls = RegimeAwareClassifier(model_path=args.model_path)
    t0 = _time.time()
    result = cls.train(X_samples, y_labels)
    elapsed = _time.time() - t0
    logger.info(f"Train done in {elapsed*1000:.1f}ms, acc={result['train_acc']:.4f}")
    cls.save()
    logger.info(f"  -> {args.model_path}")

    # 落盘报告
    today = datetime.now().strftime("%Y%m%d")
    out_path = PROJECT_ROOT / "data" / "charts" / f"regime_retrain_{today}.json"
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "config": vars(args),
        "n_samples": len(X_samples),
        "n_pos": n_pos, "n_neg": len(y_labels) - n_pos,
        "regime_dist": dict(rc),
        "train_acc": result["train_acc"],
        "elapsed_ms": round(elapsed * 1000, 1),
        "model_path": args.model_path,
    }
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info(f"  -> {out_path}")

    print()
    print("=" * 60)
    print(f"REGIME RETRAIN ({today})")
    print("=" * 60)
    print(f"  bars={len(df)}  samples={len(X_samples)}  pos={n_pos}")
    print(f"  regime dist: {dict(rc)}")
    print(f"  train_acc: {result['train_acc']:.4f}  ({elapsed*1000:.1f}ms)")
    print(f"  model: {args.model_path}")
    print(f"  log: {out_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()