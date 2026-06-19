"""
scripts/walkforward_p0_6.py
===========================

P0-6 Walk-Forward 训练:
  - 时序 rolling window, 绝不 shuffle
  - 3 fold rolling: 2 月 train / 1 月 test, 步进 1 月
  - 每 fold: 重训 XGBoost → 在 OOS 1 月上评估
  - 关键: 不泄漏未来数据, 严格时序
  - 拼接所有 OOS 预测, 算汇总指标

跟 P0-5 区别: P0-5 是 80/20 一次切分, P0-6 是多 fold OOS 模拟 live 滚动重训。

输出: 控制台 + data/charts/walkforward_report.txt + .npy
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from data.store import DataStore
from data.external_loader import ExternalDataLoader
from alpha.registry import factor_registry
from alpha.factor_engine import FactorEngine


FEATURES = ["dxy_corr_20", "macd_hist", "bb_width", "di_spread", "hour_utc"]

# Walk-Forward 参数 (M15, 96 bar/day)
# 注: dxy_corr_20 限制有效样本 ~9937 bar, 约 3.5 个月
# 改用 2 月 train / 1 月 test / 步进 1 月, 能跑 ~2 fold
TRAIN_MONTHS = 2
TEST_MONTHS = 1
STEP_MONTHS = 1
BARS_PER_DAY = 96
BARS_PER_MONTH = 30 * BARS_PER_DAY  # ≈ 2880


def make_dataset(bars: pd.DataFrame, factor_vals: dict[str, np.ndarray],
                 fwd_ret: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    n = len(bars)
    X = np.zeros((n, len(FEATURES)))
    for i, fname in enumerate(FEATURES):
        X[:, i] = factor_vals[fname]
    y = (fwd_ret > 0).astype(int)
    mask = ~np.isnan(X).any(axis=1) & ~np.isnan(fwd_ret)
    return X, y, mask


def train_xgb(X_train: np.ndarray, y_train: np.ndarray, n_estimators: int = 200,
              max_depth: int = 4, learning_rate: float = 0.05):
    from xgboost import XGBClassifier
    model = XGBClassifier(
        n_estimators=n_estimators,
        max_depth=max_depth,
        learning_rate=learning_rate,
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=42,
        n_jobs=2,
        eval_metric="logloss",
    )
    model.fit(X_train, y_train, verbose=False)
    return model


def evaluate_fold(y_true, y_pred, y_prob) -> dict:
    from sklearn.metrics import accuracy_score, roc_auc_score
    n_pos = int(y_true.sum())
    n_neg = len(y_true) - n_pos
    base_acc = max(n_pos, n_neg) / len(y_true) if len(y_true) > 0 else 0.5
    return {
        "n": int(len(y_true)),
        "n_pos": n_pos,
        "n_neg": n_neg,
        "base_acc": round(base_acc, 4),
        "acc": round(accuracy_score(y_true, y_pred), 4),
        "auc": round(roc_auc_score(y_true, y_prob), 4) if len(set(y_true)) > 1 else 0.0,
        "lift": round(accuracy_score(y_true, y_pred) - base_acc, 4),
    }


def main():
    print("=" * 78)
    print("  P0-6 Walk-Forward 训练 — XAUUSD+ M15")
    print("=" * 78)
    print(f"  配置: train={TRAIN_MONTHS}月, test={TEST_MONTHS}月, step={STEP_MONTHS}月")
    print(f"  Features: {FEATURES}")

    # 1) 加载
    store = DataStore("data/ctrader_data.duckdb")
    bars = store.load_bars("XAUUSD+", "M15")
    loader = ExternalDataLoader("data/ctrader_data.duckdb")
    ext = loader.align_to_bars(bars)
    df = bars.join(ext)
    engine = FactorEngine(df)
    engine.compute_all()

    close = bars["close"].values
    fwd_ret = np.full(len(close), np.nan)
    fwd_ret[:-1] = (close[1:] - close[:-1]) / close[:-1]

    X, y, mask = make_dataset(bars, engine._factor_cache, fwd_ret)
    valid_idx = np.where(mask)[0]
    n_valid = len(valid_idx)
    print(f"\n  有效样本: {n_valid}/{len(bars)} (dxy 限制)")

    # 2) 切分 folds
    train_size = TRAIN_MONTHS * BARS_PER_MONTH
    test_size = TEST_MONTHS * BARS_PER_MONTH
    step_size = STEP_MONTHS * BARS_PER_MONTH

    # n_folds: 第一个 fold 的 train 从 idx=0 开始
    n_folds = max(0, (n_valid - train_size) // step_size + (1 if n_valid > train_size else 0))
    # 实际能跑几个 fold 受 n_valid 限制
    max_folds = max(0, (n_valid - train_size) // step_size + 1)
    print(f"  max_folds (受 n_valid 限制): {max_folds}")
    print(f"\n  时间窗 (相对 valid_idx 起点):")

    # 3) 跑每个 fold
    fold_metrics = []
    all_oos_y = []
    all_oos_pred = []
    all_oos_prob = []
    all_oos_dates = []
    all_oos_strategy = []  # 'xgboost'

    for k in range(max_folds):
        start = k * step_size
        train_end = start + train_size
        test_end = min(train_end + test_size, n_valid)
        if train_end >= n_valid:
            break

        fold_idx_tr = valid_idx[start:train_end]
        fold_idx_te = valid_idx[train_end:test_end]

        train_start_date = bars.index[fold_idx_tr[0]].date()
        train_end_date = bars.index[fold_idx_tr[-1]].date()
        test_start_date = bars.index[fold_idx_te[0]].date()
        test_end_date = bars.index[fold_idx_te[-1]].date()

        X_tr, y_tr = X[fold_idx_tr], y[fold_idx_tr]
        X_te, y_te = X[fold_idx_te], y[fold_idx_te]

        if len(np.unique(y_tr)) < 2 or len(np.unique(y_te)) < 2:
            print(f"  Fold {k+1}: 跳过 (单类)  train {train_start_date}→{train_end_date}, "
                  f"test {test_start_date}→{test_end_date}")
            continue

        m_xgb = train_xgb(X_tr, y_tr)
        yp = m_xgb.predict(X_te)
        yprob = m_xgb.predict_proba(X_te)[:, 1]
        m = evaluate_fold(y_te, yp, yprob)
        m["fold"] = k + 1
        m["train_period"] = f"{train_start_date} → {train_end_date}"
        m["test_period"] = f"{test_start_date} → {test_end_date}"
        fold_metrics.append(m)

        all_oos_y.append(y_te)
        all_oos_pred.append(yp)
        all_oos_prob.append(yprob)
        all_oos_dates.extend([str(bars.index[i].date()) for i in fold_idx_te])
        all_oos_strategy.extend(['xgboost'] * len(y_te))

        print(f"  Fold {k+1}  train {m['train_period']} ({len(X_tr)} bar)")
        print(f"           test  {m['test_period']} ({len(X_te)} bar)  "
              f"acc={m['acc']:.4f}  AUC={m['auc']:.4f}  lift={m['lift']:+.4f}")

    # 4) 汇总
    if not fold_metrics:
        print("\n  ⚠ 无 fold 完成, 跳过汇总")
        return

    accs = [f["acc"] for f in fold_metrics]
    aucs = [f["auc"] for f in fold_metrics]
    lifts = [f["lift"] for f in fold_metrics]

    print(f"\n" + "=" * 78)
    print(f"  Walk-Forward 汇总 ({len(fold_metrics)} folds)")
    print("=" * 78)
    print(f"  acc  mean={np.mean(accs):.4f}  std={np.std(accs):.4f}  "
          f"min={min(accs):.4f}  max={max(accs):.4f}")
    print(f"  AUC  mean={np.mean(aucs):.4f}  std={np.std(aucs):.4f}  "
          f"min={min(aucs):.4f}  max={max(aucs):.4f}")
    print(f"  lift mean={np.mean(lifts):+.4f}  std={np.std(lifts):.4f}")

    # 拼接 OOS 预测, 算总指标
    oos_y = np.concatenate(all_oos_y)
    oos_pred = np.concatenate(all_oos_pred)
    oos_prob = np.concatenate(all_oos_prob)
    pooled = evaluate_fold(oos_y, oos_pred, oos_prob)
    print(f"\n  拼接所有 OOS (n={pooled['n']} bar):")
    print(f"    acc = {pooled['acc']:.4f}  (base {pooled['base_acc']:.4f},  lift {pooled['lift']:+.4f})")
    print(f"    AUC = {pooled['auc']:.4f}")

    # 5) 落盘
    out_dir = Path("data/charts")
    out_dir.mkdir(parents=True, exist_ok=True)
    np.save(out_dir / "walkforward_oos.npy", {
        "y": oos_y, "pred": oos_pred, "prob": oos_prob,
        "dates": all_oos_dates, "strategy": all_oos_strategy,
        "folds": fold_metrics,
    })

    with open(out_dir / "walkforward_report.txt", "w", encoding="utf-8") as f:
        f.write(f"P0-6 Walk-Forward 训练报告 — XAUUSD+ M15 {len(bars)} bar\n")
        f.write(f"Period: {bars.index[0]} → {bars.index[-1]}\n")
        f.write(f"Config: train={TRAIN_MONTHS}月, test={TEST_MONTHS}月, step={STEP_MONTHS}月\n")
        f.write(f"Features: {FEATURES}\n\n")
        f.write(f"== Per-Fold ==\n")
        for m in fold_metrics:
            f.write(f"  Fold {m['fold']}  train {m['train_period']}  test {m['test_period']}\n")
            f.write(f"    acc={m['acc']:.4f}  AUC={m['auc']:.4f}  lift={m['lift']:+.4f}  "
                    f"n_test={m['n']}\n")
        f.write(f"\n== Summary ({len(fold_metrics)} folds) ==\n")
        f.write(f"  acc  mean={np.mean(accs):.4f}  std={np.std(accs):.4f}\n")
        f.write(f"  AUC  mean={np.mean(aucs):.4f}  std={np.std(aucs):.4f}\n")
        f.write(f"  lift mean={np.mean(lifts):+.4f}  std={np.std(lifts):.4f}\n")
        f.write(f"\n== Pooled OOS ==\n")
        f.write(f"  n={pooled['n']}  acc={pooled['acc']:.4f}  AUC={pooled['auc']:.4f}  lift={pooled['lift']:+.4f}\n")

    print(f"\n→ 落盘: {out_dir / 'walkforward_oos.npy'}")
    print(f"→ 落盘: {out_dir / 'walkforward_report.txt'}")

    # 5.5) 训练 calibrator (用 OOS prob+y 重训, 落盘供 main.py 启动加载)
    try:
        from alpha.probability_calibrator import ProbabilityCalibrator
        cal = ProbabilityCalibrator.fit_from_predictions(
            oos_prob, oos_y, n_buckets=8, method="bucket"
        )
        cal_path = Path("data/charts/calibrator_bucket.json")
        cal_path.parent.mkdir(parents=True, exist_ok=True)
        # 备份上一版
        bak = cal_path.with_suffix(".json.bak")
        if cal_path.exists():
            import shutil
            shutil.copy2(cal_path, bak)
        cal.save(str(cal_path))
        print(f"\n  [P0-6->P0-7] Calibrator saved: {cal_path} (method={cal.method}, "
              f"buckets={len(cal.buckets)}, platt=({cal.platt_a:.3f},{cal.platt_b:.3f}))")
    except Exception as e:
        print(f"\n  [P0-6->P0-7] WARNING: calibrator fit/save failed: {type(e).__name__}: {e}")

    # 6) 框架就位判断
    print(f"\n" + "=" * 78)
    print(f"  P0-6 框架就位判断")
    print("=" * 78)
    print(f"  ✓ Walk-Forward 跑通 ({len(fold_metrics)} folds)")
    print(f"  ✓ 严格时序, 无未来函数")
    print(f"  ✓ 每 fold 重训模型, 模拟 live 滚动")
    print(f"  ✓ OOS 预测拼接, 供后续打分系统 / 自学习使用")
    print(f"  注: 单模型 acc 0.52-0.53 (跟 50/50 接近), 框架完成, 矫正留给 P0-7 + 自学习")

    print("=" * 78)


if __name__ == "__main__":
    main()
