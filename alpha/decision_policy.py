"""alpha/decision_policy.py — 统一权重决策中枢.

融合 AWE (实盘归因)、WeightPolicy (健康分)、ShadowTrading (OOS)、Regime
等来源, 输出唯一权威的 WeightDecision 列表, 解决 AWE 和 WeightPolicy
各自写 factor_portfolio_weights 的冲突问题。

用法:
    dp = DecisionPolicy()
    decisions = dp.decide(
        awe_patches=awe.adapt(...),
        weight_policy_weights=wp.compute_weights(scores),
        shadow_perfs=shadow_perf_dict,
        factor_configs=cfg.factor_signal_config,
        current_weights=cfg.factor_portfolio_weights,
    )
    # decisions → {factor: WeightDecision}
    # 唯一写路径: factor_portfolio_weights = {f: d.new_weight for f, d in decisions.items()}

设计:
    - AWE 有禁用手 (weight=0) 时无条件尊重
    - 默认 blend: 60% AWE + 40% WeightPolicy (可配置)
    - Shadow OOS 负收益 → 折价系数
    - Regime 匹配 → 涨幅系数
    - 多样性约束 → 委托 AWE._enforce_diversity 做最后一关
    - 归一化到 [min_weight, max_weight]
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from alpha.portfolio_compositor import resolve_factor_role

logger = logging.getLogger(__name__)

# ── 默认配置 ────────────────────────────────────────────────────────

DEFAULT_AWE_BLEND = 0.60           # AWE 归因分权重
DEFAULT_WP_BLEND = 0.40            # WeightPolicy 健康分权重
DEFAULT_SHADOW_PENALTY = 0.80      # 负 OOS PnL → weight × 0.8
DEFAULT_REGIME_BOOST = 1.10        # Regime 匹配 → weight × 1.1
DEFAULT_MIN_WEIGHT = 0.01
DEFAULT_MAX_WEIGHT = 0.50
DEFAULT_DIVERSITY_MAX_PCT = 0.40
DEFAULT_REDUNDANCY_MAX_GROUP_WEIGHT = 0.35


@dataclass
class WeightDecision:
    """单一因子的权重决策, 完整可审计."""
    factor: str
    old_weight: float
    new_weight: float
    reason: str                              # 可读原因
    confidence: float = 0.5                  # 0~1
    source_scores: dict[str, float] = field(default_factory=dict)

    def to_api(self) -> dict[str, Any]:
        return {
            "factor": self.factor,
            "old_weight": self.old_weight,
            "new_weight": self.new_weight,
            "reason": self.reason,
            "confidence": self.confidence,
            "source_scores": dict(self.source_scores),
        }


class DecisionPolicy:
    """统一权重决策器.

    Args:
        awe_blend: AWE 归因分融合比例 (0~1).
        wp_blend:  WeightPolicy 健康分融合比例.
                   (awe_blend + wp_blend 不一定需要 =1,
                   剩余部分由 shadow/regime 等填充.)
        shadow_penalty: 负 OOS PnL 的折价系数.
        min_weight: 单因子最小权重 (含禁用手=0).
        max_weight: 单因子最大权重.
    """

    def __init__(
        self,
        awe_blend: float = DEFAULT_AWE_BLEND,
        wp_blend: float = DEFAULT_WP_BLEND,
        shadow_penalty: float = DEFAULT_SHADOW_PENALTY,
        regime_boost: float = DEFAULT_REGIME_BOOST,
        min_weight: float = DEFAULT_MIN_WEIGHT,
        max_weight: float = DEFAULT_MAX_WEIGHT,
        diversity_max_pct: float = DEFAULT_DIVERSITY_MAX_PCT,
        redundancy_max_group_weight: float = DEFAULT_REDUNDANCY_MAX_GROUP_WEIGHT,
    ):
        self._awe_blend = awe_blend
        self._wp_blend = wp_blend
        self._shadow_penalty = shadow_penalty
        self._regime_boost = regime_boost
        self._min_weight = min_weight
        self._max_weight = max_weight
        self._diversity_max_pct = diversity_max_pct
        self._redundancy_max_group_weight = redundancy_max_group_weight

    # ── 主入口 ──────────────────────────────────────────────────────

    def decide(
        self,
        *,
        awe_patches: dict[str, dict] | None = None,
        weight_policy_weights: dict[str, float] | None = None,
        shadow_perfs: dict[str, Any] | None = None,
        factor_configs: dict[str, dict],
        current_weights: dict[str, float],
        regime: str | None = None,
        experience_priors: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, WeightDecision]:
        """完整权重决策: 融合所有来源 → 唯一权重.

        Args:
            awe_patches: AWE.adapt() 返回值 {name: {weight, reason}}.
            weight_policy_weights: WeightPolicy.compute_weights() 返回值 {name: weight}.
            shadow_perfs: shadow_trader.load_shadow_perf() 批量字典 {name: ShadowPerf}.
            factor_configs: cfg.factor_signal_config {name: config_dict}.
            current_weights: cfg.factor_portfolio_weights {name: weight}.
            regime: 当前 regime 标签 (如 "risk_on").

        Returns:
            {factor_name: WeightDecision} — 所有因子的完整决策.
        """
        # 如果没有任何来源, 返回空
        if not awe_patches and not weight_policy_weights:
            return self._fallback(current_weights)

        # 1. 收集所有涉及的因子名
        all_factors: set[str] = set(current_weights.keys())
        if awe_patches:
            all_factors.update(awe_patches.keys())
        if weight_policy_weights:
            all_factors.update(weight_policy_weights.keys())

        decisions: dict[str, WeightDecision] = {}

        for factor in sorted(all_factors):
            if not self._eligible_alpha(factor, factor_configs.get(factor, {})):
                continue
            old_w = current_weights.get(factor, 0.0)
            awe_info = (awe_patches or {}).get(factor)
            wp_w = (weight_policy_weights or {}).get(factor)

            # 2. AWE 禁用手 (weight=0) 无条件尊重
            if awe_info and awe_info.get("weight", old_w) == 0.0:
                decisions[factor] = WeightDecision(
                    factor=factor,
                    old_weight=old_w,
                    new_weight=0.0,
                    reason=awe_info.get("reason", "AWE disabled"),
                    confidence=0.9,
                    source_scores={"awe": 0.0},
                )
                continue

            # 3. 计算融合权重
            raw_weight, sources = self._blend(
                awe_info, wp_w, factor_configs.get(factor, {}), old_w,
            )

            # 4. Shadow OOS 惩罚
            raw_weight, sources = self._apply_shadow_penalty(
                raw_weight, factor, shadow_perfs, sources,
            )

            # 5. Regime 奖励
            raw_weight, sources = self._apply_regime_boost(
                raw_weight, factor, regime, factor_configs.get(factor, {}),
                sources,
            )

            # Optional learned posterior. Only effects that passed comparable
            # sample and confound checks may enter, and only as a bounded prior.
            raw_weight, sources = self._apply_experience_prior(
                raw_weight, factor, experience_priors, sources,
            )

            # 6. 权重钳制
            new_weight = max(self._min_weight, min(self._max_weight, raw_weight))
            if awe_info and awe_info.get("weight", 0) == 0.0:
                new_weight = 0.0  # 确保禁用因子权重为 0

            # 7. 构建决策
            reason = self._build_reason(awe_info, sources, new_weight, old_w)
            decisions[factor] = WeightDecision(
                factor=factor,
                old_weight=old_w,
                new_weight=round(new_weight, 4),
                reason=reason,
                confidence=min(1.0, len(sources) / 3.0),
                source_scores=sources,
            )

        # 8. 多样性约束 (委托 AWE 实现)
        decisions = self._enforce_diversity(decisions, factor_configs)
        decisions = self._enforce_redundancy_cap(decisions, factor_configs)

        return decisions

    # ── 快速决策 (用于 30 分钟 awe_adapt, 只改有变化的因子) ──────────

    def fast_decide(
        self,
        *,
        awe_patches: dict[str, dict] | None = None,
        weight_policy_weights: dict[str, float] | None = None,
        factor_configs: dict[str, dict],
        current_weights: dict[str, float],
        experience_priors: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, WeightDecision]:
        """轻量版: 仅对有变化的因子做决策, 其他保持."""
        decisions: dict[str, WeightDecision] = {}

        # AWE patches (最快来源)
        for name, pinfo in (awe_patches or {}).items():
            if not self._eligible_alpha(name, factor_configs.get(name, {})):
                continue
            old_w = current_weights.get(name, 0.0)
            new_w = pinfo.get("weight", old_w)
            source_scores = {"awe": new_w}
            new_w, source_scores = self._apply_experience_prior(
                new_w, name, experience_priors, source_scores,
            )
            decisions[name] = WeightDecision(
                factor=name,
                old_weight=old_w,
                new_weight=new_w,
                reason=pinfo.get("reason", "AWE adapt"),
                confidence=0.7,
                source_scores=source_scores,
            )

        # WeightPolicy 补充 (对 AWE 未涉及且 WP 有变化的因子)
        for name, wp_w in (weight_policy_weights or {}).items():
            if name in decisions:
                continue
            if not self._eligible_alpha(name, factor_configs.get(name, {})):
                continue
            old_w = current_weights.get(name, 0.0)
            if abs(wp_w - old_w) >= 0.005:
                decisions[name] = WeightDecision(
                    factor=name,
                    old_weight=old_w,
                    new_weight=max(self._min_weight, min(self._max_weight, wp_w)),
                    reason="WeightPolicy fast",
                    confidence=0.5,
                    source_scores={"weight_policy": wp_w},
                )

        for name, decision in decisions.items():
            if "experience_prior" in decision.source_scores:
                continue
            adjusted, scores = self._apply_experience_prior(
                decision.new_weight,
                name,
                experience_priors,
                dict(decision.source_scores),
            )
            decision.new_weight = round(max(0.0, min(self._max_weight, adjusted)), 4)
            decision.source_scores = scores

        # 多样性约束
        if decisions:
            decisions = self._enforce_diversity(decisions, factor_configs)
            decisions = self._enforce_redundancy_cap(decisions, factor_configs)

        return decisions

    # ── 内部方法 ────────────────────────────────────────────────────

    def _eligible_alpha(self, factor: str, factor_cfg: dict[str, Any] | None) -> bool:
        cfg = factor_cfg if isinstance(factor_cfg, dict) else {}
        if cfg.get("enabled") is False:
            return False
        if str(cfg.get("lifecycle_status") or "").upper() == "DEAD":
            return False
        return resolve_factor_role(factor, cfg) == "alpha"

    def _blend(
        self,
        awe_info: dict | None,
        wp_w: float | None,
        factor_cfg: dict,
        old_w: float,
    ) -> tuple[float, dict[str, float]]:
        """融合 AWE 和 WeightPolicy 权重."""
        sources: dict[str, float] = {}

        if awe_info:
            awe_w = awe_info.get("weight", old_w)
            sources["awe"] = awe_w
        else:
            awe_w = old_w

        if wp_w is not None and wp_w > 0:
            sources["weight_policy"] = wp_w
        else:
            wp_w = old_w

        # 只有一个来源 → 直接用它
        if not awe_info and wp_w is not None:
            return wp_w, sources
        if awe_info and wp_w is None:
            return awe_w, sources

        # 两个来源 → blend
        blended = awe_w * self._awe_blend + wp_w * self._wp_blend

        # 如果没有足够来源, 剩余部分用 old_w 填充
        total_blend = self._awe_blend + self._wp_blend
        if total_blend < 1.0:
            blended += old_w * (1.0 - total_blend)
            sources["previous"] = old_w * (1.0 - total_blend)

        return blended, sources

    def _apply_shadow_penalty(
        self,
        weight: float,
        factor: str,
        shadow_perfs: dict[str, Any] | None,
        sources: dict[str, float],
    ) -> tuple[float, dict[str, float]]:
        """如果 shadow OOS 负收益, 降低权重."""
        if not shadow_perfs or factor not in shadow_perfs:
            return weight, sources
        perf = shadow_perfs[factor]
        if hasattr(perf, "cumulative_pnl"):
            cum_pnl = perf.cumulative_pnl
        elif isinstance(perf, dict):
            cum_pnl = perf.get("cumulative_pnl", 0.0)
        else:
            cum_pnl = 0.0

        if cum_pnl < 0:
            discount = self._shadow_penalty
            weight *= discount
            sources["shadow_penalty"] = cum_pnl
        else:
            sources["shadow"] = cum_pnl

        return weight, sources

    def _apply_regime_boost(
        self,
        weight: float,
        factor: str,
        regime: str | None,
        factor_cfg: dict,
        sources: dict[str, float],
    ) -> tuple[float, dict[str, float]]:
        """如果因子标签匹配当前 regime, 增幅权重."""
        if not regime:
            return weight, sources
        tags = factor_cfg.get("tags", []) if isinstance(factor_cfg, dict) else []
        if not tags:
            return weight, sources

        # 简单的 regime 匹配: 如果 regime 关键词出现在因子标签中
        regime_lower = regime.lower()
        for tag in tags:
            if isinstance(tag, str) and regime_lower in tag.lower():
                weight *= self._regime_boost
                sources["regime_boost"] = 1.0
                break

        return weight, sources

    def _apply_experience_prior(
        self,
        weight: float,
        factor: str,
        priors: dict[str, dict[str, Any]] | None,
        sources: dict[str, float],
    ) -> tuple[float, dict[str, float]]:
        """Apply a small learned prior only when its attribution is bounded."""
        prior = (priors or {}).get(factor)
        if not isinstance(prior, dict):
            return weight, sources
        if not bool(prior.get("bounded_attribution_allowed", False)):
            return weight, sources
        sample_count = int(prior.get("sample_count") or 0)
        confidence = float(prior.get("confidence") or 0.0)
        if sample_count < 5 or confidence < 0.6:
            return weight, sources
        multiplier = max(0.85, min(1.15, float(prior.get("multiplier") or 1.0)))
        sources["experience_prior"] = multiplier
        sources["experience_confidence"] = confidence
        return weight * multiplier, sources

    def _enforce_diversity(
        self,
        decisions: dict[str, WeightDecision],
        factor_configs: dict[str, dict],
    ) -> dict[str, WeightDecision]:
        """相同标签类型总权重不超过 max_pct."""
        max_pct = self._diversity_max_pct
        if max_pct >= 1.0:
            return decisions

        # 收集每个因子的标签和权重
        type_weights: dict[str, float] = {}
        factor_tags: dict[str, list[str]] = {}

        for name in decisions:
            cfg = factor_configs.get(name, {})
            if isinstance(cfg, dict):
                tags = cfg.get("tags", [])
            else:
                tags = []
            factor_tags[name] = tags
            w = decisions[name].new_weight
            for tag in tags:
                type_weights[tag] = type_weights.get(tag, 0.0) + w

        total_weight = sum(d.new_weight for d in decisions.values())
        if total_weight <= 0:
            return decisions

        # 检查是否有类型超限
        over_limit_types = {
            tag: tw / total_weight
            for tag, tw in type_weights.items()
            if tw / total_weight > max_pct
        }

        if not over_limit_types:
            return decisions

        # 对首超限类型进行压降
        for tag, pct in over_limit_types.items():
            non_tag_weight = total_weight - type_weights[tag]
            if non_tag_weight <= 0:
                continue
            target_type_weight = max_pct * non_tag_weight / (1.0 - max_pct)
            scale = target_type_weight / type_weights[tag] if type_weights[tag] > 0 else 1.0

            for name in decisions:
                if tag in factor_tags.get(name, []):
                    old_w = decisions[name].new_weight
                    new_w = max(old_w * scale, 0.01)
                    decisions[name].new_weight = round(new_w, 4)
                    decisions[name].reason += f" | diversity({tag}): {pct:.0%}>{max_pct:.0%}"
                    decisions[name].source_scores["diversity"] = scale

        return decisions

    def _enforce_redundancy_cap(
        self,
        decisions: dict[str, WeightDecision],
        factor_configs: dict[str, dict],
    ) -> dict[str, WeightDecision]:
        """Cap total weight inside explicitly declared redundancy groups."""
        cap = float(self._redundancy_max_group_weight)
        if cap <= 0:
            return decisions

        groups: dict[str, list[str]] = {}
        for name in decisions:
            cfg = factor_configs.get(name, {})
            if not isinstance(cfg, dict):
                continue
            group = str(cfg.get("redundancy_group") or "").strip()
            if group:
                groups.setdefault(group, []).append(name)

        for group, names in groups.items():
            total = sum(max(0.0, decisions[name].new_weight) for name in names)
            if total <= cap or total <= 0:
                continue
            scale = cap / total
            for name in names:
                old_w = decisions[name].new_weight
                new_w = max(0.0, old_w * scale)
                decisions[name].new_weight = round(new_w, 4)
                decisions[name].reason += f" | redundancy({group}): {total:.3f}>{cap:.3f}"
                decisions[name].source_scores["redundancy_cap"] = scale
        return decisions

    def _build_reason(
        self,
        awe_info: dict | None,
        sources: dict[str, float],
        new_weight: float,
        old_w: float,
    ) -> str:
        """构建可读原因."""
        parts = []
        if awe_info:
            awe_reason = awe_info.get("reason", "")
            if awe_reason:
                parts.append(awe_reason)
        if sources.get("shadow_penalty", 0) < 0:
            parts.append(f"shadow_penalty={sources['shadow_penalty']:.4f}")
        if sources.get("regime_boost", 0) > 0:
            parts.append("regime_boost")
        if abs(new_weight - old_w) < 0.01:
            parts.append("unchanged")
        return "; ".join(parts) if parts else f"blended_w={new_weight:.4f}"

    def _fallback(
        self, current_weights: dict[str, float]
    ) -> dict[str, WeightDecision]:
        return {
            name: WeightDecision(
                factor=name,
                old_weight=w,
                new_weight=w,
                reason="fallback (no sources)",
                confidence=0.3,
            )
            for name, w in current_weights.items()
        }

    # ── 辅助: 从 decisions 提取扁平权重字典 ─────────────────────────

    @staticmethod
    def to_weights(decisions: dict[str, WeightDecision]) -> dict[str, float]:
        """{name: new_weight} — 直接用于 RuntimeConfig.patch()."""
        return {f: d.new_weight for f, d in decisions.items()}
