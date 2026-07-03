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
        {"system": {"mode": "live"}, "ctrader": {"send_orders": True}},
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
        {"system": {"mode": "live"}, "ctrader": {"send_orders": True}},
        RuntimeConfig(ctrader_send_orders=True, factor_dry_run=False),
    )

    assert opens_effective_send_orders(before, after) is True
