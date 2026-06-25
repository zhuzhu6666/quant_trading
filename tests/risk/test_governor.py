"""Tests for risk/governor.py — 最高层风控裁决器."""

import pytest
from risk.governor import RiskGovernor, GovernorState, GovernorVerdict


@pytest.fixture
def gov():
    RiskGovernor.reset()
    yield RiskGovernor.shared()
    RiskGovernor.reset()


class TestRiskGovernor:
    """核心裁决逻辑."""

    def test_allow_trade_ok(self, gov):
        """正常状态 → 允许交易."""
        v = gov.allow_trade(GovernorState())
        assert v.allowed
        assert v.reason == "ok"

    def test_circuit_broken_blocks_trade(self, gov):
        """熔断 → 禁止交易."""
        v = gov.allow_trade(GovernorState(circuit_broken=True))
        assert not v.allowed
        assert "circuit_broken" in v.reason

    def test_drawdown_blocks_trade(self, gov):
        """回撤超过 15% → 禁止交易."""
        v = gov.allow_trade(GovernorState(drawdown_pct=16.0))
        assert not v.allowed
        assert "drawdown" in v.reason

    def test_consecutive_losses_blocks_trade(self, gov):
        """连续亏损 8 笔 → 禁止交易."""
        v = gov.allow_trade(GovernorState(consecutive_losses=8))
        assert not v.allowed
        assert "consecutive_losses" in v.reason

    def test_daily_loss_blocks_trade(self, gov):
        """日亏损超 5% → 禁止交易."""
        v = gov.allow_trade(GovernorState(daily_loss_pct=6.0))
        assert not v.allowed
        assert "daily_loss_limit" in v.reason

    def test_data_lag_blocks_trade(self, gov):
        """数据延迟超过 1h → 禁止交易."""
        v = gov.allow_trade(GovernorState(data_lag_seconds=4000))
        assert not v.allowed
        assert "data_lag" in v.reason

    def test_loop_not_running_blocks_trade(self, gov):
        """live loop 未运行 → 禁止交易."""
        v = gov.allow_trade(GovernorState(loop_running=False))
        assert not v.allowed
        assert v.reason == "loop_not_running"

    def test_bridge_disconnected_blocks_trade(self, gov):
        """bridge 断开 → 禁止交易."""
        v = gov.allow_trade(GovernorState(bridge_connected=False))
        assert not v.allowed
        assert v.reason == "bridge_disconnected"

    def test_force_dry_run_blocks_trade(self, gov):
        """force_dry_run override → 禁止交易."""
        gov.set_dry_run(True)
        v = gov.allow_trade()
        assert not v.allowed
        assert v.reason == "force_dry_run"

    def test_allow_weight_update_drawdown(self, gov):
        """回撤接近上限 → 冻结权重更新."""
        v = gov.allow_weight_update(GovernorState(drawdown_pct=12.0))
        assert not v.allowed
        assert "drawdown" in v.reason

    def test_allow_weight_update_ok(self, gov):
        """正常状态 → 允许权重更新."""
        v = gov.allow_weight_update(GovernorState(drawdown_pct=5.0))
        assert v.allowed

    def test_allow_promotion_drawdown(self, gov):
        """中等回撤 → 暂停晋升."""
        v = gov.allow_promotion(GovernorState(drawdown_pct=11.0))
        assert not v.allowed

    def test_allow_promotion_ok(self, gov):
        """正常状态 → 允许晋升."""
        v = gov.allow_promotion(GovernorState(drawdown_pct=5.0))
        assert v.allowed

    def test_force_deleverage(self, gov):
        """强制降杠杆."""
        assert gov.force_deleverage() == 0.0
        gov.set_deleverage(0.5)
        assert gov.force_deleverage() == 0.5

    def test_force_dry_run_property(self, gov):
        """force_dry_run 属性."""
        assert not gov.force_dry_run()
        gov.set_dry_run(True)
        assert gov.force_dry_run()

    def test_update_config(self, gov):
        """动态调整阈值."""
        gov.update_config(max_drawdown_pct=10.0)
        v = gov.allow_trade(GovernorState(drawdown_pct=11.0))
        assert not v.allowed
        v = gov.allow_trade(GovernorState(drawdown_pct=9.0))
        assert v.allowed

    def test_singleton(self, gov):
        """单例."""
        g2 = RiskGovernor.shared()
        assert gov is g2
