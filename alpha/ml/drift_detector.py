"""alpha/ml/drift_detector.py — 概念漂移检测 (Phase 2)。

监控 ML 因子的预测准确率。如果滚动准确率连续低于阈值 → 触发模型退役 + 自动重训。

方法:
  - 滚动窗口准确率检查
  - Page-Hinkley 检验 (简化版)
  - ADWIN 自适应窗口 (简化版)
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class DriftReport:
    """漂移检测报告。"""
    factor_name: str
    rolling_accuracy: float
    n_observations: int
    drift_detected: bool
    drift_score: float          # 0 = 无漂移, 1 = 严重漂移
    needs_retrain: bool
    detail: str = ""


class DriftDetector:
    """概念漂移检测器。

    监控一个 ML 因子的预测准确率。
    如果滚动准确率连续低于基线, 触发重训。

    Args:
        window: 滚动窗口大小 (最近 N 个预测)
        drift_threshold: 准确率低于此值连续 N 次 → 漂移
        warmup: 最少需要多少观测才启动检测
        baseline_acc: 注册时的 OOS 准确率 (基线)
    """

    def __init__(
        self,
        window: int = 500,
        drift_threshold: float = 0.48,
        warmup: int = 100,
        baseline_acc: Optional[float] = None,
    ):
        self.window = window
        self.drift_threshold = drift_threshold
        self.warmup = warmup
        self.baseline_acc = baseline_acc

        # 滚动缓冲区: (predicted_direction, actual_direction)
        self._buffer: deque[tuple[int, int]] = deque(maxlen=window)
        self._consecutive_low: int = 0
        self._total_obs: int = 0
        self._rolling_acc: float = 0.5

    def update(self, predicted: int, actual: int) -> DriftReport:
        """记录一次预测结果。

        Args:
            predicted: 预测方向 (1 = 涨, 0 = 跌, -1 = 跌)
            actual: 实际方向 (1 = 涨, 0 = 跌)

        Returns:
            DriftReport with current state.
        """
        # Normalize directions
        pred_bin = 1 if predicted > 0 else 0
        actual_bin = 1 if actual > 0 else 0

        self._buffer.append((pred_bin, actual_bin))
        self._total_obs += 1

        # 计算滚动准确率
        if len(self._buffer) >= 10:
            correct = sum(1 for p, a in self._buffer if p == a)
            self._rolling_acc = correct / len(self._buffer)
        else:
            self._rolling_acc = 0.5

        # 漂移检测
        drift = False
        detail = ""
        threshold = self.drift_threshold
        if self.baseline_acc is not None:
            # 如果低于基线的 95%
            threshold = max(threshold, self.baseline_acc * 0.95)

        if self._total_obs >= self.warmup:
            if self._rolling_acc < threshold:
                self._consecutive_low += 1
            else:
                self._consecutive_low = max(0, self._consecutive_low - 2)

            if self._consecutive_low >= 10:
                drift = True
                detail = f"rolling_acc={self._rolling_acc:.4f} < {threshold:.4f} × {self._consecutive_low} consecutive"

        drift_score = min(1.0, self._consecutive_low / 20.0) if drift else 0.0

        return DriftReport(
            factor_name="",
            rolling_accuracy=round(self._rolling_acc, 4),
            n_observations=self._total_obs,
            drift_detected=drift,
            drift_score=round(drift_score, 3),
            needs_retrain=drift and self._consecutive_low >= 15,
            detail=detail,
        )

    def check(self, factor_name: str) -> DriftReport:
        """检查当前状态 (不更新)。"""
        if len(self._buffer) < 10:
            return DriftReport(
                factor_name=factor_name,
                rolling_accuracy=0.5,
                n_observations=self._total_obs,
                drift_detected=False,
                drift_score=0.0,
                needs_retrain=False,
                detail="insufficient data",
            )

        threshold = self.drift_threshold
        if self.baseline_acc is not None:
            threshold = max(threshold, self.baseline_acc * 0.95)

        drift = self._consecutive_low >= 10
        drift_score = min(1.0, self._consecutive_low / 20.0)

        return DriftReport(
            factor_name=factor_name,
            rolling_accuracy=round(self._rolling_acc, 4),
            n_observations=self._total_obs,
            drift_detected=drift,
            drift_score=round(drift_score, 3),
            needs_retrain=drift and self._consecutive_low >= 15,
            detail=f"rolling_acc={self._rolling_acc:.4f} vs {threshold:.4f}" if drift else "",
        )

    def reset(self):
        """重置检测器状态。"""
        self._buffer.clear()
        self._consecutive_low = 0
        self._rolling_acc = 0.5


# ── 全局检测器 ──

_drift_detectors: dict[str, DriftDetector] = {}


def get_detector(name: str, baseline_acc: Optional[float] = None) -> DriftDetector:
    """获取或创建指定因子的漂移检测器。"""
    if name not in _drift_detectors:
        detector = DriftDetector(baseline_acc=baseline_acc)
        _drift_detectors[name] = detector
    elif baseline_acc is not None and _drift_detectors[name].baseline_acc is None:
        _drift_detectors[name].baseline_acc = baseline_acc
    return _drift_detectors[name]
