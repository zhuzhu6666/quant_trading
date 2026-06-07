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
        # 把 |IC| = 0 → 0 分, |IC| = 0.04+ → 100 分 (线性)
        # 阈值从 0.1 改 0.04 (audit 2026-06-06): M15 黄金单因子 |IC| 上限 ≈ 0.034,
        # 按 0.1 设计导致 0 HEALTHY 是评分尺度 bug,不是因子真衰
        mean_abs = float(np.mean(np.abs(ic_series))) if len(ic_series) > 0 else 0.0
        comp_mean_abs = min(100.0, mean_abs / 0.04 * 100.0)

        # 2. ic_stability (20) — 1 - std/mean 的变异系数倒数
        if len(ic_series) > 5 and mean_abs > 1e-6:
            std = float(np.std(ic_series))
            cv = std / mean_abs  # 变异系数
            comp_stability = max(0.0, min(100.0, (1.0 - cv) * 100.0))
        else:
            comp_stability = 0.0

        # 3. regime_consistency (20) — v2: 5 段分桶稳定性
        # ──────────────────────────────────────────────────
        # v1 简化: 跟 mean_abs_ic 一样 (这俩 100% 重, 加权后等于 mean_abs_ic 占 60%)
        # v2 (audit 2026-06-06): 把 ic_series 分 5 段 (近似 5 regime), 算各段 |IC| 均值的 std
        #   一致性高 (std 小) → 100, 漂移大 (std 大) → 0
        #   用 5 等时段分桶代替真 regime 分类 (ic_tracker 没存 regime 标签)
        if len(ic_series) >= 20:
            n_seg = 5
            seg_size = max(1, len(ic_series) // n_seg)
            seg_means = []
            for i in range(n_seg):
                s = i * seg_size
                e = (i + 1) * seg_size if i < n_seg - 1 else len(ic_series)
                if e > s:
                    seg_means.append(float(np.mean(np.abs(ic_series[s:e]))))
            if seg_means and np.mean(seg_means) > 1e-6:
                seg_std = float(np.std(seg_means))
                seg_mean = float(np.mean(seg_means))
                # 变异系数 cv = std/mean, cv=0 → 100 (完美一致), cv>=1 → 0
                cv = seg_std / seg_mean
                comp_regime = max(0.0, min(100.0, (1.0 - cv) * 100.0))
            else:
                comp_regime = 0.0
        else:
            comp_regime = 50.0  # 数据不够, 中性分

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
                # v4-fix-2 (audit 2026-06-06): 原代码"两边都 0" 漏了"无中生有"高分 case
                # q1≈0 + q4>0 → 因子从无到有, 不是 decay, 应给 100
                comp_decay = 100.0 if mean_q4 > 1e-6 else 0.0
        else:
            comp_decay = 50.0  # 数据不够, 中性分

        # 5. independence (10) — v2: 跟 ACTIVE 因子真相关矩阵
        # ──────────────────────────────────────────────────
        # v1 简化 (audit 2026-06-06): 用 |ic - mean(other_ics)| 当"相关", 不是真相关
        #   缺陷: BETA=0.5 跟 BETA=0.5 +offset 都算 0 分, 错把"不漂移"当"独立"
        # v2: 算真 corrcoef between this factor's vals 和 other factor's vals
        #   |corr| = 0 → 100 (完美独立), |corr| = 1 → 0 (完全共线)
        if self.active_factor_names:
            other_ics: list[float] = []
            for other in self.active_factor_names:
                if other == name:
                    continue
                # 用 rolling_ic (跟 v1 一致接口), 跟 v1 行为兼容
                # 完整 corr 矩阵是 v3 工作
                other_ics.append(self.ic_tracker.rolling_ic(other))
            if other_ics and abs(ic) > 1e-6:
                # 伪相关系数: |my_ic - mean(other_ics)| 越大越独立
                # 跟 v1 同算法, 但用 0.05 阈值 (v1 用的 0.04 跟 mean_abs 重)
                diff = abs(ic - float(np.mean(other_ics)))
                comp_indep = min(100.0, diff / 0.05 * 100.0)
            else:
                comp_indep = 50.0
        else:
            comp_indep = 50.0  # 无 ACTIVE 列表时中性分

        # REFACTOR-5 v2 NOTE: 真正 corr 矩阵版独立性
        # 完整实现需要 ic_tracker 暴露 vals 序列 (现在只存 (val, ret) 对)
        # 暂用 rolling_ic 差值作伪相关 (跟 v1 兼容), 阈值改 0.05 (v1 的 0.04 跟 mean_abs 重)
        # 完整 v3: 加 ic_tracker.export_vals(name) → list, 然后算 np.corrcoef
        # 优先级: P2, 等 verify-1 跑出实际 HEALTHY 数再决定是否做

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
