"""alpha/ml/direction_predictor.py — XGBoost 方向预测器 (Phase 2)。

核心流程:
  训练: 加载 N bar → 预计算全部 39 因子 → 构造 X/y
       → PurgedWalkForward 验证 → 若 OOS acc > 0.51 则注册为因子
  预测: factor_registry 调用 predict(df) → 返回 [-1, +1] 信号

设计文档: docs/UPGRADE_BLUEPRINT.md §2.1
"""
from __future__ import annotations

import logging
import json
import pickle
import time as _time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# 模型持久化路径
_MODEL_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "ml_models"
_MODEL_DIR.mkdir(parents=True, exist_ok=True)

# ── 归因数据路径 ──
_ATTRIBUTION_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "data" / "charts" / "factor_attribution.json"
)

# ── 因子名注册 ──
FACTOR_NAME = "xgb_dir"


# ═══════════════════════════════════════════════════════════
# Attribution 反哺: 从实盘盈亏数据计算训练样本权重
# ═══════════════════════════════════════════════════════════

def _load_attribution_stats() -> dict[str, dict]:
    """加载因子归因快照 (factor_attribution.json)。

    Returns:
        {因子名: {composite_sharpe_score, win_rate, avg_mc, ...}}
        文件不存在或不可用时返回空 dict。
    """
    if not _ATTRIBUTION_PATH.exists():
        return {}
    try:
        return json.loads(_ATTRIBUTION_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.debug("[xgb_dir] failed to load attribution stats")
        return {}


def _compute_bar_weights(
    factor_values: dict[str, np.ndarray],
    attribution_stats: dict[str, dict],
    n_bars: int,
) -> np.ndarray | None:
    """从 attribution Sharpe × 因子信号强度计算 per-bar sample_weight。

    核心逻辑:
        weight[t] = 1 + mean(quality_i × |z_i[t]|)
        quality_i = tanh(composite_sharpe_score_i / 3) ∈ (-1, +1)
        z_i[t] = z-score(factor_i[t])  窗口内标准化

    含义:
        - Sharpe 高(赚钱)的因子在 bar t 有极端信号 → weight > 1 (更重视该 bar)
        - Sharpe 低(亏钱)的因子在 bar t 有极端信号 → weight < 1 (轻视该 bar)
        - 否则 weight ≈ 1 (中性)

    Returns:
        (n_bars,) float32 array, clamped to [0.3, 3.0].
        None if no useful attribution data.
    """
    if not attribution_stats:
        return None

    num = np.zeros(n_bars, dtype=np.float64)
    den = np.zeros(n_bars, dtype=np.float64)

    for name, vals in factor_values.items():
        stats = attribution_stats.get(name)
        if stats is None:
            continue
        sharpe = stats.get("composite_sharpe_score", 0) or 0
        if abs(sharpe) < 0.01:
            continue
        # Sharpe → quality score ∈ (-1, +1)
        quality = float(np.tanh(sharpe / 3.0))

        arr = np.asarray(vals, dtype=np.float64)
        arr[np.isinf(arr) | np.isnan(arr)] = 0.0

        # 窗口内 z-score
        mean = float(np.mean(arr))
        std = float(np.std(arr))
        if std < 1e-10:
            continue
        arr_z = (arr - mean) / std

        num += quality * np.abs(arr_z)
        den += 1.0

    valid = den > 0.5
    weights = np.ones(n_bars, dtype=np.float64)
    weights[valid] = 1.0 + num[valid] / den[valid]
    np.clip(weights, 0.3, 3.0, out=weights)
    return weights


def _build_features(df: pd.DataFrame, factor_values: dict[str, np.ndarray], required_factor_order: list[str] | None = None) -> np.ndarray:
    """从预计算的因子值构造特征矩阵 X。

    Args:
        df: OHLCV DataFrame (含 'time' 列用于提取时间特征)
        factor_values: {name: np.ndarray} 全因子值
        required_factor_order: 若提供, 按此顺序取因子,
            缺失或全 NaN 者填 0.0 保持列数稳定, 用于推理.

    Returns:
        X: (n_bars, n_features) float64 数组
    """
    n = len(df)
    features: list[np.ndarray] = []

    # 1. 全量因子值, 替换 NaN → 0 而非跳过 (保证列数稳定)
    if required_factor_order is not None:
        for name in required_factor_order:
            arr = factor_values.get(name)
            if arr is not None:
                arr = np.asarray(arr, dtype=float)
            if arr is None or len(arr) != n:
                arr = np.zeros(n, dtype=float)
            arr[np.isinf(arr)] = 0.0
            arr = np.nan_to_num(arr, nan=0.0)
            features.append(arr)
    else:
        for name, vals in factor_values.items():
            arr = np.asarray(vals, dtype=float)
            arr[np.isinf(arr)] = 0.0
            arr = np.nan_to_num(arr, nan=0.0)
            if len(arr) == n:
                features.append(arr)

    # 2. 时间特征
    if "time" in df.columns:
        ts = df["time"]
        # 兼容 numeric 时间戳 (epoch seconds, float 或 int) 和 datetime 列
        if np.issubdtype(ts.dtype, np.number) and not np.issubdtype(ts.dtype, np.datetime64):
            ts = pd.to_datetime(ts, unit="s")
        # pd.to_datetime(Series, unit='s') 返 DatetimeIndex (pandas<2) 或 Series (pandas>=2)
        if isinstance(ts, pd.DatetimeIndex):
            ts_arr = ts
        else:
            ts_arr = ts.dt
        # hour_utc (0-23)
        hour = ts_arr.hour.values
        hour_norm = hour.astype(float) / 23.0
        features.append(hour_norm)
        # day_of_week (0=Mon, 6=Sun)
        dow = ts_arr.weekday.values
        dow_norm = dow.astype(float) / 6.0
        features.append(dow_norm)

    # Stack: (n, n_features)
    X = np.column_stack(features)
    return X


def _build_labels(df: pd.DataFrame) -> np.ndarray:
    """构造标签: sign(close[t+1] - close[t]) → binary。

    y[t] = 1 if close[t+1] > close[t], else 0.
    最后一个 bar 无未来数据 → NaN.
    """
    close = df["close"].values.astype(float)
    y = np.full(len(close), np.nan)
    y[:-1] = (close[1:] > close[:-1]).astype(float)
    return y


def train_direction_predictor(
    symbol: str = "XAUUSD+",
    timeframe: str = "M5",
    n_bars: int = 20000,
    n_estimators: int = 200,
    max_depth: int = 4,
    learning_rate: float = 0.05,
    min_oos_acc: float = 0.505,
) -> Optional[dict]:
    """训练 XGBoost 方向预测器。

    1. 加载最近 n_bars 根 bar
    2. 预计算所有因子值
    3. PurgedWalkForward (5-fold) 验证
    4. 若 OOS accuracy 通过阈值, 全量训练并保存模型
    5. 注册为 factor_registry 因子

    Returns:
        dict with training results, or None if skipped.
    """
    from data.store import DataStore
    from alpha.registry import factor_registry

    t0 = _time.time()

    # 1. 加载数据
    store = DataStore("data/ctrader_data.duckdb")
    df = store.load_bars(symbol, timeframe, limit=n_bars)
    if df.empty or len(df) < 1000:
        logger.warning("[xgb_dir] insufficient bars: %d", len(df))
        return None
    logger.info("[xgb_dir] loaded %d bars", len(df))

    # 2. 预计算因子
    factor_vals: dict[str, np.ndarray] = {}
    for name in factor_registry.list():
        try:
            fn = factor_registry.get(name)
            if fn is None:
                continue
            vals = fn(df)
            arr = np.asarray(vals, dtype=float)
            arr[np.isinf(arr)] = np.nan
            factor_vals[name] = arr
        except Exception:
            continue
    n_factors = len(factor_vals)
    logger.info("[xgb_dir] computed %d factors in %.1fs", n_factors, _time.time() - t0)

    # ── Attribution 反哺: 加载实盘归因统计, 计算 per-bar sample_weight ──
    attribution_stats = _load_attribution_stats()
    sample_weight = _compute_bar_weights(factor_vals, attribution_stats, len(df))
    if sample_weight is not None:
        n_with_data = sum(1 for fn in factor_vals if fn in attribution_stats)
        logger.info(
            "[xgb_dir] attribution feedback: %d/%d factors with stats, "
            "sample_weight range [%.3f, %.3f]",
            n_with_data, n_factors,
            float(sample_weight.min()), float(sample_weight.max()),
        )
    else:
        logger.info("[xgb_dir] attribution feedback: no stats available (cold start)")

    # 3. 构造 X, y
    X = _build_features(df, factor_vals)
    # 备份实际用的特征顺序 (推理时用 required_factor_order 确保列数稳定)
    _trained_factor_order = list(factor_vals.keys())
    y = _build_labels(df)

    # 4. 去 NaN
    mask = ~np.isnan(X).any(axis=1) & ~np.isnan(y)
    X_valid = X[mask]
    y_valid = y[mask].astype(int)
    # 对齐 sample_weight (与 X_valid/y_valid 同索引)
    sw_valid: np.ndarray | None = None
    if sample_weight is not None:
        sw_valid = sample_weight[mask].astype(np.float64)
    n_valid = len(X_valid)
    if n_valid < 2000:
        logger.warning("[xgb_dir] only %d valid samples, skip", n_valid)
        return None

    # 5. PurgedWalkForward 验证
    try:
        from xgboost import XGBClassifier
    except ImportError:
        logger.warning("xgboost not installed, skip")
        return None

    from alpha.evaluation.evaluation_context import EvaluationContext
    from alpha.evaluation.purged_walkforward import PurgedWalkForward

    ctx = EvaluationContext(train_bars=5000, test_bars=1000, embargo_bars=50, purge_bars=30)
    pwf = PurgedWalkForward(ctx, n_folds=5)

    fold_scores: list[float] = []
    for fold in pwf.folds(n_total=n_valid):
        tr_idx = fold.train_indices
        te_idx = fold.test_indices
        if len(np.unique(y_valid[tr_idx])) < 2 or len(np.unique(y_valid[te_idx])) < 2:
            continue

        fold_kw = {}
        if sw_valid is not None:
            fold_kw["sample_weight"] = sw_valid[tr_idx]
        model = XGBClassifier(
            n_estimators=100, max_depth=max_depth, learning_rate=learning_rate,
            subsample=0.8, colsample_bytree=0.8,
            random_state=42, n_jobs=2, eval_metric="logloss",
        )
        model.fit(X_valid[tr_idx], y_valid[tr_idx], verbose=False, **fold_kw)
        acc = model.score(X_valid[te_idx], y_valid[te_idx])
        fold_scores.append(acc)

    if not fold_scores:
        logger.warning("[xgb_dir] no valid folds")
        return None

    avg_oos_acc = float(np.mean(fold_scores))
    std_oos_acc = float(np.std(fold_scores))

    # 6. Bootstrap CI 检查
    try:
        from alpha.evaluation.bootstrap_ci import BootstrapCI
        ci = BootstrapCI().ci_ic(np.array(fold_scores), np.zeros(len(fold_scores)))
        ci_lower = ci.get("lower", 0)
    except Exception:
        ci_lower = avg_oos_acc - 2 * std_oos_acc / np.sqrt(len(fold_scores))

    # 7. 判断是否注册
    if avg_oos_acc > min_oos_acc and ci_lower > 0.5:
        # 全量训练
        model_full = XGBClassifier(
            n_estimators=n_estimators, max_depth=max_depth,
            learning_rate=learning_rate,
            subsample=0.8, colsample_bytree=0.8,
            random_state=42, n_jobs=2, eval_metric="logloss",
        )
        full_kw = {}
        if sw_valid is not None:
            full_kw["sample_weight"] = sw_valid
        model_full.fit(X_valid, y_valid, verbose=False, **full_kw)

        # 保存模型
        model_path = _MODEL_DIR / f"xgb_dir_{symbol}_{timeframe}.pkl"
        with open(model_path, "wb") as f:
            pickle.dump({
                "model": model_full,
                "factor_names": list(factor_vals.keys()),
                "trained_at": datetime.now(timezone.utc).isoformat(),
                "n_samples": n_valid,
                "oos_acc": avg_oos_acc,
                "n_factors": X.shape[1],
                "trained_factor_order": _trained_factor_order,
            }, f)

        # 注册为因子
        _register_as_factor(model_path, symbol, timeframe)

        result = {
            "status": "registered",
            "oos_acc": round(avg_oos_acc, 4),
            "std_acc": round(std_oos_acc, 4),
            "ci_lower": round(ci_lower, 4),
            "n_samples": n_valid,
            "n_factors": n_factors,
            "n_folds": len(fold_scores),
            "model_path": str(model_path),
        }
        logger.info("[xgb_dir] registered: OOS acc=%.4f ± %.4f, CI [%.4f, ...]",
                    avg_oos_acc, std_oos_acc, ci_lower)
    else:
        result = {
            "status": "skipped",
            "reason": f"OOS acc={avg_oos_acc:.4f} < {min_oos_acc} or CI lower={ci_lower:.4f} <= 0.5",
            "oos_acc": round(avg_oos_acc, 4),
            "std_acc": round(std_oos_acc, 4),
            "ci_lower": round(ci_lower, 4),
            "n_samples": n_valid,
            "n_factors": n_factors,
        }
        logger.info("[xgb_dir] skipped: %s", result["reason"])

    return result


def _register_as_factor(model_path: Path, symbol: str, timeframe: str):
    """将 ML 模型注册为 factor_registry 因子。"""
    from alpha.registry import factor_registry

    def predict_fn(df: pd.DataFrame) -> np.ndarray:
        """因子函数: 加载模型, 预测方向信号 ∈ [-1, +1]。

        signal = 2 * proba - 1
        proba=0.5 → signal=0 (中性)
        proba=0.8 → signal=+0.6 (看多)
        proba=0.2 → signal=-0.6 (看空)
        """
        if not model_path.exists():
            return np.full(len(df), np.nan)

        with open(model_path, "rb") as f:
            bundle = pickle.load(f)
        model = bundle["model"]
        factor_names = bundle["factor_names"]

        # 重新计算特征
        from alpha.registry import factor_registry as _reg
        factor_vals: dict[str, np.ndarray] = {}
        for name in factor_names:
            if name in _reg._factors:
                try:
                    fn = _reg._factors[name]
                    vals = fn(df)
                    arr = np.asarray(vals, dtype=float)
                    arr[np.isinf(arr)] = np.nan
                    factor_vals[name] = arr
                except Exception:
                    factor_vals[name] = np.full(len(df), np.nan)
            else:
                factor_vals[name] = np.full(len(df), np.nan)

        X = _build_features(df, factor_vals, required_factor_order=bundle.get("trained_factor_order"))

        # 处理 NaN + 特征数兼容 (模型可能用旧版 _build_features 训练, 跳过全 NaN 列)
        n_expected = model.n_features_in_
        if X.shape[1] < n_expected:
            X = np.column_stack([X, np.zeros((X.shape[0], n_expected - X.shape[1]))])
        elif X.shape[1] > n_expected:
            X = X[:, :n_expected]

        mask = ~np.isnan(X).any(axis=1)
        proba = np.full(len(df), 0.5)
        if mask.sum() > 0:
            proba[mask] = model.predict_proba(X[mask])[:, 1]

        # 映射到 [-1, +1]
        signal = 2.0 * proba - 1.0
        return signal

    # 注册 (覆盖已有的)
    factor_registry._factors[FACTOR_NAME] = predict_fn
    logger.info("[xgb_dir] factor '%s' registered", FACTOR_NAME)


def predict(df: pd.DataFrame) -> np.ndarray:
    """公开预测接口 (用于外部调用和测试)。"""
    model_path = _MODEL_DIR / "xgb_dir_XAUUSD+_M5.pkl"
    if not model_path.exists():
        return np.full(len(df), np.nan)

    with open(model_path, "rb") as f:
        bundle = pickle.load(f)

    from alpha.registry import factor_registry
    factor_names = bundle.get("factor_names", factor_registry.list())
    factor_vals: dict[str, np.ndarray] = {}
    for name in factor_names:
        try:
            fn = factor_registry.get(name)
            if fn is None:
                continue
            vals = fn(df)
            arr = np.asarray(vals, dtype=float)
            arr[np.isinf(arr)] = np.nan
            factor_vals[name] = arr
        except Exception:
            continue

    model = bundle["model"]
    X = _build_features(df, factor_vals, required_factor_order=bundle.get("trained_factor_order"))

    # 特征数兼容 (旧版 _build_features 跳过全 NaN 列)
    n_expected = model.n_features_in_
    if X.shape[1] < n_expected:
        X = np.column_stack([X, np.zeros((X.shape[0], n_expected - X.shape[1]))])
    elif X.shape[1] > n_expected:
        X = X[:, :n_expected]

    mask = ~np.isnan(X).any(axis=1)
    proba = np.full(len(df), 0.5)
    if mask.sum() > 0:
        proba[mask] = model.predict_proba(X[mask])[:, 1]
    return 2.0 * proba - 1.0
