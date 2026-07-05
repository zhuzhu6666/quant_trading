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

from alpha.ic_tracker import ICTracker, safe_corrcoef

logger = logging.getLogger(__name__)


def _connect_state():
    from backend.core.db import get_state_pg_conn

    return get_state_pg_conn()


def _p(sql: str) -> str:
    return sql.replace("?", "%s")


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
        # BUG-5 fix (audit 2026-06-21): 显式按实际可用权重归一化,
        # 不再依赖"权重和恒=100"隐式假设.
        weights_dict = {
            "mean_abs_ic": 40,
            "ic_stability": 20,
            "regime_consistency": 20,
            "decay_rate": 10,
            "independence": 10,
        }
        total_weight = sum(weights_dict[k] for k in components if k in weights_dict)
        if total_weight > 0:
            score = sum(weights_dict[k] * components[k] for k in components
                        if k in weights_dict) / total_weight
        else:
            score = 0.0

        if score >= HEALTHY_SCORE_THRESHOLD:
            status_str = "HEALTHY"
        elif score >= WATCH_SCORE_THRESHOLD:
            status_str = "WATCH"
        else:
            status_str = "DECAYING"

        # Emit health score metric
        try:
            from backend.runtime.runtime_state import RuntimeState
            RuntimeState.shared().emit_metric("factor_health_score", {
                "factor": name,
                "status": status_str,
                "value": round(score, 2),
            })
        except Exception:
            pass

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

        # 5. independence (10) — v3: 跟 ACTIVE 因子真实相关矩阵
        # ──────────────────────────────────────────────────
        # v3 (2026-06-15): 用 ic_tracker.export_vals() 取因子值序列,
        #   计算 np.corrcoef 真实相关性。|corr|=0→100分（完美独立）,
        #   |corr|=1→0分（完全共线）。
        #   与 v1/v2 的 |ic-mean| 伪相关相比: 真相关直接度量因子值冗余,
        #   不受 IC 水平漂移影响。
        if self.active_factor_names:
            my_vals = self.ic_tracker.export_vals(name)
            if len(my_vals) > 1:
                other_corrs: list[float] = []
                for other in self.active_factor_names:
                    if other == name:
                        continue
                    other_vals = self.ic_tracker.export_vals(other)
                    if len(other_vals) < 2:
                        continue
                    # 对齐长度, 算 Pearson 相关
                    min_len = min(len(my_vals), len(other_vals))
                    corr = abs(safe_corrcoef(my_vals[:min_len], other_vals[:min_len]))
                    if np.isfinite(corr):
                        other_corrs.append(corr)
                if other_corrs:
                    avg_corr = float(np.mean(other_corrs))
                    # |corr| = 0 → 100, |corr| = 1 → 0
                    comp_indep = min(100.0, max(0.0, (1.0 - avg_corr) * 100.0))
                else:
                    comp_indep = 50.0
            else:
                comp_indep = 50.0  # 数据不够算 corr
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
            sub_mask = ~(np.isnan(sub_v) | np.isnan(sub_r)
                         | np.isinf(sub_v) | np.isinf(sub_r))
            sub_v = sub_v[sub_mask]
            sub_r = sub_r[sub_mask]
            if len(sub_v) < 10:
                continue
            try:
                ic = safe_corrcoef(sub_v, sub_r, min_samples=10)
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


# ── module-level orchestrator (供 service 调用) ─────────────────────
# audit 2026-06-08: backend/services/factor_health_service.py 一直
# import 不存在的 evaluate_factors/write_report. 这里补上, 内部用
# ICTracker + FactorHealth 类做实际计算.

def _build_forward_returns(df: "pd.DataFrame", horizon: int = 1) -> "np.ndarray":
    """算 1 步 forward return: (close[t+horizon] - close[t]) / close[t].
    最后 horizon 根返 NaN (无未来数据)."""
    close = df["close"].values
    n = len(close)
    fwd = np.full(n, np.nan)
    if n <= horizon:
        return fwd
    fwd[: n - horizon] = (close[horizon:] - close[: n - horizon]) / close[: n - horizon]
    return fwd


def evaluate_factors(
    df: "pd.DataFrame",
    threshold: float = 0.04,
    progress_cb: "Optional[Callable[[str, float, str], None]]" = None,
    exclude_dead: bool = True,
) -> dict:
    """遍历 factor_registry 里所有因子, 算 IC + 健康分, 汇总成 service 期望格式.

    Returns: {
        "total": int, "healthy": int, "watch": int, "decaying": int, "unknown": int,
        "factors": [FactorHealthStatus.to_dict() ...]
    }

    注: 这是 v1 简化版 — 用 1 步 forward return 当 ground truth,
    没考虑交易成本/regime. v2 应该接 factor_score_evaluator 算多 horizon.
    """
    from alpha.registry import factor_registry

    cb = progress_cb or (lambda *_: None)
    cb("loading_factors", 35, f"scanning {len(factor_registry.list())} factors")

    if len(df) < 50:
        cb("warning", 38, f"only {len(df)} bars, health report may be sparse")

    fwd_returns = _build_forward_returns(df, horizon=1)
    tracker = ICTracker(window=min(2000, len(df)))
    health = FactorHealth(tracker)

    n_factors = len(factor_registry.list())
    # 跳过 DEAD 因子 (reduce CPU, 避免 builtin DEAD 因子反复评估)
    dead_names_set: set[str] = set()
    if exclude_dead:
        try:
            from alpha.registry_adapter import RegistryAdapter
            dead_names_set = set(RegistryAdapter.shared().dead_names())
        except Exception:
            pass  # 静默降级 — 仍评估所有因子

    all_status: list[FactorHealthStatus] = []
    n_dead_skipped = 0
    for i, name in enumerate(factor_registry.list()):
        if name in dead_names_set:
            n_dead_skipped += 1
            if n_factors > 0 and (i + 1) % 5 == 0:
                cb("evaluating", 35 + 50 * (i + 1) / n_factors, f"{i+1}/{n_factors} factors ({n_dead_skipped} DEAD skipped)")
            continue
        try:
            fn = factor_registry.get(name)
            if fn is None:
                continue
            vals = fn(df)
            # 必须等长 (跟 ICTracker.update 严格校验一致)
            n = min(len(vals), len(fwd_returns))
            vals = np.asarray(vals, dtype=np.float64)[:n]
            fr = fwd_returns[:n]
            tracker.update(name, vals, fr)
            status = health.evaluate(name)
            all_status.append(status)
        except Exception as e:
            logger.warning(f"evaluate_factors: {name} failed: {e}")
            all_status.append(FactorHealthStatus(factor=name, score=0.0, status="DEAD", n_obs=0))
        if n_factors > 0 and (i + 1) % 5 == 0:
            cb("evaluating", 35 + 50 * (i + 1) / n_factors, f"{i+1}/{n_factors} factors")

    summary = {
        "total": len(all_status) + n_dead_skipped,
        "healthy": sum(1 for s in all_status if s.status == "HEALTHY"),
        "watch": sum(1 for s in all_status if s.status == "WATCH"),
        "decaying": sum(1 for s in all_status if s.status == "DECAYING"),
        "unknown": sum(1 for s in all_status if s.status == "UNKNOWN"),
        "dead": n_dead_skipped,
    }
    return {
        **summary,
        "factors": [s.to_dict() for s in all_status],
    }


def write_report(result: dict, out_txt: "Path", out_json: "Path") -> None:
    """把 evaluate_factors 结果落盘: PostgreSQL state_v1 (主) + json/txt (缓存)。"""
    import json
    from pathlib import Path as _P

    out_txt = _P(out_txt)
    out_json = _P(out_json)
    out_txt.parent.mkdir(parents=True, exist_ok=True)
    out_json.parent.mkdir(parents=True, exist_ok=True)

    # json 缓存 (API 兼容)
    out_json.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # txt 人读报告
    lines = ["=" * 72, "  FACTOR HEALTH REPORT", "=" * 72, ""]
    s = {k: v for k, v in result.items() if k in ("total", "healthy", "watch", "decaying", "unknown", "dead")}
    lines.append(f"  Summary: {s}")
    lines.append("")
    for status_name in ("HEALTHY", "WATCH", "DECAYING", "DEAD", "UNKNOWN"):
        group = [f for f in result.get("factors", []) if f.get("status") == status_name]
        if not group:
            continue
        lines.append(f"  {status_name} ({len(group)}):")
        for f in sorted(group, key=lambda x: -x.get("score", 0)):
            lines.append(
                f"    {f.get('factor', '?'):25s} score={f.get('score', 0):5.1f}  "
                f"rolling_ic={f.get('rolling_ic', 0):+.4f}  n_obs={f.get('n_obs', 0)}"
            )
        lines.append("")
    lines.append("=" * 72)
    out_txt.write_text("\n".join(lines), encoding="utf-8")

    # 写入 PostgreSQL state store (主存储)
    try:
        conn = _connect_state()
        try:
            import time as _time
            now = _time.time()
            factors = result.get("factors", [])
            for f in factors:
                name = f.get("factor", "")
                if not name:
                    continue
                score = float(f.get("score", 50.0))
                status = str(f.get("status", "UNKNOWN"))
                section = str(f.get("section", "unknown"))
                n_obs = int(f.get("n_obs", 0))
                rolling_ic = float(f.get("rolling_ic", 0.0))
                comp_json = json.dumps(f.get("components", {}), ensure_ascii=False)
                conn.execute(
                    _p("INSERT INTO factor_health "
                    "(factor, score, status, section, n_obs, rolling_ic, components_json, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(factor) DO UPDATE SET "
                    "score=excluded.score, status=excluded.status, section=excluded.section, "
                    "n_obs=excluded.n_obs, rolling_ic=excluded.rolling_ic, "
                    "components_json=excluded.components_json, updated_at=excluded.updated_at"),
                    (name, score, status, section, n_obs, rolling_ic, comp_json, now)
                )
            # 清理孤儿: 删除不在本次评估结果中的 factor_health 记录
            evaluated_names = {f.get("factor", "") for f in factors if f.get("factor")}
            if evaluated_names:
                placeholders = ",".join("?" * len(evaluated_names))
                conn.execute(
                    _p(f"DELETE FROM factor_health WHERE factor NOT IN ({placeholders})"),
                    tuple(evaluated_names),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.debug("write_report DB: %s", e)


# ── 退役检查 (Phase 2.4) ──────────────────────────────────────────────


@dataclass
class RetireCandidates:
    """退役候选列表.

    Attributes:
        candidates: 建议退役的因子名称列表
        reason: 退役原因摘要
    """

    candidates: list[str] = field(default_factory=list)
    reason: str = ""


def retirement_check(
    statuses: list[FactorHealthStatus],
    days_in_decay: dict[str, float] | None = None,
) -> RetireCandidates:
    """检查哪些因子应当退役.

    基于 RuntimeConfig 的退役阈值:
    - retire_decaying_days: 持续 DECAYING 天数阈值
    - retire_severe_threshold: 严重健康分阈值 (< 此值立即候选)
    - retire_grace_hours_severe / retire_grace_hours_mild: 宽限期 (预留)

    Args:
        statuses: FactorHealthStatus 列表 (通常来自 FactorHealth.evaluate_all)
        days_in_decay: 可选, 因子名 -> 已持续 DECAYING 天数. 缺失时仅按
                       健康分阈值筛选.

    Returns:
        RetireCandidates: 候选列表 + 原因

    用法:
        statuses = health.evaluate_all()
        result = retirement_check(statuses, days_in_decay=tracker)
        if result.candidates:
            for name in result.candidates:
                adapter.retire(name)
    """
    # BUG-1 fix (audit 2026-06-21): RuntimeConfig() 用默认构造器永远拿默认值,
    # RuntimeConfig 类没有 shared() 类方法 (那是模块级函数 shared()).
    # 改用模块级 shared() 函数获取热更后的单例.
    from config.runtime_config import shared as _rc_shared
    cfg = _rc_shared()

    severe_threshold = cfg.retire_severe_threshold  # 30.0
    decaying_days_threshold = float(cfg.retire_decaying_days)  # 7

    candidates: list[str] = []
    parts: list[str] = []

    for s in statuses:
        if s.status != "DECAYING":
            continue

        # 严重衰退: 健康分 < severe_threshold (如 30)
        if s.score < severe_threshold:
            candidates.append(s.factor)
            parts.append(f"{s.factor}:severe(score={s.score:.1f})")
            continue

        # 中度衰退: 检查持续天数
        if days_in_decay is not None and s.factor in days_in_decay:
            days = days_in_decay[s.factor]
            if days >= decaying_days_threshold:
                candidates.append(s.factor)
                parts.append(f"{s.factor}:decayed({days:.1f}d)")
            continue

        # audit v3 fix: 无 days_in_decay 数据时, 不再无脑退役所有 DECAYING 因子
        # 只有 severe(<30) 才立即退役, 中度衰退(30-40)需要 days_in_decay 确认持续天数
        # 之前的行为会导致 score 30-40 的轻微衰退因子被错误退役

    reason = "; ".join(parts) if parts else "no candidates"
    return RetireCandidates(candidates=candidates, reason=reason)
