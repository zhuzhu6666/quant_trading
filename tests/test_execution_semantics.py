from backend.services.execution_semantics import evaluate_execution_semantics, opens_effective_send_orders
from config.runtime_config import RuntimeConfig


def test_backtest_send_orders_is_invalid_and_not_effective():
    semantics = evaluate_execution_semantics(
        {"system": {"mode": "backtest"}, "ctrader": {"send_orders": True}},
        RuntimeConfig(ctrader_send_orders=True, factor_dry_run=False),
    )

    assert semantics.effective_send_orders is False
    assert semantics.blocking_reason == "ctrader_send_orders_requires_system_mode_live"


def test_live_send_orders_is_effective_when_not_dry_run():
    semantics = evaluate_execution_semantics(
        {"system": {"mode": "live"}, "ctrader": {"send_orders": True, "host": "demo.ctraderapi.com"}},
        RuntimeConfig(ctrader_send_orders=True, factor_dry_run=False),
    )

    assert semantics.effective_send_orders is True
    assert semantics.blocking_reason == ""


def test_opens_effective_send_orders_detects_transition():
    before = evaluate_execution_semantics(
        {"system": {"mode": "live"}, "ctrader": {"send_orders": False}},
        RuntimeConfig(ctrader_send_orders=False, factor_dry_run=False),
    )
    after = evaluate_execution_semantics(
        {"system": {"mode": "live"}, "ctrader": {"send_orders": True, "host": "demo.ctraderapi.com"}},
        RuntimeConfig(ctrader_send_orders=True, factor_dry_run=False),
    )

    assert opens_effective_send_orders(before, after) is True


def test_wide_demo_risk_limits_are_allowed_on_demo_host():
    semantics = evaluate_execution_semantics(
        {
            "system": {"mode": "live"},
            "ctrader": {"send_orders": True, "host": "demo.ctraderapi.com"},
        },
        RuntimeConfig(
            ctrader_send_orders=True,
            factor_dry_run=False,
            risk_max_daily_loss_pct=50.0,
            risk_max_daily_trades=100,
        ),
    )

    assert semantics.effective_send_orders is True
    assert semantics.blocking_reason == ""


def test_wide_demo_risk_limits_are_blocked_on_non_demo_host():
    semantics = evaluate_execution_semantics(
        {
            "system": {"mode": "live"},
            "ctrader": {"send_orders": True, "host": "live.ctraderapi.com"},
        },
        RuntimeConfig(
            ctrader_send_orders=True,
            factor_dry_run=False,
            risk_max_daily_loss_pct=50.0,
            risk_max_daily_trades=100,
        ),
    )

    assert semantics.effective_send_orders is False
    assert semantics.blocking_reason == "demo_daily_loss_limit_requires_demo_ctrader_host"


def test_wide_demo_trade_count_is_independently_blocked_on_non_demo_host():
    semantics = evaluate_execution_semantics(
        {
            "system": {"mode": "live"},
            "ctrader": {"send_orders": True, "host": "live.ctraderapi.com"},
        },
        RuntimeConfig(
            ctrader_send_orders=True,
            factor_dry_run=False,
            risk_max_daily_loss_pct=5.0,
            risk_max_daily_trades=100,
        ),
    )

    assert semantics.effective_send_orders is False
    assert semantics.blocking_reason == "demo_daily_trade_limit_requires_demo_ctrader_host"


def test_demo_learning_trade_count_cannot_leak_to_non_demo_host():
    semantics = evaluate_execution_semantics(
        {
            "system": {"mode": "live"},
            "ctrader": {"send_orders": True, "host": "live.ctraderapi.com"},
        },
        RuntimeConfig(
            ctrader_send_orders=True,
            factor_dry_run=False,
            risk_max_daily_loss_pct=5.0,
            risk_max_daily_trades=20,
            demo_learning_max_daily_trades=100,
        ),
    )

    assert semantics.effective_send_orders is False
    assert semantics.blocking_reason == "demo_daily_trade_limit_requires_demo_ctrader_host"
