"""alpha/factor_health.py — 因子健康评分器 (T14.1, 2026-06-02)

L1 因子生命周期第 1 步. 包装 ICTracker, 输出 0-100 健康分 + HEALTHY/WATCH/DECAYING 状态.

评分维度 (总分 100):
- mean_abs_ic (40%) — 主指标, 跟 forward returns 滚动相关
- ic_stability (20%) — IC 时间序列的稳定性 (1 - std/mean)
- regime_consistency (20%) — 跨 regime 表现一致 (这里用全期 IC 不分 regime, v2 加)
- decay_rate (10%) — 最近 25% vs 之前 25% 的 IC 比, 衡量衰减
- independence (10%) — 跟 ACTIVE 因子群的平均相关, 越低越好 (提供新信息)

v1 简化:
- regime_consistency 用全期 rolling_ic 的 abs 平均, v2 按 5 regime 分桶
- independence 用现有 22 因子群, 实际只在全量 IC 算完后跑
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field, asdict
from typing import Optional

import numpy as np
import pandas as pd

from alpha.ic_tracker import ICTracker

logger = logging.getLogger(__name__)


@dataclass
class FactorHealthStatus:
    """单因子健康状态"""
    factor: str
    score: float = 0.0
    status: str = "UNKNOWN"  # HEALTHY | WATCH | DECAYING | DEAD | UNKNOWN
    components: dict = field(default_factory=dict)
    n_obs: int = 0
    rolling_ic: float = 0.0
    n_obs_needed: int = 100

    def to_dict(self) -> dict:
        return asdict(self)


# ── 阈值配置 ────────────────────────────────────────────────────
HEALTHY_SCORE_THRESHOLD = 70.0
WATCH_SCORE_THRESHOLD = 40.0
ACTIVE_IC_THRESHOLD = 0.02  # |rolling_ic| >= 此值才算 ACTIVE
MIN_N_OBS = 100             # 至少 100 个观察点才评估


class FactorHealth:
    """
    因子健康评分器 — 包装 ICTracker, 加多维评分.

    用法:
        ic_tracker = ICTracker(window=5000)
        ic_tracker.update(name, factor_values, forward_returns)
        # ... 更多因子

        health = FactorHealth(ic_tracker)
        status = health.evaluate(name)  # 单个
        # 或:
        all_status = health.evaluate_all()  # 全部
    """

    def __init__(self, ic_tracker: ICTracker,
                 active_factor_names: list[str] | None = None):
        self.ic_tracker = ic_tracker
        # 已知 ACTIVE 因子 (用于算 independence). None = 算全部
        self.active_factor_names = active_factor_names or []

    def evaluate(self, name: str) -> FactorHealthStatus:
        """评估单个因子的健康状态"""
        ic = self.ic_tracker.rolling_ic(name)
        n_obs = len(self.ic_tracker._history.get(name, []))
        if n_obs < MIN_N_OBS:
            return FactorHealthStatus(
                factor=name, score=0.0, status="UNKNOWN",
                n_obs=n_obs, rolling_ic=ic, n_obs_needed=MIN_N_OBS,
            )

        components = self._compute_components(name, ic, n_obs)
        score = sum(
            w * components[k] for k, w in {
                "mean_abs_ic": 40,
                "ic_stability": 20,
                "regime_consistency": 20,
                "decay_rate": 10,
                "independence": 10,
            }.items() if k in components
        ) / 100.0  # normalize 0-100

        if score >= HEALTHY_SCORE_THRESHOLD:
            status_str = "HEALTHY"
        elif score >= WATCH_SCORE_THRESHOLD:
            status_str = "WATCH"
        else:
            status_str = "DECAYING"

        return FactorHealthStatus(
            factor=name, score=round(score, 2), status=status_str,
            components=components, n_obs=n_obs, rolling_ic=round(ic, 4),
        )

    def evaluate_all(self) -> list[FactorHealthStatus]:
        """评估所有已记录的因子"""
        return [self.evaluate(name) for name in self.ic_tracker._history.keys()]

    def get_active_factors(self, min_score: float = HEALTHY_SCORE_THRESHOLD) -> list[str]:
        """返回 HEALTHY 因子的名字列表 (动态 filter 用途)"""
        return [
            s.factor for s in self.evaluate_all()
            if s.score >= min_score and s.n_obs >= MIN_N_OBS
        ]

    def get_decaying_factors(self) -> list[FactorHealthStatus]:
        """返回 DECAYING 因子 (待淘汰候选)"""
        return [s for s in self.evaluate_all() if s.status == "DECAYING"]

    # ── 内部: 计算每个维度的 0-100 分数 ────────────────────────────

    def _compute_components(self, name: str, ic: float, n_obs: int) -> dict:
        """计算 5 个维度的分数 (每个 0-100)"""
        history = list(self.ic_tracker._history.get(name, []))
        if not history:
            return {}

        # 从 history 重建 IC 时间序列 (跟 ICTracker.rolling_ic 一致: corrcoef)
        window = self.ic_tracker.window
        ic_series = self._build_ic_series(history, window)

        # 1. mean_abs_ic (40)
        # 把 |IC| = 0 → 0 分, |IC| = 0.1+ → 100 分 (线性)
        mean_abs = float(np.mean(np.abs(ic_series))) if len(ic_series) > 0 else 0.0
        comp_mean_abs = min(100.0, mean_abs / 0.1 * 100.0)

        # 2. ic_stability (20) — 1 - std/mean 的变异系数倒数
        if len(ic_series) > 5 and mean_abs > 1e-6:
            std = float(np.std(ic_series))
            cv = std / mean_abs  # 变异系数
            comp_stability = max(0.0, min(100.0, (1.0 - cv) * 100.0))
        else:
            comp_stability = 0.0

        # 3. regime_consistency (20) — v1 简化: 全期 |IC| 均值
        # v2: 按 5 regime 分桶, 算各 regime |IC| 均值
        comp_regime = comp_mean_abs  # 简化版跟主指标一样

        # 4. decay_rate (10) — 最近 25% vs 之前 25% 的 |IC| 比
        n = len(ic_series)
        if n >= 20:
            q1 = ic_series[:n // 4]      # 最早 25%
            q4 = ic_series[3 * n // 4:]  # 最近 25%
            mean_q1 = float(np.mean(np.abs(q1)))
            mean_q4 = float(np.mean(np.abs(q4)))
            # decay_ratio = 1.0 (稳定) → 100 分, 0 (全衰) → 0 分
            if mean_q1 > 1e-6:
                decay_ratio = mean_q4 / mean_q1
                comp_decay = max(0.0, min(100.0, decay_ratio * 100.0))
            else:
                comp_decay = 0.0 if mean_q4 < 1e-6 else 0.0
        else:
            comp_decay = 50.0  # 数据不够, 中性分

        # 5. independence (10) — 跟 ACTIVE 因子群的平均相关, 越低越好
        # v1 简化: 没有 IC 矩阵时, 用 rolling_ic 跟 ACTIVE 列表的 avg rolling_ic 比较
        if self.active_factor_names:
            other_ics = [
                self.ic_tracker.rolling_ic(other)
                for other in self.active_factor_names
                if other != name
            ]
            if other_ics and abs(ic) > 1e-6:
                # 算伪"相关": |ic - mean(other_ics)| 越小越相似
                # 转成分数: 不相似 (差大) → 100, 相似 (差小) → 0
                diff = abs(ic - float(np.mean(other_ics)))
                comp_indep = min(100.0, diff / 0.1 * 100.0)
            else:
                comp_indep = 50.0
        else:
            comp_indep = 50.0  # 无 ACTIVE 列表时中性分

        return {
            "mean_abs_ic": round(comp_mean_abs, 2),
            "ic_stability": round(comp_stability, 2),
            "regime_consistency": round(comp_regime, 2),
            "decay_rate": round(comp_decay, 2),
            "independence": round(comp_indep, 2),
        }

    def _build_ic_series(self, history: list, window: int) -> np.ndarray:
        """
        从 (val, ret) 序列重建 IC 时间序列 (跟 ICTracker.rolling_ic 算法一致)
        window=window 大小, 每点算当前 window 内的 corrcoef
        """
        if len(history) < 30:
            return np.array([])
        vals = np.array([h[0] for h in history])
        rets = np.array([h[1] for h in history])
        mask = ~(np.isnan(vals) | np.isnan(rets))
        vals = vals[mask]
        rets = rets[mask]

        n = len(vals)
        if n < 30:
            return np.array([])

        ics = []
        # 滑动 window, 每点算 corrcoef
        step = max(1, n // 100)  # 采样 100 点
        for i in range(30, n, step):
            sub_v = vals[max(0, i - window):i]
            sub_r = rets[max(0, i - window):i]
            if len(sub_v) < 10:
                continue
            try:
                ic = float(np.corrcoef(sub_v, sub_r)[0, 1])
                if np.isfinite(ic):
                    ics.append(ic)
            except Exception:
                pass
        return np.array(ics)

    # ── 报告生成 ──────────────────────────────────────────────────

    def report(self) -> str:
        """生成可读报告 (用于落盘 + console)"""
        all_status = self.evaluate_all()
        if not all_status:
            return "FactorHealth report: no factors evaluated yet."

        lines = ["=" * 72]
        lines.append("  FACTOR HEALTH REPORT")
        lines.append("=" * 72)

        by_status = {"HEALTHY": [], "WATCH": [], "DECAYING": [], "DEAD": [], "UNKNOWN": []}
        for s in all_status:
            by_status.setdefault(s.status, []).append(s)

        for status, items in by_status.items():
            if not items:
                continue
            lines.append(f"\n  {status} ({len(items)} factors):")
            for s in sorted(items, key=lambda x: -x.score):
                lines.append(
                    f"    {s.factor:25s} score={s.score:5.1f}  "
                    f"rolling_ic={s.rolling_ic:+.4f}  n_obs={s.n_obs}"
                )

        lines.append("\n" + "-" * 72)
        lines.append(f"  Active threshold: score >= {HEALTHY_SCORE_THRESHOLD}")
        lines.append(f"  Watch threshold:  {WATCH_SCORE_THRESHOLD} <= score < {HEALTHY_SCORE_THRESHOLD}")
        lines.append(f"  Decaying:         score < {WATCH_SCORE_THRESHOLD}")
        lines.append("=" * 72)
        return "\n".join(lines)

    def report_dict(self) -> dict:
        """生成 dict 形式报告 (json 落盘用)"""
        all_status = self.evaluate_all()
        return {
            "summary": {
                "total": len(all_status),
                "healthy": sum(1 for s in all_status if s.status == "HEALTHY"),
                "watch": sum(1 for s in all_status if s.status == "WATCH"),
                "decaying": sum(1 for s in all_status if s.status == "DECAYING"),
                "unknown": sum(1 for s in all_status if s.status == "UNKNOWN"),
            },
            "factors": [s.to_dict() for s in all_status],
        }
