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


def test_open_trade_uses_risk_limit_snapshot_for_governor_thresholds():
    service = _service()

    verdict = service.evaluate(
        "open_trade",
        {
            "session": {"daily_loss_pct": 4.0},
            "risk_limits": {"max_daily_loss_pct": 3.0},
        },
    )

    assert verdict.allowed is False
    assert verdict.reason == "daily_loss_limit"
    assert verdict.audit_payload["state"]["risk_limits"]["max_daily_loss_pct"] == 3.0


def test_open_trade_uses_risk_limit_snapshot_for_var_thresholds():
    service = _service()

    verdict = service.evaluate(
        "open_trade",
        {
            "risk_snapshot": {"var": {"var_pct": 1.6}},
            "var": {"enabled": True},
            "risk_limits": {"var_threshold_pct": 1.5},
        },
    )

    assert verdict.allowed is False
    assert verdict.reason == "var_gate: VaR=1.6% > 1.5%"


def test_open_trade_blocks_cvar_threshold_from_risk_limits():
    service = _service()

    verdict = service.evaluate(
        "open_trade",
        {
            "risk_snapshot": {"var": {"var_pct": 0.5, "cvar_pct": 2.4}},
            "var": {"enabled": True},
            "risk_limits": {"var_threshold_pct": 2.0, "cvar_threshold_pct": 2.0},
        },
    )

    assert verdict.allowed is False
    assert verdict.reason == "cvar_gate: CVaR=2.4% > 2.0%"
    assert verdict.audit_payload["source"] == "cvar_gate"


def test_open_trade_blocks_event_filter_context_from_risk_policy():
    service = _service()

    verdict = service.evaluate(
        "open_trade",
        {
            "event_filter": {
                "schema_version": "event_risk_filter.v1",
                "active": True,
                "blocked": True,
                "reason": "nfp_skip:event_bucket",
                "source": "execution_gate_event_filter",
                "authority": "RiskPolicyService",
            },
        },
    )

    assert verdict.allowed is False
    assert verdict.reason == "nfp_skip:event_bucket"
    assert verdict.audit_payload["blocked_by"] == "event_risk_filter"


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


def test_open_trade_allows_after_loss_cooldown_even_at_hard_loss_threshold():
    service = _service()

    verdict = service.evaluate(
        "open_trade",
        {
            "account": {"balance": 10000, "equity": 10000},
            "session": {"consecutive_losses": 8},
            "loss_cooldown_after_losses": 2,
            "loss_cooldown_bars": 3,
            "temporal_context": {
                "timeframe": "M5",
                "timeframe_seconds": 300,
                "seconds_since_last_trade": 1800.0,
                "bars_since_last_trade": 6.0,
            },
            "total_api_volume": 0,
            "requested_api_volume": 100,
            "max_position_api_volume": 1000,
        },
    )

    assert verdict.allowed is True
    assert verdict.reason == "ok"


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


def test_open_trade_blocks_event_below_min_only_in_high_hard_window():
    service = _service()

    verdict = service.evaluate(
        "open_trade",
        {
            "total_api_volume": 0,
            "requested_api_volume": 100,
            "max_position_api_volume": 1000,
            "event_sizing": {
                "enabled": True,
                "event_importance": 3,
                "minutes_until_event": 10.0,
                "window_bucket": "pre_0_15m",
                "base_api_volume": 100.0,
                "raw_api_volume": 50.0,
                "adjusted_api_volume": 0.0,
                "effective_requested_api_volume": 100.0,
                "blocked_reason": "event_sizing_below_min: 100*0.50=50<100",
            },
        },
    )

    assert verdict.allowed is False
    assert verdict.reason.startswith("event_hard_window:")
    assert verdict.audit_payload["source"] == "event_sizing"


def test_open_trade_allows_event_below_min_outside_high_hard_window():
    service = _service()

    verdict = service.evaluate(
        "open_trade",
        {
            "account": {"balance": 10000, "equity": 10000},
            "session": {"pnl": 0, "start_balance": 10000},
            "total_api_volume": 0,
            "requested_api_volume": 100,
            "max_position_api_volume": 1000,
            "event_sizing": {
                "enabled": True,
                "event_importance": 3,
                "minutes_until_event": 45.0,
                "window_bucket": "pre_30_60m",
                "base_api_volume": 100.0,
                "raw_api_volume": 80.0,
                "adjusted_api_volume": 0.0,
                "effective_requested_api_volume": 100.0,
                "blocked_reason": "event_sizing_below_min: 100*0.80=80<100",
            },
        },
    )

    assert verdict.allowed is True
    assert verdict.audit_payload["event_sizing"]["window_bucket"] == "pre_30_60m"


def test_open_trade_blocks_approved_event_window_learning_policy():
    service = _service()

    verdict = service.evaluate(
        "open_trade",
        {
            "account": {"balance": 10000, "equity": 10000},
            "session": {"pnl": 0, "start_balance": 10000},
            "total_api_volume": 0,
            "requested_api_volume": 100,
            "max_position_api_volume": 1000,
            "event_sizing": {
                "schema_version": "event_sizing.short_window.v2",
                "enabled": True,
                "event_type": "NFP",
                "event": "Non-Farm Employment Change",
                "event_importance": 3,
                "minutes_until_event": 10.0,
                "window_bucket": "pre_0_15m",
                "multiplier": 0.5,
            },
            "event_window_learning_policy": {
                "active": True,
                "controls": [
                    {
                        "scope_key": "NFP:pre_0_15m",
                        "action": "tighten_event_window_sizing",
                        "suggestion_id": "psg_event_window_nfp",
                    }
                ],
            },
        },
    )

    assert verdict.allowed is False
    assert verdict.reason == "learning_event_window_control"
    assert verdict.audit_payload["blocked_by"] == "event_window_learning_policy"


def test_open_trade_blocks_approved_entry_quality_weak_signal_policy():
    service = _service()

    verdict = service.evaluate(
        "open_trade",
        {
            "entry_quality_gate": {
                "active": True,
                "allowed": False,
                "reason": "learning_weak_signal_threshold",
                "source": "entry_quality_gate",
                "suggestion_id": "psg_entry_quality_weak",
            },
        },
    )

    assert verdict.allowed is False
    assert verdict.reason == "learning_weak_signal_threshold"
    assert verdict.audit_payload["blocked_by"] == "entry_quality_gate"
    assert verdict.audit_payload["entry_quality_gate"]["suggestion_id"] == "psg_entry_quality_weak"


def test_open_trade_blocks_approved_entry_quality_factor_conflict_policy():
    service = _service()

    verdict = service.evaluate(
        "open_trade",
        {
            "entry_quality_gate": {
                "active": True,
                "allowed": False,
                "reason": "learning_factor_conflict_control",
                "source": "entry_quality_gate",
                "suggestion_id": "psg_entry_quality_conflict",
            },
        },
    )

    assert verdict.allowed is False
    assert verdict.reason == "learning_factor_conflict_control"
    assert verdict.audit_payload["entry_quality_gate"]["suggestion_id"] == "psg_entry_quality_conflict"


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


def test_position_supervisor_switch_requires_replay_and_counterfactual_evidence():
    service = _service()

    verdict = service.evaluate(
        "switch_position_supervisor_template",
        {
            "suggestion_status": "approved",
            "target_template_id": "position_supervisor:conservative.v1",
            "previous_template_id": "position_supervisor:default.v1",
            "evidence": {"replay_summary": {"sample_count": 10}},
        },
    )

    assert verdict.allowed is False
    assert verdict.reason == "missing_supervisor_switch_evidence"
    assert verdict.audit_payload["has_replay"] is True
    assert verdict.audit_payload["has_counterfactual"] is False


def test_position_supervisor_autonomous_switch_requires_demo_mode(monkeypatch):
    from types import SimpleNamespace
    from config import runtime_config as rc

    monkeypatch.setattr(rc, "shared", lambda: SimpleNamespace(autonomy_mode="manual"))
    service = _service()

    verdict = service.evaluate(
        "switch_position_supervisor_template",
        {
            "suggestion_status": "approved",
            "target_template_id": "position_supervisor:conservative.v1",
            "previous_template_id": "position_supervisor:default.v1",
            "autonomous_apply": True,
            "evidence": {
                "replay_summary": {"sample_count": 10},
                "counterfactual_summary": {"labels": {"over_protected": 3}},
            },
        },
    )

    assert verdict.allowed is False
    assert verdict.reason == "autonomous_deploy_mode_not_allowed"


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


def test_switch_position_supervisor_template_requires_approval_and_valid_template():
    service = _service()

    blocked = service.evaluate(
        "switch_position_supervisor_template",
        {
            "suggestion_status": "proposed",
            "target_template_id": "position_supervisor:conservative.v1",
            "evidence": {"day": "2026-06-29"},
        },
    )
    assert blocked.allowed is False
    assert blocked.reason == "suggestion_not_approved"

    invalid = service.evaluate(
        "switch_position_supervisor_template",
        {
            "suggestion_status": "approved",
            "target_template_id": "position_supervisor:unknown.v1",
            "evidence": {"day": "2026-06-29"},
        },
    )
    assert invalid.allowed is False
    assert invalid.reason == "invalid_position_supervisor_template"

    allowed = service.evaluate(
        "switch_position_supervisor_template",
        {
            "suggestion_status": "approved",
            "target_template_id": "position_supervisor:conservative.v1",
            "previous_template_id": "position_supervisor:default.v1",
            "evidence": {
                "replay_summary": {"sample_count": 3},
                "counterfactual_summary": {"labels": {"over_protected": 1}},
            },
        },
    )
    assert allowed.allowed is True
    assert allowed.audit_payload["target_template_id"] == "position_supervisor:conservative.v1"


def test_update_weight_blocks_when_drawdown_near_limit():
    service = _service()

    verdict = service.evaluate(
        "update_weight",
        {"session": {"drawdown_pct": 12.0}},
    )

    assert verdict.allowed is False
    assert verdict.reason == "drawdown_approaching_limit"
    assert verdict.audit_payload["source"] == "RiskGovernor"


def test_switch_parameter_template_uses_template_switch_governor_thresholds():
    service = _service()

    verdict = service.evaluate(
        "switch_parameter_template",
        {"session": {"drawdown_pct": 12.0}, "required_mode": "governed"},
    )

    assert verdict.allowed is False
    assert verdict.reason == "drawdown_approaching_limit"
    assert verdict.required_mode == "governed"
    assert verdict.audit_payload["action"] == "switch_parameter_template"


def test_factor_disable_has_independent_governor_freeze():
    service = _service()
    service.governor.set_override("force_factor_disable_freeze", True)

    verdict = service.evaluate(
        "disable_factor_live",
        {"session": {"drawdown_pct": 0.0}, "required_mode": "autonomous_governance"},
    )

    assert verdict.allowed is False
    assert verdict.reason == "force_factor_disable_freeze"
    assert verdict.required_mode == "autonomous_governance"


def test_factor_rollback_is_not_blocked_by_weight_freeze():
    service = _service()
    service.governor.set_override("force_weight_freeze", True)

    verdict = service.evaluate(
        "rollback_factor_action",
        {"session": {"drawdown_pct": 12.0}, "required_mode": "autonomous_governance"},
    )

    assert verdict.allowed is True


def test_incident_control_blocks_new_risk_but_allows_risk_reduction():
    service = _service()

    blocked = service.evaluate("open_trade", {"runtime_incident_mode": "no_new_risk"})
    close_allowed = service.evaluate("close_position", {"runtime_incident_mode": "no_new_risk"})
    rollback_allowed = service.evaluate("rollback_factor_action", {"runtime_incident_mode": "no_new_risk"})

    assert blocked.allowed is False
    assert blocked.reason == "incident_no_new_risk"
    assert blocked.audit_payload["source"] == "runtime_incident_control"
    assert close_allowed.allowed is True
    assert rollback_allowed.allowed is True


def test_incident_control_only_close_blocks_adjustments_and_governance():
    service = _service()

    tighten = service.evaluate(
        "tighten_position",
        {"runtime_incident_mode": "only_close", "loop_running": True, "bridge_connected": True},
    )
    update_weight = service.evaluate("update_weight", {"runtime_incident_mode": "only_close"})
    close_allowed = service.evaluate("close_position", {"runtime_incident_mode": "only_close"})

    assert tighten.allowed is False
    assert tighten.reason == "incident_only_close"
    assert update_weight.allowed is False
    assert update_weight.reason == "incident_only_close"
    assert close_allowed.allowed is True


def test_incident_control_relax_requires_confirm():
    service = _service()

    blocked = service.evaluate(
        "set_incident_control",
        {"current_mode": "frozen", "target_mode": "normal"},
    )
    allowed = service.evaluate(
        "set_incident_control",
        {"current_mode": "frozen", "target_mode": "normal", "confirm_thaw": True},
    )

    assert blocked.allowed is False
    assert blocked.reason == "incident_control_relax_requires_confirm"
    assert allowed.allowed is True
    assert allowed.audit_payload["relaxing"] is True


def test_tighten_position_allows_bounded_tp_extension_with_profit_lock():
    service = _service()

    verdict = service.evaluate(
        "tighten_position",
        {
            "loop_running": True,
            "bridge_connected": True,
            "position_id": "p1",
            "position": {
                "direction": "buy",
                "entry_price": 4000.0,
                "sl": 4002.0,
                "tp": 4030.0,
            },
            "recommended_controls": {
                "target_stop_loss": 4010.0,
                "target_take_profit": 4038.0,
                "max_tp_extension_factor": 0.35,
            },
        },
    )

    assert verdict.allowed is True
    assert verdict.reason == "risk_reducing_action"


def test_tighten_position_blocks_tp_extension_without_profit_lock():
    service = _service()

    verdict = service.evaluate(
        "tighten_position",
        {
            "loop_running": True,
            "bridge_connected": True,
            "position_id": "p1",
            "position": {
                "direction": "buy",
                "entry_price": 4000.0,
                "sl": 3990.0,
                "tp": 4030.0,
            },
            "recommended_controls": {
                "target_take_profit": 4038.0,
                "max_tp_extension_factor": 0.35,
            },
        },
    )

    assert verdict.allowed is False
    assert verdict.reason == "tp_extension_requires_profit_lock"
    assert verdict.audit_payload["source"] == "tp_extension_guard"


def test_tighten_position_blocks_oversized_tp_extension():
    service = _service()

    verdict = service.evaluate(
        "tighten_position",
        {
            "loop_running": True,
            "bridge_connected": True,
            "position_id": "p1",
            "position": {
                "direction": "buy",
                "entry_price": 4000.0,
                "sl": 4010.0,
                "tp": 4030.0,
            },
            "recommended_controls": {
                "target_take_profit": 4050.0,
                "max_tp_extension_factor": 0.35,
            },
        },
    )

    assert verdict.allowed is False
    assert verdict.reason == "tp_extension_exceeds_max_factor"


def test_tighten_position_blocks_tp_extension_after_position_limit():
    service = _service()

    verdict = service.evaluate(
        "tighten_position",
        {
            "loop_running": True,
            "bridge_connected": True,
            "position_id": "p1",
            "tp_extension_count": 2,
            "position": {
                "direction": "buy",
                "entry_price": 4000.0,
                "sl": 4010.0,
                "tp": 4030.0,
            },
            "recommended_controls": {
                "target_take_profit": 4038.0,
                "max_tp_extension_factor": 0.35,
                "max_tp_extensions_per_position": 2,
            },
        },
    )

    assert verdict.allowed is False
    assert verdict.reason == "tp_extension_count_exceeded"


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


def test_start_shadow_model_reuses_model_permission_guardrail():
    service = _service()

    verdict = service.evaluate(
        "start_shadow_model",
        {
            "candidate_id": "cand_unsafe",
            "capabilities": {
                "live_trading": False,
                "can_bypass_risk_policy": True,
                "advisory_only": True,
                "shadow_only": True,
            },
        },
    )

    assert verdict.allowed is False
    assert verdict.reason == "model_permission_violation"
    assert verdict.audit_payload["source"] == "model_permissions"


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
