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
            "temporal_context": {"session_label": "europe", "hour_utc": 9},
        },
    )

    assert verdict.allowed is True
    assert verdict.reason == "ok"
    assert verdict.to_dict()["audit_payload"]["action"] == "open_trade"
    assert verdict.to_dict()["audit_payload"]["temporal_context"]["session_label"] == "europe"


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


def test_open_trade_blocks_on_disk_space_critical():
    service = _service()

    verdict = service.evaluate(
        "open_trade",
        {
            "runtime_health": {
                "system_health": {
                    "component_status": {"disk_space": "critical"},
                    "critical_components": ["disk_space"],
                }
            },
            "block_on_disk_critical": True,
        },
    )

    assert verdict.allowed is False
    assert verdict.reason == "disk_space_critical"
    assert verdict.audit_payload["source"] == "RiskGovernor"


def test_open_trade_blocks_on_l2_depth_only_when_required():
    service = _service()

    allowed = service.evaluate(
        "open_trade",
        {
            "runtime_health": {
                "system_health": {
                    "component_status": {"l2_depth": "critical"},
                    "critical_components": ["l2_depth"],
                }
            },
            "require_l2_depth": False,
        },
    )
    blocked = service.evaluate(
        "open_trade",
        {
            "runtime_health": {
                "system_health": {
                    "component_status": {"l2_depth": "critical"},
                    "critical_components": ["l2_depth"],
                }
            },
            "require_l2_depth": True,
        },
    )

    assert allowed.allowed is True
    assert blocked.allowed is False
    assert blocked.reason == "l2_depth_unavailable"


def test_open_trade_blocks_loss_cooldown_when_gap_too_short():
    service = _service()

    verdict = service.evaluate(
        "open_trade",
        {
            "session": {"consecutive_losses": 2},
            "loss_cooldown_after_losses": 2,
            "loss_cooldown_bars": 3,
            "temporal_context": {
                "timeframe": "M5",
                "timeframe_seconds": 300,
                "seconds_since_last_trade": 240.0,
                "bars_since_last_trade": 0.8,
            },
        },
    )

    assert verdict.allowed is False
    assert verdict.reason == "loss_cooldown_active"
    assert verdict.audit_payload["temporal_context"]["timeframe"] == "M5"


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


def test_close_position_marks_holding_timeout_in_audit():
    service = _service()

    verdict = service.evaluate(
        "close_position",
        {
            "position_id": "268",
            "close_reason": "holding_timeout",
            "holding_seconds": 3900.0,
            "max_holding_bars": 12,
            "timeframe_seconds": 300,
            "temporal_context": {"timeframe": "M5", "timeframe_seconds": 300},
        },
    )

    assert verdict.allowed is True
    assert verdict.audit_payload["holding_timeout_exceeded"] is True
    assert verdict.audit_payload["holding_minutes"] == 65.0


def test_update_weight_blocks_when_drawdown_near_limit():
    service = _service()

    verdict = service.evaluate(
        "update_weight",
        {"session": {"drawdown_pct": 12.0}},
    )

    assert verdict.allowed is False
    assert verdict.reason == "drawdown_approaching_limit"
    assert verdict.audit_payload["source"] == "RiskGovernor"


def test_switch_parameter_template_uses_weight_governor_thresholds():
    service = _service()

    verdict = service.evaluate(
        "switch_parameter_template",
        {"session": {"drawdown_pct": 12.0}, "required_mode": "governed"},
    )

    assert verdict.allowed is False
    assert verdict.reason == "drawdown_approaching_limit"
    assert verdict.required_mode == "governed"
    assert verdict.audit_payload["action"] == "switch_parameter_template"


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
