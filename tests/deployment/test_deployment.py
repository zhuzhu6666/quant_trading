"""tests/deployment/test_deployment.py — Phase 2.3 金丝雀部署单元测试

覆盖:
    weight_policy:  3 种策略, 边界, 空输入, 上限钳制
    canary:         check_promotion | promote | rollback | evaluate_all
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from deployment.weight_policy import (
    WeightPolicy,
    WeightConfig,
    DEFAULT_MAX_SINGLE_WEIGHT,
)
from deployment.canary import (
    CanaryDirector,
    CanaryEvalContext,
    SHADOW,
    CANARY_5,
    CANARY_20,
    CANARY_50,
    ACTIVE,
)


# ====================================================================
# WeightPolicy
# ====================================================================


class TestWeightPolicy:
    """WeightPolicy — 3 策略 + 边界"""

    def test_linear_policy(self):
        """linear 策略: 高分得高权, 低分裁掉"""
        wp = WeightPolicy(WeightConfig(policy="linear"))
        scores = {"a": 80.0, "b": 50.0, "c": 20.0}  # c < watch_threshold=40
        result = wp.compute_weights(scores)
        assert "a" in result
        assert "b" in result
        assert "c" in result
        # a 权重应 >= b (上限钳制 0.5 后可能相等)
        assert result["a"] >= result["b"]
        # c 应得极低权重 (低于阈值, linear 会最小权)
        assert result["c"] < result["b"]
        # 总和 = 1
        assert abs(sum(result.values()) - 1.0) < 1e-5
        # 单因子上限
        for w in result.values():
            assert w <= DEFAULT_MAX_SINGLE_WEIGHT + 1e-6

    def test_softmax_policy(self):
        """softmax 策略: 拉开高分差距, 低分裁掉"""
        wp = WeightPolicy(WeightConfig(policy="softmax"))
        scores = {"x": 90.0, "y": 80.0, "z": 10.0}
        result = wp.compute_weights(scores)
        # x 和 y 获得几乎全部权重, z 极低
        # 经 max_single_weight=0.5 钳制后 x 的超额分给 y, 最终均达上限
        assert result["x"] >= result["y"]
        assert result["z"] < 0.01
        assert abs(sum(result.values()) - 1.0) < 1e-5

    def test_threshold_policy(self):
        """threshold 策略: >=70 满权, >=40 半权, <40 裁 0"""
        wp = WeightPolicy(WeightConfig(policy="threshold"))
        scores = {"a": 85.0, "b": 55.0, "c": 20.0}
        result = wp.compute_weights(scores)
        # a(1) + b(0.5) = 1.5, 归一化后 a 权重 = 1/1.5 ≈ 0.6667, b = 0.5/1.5 ≈ 0.3333
        # 经 max_single_weight=0.5 钳制后, a 的超额分配给 b, 最终各 0.5
        assert abs(result["a"] + result["b"] - 1.0) < 1e-5
        assert result["c"] < 1e-6  # c=20 被裁 0
        assert result["a"] >= result["b"]

    def test_empty_input(self):
        """空输入返回空 dict"""
        wp = WeightPolicy()
        assert wp.compute_weights({}) == {}

    def test_max_single_weight_clamp(self):
        """上限钳制 max_single_weight=0.5"""
        wp = WeightPolicy(WeightConfig(policy="equal_weight", max_single_weight=0.5))
        # 只有 2 个因子, 按理各 0.5, 恰好在边界
        result = wp.compute_weights({"a": 80.0, "b": 80.0})
        assert abs(result["a"] - 0.5) < 1e-6
        assert abs(result["b"] - 0.5) < 1e-6

    def test_single_factor(self):
        """单因子时权重应为 1.0"""
        wp = WeightPolicy()
        result = wp.compute_weights({"only": 95.0})
        assert abs(result["only"] - 1.0) < 1e-6

    def test_register_custom_policy(self):
        """register_policy: 自定义策略"""
        wp = WeightPolicy()
        wp.register_policy("custom", lambda s, c: np.full(len(s), 0.5))
        result = wp.compute_weights({"a": 50.0, "b": 50.0})
        # 全 0.5, 归一化后各 0.5
        assert abs(result["a"] - 0.5) < 1e-6
        assert abs(result["b"] - 0.5) < 1e-6

    def test_available_policies(self):
        """available_policies 列出所有注册策略"""
        wp = WeightPolicy()
        assert "linear" in wp.available_policies
        assert "softmax" in wp.available_policies
        assert "threshold" in wp.available_policies

    def test_all_scores_below_watch(self):
        """全低于阈值 → 均匀分配 (兜底)"""
        wp = WeightPolicy(WeightConfig(policy="linear"))
        result = wp.compute_weights({"a": 10.0, "b": 20.0, "c": 30.0})
        # 全低于 40, 兜底均匀分
        assert abs(sum(result.values()) - 1.0) < 1e-5
        for v in result.values():
            assert abs(v - 1.0 / 3) < 1e-4


# ====================================================================
# CanaryDirector
# ====================================================================


class TestCanaryDirector:
    """CanaryDirector — 晋升/回滚/批量"""

    def test_initial_stage_is_shadow(self):
        """因子初始状态为 SHADOW"""
        d = CanaryDirector()
        assert d.get_stage("test") == SHADOW

    def test_promote_from_shadow_to_canary5(self):
        """晋升: SHADOW -> CANARY_5"""
        d = CanaryDirector()
        ctx = CanaryEvalContext(oos_bars=10, oos_pnl=0.005)
        action = d.check_promotion("f1", ctx)
        assert action == "promote"
        ok = d.promote("f1")
        assert ok
        assert d.get_stage("f1") == CANARY_5

    def test_promote_full_ladder(self):
        """完整晋升梯: SHADOW -> CANARY_5 -> CANARY_20 -> CANARY_50 -> ACTIVE"""
        d = CanaryDirector()

        promotions = [
            (CanaryEvalContext(oos_bars=10, oos_pnl=0.005), CANARY_5),
            (CanaryEvalContext(oos_bars=25, oos_pnl=0.004), CANARY_20),
            (CanaryEvalContext(oos_bars=60, oos_pnl=0.006), CANARY_50),
            (CanaryEvalContext(oos_bars=100, oos_pnl=0.01), ACTIVE),
        ]
        for ctx, expected_stage in promotions:
            action = d.check_promotion("f1", ctx)
            assert action == "promote", (
                f"check_promotion expected 'promote', got '{action}' "
                f"for {expected_stage}"
            )
            ok = d.promote("f1")
            assert ok
            assert d.get_stage("f1") == expected_stage, (
                f"expected {expected_stage}, got {d.get_stage('f1')}"
            )

        # ACTIVE 之后不能再晋升
        assert not d.can_promote("f1")
        action = d.check_promotion("f1", CanaryEvalContext())
        assert action == "stay"
        assert not d.promote("f1")

    def test_insufficient_bars_stays(self):
        """bar 数不够 → stay"""
        d = CanaryDirector()
        ctx = CanaryEvalContext(oos_bars=2, oos_pnl=0.01)  # < 5 bars
        action = d.check_promotion("f1", ctx)
        assert action == "stay"

    def test_insufficient_pnl_stays(self):
        """PnL 不够 → stay (非 rollback 范围)"""
        d = CanaryDirector()
        ctx = CanaryEvalContext(oos_bars=10, oos_pnl=0.0001)  # < 0.001
        action = d.check_promotion("f1", ctx)
        assert action == "stay"

    def test_negative_pnl_triggers_rollback(self):
        """PnL 严重负值 → rollback"""
        d = CanaryDirector()
        # 先晋升到 CANARY_5
        assert d.promote("f1")  # 从 SHADOW 升
        assert d.get_stage("f1") == CANARY_5

        # PnL 低于 rollback 阈值 (min_pnl * ROLLBACK_PNL_RATIO = 0.001 * -0.5 = -0.0005)
        ctx = CanaryEvalContext(oos_bars=10, oos_pnl=-0.01)
        action = d.check_promotion("f1", ctx)
        assert action == "rollback", f"expected rollback, got {action}"

    def test_rollback_to_shadow(self):
        """rollback: 回到 SHADOW, rollback_count+1"""
        d = CanaryDirector()
        d.promote("f1")
        assert d.get_stage("f1") == CANARY_5

        ok = d.rollback("f1")
        assert ok
        assert d.get_stage("f1") == SHADOW
        assert d.get_state("f1").rollback_count == 1

    def test_rollback_already_shadow(self):
        """已在 SHADOW → rollback 返回 False"""
        d = CanaryDirector()
        assert not d.rollback("f1")

    def test_evaluate_all_batch(self):
        """evaluate_all: 批量处理"""
        d = CanaryDirector()
        results = d.evaluate_all({
            "f_good": CanaryEvalContext(oos_bars=10, oos_pnl=0.005),
            "f_bad": CanaryEvalContext(oos_bars=2, oos_pnl=0.0),
        })
        assert len(results) == 2
        by_action = {r["factor"]: r["action"] for r in results}
        assert by_action["f_good"] == "promote"
        assert by_action["f_bad"] == "stay"

    def test_summary(self):
        """summary: 正确统计各阶段"""
        d = CanaryDirector()
        d.promote("f1")     # -> CANARY_5
        d.promote("f2")     # -> CANARY_5
        # 手动设一个到 ACTIVE 用尽量多的晋升
        for _ in range(4):
            d.promote("f3")

        s = d.summary()
        assert s[CANARY_5]["count"] == 2  # f1, f2
        assert ACTIVE in s
        # f3 的 count 判断
        # 注意: f3 promote 了 4 次从 SHADOW -> CANARY_5 -> CANARY_20 -> CANARY_50 -> ACTIVE
        assert s[ACTIVE]["count"] == 1

    def test_summary_text(self):
        """summary_text: 可读报告不为空"""
        d = CanaryDirector()
        d.promote("f1")
        text = d.summary_text()
        assert "CANARY DEPLOYMENT SUMMARY" in text
        assert "SHADOW" in text
        assert "CANARY_5" in text

    def test_factor_report(self):
        """factor_report: 单因子详情"""
        d = CanaryDirector()
        d.promote("f1")
        report = d.factor_report("f1")
        assert report["factor"] == "f1"
        assert report["stage"] == CANARY_5
        assert report["oos_bars"] == 0
        assert report["rollback_count"] == 0

    def test_progress_cb(self):
        """progress_cb 被调用"""
        d = CanaryDirector()
        events = []

        def cb(phase, pct, msg):
            events.append((phase, pct))

        ctx = CanaryEvalContext(oos_bars=10, oos_pnl=0.005)
        d.check_promotion("f1", ctx, progress_cb=cb)
        assert len(events) > 0
        assert any(e[0] == "check_promotion" for e in events)

        # promote 也有 cb
        events.clear()
        d.promote("f1", progress_cb=cb)
        assert any(e[0] == "promote" for e in events)

    def test_can_promote(self):
        """can_promote: ACTIVE 前都能升"""
        d = CanaryDirector()
        assert d.can_promote("f1")
        # 升到 ACTIVE
        for _ in range(4):
            d.promote("f1")
        assert not d.can_promote("f1")

    def test_from_pnl_series(self):
        """CanaryEvalContext.from_pnl_series"""
        pnl = [0.001, 0.002, -0.0005, 0.003]
        ctx = CanaryEvalContext.from_pnl_series(pnl)
        assert ctx.oos_bars == 4
        assert abs(ctx.oos_pnl - 0.0055) < 1e-8  # 0.001+0.002-0.0005+0.003 = 0.0055


