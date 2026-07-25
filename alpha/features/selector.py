"""alpha/features/selector.py — 特征自动筛选 (Phase 3)。

从候选特征池中选出最终使用的特征集，步骤:
  1. IC 筛选: 与 forward return 的滚动相关 > 0.02
  2. 共线性约束: 与已选中因子的最大相关 < 0.7
  3. VIF < 5 (方差膨胀因子)
  4. 健康分 > 40

输出: selected_features: list[str]

设计事实源: docs/system-source-of-truth.md
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# 筛选阈值 (Phase 1 后收紧以控制过拟合, 见 §12.2)
MIN_IC = 0.02           # 最小 IC (与 forward return)
MAX_CORR = 0.5           # 最大允许共线性 (§12.2: 260特征时收紧到 0.5)
MAX_VIF = 5.0           # 最大 VIF
MIN_HEALTH_SCORE = 40.0  # 最小健康分
MAX_FEATURES = 80        # 最大选取特征数


class FeatureSelector:
    """特征自动筛选器。

    循环: 每次选 IC 最高的未选中特征 → 检查共线性 → 通过则加入。

    Args:
        min_ic: 最小 IC 阈值
        max_corr: 最大允许与已选中特征的共线性
        max_vif: 最大 VIF
        max_features: 最多选取特征数
    """

    def __init__(
        self,
        min_ic: float = MIN_IC,
        max_corr: float = MAX_CORR,
        max_vif: float = MAX_VIF,
        max_features: int = MAX_FEATURES,
    ):
        self.min_ic = min_ic
        self.max_corr = max_corr
        self.max_vif = max_vif
        self.max_features = max_features

    def select(
        self,
        feature_df: pd.DataFrame,
        forward_returns: np.ndarray,
        active_factors: Optional[list[str]] = None,
    ) -> dict:
        """从特征池中筛选最优特征子集。

        Args:
            feature_df: (n_bars, n_features) 候选特征 DataFrame
            forward_returns: (n_bars,) 远期收益 (已去 NaN)
            active_factors: 已激活的因子名列表 (其对应特征会被优先保留)

        Returns:
            {
                "selected": list[str],
                "rejected": list[str],
                "ic_scores": {name: float},
                "n_candidates": int,
                "n_selected": int,
            }
        """
        n_total = feature_df.shape[1]
        if n_total == 0:
            return {"selected": [], "rejected": [], "ic_scores": {}, "n_candidates": 0, "n_selected": 0}

        # 对齐长度
        n = min(len(feature_df), len(forward_returns))
        fwd = forward_returns[:n]

        # 1. 计算所有特征的 IC
        ic_scores: dict[str, float] = {}
        for col in feature_df.columns:
            vals = feature_df[col].values[:n].astype(float)
            mask = ~np.isnan(vals) & ~np.isnan(fwd)
            if mask.sum() < 30:
                ic_scores[col] = 0.0
                continue
            ic_scores[col] = abs(float(np.corrcoef(vals[mask], fwd[mask])[0, 1]))

        # 2. 按 IC 降序排序
        ranked = sorted(ic_scores.items(), key=lambda x: -x[1])
        selected: list[str] = []
        rejected: list[str] = []

        # 3. 预先保留激活因子 (如果特征池中有对应列)
        if active_factors:
            for name in active_factors:
                if name in feature_df.columns and name not in selected:
                    selected.append(name)

        # 4. 贪心筛选
        for name, ic in ranked:
            if len(selected) >= self.max_features:
                rejected.append(name)
                continue

            if name in selected:
                continue

            # IC 门槛
            if ic < self.min_ic:
                rejected.append(name)
                continue

            # 共线性检查
            if selected:
                sel_matrix = feature_df[selected].values[:n].astype(float)
                cand_vals = feature_df[name].values[:n].astype(float)
                mask = ~np.isnan(sel_matrix).any(axis=1) & ~np.isnan(cand_vals)
                if mask.sum() >= 10:
                    corrs = np.abs(np.corrcoef(
                        np.column_stack([sel_matrix[mask], cand_vals[mask]]).T
                    )[-1, :-1])
                    if np.max(corrs) > self.max_corr:
                        rejected.append(name)
                        continue

            # VIF 检查 (如果选中特征 >= 2)
            if len(selected) >= 2:
                sel_vals = feature_df[selected + [name]].values[:n].astype(float)
                mask = ~np.isnan(sel_vals).any(axis=1)
                if mask.sum() >= 10:
                    vif = _compute_vif(sel_vals[mask])
                    if vif > self.max_vif:
                        rejected.append(name)
                        continue

            selected.append(name)

        logger.info(
            "FeatureSelector: %d candidates → %d selected, %d rejected",
            n_total, len(selected), len(rejected),
        )
        return {
            "selected": selected,
            "rejected": rejected,
            "ic_scores": {k: round(v, 4) for k, v in ic_scores.items()},
            "n_candidates": n_total,
            "n_selected": len(selected),
        }


def _compute_vif(X: np.ndarray) -> float:
    """计算最大 VIF (方差膨胀因子)。

    VIF_j = 1 / (1 - R_j^2), 其中 R_j^2 是第 j 个变量对其他变量回归的 R^2。
    返回最大 VIF。
    """
    n_features = X.shape[1]
    if n_features < 2:
        return 1.0

    max_vif = 1.0
    for j in range(n_features):
        y = X[:, j]
        X_others = np.delete(X, j, axis=1)
        # 去 Inf/NaN
        clean_mask = ~(np.isnan(y) | np.isinf(y)
                       | np.isnan(X_others).any(axis=1)
                       | np.isinf(X_others).any(axis=1))
        y_clean = y[clean_mask]
        X_clean = X_others[clean_mask]
        if len(y_clean) < 3 or X_clean.shape[1] < 1:
            continue
        # OLS: beta = (X'X)^-1 X'y
        try:
            beta = np.linalg.lstsq(X_clean, y_clean, rcond=None)[0]
            y_pred = X_clean @ beta
            ss_res = np.sum((y_clean - y_pred) ** 2)
            ss_tot = np.sum((y_clean - np.mean(y_clean)) ** 2)
            r_squared = 1 - ss_res / ss_tot if ss_tot > 0 else 0
            vif = 1.0 / (1.0 - r_squared) if r_squared < 1.0 else 100.0
            max_vif = max(max_vif, vif)
        except np.linalg.LinAlgError:
            continue

    return float(max_vif)


def run_feature_selection(
    df: pd.DataFrame,
    forward_returns: np.ndarray,
    factor_values: Optional[dict[str, np.ndarray]] = None,
) -> dict:
    """一站式特征筛选: 衍生 → PCA → 筛选。

    Args:
        df: OHLCV DataFrame
        forward_returns: 远期收益数组
        factor_values: 已有因子值 (可选)

    Returns:
        selector.select() 输出 + pca_n_components
    """
    from alpha.features.derivatives import FeatureDeriver
    from alpha.features.compression import FeatureCompressor, register_pca_factors

    # 1. 衍生
    deriver = FeatureDeriver()
    derived = deriver.derive(df, factor_values)
    logger.info("derived %d features from %d bars", derived.shape[1], len(df))

    # 2. PCA 压缩
    compressor = FeatureCompressor()
    compressor.fit(derived)
    pca_transformed = compressor.transform(derived)
    compressor.save()

    # 注册 PCA 因子
    register_pca_factors(compressor)

    # 将 PCA 分量加入特征池
    for i in range(compressor.n_components):
        derived[f"pca_{i}"] = pca_transformed[:, i]

    # 3. 筛选
    selector = FeatureSelector()
    active_factors = list(factor_values.keys()) if factor_values else None
    result = selector.select(derived, forward_returns, active_factors)
    result["pca_n_components"] = compressor.n_components
    result["pca_variance"] = round(float(compressor.explained_variance_ratio.sum()), 4)
    result["n_derived"] = derived.shape[1] - compressor.n_components

    return result
