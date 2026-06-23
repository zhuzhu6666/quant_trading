"""Tests for alpha/decision_policy.py — 统一权重决策中枢."""

import pytest
from alpha.decision_policy import DecisionPolicy, WeightDecision


class TestDecisionPolicy:
    """核心决策逻辑验证."""

    def test_fallback_empty_sources(self):
        """没有来源时返回当前权重."""
        dp = DecisionPolicy()
        decisions = dp.decide(
            factor_configs={},
            current_weights={"a": 0.3, "b": 0.2},
        )
        assert set(decisions.keys()) == {"a", "b"}
        assert decisions["a"].new_weight == 0.3
        assert decisions["b"].new_weight == 0.2
        assert decisions["a"].reason == "fallback (no sources)"

    def test_awe_disable_respected(self):
        """AWE 禁用手 (weight=0) 无条件尊重."""
        dp = DecisionPolicy()
        decisions = dp.decide(
            awe_patches={"bad_factor": {"weight": 0.0, "reason": "health too low"}},
            weight_policy_weights={"bad_factor": 0.5, "good_factor": 0.5},
            factor_configs={"bad_factor": {}, "good_factor": {}},
            current_weights={"bad_factor": 0.3, "good_factor": 0.7},
        )
        assert decisions["bad_factor"].new_weight == 0.0
        assert "health too low" in decisions["bad_factor"].reason
        assert decisions["good_factor"].new_weight > 0

    def test_blend_awe_and_wp(self):
        """AWE 和 WeightPolicy blend 在两者之间."""
        dp = DecisionPolicy(awe_blend=0.6, wp_blend=0.4, max_weight=1.0)
        decisions = dp.decide(
            awe_patches={"f1": {"weight": 0.4, "reason": "score=0.5"}},
            weight_policy_weights={"f1": 0.3, "f2": 0.7},
            factor_configs={"f1": {}, "f2": {}},
            current_weights={"f1": 0.5, "f2": 0.5},
        )
        # f1: 0.4 * 0.6 + 0.3 * 0.4 + 0.5 * 0.0 = 0.24 + 0.12 = 0.36
        assert abs(decisions["f1"].new_weight - 0.36) < 0.01
        # f2: only WP source → 0.7 (direct return in _blend)
        assert abs(decisions["f2"].new_weight - 0.70) < 0.01

    def test_shadow_penalty_applied(self):
        """负 shadow OOS PnL 降低权重."""
        dp = DecisionPolicy(shadow_penalty=0.5)
        shadow_perfs = {"bad": type("ShadowPerf", (), {"cumulative_pnl": -0.05})()}
        decisions = dp.decide(
            weight_policy_weights={"bad": 0.5, "good": 0.5},
            shadow_perfs=shadow_perfs,
            factor_configs={"bad": {}, "good": {}},
            current_weights={"bad": 0.5, "good": 0.5},
        )
        # bad: 0.5 * 0.4 (WP only) + 0.5 * 0.6 (prev) = 0.5 → *0.5 shadow = 0.25
        assert decisions["bad"].new_weight < decisions["good"].new_weight
        assert "shadow_penalty" in decisions["bad"].source_scores

    def test_regime_boost(self):
        """Regime 匹配时权重增加."""
        dp = DecisionPolicy(regime_boost=1.2, diversity_max_pct=1.0)
        decisions = dp.decide(
            weight_policy_weights={"risk_on_factor": 0.3, "neutral": 0.3},
            factor_configs={
                "risk_on_factor": {"tags": ["risk_on"]},
                "neutral": {"tags": ["均值回归"]},
            },
            current_weights={"risk_on_factor": 0.5, "neutral": 0.5},
            regime="risk_on",
        )
        # risk_on_factor: blend = 0.3*0.4 + 0.5*0.6 = 0.42 → *1.2 = 0.504 → clamp=0.5
        # neutral: blend = 0.3*0.4 + 0.5*0.6 = 0.42 → no boost
        assert decisions["risk_on_factor"].new_weight >= decisions["neutral"].new_weight
        assert "regime_boost" in decisions["risk_on_factor"].source_scores

    def test_weight_clamping(self):
        """权重钳制到 [min, max]."""
        dp = DecisionPolicy(min_weight=0.01, max_weight=0.50)
        decisions = dp.decide(
            awe_patches={"f1": {"weight": 2.0, "reason": "huge score"}},
            factor_configs={"f1": {}},
            current_weights={"f1": 1.0},
        )
        assert decisions["f1"].new_weight <= 0.50
        assert decisions["f1"].new_weight >= 0.01

    def test_fast_decide_awe_only(self):
        """fast_decide 只处理有变化的因子."""
        dp = DecisionPolicy()
        decisions = dp.fast_decide(
            awe_patches={"f1": {"weight": 0.0, "reason": "disabled"}},
            factor_configs={"f1": {}, "f2": {}},
            current_weights={"f1": 0.3, "f2": 0.7},
        )
        assert "f1" in decisions
        assert decisions["f1"].new_weight == 0.0
        # f2 不在 patches 中, fast_decide 不应处理它
        assert "f2" not in decisions

    def test_to_weights(self):
        """辅助方法输出扁平字典."""
        decisions = {
            "a": WeightDecision(factor="a", old_weight=0.3, new_weight=0.5, reason="test"),
            "b": WeightDecision(factor="b", old_weight=0.7, new_weight=0.5, reason="test"),
        }
        flat = DecisionPolicy.to_weights(decisions)
        assert flat == {"a": 0.5, "b": 0.5}
