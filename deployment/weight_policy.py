"""deployment/weight_policy.py — 动态因子权重分配器 (Phase 2.3, 2026-06-12)

根据因子健康评分, 动态分配各因子的权重.
支持 3 种策略:
- linear:    权重线性映射到 [min_weight, max_weight], 不满足下界的裁 0
- softmax:   健康分过阈值后用 softmax 归一化 (拉开差距)
- threshold: 健康分 3 档: >=70 满权, >=40 半权, <40 裁 0

输出经 max_single_weight=0.5 上限钳制 + 归一化至 1.0.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

logger = logging.getLogger(__name__)

# ── 默认配置 ──────────────────────────────────────────────────────────
DEFAULT_MAX_SINGLE_WEIGHT = 0.5       # 单因子上限
DEFAULT_MIN_WEIGHT = 0.01             # linear 下界
HEALTHY_THRESHOLD = 70.0              # threshold 策略: 满权线
WATCH_THRESHOLD = 40.0                # threshold 策略: 半权线


@dataclass
class WeightConfig:
    """权重策略配置"""
    policy: str = "linear"             # linear | softmax | threshold
    max_single_weight: float = DEFAULT_MAX_SINGLE_WEIGHT
    min_weight: float = DEFAULT_MIN_WEIGHT
    health_threshold: float = HEALTHY_THRESHOLD
    watch_threshold: float = WATCH_THRESHOLD
    # softmax 温度参数 (越大越均等, 越小越拉开)
    softmax_temperature: float = 1.0


# ── 策略类型别名 ───────────────────────────────────────────────────
PolicyFn = Callable[[np.ndarray, WeightConfig], np.ndarray]


class WeightPolicy:
    """
    动态因子权重分配器.

    用法:
        wp = WeightPolicy()
        scores = {"factor_a": 85.0, "factor_b": 45.0, "factor_c": 20.0}
        weights = wp.compute_weights(scores)
        # -> {"factor_a": 0.5, "factor_b": 0.3, ...}  (根据策略)
    """

    def __init__(self, config: WeightConfig | None = None):
        self.config = config or WeightConfig()
        # 注册策略函数
        self._policies: dict[str, PolicyFn] = {
            "linear": _linear_weights,
            "softmax": _softmax_weights,
            "threshold": _threshold_weights,
        }

    # ── 主入口 ─────────────────────────────────────────────────────

    def compute_weights(self,
                        factor_health_scores: dict[str, float]
                        ) -> dict[str, float]:
        """
        根据因子健康分计算每个因子的权重.

        Args:
            factor_health_scores: {因子名: 健康分 (0-100)}

        Returns:
            {因子名: 权重 (0-1, 总和=1)} — 空输入返回 {}.
        """
        if not factor_health_scores:
            return {}

        names = list(factor_health_scores.keys())
        scores = np.array([factor_health_scores[n] for n in names], dtype=np.float64)

        # 选择策略
        policy_fn = self._policies.get(self.config.policy, _linear_weights)
        raw = policy_fn(scores, self.config)

        # 上限钳制 — 迭代 cap + redistribute
        clamped = _apply_upper_bound(raw, self.config.max_single_weight)

        return {name: round(float(w), 6) for name, w in zip(names, clamped)}

    def register_policy(self, name: str, fn: PolicyFn) -> None:
        """注册自定义策略函数"""
        if name in self._policies:
            logger.warning(f"[WeightPolicy] 策略 '{name}' 已存在, 覆盖")
        self._policies[name] = fn

    @property
    def available_policies(self) -> list[str]:
        return list(self._policies.keys())


# ── 内置策略实现 ────────────────────────────────────────────────────


def _linear_weights(scores: np.ndarray, config: WeightConfig) -> np.ndarray:
    """
    linear 策略: 得分线性映射到 [min_weight, 1.0] / 归一化.
    < watch_threshold → 0.
    """
    n = len(scores)
    if n == 0:
        return np.array([], dtype=np.float64)

    # 低于阈值的裁掉
    mask = scores >= config.watch_threshold
    if not np.any(mask):
        # 全低于 → 均匀分
        return np.full(n, 1.0 / n)

    weights = np.zeros(n, dtype=np.float64)
    sub = scores[mask]
    # 线性映射到 [min_weight, 1.0]
    sub_min = float(np.min(sub))
    sub_max = float(np.max(sub))
    if sub_max > sub_min:
        scaled = config.min_weight + (1.0 - config.min_weight) * (sub - sub_min) / (sub_max - sub_min)
    else:
        scaled = np.full_like(sub, 1.0)
    weights[mask] = scaled
    return weights


def _softmax_weights(scores: np.ndarray, config: WeightConfig) -> np.ndarray:
    """
    softmax 策略: 仅对 >= watch_threshold 的因子做 softmax,
    低于阈值的权重为 0.
    """
    n = len(scores)
    if n == 0:
        return np.array([], dtype=np.float64)

    mask = scores >= config.watch_threshold
    if not np.any(mask):
        return np.full(n, 1.0 / n)

    weights = np.zeros(n, dtype=np.float64)
    sub = scores[mask]
    # softmax with temperature
    scaled = sub / config.softmax_temperature
    exp_s = np.exp(scaled - np.max(scaled))  # 数值稳定
    soft = exp_s / (np.sum(exp_s) + 1e-12)
    weights[mask] = soft
    return weights


def _threshold_weights(scores: np.ndarray, config: WeightConfig) -> np.ndarray:
    """
    threshold 策略:
        >= health_threshold (70): 满权 = 1.0
        >= watch_threshold (40):  半权 = 0.5
        < watch_threshold:        0
    最终归一化.
    """
    n = len(scores)
    if n == 0:
        return np.array([], dtype=np.float64)

    weights = np.zeros(n, dtype=np.float64)
    weights[scores >= config.health_threshold] = 1.0
    between = (scores >= config.watch_threshold) & (scores < config.health_threshold)
    weights[between] = 0.5
    return weights


def _apply_upper_bound(weights: np.ndarray, max_weight: float) -> np.ndarray:
    """
    迭代上限钳制: 将超过 max_weight 的权重裁到 max_weight,
    多余预算按比例重新分配给未超限的因子.
    如所有因子都已超限则均匀分配.
    """
    n = len(weights)
    if n == 0 or max_weight <= 0:
        return weights

    w = weights.astype(np.float64).copy()
    # 先归一化
    total = float(np.sum(w))
    if total > 0.0:
        w = w / total
    else:
        return np.full(n, 1.0 / n)

    # 迭代 cap + redistribute
    for _ in range(n + 1):  # 最多 n+1 轮
        above = w > max_weight
        if not np.any(above):
            break
        # 超限 → 裁到 max_weight
        excess = float(np.sum(w[above] - max_weight))
        w[above] = max_weight
        # 剩余预算分配给未超限且 > 0 的因子
        remaining = (~above) & (w > 0)
        total_remaining = float(np.sum(w[remaining]))
        if total_remaining > 0 and excess > 0:
            w[remaining] += excess * (w[remaining] / total_remaining)
        elif total_remaining <= 0 and excess > 0:
            # 无剩余可分配, 把 excess 平分给全部
            w[:] = max_weight
            break

    # 最终归一化 (因 redistribute 可能有余数)
    total = float(np.sum(w))
    if total > 0:
        w = w / total
    return w
