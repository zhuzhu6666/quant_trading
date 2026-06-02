"""
scripts/train_xgb_walkforward.py
================================

P0-5 XGBoost 训练 + 严格 OOS 检验:
  - 特征: 4 有效因子 (dxy_corr_20 / macd_hist / bb_width / di_spread) + hour_utc
            (避免 noise factor, 优先用 PCA 选出的)
  - 标签: 1-bar forward return > 0 (binary classification)
  - 训练: 80% 前段 (40K bar), 测试: 20% 后段 (10K bar, ~4 个月)
  - 同时: 6-fold Walk-Forward (rolling 6 个月 train + 1 个月 test, 2 年 + 数据)
  - 评估: 准确率 / 精确率 / 召回率 / AUC / 跟 baseline (always-up) 对比
  - 对比: XGBoost vs sklearn LogisticRegression

输出: 控制台报告 + 落盘 .npy (predictions) + .txt
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


# PCA 选出的 4 有效因子 + 1 个时段
FEATURES = ["dxy_corr_20", "macd_hist", "bb_width", "di_spread", "hour_utc"]


def make_dataset(df_bars: pd.DataFrame, factor_vals: dict[str, np.ndarray],
                 fwd_ret: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    组装 X, y, mask.

    X: (n, 5) - 5 因子 (PCA 选出的 + hour)
    y: (n,) - 1 = 涨, 0 = 跌/平
    mask: (n,) - 有效 (无 NaN)
    """
    n = len(df_bars)
    X = np.zeros((n, len(FEATURES)))
    for i, fname in enumerate(FEATURES):
        X[:, i] = factor_vals[fname]

    # 标签: 未来 1-bar 收益 > 0
    y = (fwd_ret > 0).astype(int)

    # mask: 任何特征 NaN 或 fwd_ret NaN 都剔除
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


def train_logreg(X_train: np.ndarray, y_train: np.ndarray):
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    scaler = StandardScaler()
    X_s = scaler.fit_transform(X_train)
    model = LogisticRegression(max_iter=1000, random_state=42)
    model.fit(X_s, y_train)
    return model, scaler


def evaluate(y_true: np.ndarray, y_pred: np.ndarray, y_prob: np.ndarray) -> dict:
    from sklearn.metrics import accuracy_score, precision_score, recall_score, roc_auc_score
    n_pos = int(y_true.sum())
    n_neg = len(y_true) - n_pos
    base_acc = max(n_pos, n_neg) / len(y_true)  # always predict majority
    return {
        "n": len(y_true),
        "n_pos": n_pos,
        "n_neg": n_neg,
        "base_acc": round(base_acc, 4),
        "acc": round(accuracy_score(y_true, y_pred), 4),
        "prec": round(precision_score(y_true, y_pred, zero_division=0), 4),
        "recall": round(recall_score(y_true, y_pred, zero_division=0), 4),
        "auc": round(roc_auc_score(y_true, y_prob), 4) if len(set(y_true)) > 1 else 0.0,
        "lift": round(accuracy_score(y_true, y_pred) - base_acc, 4),
    }


def main():
    print("=" * 78)
    print("  P0-5 XGBoost 训练 + OOS 检验 + Walk-Forward")
    print("=" * 78)

    # 1) 加载数据
    store = DataStore("data/market_data.db")
    bars = store.load_bars("XAUUSD+", "M15")
    assert not bars.empty
    print(f"\nLoaded {len(bars)} bars, {bars.index[0]} → {bars.index[-1]}")

    loader = ExternalDataLoader("data/market_data.db")
    ext = loader.align_to_bars(bars)
    df = bars.join(ext)

    # 2) 算因子
    engine = FactorEngine(df)
    engine.compute_all()

    # 3) 1-bar 未来收益
    close = bars["close"].values
    fwd_ret = np.full(len(close), np.nan)
    fwd_ret[:-1] = (close[1:] - close[:-1]) / close[:-1]

    # 4) 数据集
    X, y, mask = make_dataset(bars, engine._factor_cache, fwd_ret)
    n_valid = int(mask.sum())
    print(f"\n有效样本: {n_valid}/{len(bars)} (NaN dropped)")
    print(f"标签分布: 涨={int(y[mask].sum())} ({y[mask].mean():.2%}), 跌={int((1-y[mask]).sum())}")

    # 5) 8:2 train/test 切分 (时序! 前 80% train, 后 20% test)
    valid_idx = np.where(mask)[0]
    split = int(n_valid * 0.8)
    train_idx = valid_idx[:split]
    test_idx = valid_idx[split:]
    X_train, y_train = X[train_idx], y[train_idx]
    X_test, y_test = X[test_idx], y[test_idx]
    print(f"\n切分: train={len(X_train)} ({bars.index[train_idx[0]]} → {bars.index[train_idx[-1]]})")
    print(f"      test ={len(X_test)} ({bars.index[test_idx[0]]} → {bars.index[test_idx[-1]]})")

    # 6) 训练 XGBoost
    print("\n" + "=" * 78)
    print("  训练 1: XGBoost (n=200, depth=4, lr=0.05)")
    print("=" * 78)
    xgb = train_xgb(X_train, y_train)
    y_pred_xgb = xgb.predict(X_test)
    y_prob_xgb = xgb.predict_proba(X_test)[:, 1]
    metrics_xgb = evaluate(y_test, y_pred_xgb, y_prob_xgb)
    for k, v in metrics_xgb.items():
        print(f"  {k:<10s}  {v}")

    # 特征重要性
    print(f"\n  XGBoost 特征重要性:")
    importances = xgb.feature_importances_
    for fname, imp in sorted(zip(FEATURES, importances), key=lambda x: -x[1]):
        print(f"    {fname:<14s}  {imp:.4f}")

    # 7) 训练 sklearn LogisticRegression (基线对比)
    print("\n" + "=" * 78)
    print("  训练 2: sklearn LogisticRegression (基线)")
    print("=" * 78)
    lr, scaler = train_logreg(X_train, y_train)
    X_test_s = scaler.transform(X_test)
    y_pred_lr = lr.predict(X_test_s)
    y_prob_lr = lr.predict_proba(X_test_s)[:, 1]
    metrics_lr = evaluate(y_test, y_pred_lr, y_prob_lr)
    for k, v in metrics_lr.items():
        print(f"  {k:<10s}  {v}")

    # 8) 对比
    print("\n" + "=" * 78)
    print("  对比: XGBoost vs LogReg")
    print("-" * 78)
    print(f"  baseline (always majority): acc = {metrics_xgb['base_acc']}")
    print(f"  LogReg OOS acc            : {metrics_lr['acc']}  (lift {metrics_lr['lift']:+.4f})")
    print(f"  XGBoost OOS acc           : {metrics_xgb['acc']}  (lift {metrics_xgb['lift']:+.4f})")
    print(f"  LogReg OOS AUC            : {metrics_lr['auc']}")
    print(f"  XGBoost OOS AUC           : {metrics_xgb['auc']}")

    # 9) Walk-Forward: 滚动 6-fold 检验
    # 注: 有效样本只有 9937 (受 dxy_corr_20 限制), 用更小的窗口
    print("\n" + "=" * 78)
    print("  Walk-Forward (rolling 4 月 train / 1 月 test, 步进 1 月)")
    print("=" * 78)
    folds = []
    # 1 个月 ≈ 30 * 96 = 2880 bar
    # 4 个月 ≈ 4 * 2880 = 11520 bar
    fold_size = 2880
    train_size = 5760  # 2 个月 (因 dxy 数据只覆盖 9937)
    n_folds = max(0, (n_valid - train_size) // fold_size)
    print(f"  n_valid={n_valid}, train_size={train_size}, fold_size={fold_size}, n_folds={n_folds}")
    for k in range(n_folds):
        start = k * fold_size
        train_end = start + train_size
        test_end = min(train_end + fold_size, n_valid)
        if test_end > n_valid or train_end > n_valid:
            break
        fold_idx_tr = valid_idx[start:train_end]
        fold_idx_te = valid_idx[train_end:test_end]
        X_tr, y_tr = X[fold_idx_tr], y[fold_idx_tr]
        X_te, y_te = X[fold_idx_te], y[fold_idx_te]
        if len(np.unique(y_tr)) < 2 or len(np.unique(y_te)) < 2:
            print(f"  Fold {k+1}: 跳过 (单类)")
            continue
        m_xgb = train_xgb(X_tr, y_tr, n_estimators=100)  # 折内用更少树 (快)
        yp = m_xgb.predict(X_te)
        yprob = m_xgb.predict_proba(X_te)[:, 1]
        m = evaluate(y_te, yp, yprob)
        m["fold"] = k + 1
        m["period"] = f"{bars.index[fold_idx_te[0]].date()} → {bars.index[fold_idx_te[-1]].date()}"
        folds.append(m)
        print(f"  Fold {k+1}  {m['period']}  "
              f"acc={m['acc']:.4f}  AUC={m['auc']:.4f}  lift={m['lift']:+.4f}")

    if folds:
        accs = [f["acc"] for f in folds]
        aucs = [f["auc"] for f in folds]
        lifts = [f["lift"] for f in folds]
        print(f"\n  Walk-Forward 汇总 ({len(folds)} folds):")
        print(f"    acc  mean={np.mean(accs):.4f}  std={np.std(accs):.4f}  min={min(accs):.4f}  max={max(accs):.4f}")
        print(f"    AUC  mean={np.mean(aucs):.4f}  std={np.std(aucs):.4f}  min={min(aucs):.4f}  max={max(aucs):.4f}")
        print(f"    lift mean={np.mean(lifts):+.4f}  std={np.std(lifts):.4f}")
        # 跟 base 对比
        mean_lift = np.mean(lifts)
        if mean_lift > 0.01:
            verdict = "✅ XGBoost 有微弱提升, 谨慎使用"
        elif mean_lift > 0:
            verdict = "⚠️  XGBoost 提升 < 1%, 不建议上 paper"
        else:
            verdict = "❌ XGBoost 没提升甚至负提升, 不用上 paper"
        print(f"\n  结论: {verdict}")

    # 10) 落盘
    out_dir = Path("data/charts")
    out_dir.mkdir(parents=True, exist_ok=True)
    np.save(out_dir / "xgb_predictions.npy", {
        "y_test": y_test,
        "y_pred_xgb": y_pred_xgb,
        "y_prob_xgb": y_prob_xgb,
        "y_pred_lr": y_pred_lr,
        "y_prob_lr": y_prob_lr,
        "test_dates": [str(bars.index[i]) for i in test_idx],
        "features": FEATURES,
        "folds": folds,
    })
    print(f"\n→ 落盘: {out_dir / 'xgb_predictions.npy'}")

    with open(out_dir / "xgb_report.txt", "w", encoding="utf-8") as f:
        f.write(f"P0-5 XGBoost 训练报告 — XAUUSD+ M15 {len(bars)} bar\n")
        f.write(f"Period: {bars.index[0]} → {bars.index[-1]}\n")
        f.write(f"Features: {FEATURES}\n\n")
        f.write("XGBoost 80/20 OOS:\n")
        for k, v in metrics_xgb.items():
            f.write(f"  {k}: {v}\n")
        f.write("\nLogReg 80/20 OOS:\n")
        for k, v in metrics_lr.items():
            f.write(f"  {k}: {v}\n")
        f.write(f"\nWalk-Forward ({len(folds)} folds):\n")
        for fold in folds:
            f.write(f"  Fold {fold['fold']} {fold['period']}  "
                    f"acc={fold['acc']:.4f}  AUC={fold['auc']:.4f}  lift={fold['lift']:+.4f}\n")
    print(f"→ 落盘: {out_dir / 'xgb_report.txt'}")
    print("=" * 78)


if __name__ == "__main__":
    main()
