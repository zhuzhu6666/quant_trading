"""scripts/test_regime_classifier.py — RegimeAwareClassifier 验证

加载 50K M15 bar, 构造训练数据 (regime + 4 因子 → label),
前 40K 训练, 后 10K 测试.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import time as _time
import numpy as np

from data.store import DataStore
from alpha.regime_classifier import RegimeAwareClassifier, REGIMES
from strategies.trend_following import TrendFollowingStrategy
from scripts.equity_by_regime import _vector_atr
from scripts.factor_ic_report import (
    vec_aroon_up, vec_cci, vec_mfi, vec_williams_r,
)
from strategy.mab_router import classify_regime


def main():
    print("=" * 78)
    print("  RegimeAwareClassifier — XAUUSD+ M15, 50K bar")
    print("=" * 78)

    # 1. 加载数据
    store = DataStore("data/market_data.db")
    df = store.load_bars("XAUUSD+", "M15")
    assert not df.empty
    n = len(df)
    print(f"Loaded {n} bars")

    closes = df["close"].values.astype(np.float64)
    highs = df["high"].values.astype(np.float64)
    lows = df["low"].values.astype(np.float64)

    # 2. batch 算 4 因子
    print("\n计算 4 因子...")
    t0 = _time.time()
    aroon = vec_aroon_up(highs, 14)
    cci = vec_cci(highs, lows, closes, 20)
    mfi = vec_mfi(highs, lows, closes, df["volume"].values.astype(np.float64) if "volume" in df.columns else np.zeros(n), 14)
    williams_r = vec_williams_r(highs, lows, closes, 14)
    print(f"  done [{_time.time()-t0:.1f}s]")

    # 3. batch 算 ATR + regime
    print("\n计算 ATR + regime (batch)...")
    t0 = _time.time()
    atr = _vector_atr(highs, lows, closes, 14)
    ema50 = TrendFollowingStrategy._vector_ema(closes, 50)
    ema200 = TrendFollowingStrategy._vector_ema(closes, 200)
    regimes = np.array([
        classify_regime(closes[max(0, i-200):i+1],
                        ema50=ema50[max(0, i-200):i+1],
                        ema200=ema200[max(0, i-200):i+1],
                        atr=atr[max(0, i-200):i+1])
        for i in range(n)
    ], dtype=object)
    print(f"  done [{_time.time()-t0:.1f}s]")

    # 4. 构造训练数据
    # label = 1 if close[t+1] - close[t] > 0.5 * ATR[t]
    # label = 0 if < -0.5 * ATR[t]
    # else 丢弃
    print("\n构造训练数据 (label = 1bar 未来收益 > 0.5 ATR)...")
    t0 = _time.time()
    X = []
    y = []
    skipped = 0
    for i in range(n - 1):
        atr_i = atr[i]
        if np.isnan(atr_i) or atr_i <= 0 or np.isnan(closes[i+1]):
            skipped += 1
            continue
        ret = closes[i+1] - closes[i]
        if ret > 0.5 * atr_i:
            label = 1
        elif ret < -0.5 * atr_i:
            label = 0
        else:
            skipped += 1
            continue
        # 任一因子 nan 就丢
        if any(np.isnan([aroon[i], cci[i], mfi[i], williams_r[i]])):
            skipped += 1
            continue
        X.append({
            "regime": str(regimes[i]),
            "factor_values": {
                "aroon": float(aroon[i]),
                "cci": float(cci[i]),
                "mfi": float(mfi[i]),
                "williams_r": float(williams_r[i]),
            },
        })
        y.append(label)
    print(f"  samples: {len(X)} (skipped {skipped} no-signal or nan)")
    print(f"  label distribution: 1={sum(y)} ({sum(y)/len(y)*100:.1f}%)  0={len(y)-sum(y)} ({(len(y)-sum(y))/len(y)*100:.1f}%)")
    print(f"  [{_time.time()-t0:.1f}s]")

    # 5. 切分
    n_train = 40000
    train_n = min(n_train, int(len(X) * 0.8))
    X_train, y_train = X[:train_n], y[:train_n]
    X_test, y_test = X[train_n:], y[train_n:]
    print(f"\n切分: train={len(X_train)}  test={len(X_test)}")

    # 6. 训练
    print("\n训练 LogisticRegression...")
    clf = RegimeAwareClassifier()
    t0 = _time.time()
    result = clf.train(X_train, y_train)
    print(f"  train_acc: {result['train_acc']:.4f}  [{_time.time()-t0:.1f}s]")
    print(f"  n_pos: {result['n_pos']}  n_neg: {result['n_neg']}")

    # 7. 特征权重
    print("\n特征权重 (按 |权重| 降序):")
    for name, w in clf.feature_importance():
        print(f"  {name:<25s}  {w:+.4f}")
    print(f"  intercept             {result['intercept']:+.4f}")

    # 8. 测试
    print("\n测试...")
    y_pred = []
    y_prob = []
    for s in X_test:
        label, prob = clf.predict(s["regime"], s["factor_values"])
        y_pred.append(label)
        y_prob.append(prob)
    y_pred = np.array(y_pred)
    y_test = np.array(y_test)
    test_acc = float((y_pred == y_test).mean())
    print(f"  test_acc: {test_acc:.4f}")

    # 9. baseline (always 0 / always 1)
    base_0 = float((y_test == 0).mean())
    base_1 = float((y_test == 1).mean())
    print(f"\n  baseline (always 0): {base_0:.4f}")
    print(f"  baseline (always 1): {base_1:.4f}")
    print(f"  baseline (majority class): {max(base_0, base_1):.4f}")

    # 10. 预测分布
    print(f"\n预测分布:")
    print(f"  pred=1: {y_pred.sum()} / {len(y_pred)} ({y_pred.mean()*100:.1f}%)")
    print(f"  pred=0: {(y_pred==0).sum()} / {len(y_pred)} ({(y_pred==0).mean()*100:.1f}%)")
    print(f"  prob mean: {np.mean(y_prob):.3f}  median: {np.median(y_prob):.3f}")

    # 11. 保存模型
    clf.save()
    print(f"\n  模型已保存: {clf.model_path}")

    print()
    print("=" * 78)
    print(f"  train_acc={result['train_acc']:.4f}  test_acc={test_acc:.4f}  baseline={max(base_0, base_1):.4f}")
    print("=" * 78)


if __name__ == "__main__":
    main()
