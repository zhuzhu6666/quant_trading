"""
alpha/probability_calibrator.py
================================

P0-7 校准后处理: 把模型原始预测概率矫正为接近实际胜率的概率。

方法:
  1. 桶级查找 (isotonic-lite): 用 meta_learner_monitor 输出的 6-bin 校准表
     直接替换 avg_pred → actual_wr
  2. Platt 缩放 (logistic regression): 二参 a·logit(p) + b, 找最佳拟合
  3. 集成: 默认桶级, 桶空时回退到 Platt, 都不行就原值

用法:
    calibrator = ProbabilityCalibrator.load("data/charts/meta_learner_report.txt")
    p_calibrated = calibrator.calibrate(0.65)  # 假设模型说 65% 概率涨, 矫正后 ≈ 实际胜率

跟 scoring 系统集成:
    WeightedScorer.score(signals) 之前, 把每个 signal 的 confidence 用 calibrator.calibrate() 矫正
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)


class ProbabilityCalibrator:
    """
    概率校准器: 把模型原始概率 p 矫正为更接近实际胜率的 p_calibrated.

    三种方法:
      - bucket: 桶级查找 (isotonic 简化版)
      - platt: Platt scaling (二参 logistic)
      - identity: 原值返回
    """

    def __init__(self, method: str = "bucket"):
        self.method = method
        # bucket 方法: [(lo, hi, actual_wr), ...]
        self.buckets: list[tuple[float, float, float]] = []
        # platt 方法: (a, b)
        self.platt_a: float = 1.0
        self.platt_b: float = 0.0

    @classmethod
    def from_calibration_table(cls, table: list[dict],
                               method: str = "bucket") -> "ProbabilityCalibrator":
        """从 meta_learner_monitor 的 calibration_table() 结果构造"""
        cal = cls(method=method)
        for row in table:
            # row["bin"] 形如 "[0.5, 0.6)"
            lo = float(row["bin"].split(",")[0].lstrip("[").strip())
            hi = float(row["bin"].split(",")[1].rstrip(")").strip())
            wr = float(row["actual_wr"])
            cal.buckets.append((lo, hi, wr))
        # 桶按 lo 升序
        cal.buckets.sort()
        return cal

    @classmethod
    def from_platt(cls, a: float, b: float) -> "ProbabilityCalibrator":
        """从 Platt 参数构造 (a·logit(p) + b → sigmoid)"""
        cal = cls(method="platt")
        cal.platt_a = a
        cal.platt_b = b
        return cal

    @classmethod
    def identity(cls) -> "ProbabilityCalibrator":
        return cls(method="identity")

    def calibrate(self, p: float) -> float:
        """单值校准"""
        if p is None or np.isnan(p):
            return p
        p = max(0.001, min(0.999, float(p)))  # 边界保护
        if self.method == "identity" or not self.buckets:
            return p
        if self.method == "platt":
            return self._calibrate_platt(p)
        if self.method == "bucket":
            return self._calibrate_bucket(p)
        return p

    def calibrate_array(self, probs: np.ndarray) -> np.ndarray:
        """数组校准 (向量化)"""
        if self.method == "identity" or not self.buckets:
            return probs
        if self.method == "platt":
            return self._calibrate_platt_array(probs)
        return np.array([self._calibrate_bucket(p) for p in probs])

    def _calibrate_bucket(self, p: float) -> float:
        for lo, hi, wr in self.buckets:
            if lo <= p < hi or (hi == 1.0 and p == hi):
                return wr
        # 兜底: 用最近桶
        if p < self.buckets[0][0]:
            return self.buckets[0][2]
        return self.buckets[-1][2]

    def _calibrate_platt(self, p: float) -> float:
        logit = np.log(p / (1 - p))
        new_logit = self.platt_a * logit + self.platt_b
        return float(1.0 / (1.0 + np.exp(-new_logit)))

    def _calibrate_platt_array(self, probs: np.ndarray) -> np.ndarray:
        probs = np.clip(probs, 0.001, 0.999)
        logit = np.log(probs / (1 - probs))
        new_logit = self.platt_a * logit + self.platt_b
        return 1.0 / (1.0 + np.exp(-new_logit))

    def fit_platt(self, probs: np.ndarray, y_true: np.ndarray):
        """
        用 OOS 数据拟合 Platt 参数 (二参 logistic).
        minimization: minimize NLL(p_cal · y · log + (1-p_cal) · log(1-p_cal))
        """
        from scipy.optimize import minimize
        probs = np.clip(probs, 0.001, 0.999)
        logit = np.log(probs / (1 - probs))

        def nll(params):
            a, b = params
            new_logit = a * logit + b
            p_cal = 1.0 / (1.0 + np.exp(-new_logit))
            p_cal = np.clip(p_cal, 1e-9, 1 - 1e-9)
            return -np.mean(y_true * np.log(p_cal) + (1 - y_true) * np.log(1 - p_cal))

        # 初始: a=1, b=0 (恒等)
        res = minimize(nll, x0=[1.0, 0.0], method="Nelder-Mead",
                       options={"xatol": 1e-4, "fatol": 1e-4})
        self.platt_a, self.platt_b = res.x
        self.method = "platt"
        logger.info(f"Platt fit: a={self.platt_a:.4f}, b={self.platt_b:.4f}, NLL={res.fun:.4f}")

    def to_dict(self) -> dict:
        return {
            "method": self.method,
            "buckets": self.buckets,
            "platt_a": self.platt_a,
            "platt_b": self.platt_b,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ProbabilityCalibrator":
        cal = cls(method=d.get("method", "bucket"))
        cal.buckets = [tuple(b) for b in d.get("buckets", [])]
        cal.platt_a = d.get("platt_a", 1.0)
        cal.platt_b = d.get("platt_b", 0.0)
        return cal

    def save(self, path: str):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)

    @classmethod
    def load(cls, path: str) -> "ProbabilityCalibrator":
        p = Path(path)
        if not p.exists():
            logger.warning(f"Calibrator not found at {path}, returning identity")
            return cls.identity()
        with open(p, "r", encoding="utf-8") as f:
            return cls.from_dict(json.load(f))

    @classmethod
    def fit_from_predictions(cls, probs, y_true, n_buckets: int = 8,
                             method: str = "bucket") -> "ProbabilityCalibrator":
        """
        从一组 OOS 预测概率 + 真实标签拟合 calibrator. 供 walkforward / retrain 调用.
        桶级 (bucket): 把 [0,1] 分成 n_buckets 段, 每段用该段实际命中率校准.
        """
        import numpy as np
        probs = np.asarray(probs, dtype=float)
        y_true = np.asarray(y_true, dtype=int)
        mask = ~np.isnan(probs) & ~np.isnan(y_true)
        probs, y_true = probs[mask], y_true[mask]
        if len(probs) < n_buckets * 5:
            # 样本不够, 回退 identity
            logger.warning(f"fit_from_predictions: only {len(probs)} samples, "
                           f"need >={n_buckets * 5}, returning identity")
            return cls.identity()
        cal = cls(method=method)
        # 桶边界
        edges = np.linspace(0.0, 1.0, n_buckets + 1)
        buckets = []   # [lo, hi, wr] 3-tuple (与 _calibrate_bucket 兼容)
        bucket_n = []   # 单独记录每桶样本数 (用于 audit log)
        for i in range(n_buckets):
            lo, hi = float(edges[i]), float(edges[i + 1])
            in_bucket = (probs >= lo) & (probs < hi if i < n_buckets - 1 else probs <= hi)
            n_in = int(in_bucket.sum())
            if n_in < 5:
                fallback = float(y_true.mean()) if len(y_true) > 0 else 0.5
                buckets.append([lo, hi, round(fallback, 4)])
                bucket_n.append(n_in)
                continue
            emp_rate = float(y_true[in_bucket].mean())
            buckets.append([lo, hi, round(emp_rate, 4)])
            bucket_n.append(n_in)
            logger.debug(f"  bucket [{lo:.3f},{hi:.3f}] n={n_in} emp={emp_rate:.4f}")
        cal.buckets = buckets
        cal.method = "bucket"
        # 把 n 计数存到 meta 字段 (to_dict 不动, 单独属性供 audit)
        cal._bucket_n = bucket_n
        cal._fit_n_samples = int(len(probs))
        cal._fit_n_pos = int(y_true.sum())
        logger.info(f"fit_from_predictions: {len(probs)} samples, "
                    f"{n_buckets} buckets, method=bucket, "
                    f"n_per_bucket={bucket_n}")
        return cal


# ── 跟 WeightedScorer 集成 ─────────────────────────────

def calibrate_signal_confidence(signal, calibrator: ProbabilityCalibrator):
    """
    就地修改 signal.confidence 为校准后的概率.
    signal: strategy.base.Signal 对象
    """
    if signal.confidence is None or np.isnan(signal.confidence):
        return signal
    raw = float(signal.confidence)
    cal = calibrator.calibrate(raw)
    signal.confidence = cal
    if signal.meta is None:
        signal.meta = {}
    signal.meta["confidence_raw"] = round(raw, 4)
    signal.meta["confidence_calibrated"] = round(cal, 4)
    return signal
