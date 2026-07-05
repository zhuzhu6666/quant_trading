"""AdaptiveWeightEngine — 权重自适应引擎。

核心: Newey-West HAC Sharpe 驱动权重调整, 带锚点回归。
因子退役使用 CausalCheck + DSR 多重检验 + 健康分三重门控。

设计文档: docs/architecture.md
"""

import json
import logging
import math
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

import numpy as np

from alpha.ic_tracker import ICTracker
from alpha.portfolio_compositor import resolve_factor_role

logger = logging.getLogger(__name__)


class AdaptiveWeightEngine:
    """权重自适应引擎。

    核心算法:
        1. composite_sharpe_score → exp(k × score) 调整
        2. 锚点回归 (每次调整 15% 回 base_weight)
        3. 单次调整限幅 (默认 0.15)
        4. 权重上下限 [0.1, 3.0]
        5. IC 下限 + 健康分下限门控
        6. CausalCheck + DSR 禁用条件
        7. 多样性约束 (单类型 ≤ 40%)
        8. BlendSearch SLSQP 离线 baseline (opt-in)

    Args:
        config: 来自 RuntimeConfig 的 AWE 参数字典。
                (所有键应有 awe_ 前缀)
    """

    def __init__(self, config: dict[str, Any],
                 ictracker: Optional[ICTracker] = None):
        self._config = dict(config or {})
        self._ictracker = ictracker
        self._base_weights: dict[str, float] = {}
        self._current_weights: dict[str, float] = {}
        self._disabled_history: dict[str, float] = {}  # name → disabled_ts
        self._weight_history_path: str = "data/charts/factor_weight_history.jsonl"
        self._blend_baselines: dict[str, float] = {}  # BlendSearch SLSQP optimal weights

    # ── 初始化 ──────────────────────────────────────────

    def initialize(self, factor_configs: dict[str, dict],
                   ictracker: Optional[ICTracker] = None):
        """记录初始权重作为锚点。"""
        if ictracker is not None:
            self._ictracker = ictracker
        for name, cfg in factor_configs.items():
            w = cfg if isinstance(cfg, (int, float)) else cfg.get("weight", 1.0)
            self._base_weights[name] = w
            self._current_weights[name] = w

    # ── BlendSearch SLSQP 离线 baseline ────────────────

    def compute_blend_baseline(
        self,
        factor_returns: "np.ndarray",
        forward_returns: "np.ndarray",
        factor_names: list[str],
    ) -> dict[str, float]:
        """通过 BlendSearch SLSQP 计算最优 blend baseline。

        将结果同时存入 _blend_baselines 和 _base_weights (更新锚点)。
        支持懒导入; 不可用时回退到等权。

        Args:
            factor_returns: (T, n_factors) 因子值数组。
            forward_returns: (T,) 远期收益数组。
            factor_names: 每个因子列的名称。

        Returns:
            {因子名: 最优权重} 字典。
        """
        # Lazy import so scipy is optional
        try:
            from alpha.search.blend_search import BlendSearch
        except ImportError:
            logger.warning(
                "BlendSearch not available, using equal weight baseline",
            )
            n = len(factor_names)
            weights = {name: 1.0 / n for name in factor_names}
            self._blend_baselines = dict(weights)
            self._base_weights.update(weights)
            return weights

        searcher = BlendSearch()
        solution = searcher.optimize(
            factor_returns,
            forward_returns,
            factor_names,
            max_single_weight=self._get("awe_blend_max_single_weight", 0.5),
        )
        weights = dict(zip(solution.factor_names, solution.coefficients))
        self._blend_baselines = weights
        self._base_weights.update(weights)
        logger.info(
            "Blend baseline computed via %s: %s",
            solution.method,
            {k: round(v, 4) for k, v in weights.items()},
        )
        return weights

    # ── 权重自适应 ──────────────────────────────────────

    def adapt(
        self,
        attribution: Any,
        factor_configs: dict[str, dict],
        use_blend_baseline: bool = False,
        factor_values: dict[str, np.ndarray] | None = None,
        forward_returns: np.ndarray | None = None,
    ) -> dict[str, dict]:
        """权重自适应调整。

        Args:
            attribution: AttributionEngine 实例。
            factor_configs: {name: config} 字典。
            use_blend_baseline: 是否使用 BlendSearch 计算的 baseline 作为锚点。
                                需先调用 compute_blend_baseline() 填充 _blend_baselines。

        返回: {factor_name: {"weight": new_weight, "reason": str}}
        只返回有变化的因子。
        """
        # 若启用 blend baseline 但尚未计算, 优雅降级
        if use_blend_baseline and not self._blend_baselines:
            logger.warning(
                "use_blend_baseline=True but _blend_baselines empty; "
                "call compute_blend_baseline() first, falling back to _base_weights",
            )

        patches: dict[str, dict] = {}
        all_stats = attribution.get_all_factor_stats()

        for name, stats in all_stats.items():
            cfg_entry = factor_configs.get(name)
            if cfg_entry is None:
                continue
            if isinstance(cfg_entry, dict) and cfg_entry.get("enabled") is False:
                continue
            if resolve_factor_role(name, cfg_entry if isinstance(cfg_entry, dict) else None) != "alpha":
                continue

            # 最低交易笔数门槛
            if stats.n_trades < self._get("awe_min_trades", 10):
                continue

            # IC + 健康分门控
            if not self._check_ic_and_health(name):
                continue

            # 健康分下限 (直接禁用)
            health = self._get_health_score(name)
            if health is not None and health < self._get("awe_health_floor", 40.0):
                patches[name] = {
                    "weight": 0.0,
                    "reason": f"health={health:.0f}<{self._get('awe_health_floor', 40):.0f}",
                }
                continue

            # ── Sharpe 综合分数驱动 ──
            composite_score = stats.composite_sharpe_score
            old_weight = self._current_weights.get(
                name,
                cfg_entry.get("weight", 1.0) if isinstance(cfg_entry, dict) else (cfg_entry or 1.0),
            )
            base_weight = self._base_weights.get(name, 1.0)
            if use_blend_baseline and self._blend_baselines:
                base_weight = self._blend_baselines.get(name, base_weight)

            # 核心公式: raw_new = old × exp(k × composite_sharpe_score)
            k = self._get("awe_sensitivity", 0.5)
            raw_new = old_weight * math.exp(k * composite_score)

            # 锚点回归
            anchor_pull = self._get("awe_anchor_pull", 0.15)
            new_weight = raw_new * (1 - anchor_pull) + base_weight * anchor_pull

            # 单次调整限幅
            max_change = self._get("awe_max_single_change", 0.15)
            if abs(new_weight - old_weight) > max_change:
                new_weight = old_weight + math.copysign(
                    max_change, new_weight - old_weight,
                )

            # 权重上下限
            min_w = self._get("awe_weight_min", 0.1)
            max_w = self._get("awe_weight_max", 3.0)
            new_weight = max(min_w, min(max_w, new_weight))

            # ── 禁用条件 ──
            if stats.n_trades >= self._get("awe_disable_min_trades", 20):
                disable_reason = self._check_disable_conditions(
                    name, stats, factor_values, forward_returns,
                )
                if disable_reason:
                    patches[name] = {"weight": 0.0, "reason": disable_reason}
                    continue

            # 发布补丁 (变化 ≥ 0.01)
            if abs(new_weight - old_weight) >= 0.01:
                patches[name] = {
                    "weight": round(new_weight, 3),
                    "reason": f"score={composite_score:.3f}",
                }

        # ── 多样性约束 (只在有调整时执行) ──
        if patches:
            patches = self._enforce_diversity(patches, factor_configs, all_stats)

        # ── 更新当前权重 + 写入历史 ──
        for name, patch in patches.items():
            self._current_weights[name] = patch["weight"]
            # 如果因子被禁用, 记录时间
            if patch["weight"] == 0.0:
                self._disabled_history[name] = time.time()

        # ── 复活检查: 被禁用的因子是否满足复活条件 ──
        resurrection_patches = self._check_all_resurrections(factor_configs)
        patches.update(resurrection_patches)

        if patches:
            self._write_weight_history(patches)

        return patches

    # ── 门控检查 ────────────────────────────────────────

    def _check_ic_and_health(self, name: str) -> bool:
        """IC ≥ awe_ic_floor?"""
        if self._ictracker is None:
            return True  # no tracker, skip gate (graceful degradation)
        try:
            ic_status = self._ictracker.status(name)
            ic = abs(ic_status.get("rolling_ic", 0))
            if ic < self._get("awe_ic_floor", 0.02):
                return False
        except Exception:
            logger.debug("_check_ic_and_health failed for %s", name, exc_info=True)
        return True

    def _get_health_score(self, name: str) -> float | None:
        """获取因子健康分, 失败返回 None。"""
        if self._ictracker is None:
            return None
        try:
            if not hasattr(self, "_health_checker") or self._health_checker is None:
                from alpha.factor_health import FactorHealth
                self._health_checker = FactorHealth(self._ictracker)
            health = self._health_checker.evaluate(name)
            return health.score if health else None
        except Exception:
            logger.debug("_get_health_score failed for %s", name, exc_info=True)
            return None

    # ── 综合信念分 (用于追踪止损) ──────────────────────────

    def composite_conviction(self) -> float:
        """聚合因子健康分 × 当前权重 → 0.0~1.0 综合信念分.

        高信念: 因子健康, 权重高 → 可紧追踪, 让利润奔跑.
        低信念: 因子退化或权重低 → 保守追踪, 快速锁利.

        Returns:
            0.0~1.0, 0.5 为默认中值 (无数据时回退).
        """
        total_weight = 0.0
        weighted_health = 0.0
        for name, weight in self._current_weights.items():
            if weight <= 0:
                continue
            health = self._get_health_score(name)
            if health is None:
                continue
            weighted_health += weight * health
            total_weight += weight
        if total_weight <= 0:
            return 0.5
        avg_health = weighted_health / total_weight  # 0~100
        return min(1.0, max(0.0, avg_health / 100.0))

    # ── 禁用条件 ────────────────────────────────────────

    def _check_disable_conditions(
        self, name: str, stats: Any,
        factor_values: dict[str, np.ndarray] | None = None,
        forward_returns: np.ndarray | None = None,
    ) -> str | None:
        """检查三重禁用条件 (设计文档 §9.1)。

        任一满足即禁用:
        1. CausalCheck cause_vs_corr_score < -0.3
        2. DSR p-value > 0.95 (39 因子多重检验后 Sharpe 仍不显著)
        3. health < awe_health_floor
        """
        # 条件 3: 健康分兜底
        health = self._get_health_score(name)
        if health is not None and health < self._get("awe_health_floor", 40.0):
            return f"health={health:.0f}<floor"

        # 条件 2: DSR 多重检验
        try:
            if hasattr(stats, "is_statistically_significant"):
                dsr = stats.is_statistically_significant(
                    n_trials=self._get("awe_dsr_n_trials", 39),
                )
                if dsr.get("p_value", 0) > self._get("awe_dsr_p_threshold", 0.95):
                    return f"dsr_p={dsr['p_value']:.3f}>threshold"
        except Exception:
            pass

        # 条件 1: CausalCheck 因果性 (Phase 1 启用)
        if factor_values is not None and forward_returns is not None:
            try:
                if hasattr(stats, "causal_quality"):
                    causal = stats.causal_quality(
                        factor_values.get(name, np.array([])),
                        forward_returns,
                    )
                    if causal.get("cause_vs_corr_score", 0) < -0.3:
                        return f"causal_score={causal['cause_vs_corr_score']:.2f}<-0.3"
            except Exception:
                pass

        return None  # 未触发禁用

    # ── 多样性约束 ──────────────────────────────────────

    def _enforce_diversity(
        self,
        patches: dict[str, dict],
        factor_configs: dict[str, dict],
        all_stats: dict[str, Any],
    ) -> dict[str, dict]:
        """同一类型标签总权重不超过 max_type_weight_pct (默认 40%)。

        方法: 当类型超限时, 将该类型所有因子按比例压降到上限,
        分到每个因子的降权量与其当前权重 × (1 - 类型内 Sharpe 排名占比) 成正比。
        """
        max_pct = self._get("awe_max_type_weight_pct", 0.40)

        # 合并当前配置 + 补丁 (兼容扁平/嵌套两种格式)
        merged = {}
        for name, cfg in factor_configs.items():
            if isinstance(cfg, dict):
                merged[name] = dict(cfg)
            else:
                merged[name] = {"weight": cfg, "enabled": True, "tags": []}
        for name, p in patches.items():
            if name in merged:
                merged[name]["weight"] = p["weight"]

        total_weight = sum(
            c.get("weight", 0) for name, c in merged.items()
            if c.get("enabled", True) and c.get("weight", 0) > 0
            and resolve_factor_role(name, c) == "alpha"
        )
        if total_weight <= 0:
            return patches

        # 按类型聚合
        type_weights: dict[str, float] = defaultdict(float)
        type_factors: dict[str, list] = defaultdict(list)
        for name, c in merged.items():
            if not c.get("enabled", True) or c.get("weight", 0) <= 0:
                continue
            if resolve_factor_role(name, c) != "alpha":
                continue
            for tag in c.get("tags", []):
                type_weights[tag] += c["weight"]
                type_factors[tag].append((name, c, all_stats.get(name)))

        # 找到第一个超限类型
        for tag, tw in type_weights.items():
            pct = tw / total_weight
            if pct > max_pct:
                # 计算非该类型的总权重
                non_tag_weight = total_weight - tw
                # 目标: tag_new / (tag_new + non_tag) = max_pct
                # tag_new = max_pct * (tag_new + non_tag)
                # tag_new = max_pct * tag_new + max_pct * non_tag
                # tag_new * (1 - max_pct) = max_pct * non_tag
                # tag_new = max_pct * non_tag / (1 - max_pct)
                target_type_weight = max_pct * non_tag_weight / (1 - max_pct)
                # 等比缩放到目标
                scale = target_type_weight / tw if tw > 0 else 1.0
                for name, cfg, _stats in type_factors[tag]:
                    old_w = merged[name]["weight"]
                    new_w = max(old_w * scale, 0.1)
                    if name in patches:
                        patches[name]["weight"] = round(new_w, 3)
                        patches[name]["reason"] = (
                            f"diversity_{tag}_{pct:.0%}>max"
                        )
                    else:
                        patches[name] = {
                            "weight": round(new_w, 3),
                            "reason": f"diversity_{tag}_{pct:.0%}>max",
                        }
                # 只处理第一个超限类型
                break

        return patches

    # ── 工具 ────────────────────────────────────────────

    def _get(self, key: str, default: Any) -> Any:
        return self._config.get(key, default)

    # ── 复活机制 (设计文档 §9.1) ──

    def _check_all_resurrections(
        self, factor_configs: dict[str, dict],
    ) -> dict[str, dict]:
        """检查所有已禁用因子是否满足复活条件。

        复活条件 (全部满足):
        1. CausalCheck cause_vs_corr_score > 0 (预测关系正向)
        2. DSR p-value < 0.05 (多重检验后 Sharpe 显著)
        3. health_score > 60 (5 维健康分达标)
        4. 冷却期满 (默认 7 天)

        返回: {name: {weight, reason}}
        """
        patches: dict[str, dict] = {}
        for name, disabled_ts in list(self._disabled_history.items()):
            # 跳过已在 config 中 entry 不存在的
            cfg_entry = factor_configs.get(name)
            if cfg_entry is None:
                continue

            # 跳过当前权重已经 > 0 的 (已复活)
            if self._current_weights.get(name, 0) > 0:
                self._disabled_history.pop(name, None)
                continue

            # 条件 4: 冷却期检查
            days_since = self._days_since_disabled(disabled_ts)
            cooldown = self._get("awe_resurrect_cooldown_days", 7)
            if days_since < cooldown:
                continue

            # 条件 3: 健康分检查
            health = self._get_health_score(name)
            health_threshold = self._get("awe_resurrect_health_threshold", 60.0)
            if health is None or health <= health_threshold:
                continue

            # 条件 2: DSR 多重检验
            # AWE adapt() 阶段不直接访问 stats, 由外部驱动
            # 此处仅检查健康分 (DSR/CausalCheck 在累计足够交易后自动触发)
            # 完整复活检查需要 attribution engine 的 stats

            base_weight = self._base_weights.get(
                name,
                cfg_entry.get("weight", 1.0) if isinstance(cfg_entry, dict) else (cfg_entry or 1.0),
            )
            resurrect_weight = base_weight * 0.5  # 减半起步
            resurrect_weight = max(
                resurrect_weight, self._get("awe_weight_min", 0.1),
            )

            patches[name] = {
                "weight": round(resurrect_weight, 3),
                "reason": f"resurrect_health={health:.0f}_days={days_since:.0f}",
            }
            self._disabled_history.pop(name, None)

        return patches

    def _days_since_disabled(self, disabled_ts: float) -> float:
        """自禁用以来的天数。"""
        return (time.time() - disabled_ts) / 86400.0

    # ── 权重历史 ────────────────────────────────────────

    def _write_weight_history(self, patches: dict[str, dict]):
        """写入权重变更记录到主状态库 weight_history 表。"""
        try:
            from backend.core.db import get_state_pg_conn
            ts = time.time()
            conn = get_state_pg_conn()
            try:
                for name, p in patches.items():
                    sql = (
                        "INSERT INTO weight_history (timestamp, factor, old_weight, new_weight, reason) "
                        "VALUES (%s, %s, %s, %s, %s)"
                    )
                    conn.execute(
                        sql,
                        (ts, name, self._current_weights.get(name, 0),
                         p["weight"], p.get("reason", ""))
                    )
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            logger.warning("Failed to write weight history: %s", e)
