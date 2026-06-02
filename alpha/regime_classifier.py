"""alpha/regime_classifier.py — Regime-aware binary classifier (sklearn LogisticRegression)

预测给定 (regime, 4 因子值) 下一根 bar 是否会上涨 > 0.5×ATR (label=1) / 下跌 < -0.5×ATR (label=0).

设计: 5 regime onehot + 4 因子数值 = 9 维特征. 训练/预测 < 10ms / 50K bar.
后期可换 XGBoost: 把 LogisticRegression 换 xgboost.XGBClassifier, 接口不变.

依赖: sklearn 1.x (已有), numpy, pickle
"""
from __future__ import annotations

import logging
import pickle
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# 5 个 regime (与 mab_router.REGIMES 一致)
REGIMES = ["TRENDING_UP", "TRENDING_DOWN", "RANGING", "HIGH_VOL", "LOW_VOL"]
# 4 因子 (与 factors/__init__.py 一致)
FACTORS = ["aroon", "cci", "mfi", "williams_r"]
N_FEATURES = len(REGIMES) + len(FACTORS)  # 9 维


class RegimeAwareClassifier:
    """Regime + 因子 → 二分类 (该开仓?)"""

    def __init__(self, model_path: str = "data/regime_classifier.pkl"):
        from sklearn.linear_model import LogisticRegression
        self.model = LogisticRegression(max_iter=1000, random_state=42)
        self.model_path = Path(model_path)
        self.model_path.parent.mkdir(parents=True, exist_ok=True)
        self._trained = False

    @staticmethod
    def _featurize(regime: str, factor_values: dict[str, float]) -> np.ndarray:
        """(regime, 4 因子值) → 9 维向量"""
        v = np.zeros(N_FEATURES)
        # regime onehot
        if regime in REGIMES:
            v[REGIMES.index(regime)] = 1.0
        else:
            v[REGIMES.index("RANGING")] = 1.0  # unknown -> RANGING
        # 因子数值 (缺失填 0)
        for i, fname in enumerate(FACTORS):
            val = factor_values.get(fname)
            if val is not None and not (isinstance(val, float) and np.isnan(val)):
                v[len(REGIMES) + i] = float(val)
        return v

    def train(self, X: list[dict], y: list[int]) -> dict:
        """
        X: [{regime, factor_values}, ...]
        y: [0/1, ...]  1=开仓信号, 0=不开

        Returns: {train_acc, n_samples, n_pos, weights, intercept}
        """
        if len(X) != len(y):
            raise ValueError(f"X/y 长度不一致: {len(X)} vs {len(y)}")
        if len(X) < 50:
            raise ValueError(f"训练样本太少 ({len(X)}), 至少 50")

        feats = np.array([self._featurize(s["regime"], s["factor_values"]) for s in X])
        labels = np.array(y, dtype=int)

        self.model.fit(feats, labels)
        self._trained = True

        train_acc = float(self.model.score(feats, labels))
        n_pos = int(labels.sum())
        n_neg = int(len(labels) - n_pos)

        result = {
            "train_acc": train_acc,
            "n_samples": len(X),
            "n_pos": n_pos,
            "n_neg": n_neg,
            "weights": {f"feat_{i}": float(w) for i, w in enumerate(self.model.coef_[0])},
            "intercept": float(self.model.intercept_[0]),
        }
        return result

    def predict(self, regime: str, factor_values: dict) -> tuple[int, float]:
        """
        Returns: (label 0/1, prob 0-1)
        """
        if not self._trained:
            raise RuntimeError("模型未训练, 先调 train() 或 load()")
        v = self._featurize(regime, factor_values).reshape(1, -1)
        prob = float(self.model.predict_proba(v)[0, 1])
        label = 1 if prob >= 0.5 else 0
        return label, prob

    def feature_names(self) -> list[str]:
        return [f"regime_{r}" for r in REGIMES] + [f"factor_{f}" for f in FACTORS]

    def feature_importance(self) -> list[tuple[str, float]]:
        if not self._trained:
            return []
        names = self.feature_names()
        weights = self.model.coef_[0]
        return sorted(zip(names, weights), key=lambda x: abs(x[1]), reverse=True)

    def save(self):
        if not self._trained:
            logger.warning("模型未训练, 保存空模型")
        with open(self.model_path, "wb") as f:
            pickle.dump({
                "model": self.model,
                "trained": self._trained,
            }, f)

    def load(self) -> bool:
        if not self.model_path.exists():
            return False
        with open(self.model_path, "rb") as f:
            data = pickle.load(f)
        self.model = data["model"]
        self._trained = data["trained"]
        return True
