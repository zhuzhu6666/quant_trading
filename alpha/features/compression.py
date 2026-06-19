"""alpha/features/compression.py — PCA/KPCA 特征压缩 (Phase 3)。

将 200+ 衍生特征压缩到 10-20 维正交特征，
输出注册为因子 pca_1, pca_2, ..., pca_n。

设计文档: docs/UPGRADE_BLUEPRINT.md §3.2

§12 过拟合警告: PCA 优先于 KPCA/AE。在 20K bar 上 KPCA 的 rbf kernel
极度过拟合; 仅当 bar 积累到 100K+ 时考虑。
"""
from __future__ import annotations

import logging
import pickle
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

_MODEL_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "feature_models"
_MODEL_DIR.mkdir(parents=True, exist_ok=True)


class FeatureCompressor:
    """PCA 特征压缩器。

    将高维特征矩阵压缩到低维正交特征。
    支持保留固定方差比例或固定组件数。

    Args:
        n_components: 保留组件数 (None = 自动: 保留 80% 方差)
        variance_ratio: 方差保留比例 (默认 0.8, 仅在 n_components=None 时生效)
    """

    def __init__(
        self,
        n_components: Optional[int] = None,
        variance_ratio: float = 0.8,
    ):
        self._n_components_param = n_components
        self.variance_ratio = variance_ratio
        self._pca = None
        self._fitted_n: int = 0
        self._feature_names: list[str] = []
        self._loadings: Optional[np.ndarray] = None
        self._scaler: Optional["StandardScaler"] = None  # type: ignore[name-defined]

    def fit(self, X: pd.DataFrame) -> "FeatureCompressor":
        """在特征矩阵上拟合 PCA。

        Args:
            X: (n_bars, n_features) 特征 DataFrame
        """
        from sklearn.decomposition import PCA
        from sklearn.preprocessing import StandardScaler

        # 保存特征名
        self._feature_names = list(X.columns)

        # 去 NaN + inf (前向填充 → 均值填充)
        X_clean = X.copy()
        X_clean = X_clean.replace([np.inf, -np.inf], np.nan)
        X_clean = X_clean.ffill().bfill().fillna(X_clean.mean())
        # 丢弃残留全 NaN 列 (均值填充后仍为 NaN → 无任何有效数据)
        nan_cols = X_clean.columns[X_clean.isna().any()].tolist()
        if nan_cols:
            logger.warning("PCA: dropping %d columns with all-NaN after fill: %s",
                          len(nan_cols), nan_cols[:5])
            X_clean = X_clean.drop(columns=nan_cols)
            self._feature_names = [c for c in self._feature_names if c not in nan_cols]

        # 标准化
        self._scaler = StandardScaler()
        X_scaled = self._scaler.fit_transform(X_clean.values)

        # PCA
        if self._n_components_param is not None:
            self._pca = PCA(n_components=min(self._n_components_param, X_scaled.shape[1]))
        else:
            self._pca = PCA(n_components=self.variance_ratio)

        self._pca.fit(X_scaled)
        self._fitted_n = len(X)
        self._loadings = self._pca.components_

        logger.info(
            "PCA fitted: %d → %d components, explained variance: %.1f%% (n=%d)",
            X_scaled.shape[1],
            self._pca.n_components_,
            self._pca.explained_variance_ratio_.sum() * 100,
            self._fitted_n,
        )
        return self

    def transform(self, X: pd.DataFrame) -> np.ndarray:
        """将新数据映射到 PCA 空间。

        Args:
            X: 特征 DataFrame (列应与 fit 时一致)

        Returns:
            (n_bars, n_components) PCA 分量数组
        """
        if self._pca is None:
            raise RuntimeError("PCA not fitted. Call fit() first.")

        # 对齐列 — build dict then create DataFrame at once to avoid fragmentation
        aligned_data: dict = {}
        for col in self._feature_names:
            if col in X.columns:
                aligned_data[col] = X[col].values
            else:
                aligned_data[col] = np.full(len(X), np.nan)
        X_aligned = pd.DataFrame(aligned_data, index=X.index)

        X_clean = X_aligned.replace([np.inf, -np.inf], np.nan).ffill().bfill().fillna(0.0)
        if self._scaler is None:
            raise RuntimeError("Scaler not fitted. Call fit() first.")
        X_scaled = self._scaler.transform(X_clean.values)

        return self._pca.transform(X_scaled)

    def save(self, path: Optional[str] = None):
        """保存 PCA 模型。"""
        p = Path(path) if path else _MODEL_DIR / "pca_model.pkl"
        with open(p, "wb") as f:
            pickle.dump({
                "pca": self._pca,
                "scaler": self._scaler,
                "feature_names": self._feature_names,
                "n_components": self._pca.n_components_ if self._pca else 0,
                "fitted_n": self._fitted_n,
            }, f)
        logger.info("PCA model saved to %s", p)

    @classmethod
    def load(cls, path: Optional[str] = None) -> Optional["FeatureCompressor"]:
        """加载 PCA 模型。"""
        p = Path(path) if path else _MODEL_DIR / "pca_model.pkl"
        if not p.exists():
            return None
        with open(p, "rb") as f:
            bundle = pickle.load(f)
        compressor = cls()
        compressor._pca = bundle["pca"]
        compressor._scaler = bundle.get("scaler")  # backward-compat: old saves have None
        compressor._feature_names = bundle["feature_names"]
        compressor._fitted_n = bundle["fitted_n"]
        return compressor

    @property
    def n_components(self) -> int:
        if self._pca is None:
            return 0
        return self._pca.n_components_

    @property
    def is_fitted(self) -> bool:
        return self._pca is not None

    @property
    def explained_variance_ratio(self) -> np.ndarray:
        if self._pca is None:
            return np.array([])
        return self._pca.explained_variance_ratio_


# ── 因子注册 ──────────────────────────────────────────────

_PCA_FACTOR_PREFIX = "pca"


def register_pca_factors(compressor: FeatureCompressor):
    """将 PCA 压缩器的各分量注册为 factor_registry 因子。

    因子名: pca_0, pca_1, ..., pca_N-1
    """
    from alpha.registry import factor_registry
    from alpha.features.derivatives import FeatureDeriver

    if not compressor.is_fitted:
        logger.warning("PCA not fitted, skip factor registration")
        return

    n = compressor.n_components
    deriver = FeatureDeriver()

    for i in range(n):
        idx = i  # capture in closure

        def make_pca_fn(component_idx: int = idx):
            def pca_fn(df: pd.DataFrame) -> np.ndarray:
                try:
                    # 重新加载模型 (避免 pickle 引用问题)
                    comp = FeatureCompressor.load()
                    if comp is None:
                        return np.full(len(df), np.nan)

                    # 生成衍生特征
                    X = deriver.derive(df)
                    transformed = comp.transform(X)

                    if component_idx < transformed.shape[1]:
                        return transformed[:, component_idx]
                    return np.full(len(df), np.nan)
                except Exception:
                    return np.full(len(df), np.nan)
            return pca_fn

        factor_name = f"{_PCA_FACTOR_PREFIX}_{i}"
        factor_registry._factors[factor_name] = make_pca_fn(i)
        logger.info("registered PCA factor: %s", factor_name)
