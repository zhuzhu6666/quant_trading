"""execution/latency.py — LatencySimulator

模拟信号 → 订单 → 撮合之间的延迟 (ms 级), 用对数正态分布生成.

设计:
  1. 对数正态分布 (lognormal): 给定 target_mean 和 target_std,
     自动计算底层正态的 mu/sigma::
       sigma = sqrt(ln(1 + std² / mean²))
       mu    = ln(mean² / sqrt(mean² + std²))
  2. 采样后 clamp 到 [min_ms, max_ms]
  3. 所有采样记录在 _samples 中, 支持统计查询

集成: 暂独立, 后续由 PaperExecutionEngine 在 _open 前调用.
"""
from __future__ import annotations

import logging
from math import log, sqrt
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


class LatencySimulator:
    """模拟交易延迟 (ms)"""

    DEFAULT_CONFIG = {
        "mean_ms": 50,      # 平均延迟 (ms)
        "std_ms": 20,       # 标准差
        "min_ms": 1,        # 最小延迟
        "max_ms": 500,      # 最大延迟
        "seed": 42,         # 随机种子 (可复现)
    }

    def __init__(self, config: Optional[dict] = None):
        """
        Args:
            config: 覆盖默认配置的字典.
                默认值见 DEFAULT_CONFIG.
        """
        self.config = {**self.DEFAULT_CONFIG, **(config or {})}
        self._rng = np.random.default_rng(self.config["seed"])
        self._samples: list[float] = []

    # ── 内部: 对数正态参数转换 ────────────────────────

    @staticmethod
    def _lognorm_params(mean: float, std: float) -> tuple[float, float]:
        """
        从目标 lognormal 均值/标准差反推底层正态的 mu 和 sigma.

        lognormal 的均值和方差:
            E[X]     = exp(mu + sigma²/2)
            Var(X)   = (exp(sigma²) - 1) * exp(2*mu + sigma²)

        反过来:
            sigma²   = ln(1 + std² / mean²)
            mu       = ln(mean) - sigma² / 2
        """
        if mean <= 0 or std < 0:
            raise ValueError(f"mean=±0 且 std=±0 不可用: mean={mean}, std={std}")
        sigma_sq = log(1 + (std * std) / (mean * mean))
        sigma = sqrt(sigma_sq)
        mu = log(mean) - sigma_sq / 2.0
        return mu, sigma

    # ── 采样 ──────────────────────────────────────────

    def sample(self) -> float:
        """
        返回单次延迟 (ms).

        从 lognormal 采样后 clamp 到 [min_ms, max_ms],
        同时记录到 self._samples.
        """
        cfg = self.config
        mu, sigma = self._lognorm_params(cfg["mean_ms"], cfg["std_ms"])
        raw = self._rng.lognormal(mu, sigma)
        val = max(cfg["min_ms"], min(raw, cfg["max_ms"]))
        self._samples.append(val)
        return val

    def sample_batch(self, n: int) -> np.ndarray:
        """
        批量采样 n 个延迟值.

        Returns:
            ndarray, shape=(n,), 每个值已 clamp.
        """
        cfg = self.config
        mu, sigma = self._lognorm_params(cfg["mean_ms"], cfg["std_ms"])
        raw = self._rng.lognormal(mu, sigma, size=n)
        vals = np.clip(raw, cfg["min_ms"], cfg["max_ms"])
        self._samples.extend(vals.tolist())
        return vals

    # ── 状态管理 ──────────────────────────────────────

    def reset(self):
        """清空 _samples 采样历史"""
        self._samples.clear()

    @property
    def samples(self) -> list[float]:
        """所有历史采样 (ms), 只读副本"""
        return list(self._samples)

    @property
    def n_samples(self) -> int:
        return len(self._samples)

    # ── 统计 ──────────────────────────────────────────

    def stats(self) -> dict:
        """
        返回采样统计报告.

        Returns:
            {
                'n': int,           # 采样数
                'mean_ms': float,   # 均值
                'std_ms': float,    # 标准差
                'min_ms': float,    # 最小值
                'max_ms': float,    # 最大值
                'p50_ms': float,    # 中位数
                'p95_ms': float,    # 95 分位
                'p99_ms': float,    # 99 分位
            }
        """
        n = len(self._samples)
        if n == 0:
            return {
                "n": 0,
                "mean_ms": 0.0,
                "std_ms": 0.0,
                "min_ms": 0.0,
                "max_ms": 0.0,
                "p50_ms": 0.0,
                "p95_ms": 0.0,
                "p99_ms": 0.0,
            }

        arr = np.array(self._samples, dtype=np.float64)
        mean_ms = float(arr.mean())
        std_ms = float(arr.std(ddof=1)) if n > 1 else 0.0
        return {
            "n": n,
            "mean_ms": mean_ms,
            "std_ms": std_ms if not np.isnan(std_ms) else 0.0,
            "min_ms": float(arr.min()),
            "max_ms": float(arr.max()),
            "p50_ms": float(np.median(arr)),
            "p95_ms": float(np.percentile(arr, 95)),
            "p99_ms": float(np.percentile(arr, 99)),
        }
