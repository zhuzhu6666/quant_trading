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


def test_open_trade_blocks_when_loop_not_running():
    service = _service()

    verdict = service.evaluate(
        "open_trade",
        {"loop_running": False},
    )

    assert verdict.allowed is False
    assert verdict.reason == "loop_not_running"


def test_open_trade_blocks_when_bridge_disconnected():
    service = _service()

    verdict = service.evaluate(
        "open_trade",
        {"bridge_connected": False},
    )

    assert verdict.allowed is False
    assert verdict.reason == "bridge_disconnected"


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


def test_close_position_is_allowed_as_risk_reducing_action():
    service = _service()

    verdict = service.evaluate(
        "close_position",
        {"position_id": "268", "close_reason": "manual"},
    )

    assert verdict.allowed is True
    assert verdict.reason == "risk_reducing_action"
    assert verdict.audit_payload["position_id"] == "268"


def test_update_weight_blocks_when_drawdown_near_limit():
    service = _service()

    verdict = service.evaluate(
        "update_weight",
        {"session": {"drawdown_pct": 12.0}},
    )

    assert verdict.allowed is False
    assert verdict.reason == "drawdown_approaching_limit"
    assert verdict.audit_payload["source"] == "RiskGovernor"


def test_promote_and_register_factor_use_governor_thresholds():
    service = _service()

    promote = service.evaluate("promote_factor", {"session": {"drawdown_pct": 11.0}})
    register = service.evaluate("register_factor", {"session": {"drawdown_pct": 10.0}})

    assert promote.allowed is False
    assert promote.reason == "drawdown_too_high_for_promotion"
    assert register.allowed is False
    assert register.reason == "drawdown_too_high_for_new_factor"


def test_start_shadow_model_blocks_live_trading_capability():
    service = _service()

    verdict = service.evaluate(
        "start_shadow_model",
        {"candidate_id": "cand_1", "capabilities": {"live_trading": True}},
    )

    assert verdict.allowed is False
    assert verdict.reason == "live_trading_capability_not_allowed"
    assert verdict.required_mode == "shadow"


def test_start_canary_model_blocks_unexpected_candidate_status():
    service = _service()

    verdict = service.evaluate(
        "start_canary_model",
        {
            "candidate_id": "cand_2",
            "candidate_status": "queued",
            "allowed_statuses": ["shadow_passed", "canary_ready"],
        },
    )

    assert verdict.allowed is False
    assert verdict.reason == "candidate_status_not_allowed"
    assert verdict.audit_payload["candidate_status"] == "queued"


def test_start_canary_model_allows_advisory_candidate():
    service = _service()

    verdict = service.evaluate(
        "start_canary_model",
        {
            "candidate_id": "cand_3",
            "candidate_status": "shadow_passed",
            "allowed_statuses": ["shadow_passed", "canary_ready"],
            "capabilities": {"live_trading": False},
        },
    )

    assert verdict.allowed is True
    assert verdict.required_mode == "canary"
