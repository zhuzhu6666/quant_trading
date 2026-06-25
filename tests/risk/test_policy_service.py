from risk.governor import RiskGovernor
from risk.policy_service import RiskPolicyService


def _service() -> RiskPolicyService:
    RiskGovernor.reset()
    RiskPolicyService.reset()
    return RiskPolicyService.shared()


def test_open_trade_allowed_with_clean_context():
    service = _service()

    verdict = service.evaluate(
        "open_trade",
        {
            "account": {"balance": 10000, "equity": 10000},
            "session": {"pnl": 0, "start_balance": 10000},
            "open_position_count": 1,
            "max_position_count": 3,
            "total_api_volume": 100,
            "requested_api_volume": 100,
            "max_position_api_volume": 1000,
        },
    )

    assert verdict.allowed is True
    assert verdict.reason == "ok"
    assert verdict.to_dict()["audit_payload"]["action"] == "open_trade"


def test_open_trade_blocks_circuit_breaker():
    service = _service()

    verdict = service.evaluate(
        "open_trade",
        {"session": {"circuit_breaker": True}},
    )

    assert verdict.allowed is False
    assert verdict.reason == "circuit_broken"
    assert verdict.audit_payload["source"] == "RiskGovernor"


def test_open_trade_blocks_var_threshold():
    service = _service()

    verdict = service.evaluate(
        "open_trade",
        {
            "risk_snapshot": {"var": {"var_pct": 3.5}},
            "var": {"enabled": True, "threshold_pct": 2.0},
        },
    )

    assert verdict.allowed is False
    assert verdict.reason == "var_gate: VaR=3.5% > 2.0%"
    assert verdict.audit_payload["source"] == "var_gate"


def test_open_trade_blocks_position_count():
    service = _service()

    verdict = service.evaluate(
        "open_trade",
        {"open_position_count": 3, "max_position_count": 3},
    )

    assert verdict.allowed is False
    assert verdict.reason == "仓位上限: 3/3"
    assert verdict.audit_payload["source"] == "position_count"


def test_open_trade_blocks_api_volume():
    service = _service()

    verdict = service.evaluate(
        "open_trade",
        {
            "total_api_volume": 950,
            "requested_api_volume": 100,
            "max_position_api_volume": 1000,
        },
    )

    assert verdict.allowed is False
    assert verdict.reason == "API量上限: 950+100>1000"
    assert verdict.max_size == 50


def test_open_trade_blocks_pyramid_weaker_signal():
    service = _service()

    verdict = service.evaluate(
        "open_trade",
        {
            "open_position_count": 1,
            "pyramid_enabled": True,
            "max_abs_entry_score": 0.7,
            "signal_score": 0.6,
        },
    )

    assert verdict.allowed is False
    assert verdict.reason == "金字塔: 需超 0.7000"
    assert verdict.audit_payload["source"] == "pyramid"
