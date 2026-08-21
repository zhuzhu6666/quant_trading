import json
import time
from types import SimpleNamespace

from backend.services.live_position_lifecycle import (
    active_pending_open_attach_ids,
    adjust_sl_plan_for_tp_only_protection,
    apply_unrealized_pnl_fields,
    build_applied_entry_protection_plan_payload,
    build_bar_context_snapshot,
    build_close_position_risk_context_payload,
    build_decision_quality_context,
    build_entry_cluster_context,
    build_entry_data_quality_context,
    build_entry_execution_context,
    build_filled_open_ledger_payloads,
    build_filled_open_recovery_payloads,
    build_entry_protection_plan_payload,
    build_open_learning_context_payload,
    build_open_trade_risk_context_payload,
    build_holding_summary_from_close_context,
    build_holding_timeout_trace_fields,
    build_holding_timeout_result_trace_fields,
    build_holding_timeout_verdict_payload,
    build_market_micro_context_payload,
    build_portfolio_exposure_context,
    validate_open_learning_context,
    build_position_path_metrics_result,
    build_position_path_metrics_inputs,
    build_position_path_metrics_update,
    build_position_path_recovery_meta,
    build_position_protection_cycle_result,
    build_replayed_close_payloads,
    build_position_supervisor_context_inputs,
    build_position_supervisor_context_payload,
    build_protection_execution_plan,
    build_protection_candidate_verdict_payload,
    build_protection_candidate_risk_context_from_candidate,
    build_protection_candidate_risk_context_payload,
    build_protection_recovery_meta,
    build_protection_execution_result_payloads,
    build_protection_state_upsert_payload,
    build_protection_execution_trace_fields,
    build_protection_position_event_details,
    build_protection_superseded_trace_fields,
    build_recovery_closed_update_payload,
    build_recovery_meta_update_payload,
    build_recovered_open_ledger_payloads,
    build_recovery_upsert_defaults,
    build_risk_state_with_policy_verdict,
    build_pending_supervisor_reentry_block_payload,
    build_supervisor_decision_ledger_payload,
    build_supervisor_position_event_payload,
    build_supervisor_reentry_block_payload,
    build_supervisor_recovery_meta,
    build_supervisor_state_upsert_payload,
    build_supervisor_trace_ledger_payload,
    build_supervisor_close_context_inputs,
    build_supervisor_action_fingerprint,
    build_supervisor_risk_context_payload,
    build_supervisor_tighten_execution_plan,
    build_supervisor_tighten_result_payloads,
    build_supervisor_tighten_sl_plan,
    build_supervisor_tighten_sl_plan_inputs,
    build_target_tp_extension_inputs,
    build_trade_attribution_payload_from_composite,
    classify_trading_session,
    classify_close_source_from_evidence,
    consume_close_reason,
    consume_close_verdict,
    current_regime_hint_from_composite,
    estimate_close_pnl_from_state,
    enrich_positions_with_lifecycle_metrics,
    entry_quality_gate_from_learning_policy,
    filter_removed_live_position,
    float_payload_value,
    forget_pending_close_state,
    holding_timeout_is_expired,
    latest_close_evidence,
    legacy_awe_trailing_atr_config,
    max_abs_entry_score_for_positions,
    merge_recovery_meta_json,
    normalize_protection_trace_row,
    normalize_recovery_position_row,
    normalize_supervisor_event_row,
    payload_get,
    protection_candidate_supersede_reason,
    normalize_position_snapshot,
    position_api_volume,
    position_direction_from_payload,
    position_direction_sign,
    position_id_value,
    position_open_price,
    position_open_timestamp,
    position_symbol_value,
    position_unrealized_pnl,
    remember_close_reason,
    remember_close_verdict,
    remember_pending_open_attach,
    recovery_active_position_ids,
    recovery_missing_position_ids,
    recovery_replay_lookback_from,
    restore_attribution_for_positions,
    serialize_close_verdict,
    side_name,
    same_symbol_position,
    supervisor_recently_applied_from_meta,
    supervisor_noop_fingerprint_seen,
    supervisor_reentry_block_view,
    supervisor_reentry_cooldown_seconds,
    supervisor_reentry_key,
    supervisor_risk_action_for_action,
    build_supervisor_runtime_risk_evaluation_inputs,
    target_tp_is_extension,
    temporal_context_for_trade,
    timeframe_seconds,
    tracked_total_api_volume,
    update_entry_protection_plan_payload,
    build_legacy_awe_trailing_update,
)


def test_payload_get_supports_dict_objects_and_default():
    assert payload_get({"ticket": 101}, "ticket") == 101
    assert payload_get(SimpleNamespace(ticket=202), "ticket") == 202
    assert payload_get(None, "ticket", 0) == 0


def test_float_payload_value_uses_first_valid_key():
    payload = {"bad": "", "also_bad": "nope", "price": "4001.25"}
    obj = SimpleNamespace(first=None, second="12.5")

    assert float_payload_value(payload, "bad", "also_bad", "price") == 4001.25
    assert float_payload_value(obj, "first", "second") == 12.5
    assert float_payload_value({}, "missing") == 0.0


def test_position_symbol_value_normalizes_symbol_aliases():
    assert position_symbol_value({"symbol": "xauusd+"}) == "XAUUSD"
    assert position_symbol_value({"symbol_name": " eurusd+ "}) == "EURUSD"
    assert position_symbol_value({}, default="GBPUSD+") == "GBPUSD"


def test_position_api_volume_prefers_broker_volume_field():
    assert position_api_volume({"volume": 100, "api_volume": 50}) == 100.0
    assert position_api_volume({"api_volume": 75}) == 75.0
    assert position_api_volume(SimpleNamespace(volume=125)) == 125.0
    assert position_api_volume({"lots": 0.01}) == 0.0


def test_tracked_total_api_volume_prefers_cached_open_volume():
    positions = [
        {"position_id": 1, "volume": 100.0},
        {"ticket": 2, "volume": 200.0},
        SimpleNamespace(position_id=3, volume=300.0),
    ]

    assert tracked_total_api_volume(
        positions,
        open_api_volumes={1: 150.0, 3: 350.0},
    ) == 700.0


def test_max_abs_entry_score_for_positions_uses_cached_scores():
    positions = [
        {"position_id": 1},
        {"ticket": 2},
        SimpleNamespace(position_id=3),
        {"position_id": 4},
    ]

    assert max_abs_entry_score_for_positions(
        positions,
        entry_scores={1: 0.2, 2: -0.9, 3: 0.4},
    ) == 0.9


def test_position_direction_and_side_normalization():
    assert position_direction_sign({"direction": 1, "type": "sell"}) == 1
    assert position_direction_sign({"direction": 0, "type": "sell"}) == -1
    assert position_direction_from_payload({"side": "long"}) == 1
    assert position_direction_from_payload(SimpleNamespace(type="sell")) == -1
    assert position_direction_from_payload({"direction": -2, "side": "long"}) == -1
    assert position_direction_from_payload({"side": "flat"}) == 0
    assert side_name(1) == "long"
    assert side_name(-1) == "short"
    assert side_name(0) == "unknown"
    assert position_unrealized_pnl({"profit": 0.0, "pnl_state": "known"}) == 0.0


def test_apply_unrealized_pnl_fields_requires_broker_component_truth():
    unknown = apply_unrealized_pnl_fields(
        [{"open_price": 4000.0, "current_price": 4010.0, "direction": 1, "volume": 100.0}],
    )
    assert unknown[0]["pnl"] is None
    assert unknown[0]["pnl_state"] == "unknown"
    assert unknown[0]["pnl_source"] == "unknown"

    known = apply_unrealized_pnl_fields(
        [{"profit": 0.0, "pnl": 0.0, "pnl_state": "known"}],
    )
    assert known[0]["pnl"] == 0.0
    assert known[0]["pnl_source"] == "broker"


def test_position_payload_extractors_accept_objects_and_millisecond_timestamps():
    pos = SimpleNamespace(ticket=303, open_time=1_700_000_000_000, open_price=4001.5)
    assert position_id_value(pos) == 303
    assert position_open_timestamp(pos) == 1_700_000_000.0
    assert position_open_price(pos) == 4001.5
    assert position_unrealized_pnl(SimpleNamespace(profit=12.3)) == 12.3


def test_normalize_position_snapshot_handles_dict_and_object_payloads():
    dict_snapshot = normalize_position_snapshot(
        {
            "ticket": 101,
            "symbol": "XAUUSD",
            "type": "sell",
            "open_price": 4000.0,
            "api_volume": 250.0,
        }
    )
    object_snapshot = normalize_position_snapshot(
        SimpleNamespace(
            position_id=202,
            symbol="EURUSD",
            direction=1,
            entry_price=1.085,
            volume=1000.0,
        )
    )

    assert dict_snapshot["position_id"] == 101
    assert dict_snapshot["direction"] == -1
    assert dict_snapshot["volume"] == 250.0
    assert dict_snapshot["type"] == "sell"
    assert dict_snapshot["raw"]["ticket"] == 101
    assert object_snapshot == {
        "position_id": 202,
        "symbol": "EURUSD",
        "direction": 1,
        "open_price": 1.085,
        "volume": 1000.0,
        "type": "buy",
        "raw": {
            "position_id": 202,
            "ticket": 202,
            "symbol": "EURUSD",
            "type": "buy",
            "direction": 1,
            "open_price": 1.085,
            "entry_price": 1.085,
            "volume": 1000.0,
        },
    }


def test_estimate_close_pnl_from_state_uses_caches_then_recovery_fallback():
    assert estimate_close_pnl_from_state(
        position_id=7,
        current_price=4010.0,
        recovery_row={"open_price": 4000.0, "direction": 1, "volume": 50.0},
        open_prices={7: 3990.0},
        open_api_volumes={7: 100.0},
    ) == 2000.0
    assert estimate_close_pnl_from_state(
        position_id=8,
        current_price=3990.0,
        recovery_row={"open_price": 4000.0, "type": "sell", "volume": 25.0},
        open_prices={},
        open_api_volumes={},
    ) == 250.0


def test_build_entry_cluster_context_counts_symbol_direction_and_recent_ages():
    positions = [
        {
            "position_id": 1,
            "symbol_name": "XAUUSD+",
            "direction": 1,
            "volume": 100.0,
            "open_price": 4000.0,
            "open_ts": 900.0,
        },
        {
            "position_id": 2,
            "symbol": "XAUUSD",
            "side": "buy",
            "volume": 200.0,
            "entry_price": 4010.0,
            "open_timestamp": 200.0,
        },
        {
            "position_id": 3,
            "symbol": "XAUUSD",
            "type": "sell",
            "volume": 50.0,
            "price": 4020.0,
            "open_time": 700.0,
        },
        {
            "position_id": 4,
            "symbol": "EURUSD",
            "direction": 1,
            "volume": 999.0,
            "open_ts": 990.0,
        },
    ]

    context = build_entry_cluster_context(
        positions_before=positions,
        direction=1,
        symbol="XAUUSD",
        now_ts=1000.0,
        new_position_id=9,
        new_api_volume=25.0,
    )

    assert same_symbol_position("XAUUSD", positions[0]) is True
    assert context["schema_version"] == "entry_cluster_context.v1"
    assert context["open_position_count_before"] == 3
    assert context["open_position_count_after"] == 4
    assert context["same_direction_open_count_before"] == 2
    assert context["same_direction_open_count_after"] == 3
    assert context["opposite_direction_open_count_before"] == 1
    assert context["same_direction_api_volume_before"] == 300.0
    assert context["same_direction_api_volume_after"] == 325.0
    assert context["opposite_direction_api_volume_before"] == 50.0
    assert context["net_direction_api_volume_before"] == 250.0
    assert context["net_direction_api_volume_after"] == 275.0
    assert context["seconds_since_last_same_direction_open"] == 100.0
    assert context["recent_same_direction_entries"] == {"5m": 1, "15m": 2, "30m": 2}
    assert context["same_direction_position_ids"] == [1, 2]
    assert context["position_slot_index"] == 3
    assert context["is_pyramid"] is True
    assert context["pyramid_depth"] == 2


def test_build_entry_cluster_context_handles_neutral_direction_without_pyramid():
    context = build_entry_cluster_context(
        positions_before=[{"position_id": 1, "symbol": "XAUUSD", "direction": 1, "volume": 100.0}],
        direction=0,
        symbol="XAUUSD",
        now_ts=1000.0,
        new_position_id=9,
        new_api_volume=25.0,
    )

    assert context["direction"] == 0
    assert context["same_direction_open_count_before"] == 0
    assert context["same_direction_open_count_after"] == 0
    assert context["net_direction_api_volume_after"] == 100.0
    assert context["is_pyramid"] is False


def test_build_entry_cluster_context_marks_missing_broker_open_time_unknown():
    context = build_entry_cluster_context(
        positions_before=[
            {
                "position_id": 7,
                "symbol": "XAUUSD+",
                "direction": 1,
                "volume": 100.0,
            }
        ],
        direction=1,
        symbol="XAUUSD+",
        now_ts=1000.0,
    )

    assert context["same_direction_open_timestamp_state"] == "unknown"
    assert context["seconds_since_last_same_direction_open"] is None
    assert context["unknown_open_timestamp_position_ids"] == [7]
    assert context["recent_same_direction_entries"] == {"5m": 0, "15m": 0, "30m": 0}


def test_build_market_micro_context_payload_computes_spread_and_slippage():
    payload = build_market_micro_context_payload(
        quote={"bid": 4000.0, "ask": 4000.5, "mid": 4000.25, "ts": 123.0},
        current_price=4000.0,
        fill_price=4001.0,
        direction=1,
        quote_age_seconds=2.5,
        quote_fresh=True,
    )

    assert payload == {
        "schema_version": "market_micro_context.v1",
        "bid": 4000.0,
        "ask": 4000.5,
        "mid": 4000.25,
        "spread": 0.5,
        "quote_ts": 123.0,
        "quote_age_seconds": 2.5,
        "quote_fresh": True,
        "signal_price": 4000.0,
        "fill_price": 4001.0,
        "fill_delta_points": 1.0,
        "adverse_slippage_points": 1.0,
    }


def test_build_bar_context_snapshot_computes_shape_metrics():
    payload = build_bar_context_snapshot(
        {
            "time": 1000.0,
            "timeframe": "M5",
            "open": 4000.0,
            "high": 4010.0,
            "low": 3990.0,
            "close": 4005.0,
            "volume": 12,
            "complete": True,
        }
    )

    assert payload["schema_version"] == "entry_bar_context.v1"
    assert payload["range_points"] == 20.0
    assert payload["body_points"] == 5.0
    assert payload["body_ratio"] == 0.25
    assert payload["close_location"] == 0.75
    assert payload["upper_wick_ratio"] == 0.25
    assert payload["lower_wick_ratio"] == 0.5
    assert payload["complete"] is True


def test_build_decision_quality_context_reports_conflict_and_top_contributors():
    composite = SimpleNamespace(
        score=0.2,
        tactical_score=0.3,
        macro_score=-0.1,
        n_active_factors=3,
        n_abstain_factors=1,
        factor_signals={"a": 1.0, "b": -0.5, "c": None, "d": 0.2},
        active_weights={"a": 0.4, "b": 0.6, "d": 0.5},
    )

    payload = build_decision_quality_context(composite)

    assert payload["schema_version"] == "decision_quality_context.v1"
    assert payload["positive_contribution_abs"] == 0.5
    assert payload["negative_contribution_abs"] == 0.3
    assert payload["factor_conflict_ratio"] == 0.3 / 0.8
    assert [item["factor"] for item in payload["top_contributors"]] == ["a", "b", "d"]


def test_build_open_learning_child_context_payloads_match_live_shapes():
    entry_cluster = {
        "open_position_count_before": 1,
        "open_position_count_after": 2,
        "same_direction_open_count_before": 1,
        "same_direction_open_count_after": 2,
        "same_direction_api_volume_before": 100.0,
        "same_direction_api_volume_after": 125.0,
    }
    portfolio = build_portfolio_exposure_context(
        entry_cluster=entry_cluster,
        total_api_volume_before=300.0,
        actual_api_volume=25.0,
    )
    execution = build_entry_execution_context(
        requested_volume=50.0,
        base_requested_volume=40.0,
        actual_api_volume=25.0,
        current_price=4000.0,
        fill_price=4001.0,
        sl_price=3990.0,
        tp_price=4020.0,
        sl_dist=10.0,
        tp_dist=20.0,
    )
    data_quality = build_entry_data_quality_context(
        market_micro={"quote_fresh": True, "quote_age_seconds": 1.5},
        runtime_health={"positions_cache_age_seconds": 2.0},
    )

    assert portfolio == {
        "schema_version": "portfolio_exposure_context.v1",
        "open_position_count_before": 1,
        "open_position_count_after": 2,
        "same_direction_open_count_before": 1,
        "same_direction_open_count_after": 2,
        "same_direction_api_volume_before": 100.0,
        "same_direction_api_volume_after": 125.0,
        "total_api_volume_before": 300.0,
        "total_api_volume_after": 325.0,
    }
    assert execution["schema_version"] == "entry_execution_context.v1"
    assert execution["entry_protection_expected"] is True
    assert execution["sl_distance_points"] == 10.0
    assert data_quality == {
        "schema_version": "entry_data_quality_context.v1",
        "quote_fresh": True,
        "quote_age_seconds": 1.5,
        "runtime_health": {"positions_cache_age_seconds": 2.0},
    }


def test_build_open_learning_context_payload_preserves_live_shape():
    entry_cluster = {
        "open_position_count_before": 1,
        "open_position_count_after": 2,
        "same_direction_open_count_before": 1,
        "same_direction_open_count_after": 2,
        "same_direction_api_volume_before": 100.0,
        "same_direction_api_volume_after": 125.0,
        "recent_same_direction_entries": [{"position_id": 1}],
    }
    market_micro = {
        "spread": 0.2,
        "bid": 4000.1,
        "ask": 4000.3,
        "quote_fresh": True,
        "quote_age_seconds": 0.4,
    }
    composite = SimpleNamespace(
        score=0.3,
        tactical_score=0.2,
        macro_score=0.1,
        n_active_factors=2,
        n_abstain_factors=0,
        factor_signals={"trend": 1.0, "macro": -0.2},
        active_weights={"trend": 0.5, "macro": 0.25},
    )

    payload = build_open_learning_context_payload(
        entry_cluster=entry_cluster,
        market_micro=market_micro,
        bar={"time": 123, "open": 3999.0, "high": 4001.0, "low": 3998.5, "close": 4000.0},
        composite=composite,
        total_api_volume_before=300.0,
        actual_api_volume=25.0,
        requested_volume=50.0,
        base_requested_volume=40.0,
        current_price=4000.0,
        fill_price=4000.2,
        sl_price=3990.0,
        tp_price=4020.0,
        sl_dist=10.0,
        tp_dist=20.0,
        sizing_trace={"sized": True},
        event_sizing_context={"event": "none"},
        runtime_health={"ready": True},
        market_session={"status": "open"},
        decision_freshness={"schema_version": "decision_bar_freshness.v1", "fresh": True},
        entry_timing_context={"schema_version": "entry_timing_context.v1", "signal_to_fill_delay_seconds": 1.0},
    )

    assert payload["entry_cluster"] is entry_cluster
    assert payload["same_direction_open_count"] == 1
    assert payload["recent_same_direction_entries"] == [{"position_id": 1}]
    assert payload["portfolio_exposure"]["total_api_volume_after"] == 325.0
    assert payload["market_micro_context"] is market_micro
    assert payload["spread"] == 0.2
    assert payload["bid"] == 4000.1
    assert payload["ask"] == 4000.3
    assert payload["bar_context"]["schema_version"] == "entry_bar_context.v1"
    assert payload["execution_context"]["fill_price"] == 4000.2
    assert payload["sizing_trace"] == {"sized": True}
    assert payload["event_context"] == {"event": "none"}
    assert payload["decision_quality_context"]["schema_version"] == "decision_quality_context.v1"
    assert payload["data_quality_context"] == {
        "schema_version": "entry_data_quality_context.v1",
        "quote_fresh": True,
        "quote_age_seconds": 0.4,
        "runtime_health": {"ready": True},
        "decision_freshness": {"schema_version": "decision_bar_freshness.v1", "fresh": True},
    }
    assert payload["market_session"] == {"status": "open"}
    assert payload["decision_freshness"] == {"schema_version": "decision_bar_freshness.v1", "fresh": True}
    assert payload["entry_timing_context"]["signal_to_fill_delay_seconds"] == 1.0


def test_validate_open_learning_context_requires_all_future_training_inputs():
    payload = {
        "entry_cluster": {"schema_version": "entry_cluster_context.v1", "direction": 1},
        "market_micro_context": {
            "bid": 4000.0,
            "ask": 4000.2,
            "mid": 4000.1,
            "spread": 0.2,
            "quote_ts": 1000.0,
            "signal_price": 4000.1,
            "quote_fresh": True,
        },
        "bar_context": {
            "bar_ts": 999.0,
            "open": 3999.0,
            "high": 4001.0,
            "low": 3998.5,
            "close": 4000.0,
            "complete": True,
        },
        "execution_context": {
            "requested_volume": 100.0,
            "actual_api_volume": 100.0,
            "signal_price": 4000.1,
            "fill_price": 4000.2,
        },
        "decision_quality_context": {
            "schema_version": "decision_quality_context.v1",
            "composer_version": "factor_roles.v2",
            "factor_roles": {"rsi": "alpha"},
            "n_active_alpha_factors": 1,
        },
        "event_context": {"multiplier": 1.0},
        "data_quality_context": {
            "schema_version": "entry_data_quality_context.v1",
            "quote_fresh": True,
        },
        "market_session": {"status": "open_confirmed"},
    }

    assert validate_open_learning_context(payload)["ready"] is True
    payload["market_micro_context"] = dict(payload["market_micro_context"], spread=0.0)
    invalid = validate_open_learning_context(payload)
    assert invalid["ready"] is False
    assert "market_micro_context.spread" in invalid["invalid_fields"]


def test_build_open_trade_risk_context_payload_preserves_live_shape():
    payload = build_open_trade_risk_context_payload(
        cfg=SimpleNamespace(
            var_enabled=True,
            var_cvar_threshold=0.03,
            max_position_count=4,
            max_position_api_volume=1200.0,
            pyramid_enabled=False,
            risk_max_daily_loss_pct=5.0,
            risk_loss_cooldown_after_losses=2,
            risk_loss_cooldown_bars=5,
            risk_block_on_disk_critical=False,
        ),
        acct={"equity": 10000.0},
        positions=[{"position_id": 1}],
        requested_api_volume=100.0,
        signal_score=0.7,
        symbol="XAUUSD+",
        direction=1,
        current_price=4000.5,
        atr_price=12.3,
        risk_snapshot={"daily_loss": 1.0},
        session_state={
            "pnl": -10.0,
            "start_balance": 10000.0,
            "trades": 3,
            "consecutive_losses": 1,
            "drawdown_pct": 0.4,
            "circuit_breaker": False,
        },
        total_api_volume=250.0,
        event_sizing_context={"enabled": True, "multiplier": 0.5},
        event_window_learning_policy={"active": True},
        entry_quality_gate={"allowed": True},
        entry_cluster_context={"open_position_count_before": 1},
        entry_cluster_learning_policy={"loaded": True},
        same_direction_cooldown_seconds=900.0,
        max_abs_entry_score=0.8,
        loop_running=True,
        bridge_connected=False,
        data_lag_seconds=12.0,
        runtime_health={"sync_health": {"fresh": True}},
        temporal_context={"timeframe": "M5"},
        decision_freshness={"fresh": True},
        supervisor_reentry_block={"active": True},
    )

    assert payload["trade"] == {
        "symbol": "XAUUSD+",
        "direction": 1,
        "current_price": 4000.5,
        "atr_price": 12.3,
    }
    assert payload["account"] == {"equity": 10000.0}
    assert payload["session"]["pnl"] == -10.0
    assert payload["risk_snapshot"]["daily_loss"] == 1.0
    assert payload["risk_snapshot"]["var"]["status"] == "unknown"
    assert (
        payload["risk_snapshot"]["var"]["reason"]
        == "missing_closed_bar_prices"
    )
    assert payload["risk_limits"]["schema_version"] == "risk_limit_snapshot.v1"
    assert payload["risk_limits"]["max_daily_loss_pct"] == 5.0
    assert payload["var"] == {"enabled": True, "threshold_pct": 3.0, "cvar_threshold_pct": 3.0}
    assert payload["open_position_count"] == 1
    assert payload["max_position_count"] == 4
    assert payload["total_api_volume"] == 250.0
    assert payload["requested_api_volume"] == 100.0
    assert payload["max_position_api_volume"] == 1200.0
    assert payload["event_sizing"] == {"enabled": True, "multiplier": 0.5}
    assert payload["event_filter"] == {}
    assert payload["event_window_learning_policy"] == {"active": True}
    assert payload["entry_quality_gate"] == {"allowed": True}
    assert payload["entry_cluster"] == {"open_position_count_before": 1}
    assert payload["entry_cluster_learning_policy"] == {"loaded": True}
    assert payload["same_direction_cooldown_seconds"] == 900.0
    assert payload["pyramid_enabled"] is False
    assert payload["max_abs_entry_score"] == 0.8
    assert payload["signal_score"] == 0.7
    assert payload["loop_running"] is True
    assert payload["bridge_connected"] is False
    assert payload["data_lag_seconds"] == 12.0
    assert payload["runtime_health"] == {"sync_health": {"fresh": True}}
    assert payload["decision_freshness"] == {"fresh": True}
    assert payload["loss_cooldown_after_losses"] == 2
    assert payload["loss_cooldown_bars"] == 5
    assert payload["block_on_disk_critical"] is False
    assert payload["temporal_context"] == {"timeframe": "M5"}
    assert payload["supervisor_reentry_block"] == {"active": True}


def test_build_open_trade_risk_context_payload_uses_safe_defaults():
    payload = build_open_trade_risk_context_payload(
        cfg=SimpleNamespace(),
        acct=None,
        positions=None,
        requested_api_volume=0.0,
        signal_score=0.0,
        symbol="",
        direction=0,
        current_price=0.0,
        atr_price=0.0,
        risk_snapshot=None,
        session_state={},
        total_api_volume=0.0,
        event_sizing_context=None,
        event_window_learning_policy=None,
        entry_quality_gate=None,
        entry_cluster_context={},
        entry_cluster_learning_policy=None,
        same_direction_cooldown_seconds=0.0,
        max_abs_entry_score=0.0,
        loop_running=False,
        bridge_connected=False,
        data_lag_seconds=0.0,
        runtime_health={},
        temporal_context={},
        decision_freshness=None,
        supervisor_reentry_block=None,
    )

    assert payload["trade"]["symbol"] == "XAUUSD"
    assert payload["account"] == {}
    assert payload["risk_snapshot"] == {}
    assert payload["risk_limits"]["schema_version"] == "risk_limit_snapshot.v1"
    assert payload["event_sizing"] == {"enabled": False, "multiplier": 1.0}
    assert payload["event_filter"] == {}
    assert payload["event_window_learning_policy"] == {}
    assert payload["entry_quality_gate"] == {}
    assert payload["entry_cluster_learning_policy"] == {}
    assert payload["decision_freshness"] == {}
    assert payload["supervisor_reentry_block"] == {}


def test_build_filled_open_ledger_payloads_preserves_live_shape():
    composite = SimpleNamespace(direction=1)
    gate_result = SimpleNamespace(allowed=True)
    risk_verdict = SimpleNamespace(to_dict=lambda: {"allowed": True, "reason": "ok"})

    payloads = build_filled_open_ledger_payloads(
        cfg=SimpleNamespace(timeframe="M5"),
        bar={"time": 1234.5},
        tick=9,
        pid=268,
        actual_api_volume=100.0,
        requested_volume=120.0,
        fill_price=4001.37,
        current_price=4001.55,
        sl_price=3990.22,
        tp_price=4020.88,
        acct={"balance": 10000.0, "equity": 10025.0},
        positions_before=[{"position_id": 1}],
        composite=composite,
        gate_result=gate_result,
        learning_context={"sizing_trace": {"source": "test"}, "entry_cluster": {"count": 1}},
        risk_state={"risk": "snapshot"},
        session_pnl=25.0,
        risk_verdict=risk_verdict,
        decision_ts_fallback=999.0,
        event_ts=1240.0,
    )

    decision = payloads["composite_decision_payload"]
    assert decision["event_type"] == "open"
    assert decision["composite"] is composite
    assert decision["gate_result"] is gate_result
    assert decision["symbol"] == "XAUUSD+"
    assert decision["timeframe"] == "M5"
    assert decision["decision_ts"] == 1234.5
    assert decision["trade_id"] == "268"
    assert decision["position_id"] == "268"
    assert decision["portfolio_state"] == {
        "balance": 10000.0,
        "equity": 10025.0,
        "n_positions": 1,
        "session_pnl": 25.0,
    }
    assert decision["risk_state"] == {"risk": "snapshot"}
    assert decision["action_reason"] == "executed"
    assert decision["action_json"] == {
        "position_id": 268,
        "volume": 100.0,
        "requested_volume": 120.0,
        "price": 4001.55,
        "fill_price": 4001.37,
        "sl": 3990.22,
        "tp": 4020.88,
        "tick": 9,
        "sizing_trace": {"source": "test"},
        "entry_cluster": {"count": 1},
        "risk_verdict": {"allowed": True, "reason": "ok"},
    }
    assert payloads["submitted_order_payload"] == {
        "event_type": "submitted",
        "trade_id": "268",
        "order_id": "268",
        "broker_order_id": "268",
        "price": 4001.55,
        "volume": 100.0,
        "status": "submitted",
        "details": {
            "tick": 9,
            "direction": 1,
            "requested_price": 4001.55,
            "fill_price": 0.0,
            "capture_schema": "execution_quality_event.v1",
        },
    }
    assert payloads["filled_order_payload"]["event_type"] == "filled"
    assert payloads["filled_order_payload"]["price"] == 4001.37
    assert payloads["filled_order_payload"]["details"] == {
        "tick": 9,
        "direction": 1,
        "requested_price": 4001.55,
        "fill_price": 4001.37,
        "capture_schema": "execution_quality_event.v1",
    }
    assert payloads["position_event_payload"] == {
        "position_id": "268",
        "trade_id": "268",
        "symbol": "XAUUSD+",
        "event_type": "opened",
        "net_volume": 100.0,
        "avg_price": 4001.37,
        "details": {"tick": 9, "direction": 1, "sl": 3990.22, "tp": 4020.88},
        "event_ts": 1240.0,
    }


def test_build_filled_open_recovery_payloads_preserves_live_shape():
    payloads = build_filled_open_recovery_payloads(
        position_id=268,
        broker="ctrader",
        strategy_name="factor_v4",
        direction=1,
        fill_price=4001.37,
        current_price=4001.55,
        sl_price=3990.22,
        tp_price=4020.88,
        requested_volume=120.0,
        actual_api_volume=100.0,
        tick=9,
        entry_decision_id="decision-268",
        entry_protection_plan={"schema_version": "entry_protection_plan.v1", "status": "pending"},
        trade_attribution_payload={"attribution_integrity": "full"},
        learning_context={"entry_cluster": {"count": 1}, "sizing_trace": {}},
        context_integrity="full",
    )

    assert payloads["state_payload"] == {
        "position_id": 268,
        "symbol": "XAUUSD+",
        "direction": 1,
        "open_price": 4001.37,
        "volume": 100.0,
        "entry_decision_id": "decision-268",
    }
    assert payloads["state_kwargs"] == {
        "broker": "ctrader",
        "strategy_name": "factor_v4",
        "status": "open",
        "context_integrity": "full",
    }
    assert payloads["meta"]["tick"] == 9
    assert payloads["meta"]["sl"] == 3990.22
    assert payloads["meta"]["tp"] == 4020.88
    assert payloads["meta"]["trade_attribution"] == {"attribution_integrity": "full"}
    assert payloads["meta"]["entry_cluster"] == {"count": 1}
    assert payloads["meta"]["sizing_trace"] == {}
    assert payloads["meta"]["entry_protection_plan"] == {
        "schema_version": "entry_protection_plan.v1",
        "status": "pending",
    }


def test_build_trade_attribution_payload_from_composite_matches_live_shape():
    composite = SimpleNamespace(
        factor_signals={"trend": 0.5, "macro": -0.25, "empty": None},
        factor_values={"trend": 1.2},
        active_weights={"trend": 0.7},
        score=0.4,
        tactical_score=0.3,
        macro_score=0.1,
        tags_breakdown={"trend": ["momentum"]},
    )

    payload = build_trade_attribution_payload_from_composite(
        position_id=101,
        open_ts=123.4,
        open_price=4001.5,
        direction=1,
        actual_api_volume=200.0,
        composite=composite,
    )

    assert payload == {
        "position_id": 101,
        "open_ts": 123.4,
        "open_price": 4001.5,
        "direction": 1,
        "factor_signals": {"trend": 0.5, "macro": -0.25, "empty": None},
        "factor_values": {"trend": 1.2},
        "active_weights": {"trend": 0.7},
        "factor_roles": {},
        "context_signals": {},
        "composite_score": 0.4,
        "alpha_score": 0.4,
        "tactical_score": 0.3,
        "macro_score": 0.1,
        "calibrated_confidence": {},
        "tags_breakdown": {"trend": ["momentum"]},
        "total_signal_abs": 0.5,
        "api_volume": 200.0,
        "attribution_integrity": "full",
    }


def test_trade_attribution_total_signal_abs_uses_scored_alpha_only():
    composite = SimpleNamespace(
        factor_signals={"trend": 0.5, "bb_width": 1.0, "disabled": -0.9},
        factor_values={},
        active_weights={"trend": 0.7, "bb_width": 0.0, "disabled": 0.0},
        factor_roles={"trend": "alpha", "bb_width": "context", "disabled": "alpha"},
        score=0.4,
        tactical_score=0.4,
        macro_score=0.0,
        tags_breakdown={},
    )

    payload = build_trade_attribution_payload_from_composite(
        position_id=102,
        open_ts=123.4,
        open_price=4001.5,
        direction=1,
        actual_api_volume=200.0,
        composite=composite,
    )

    assert payload["total_signal_abs"] == 0.5


def test_entry_quality_gate_from_learning_policy_handles_inactive_and_passed():
    assert entry_quality_gate_from_learning_policy(
        policy={},
        decision_quality={},
        signal_score=0.1,
    ) == {
        "active": False,
        "allowed": True,
        "reason": "inactive",
        "source": "entry_quality_gate",
    }

    passed = entry_quality_gate_from_learning_policy(
        policy={"active": True, "controls": [{"action": "unknown"}]},
        decision_quality={"factor_conflict_ratio": 0.2},
        signal_score=0.8,
    )

    assert passed["allowed"] is True
    assert passed["reason"] == "passed"
    assert passed["control_count"] == 1
    assert passed["metrics"] == {"signal_score_abs": 0.8, "factor_conflict_ratio": 0.2}


def test_entry_quality_gate_from_learning_policy_blocks_weak_signal():
    verdict = entry_quality_gate_from_learning_policy(
        policy={
            "active": True,
            "controls": [
                {
                    "action": "raise_weak_signal_threshold",
                    "min_abs_signal_score": 0.5,
                    "strong_signal_override": 1.2,
                    "suggestion_id": "s1",
                    "scope_key": "weak",
                }
            ],
        },
        decision_quality={"factor_conflict_ratio": 0.1},
        signal_score=0.3,
    )

    assert verdict["allowed"] is False
    assert verdict["reason"] == "learning_weak_signal_threshold"
    assert verdict["thresholds"] == {"min_abs_signal_score": 0.5, "strong_signal_override": 1.2}


def test_entry_quality_gate_from_learning_policy_blocks_conflict_and_suppressed_factor():
    conflict = entry_quality_gate_from_learning_policy(
        policy={
            "active": True,
            "controls": [
                {
                    "action": "require_factor_agreement",
                    "max_factor_conflict_ratio": 0.4,
                    "strong_signal_override": 1.0,
                }
            ],
        },
        decision_quality={"factor_conflict_ratio": 0.6},
        signal_score=0.5,
    )
    suppressed = entry_quality_gate_from_learning_policy(
        policy={
            "active": True,
            "controls": [
                {
                    "action": "suppress_recent_worst_factor",
                    "scope_key": "macro_drag",
                    "strong_signal_override": 1.0,
                }
            ],
        },
        decision_quality={
            "factor_conflict_ratio": 0.1,
            "top_contributors": [
                {"factor": "macro_drag", "contribution_score": -0.4},
                {"factor": "trend", "contribution_score": 0.2},
            ],
        },
        signal_score=0.5,
    )

    assert conflict["reason"] == "learning_factor_conflict_control"
    assert conflict["allowed"] is False
    assert suppressed["reason"] == "learning_recent_worst_factor_control"
    assert suppressed["evidence"] == {"matched_factor_count": 1, "suppressed_factor": "macro_drag"}


def test_temporal_context_for_trade_reports_market_and_runtime_time():
    assert timeframe_seconds("m5") == 300
    assert timeframe_seconds("unknown") == 0
    assert classify_trading_session(2) == "asia"
    assert classify_trading_session(8) == "europe"
    assert classify_trading_session(15) == "us"
    assert classify_trading_session(23) == "rollover"

    context = temporal_context_for_trade(
        decision_ts=1_704_068_100.0,  # 2024-01-01 00:15:00 UTC
        timeframe="M5",
        evaluated_at_ts=1_704_068_700.0,
        session_last_trade_ts=1_704_067_200.0,
        loop_started_at=1_704_066_200.0,
    )

    assert context["time_basis"] == "market_epoch_seconds_utc"
    assert context["runtime_basis"] == "system_epoch_seconds_utc"
    assert context["hour_utc"] == 0
    assert context["minute_utc"] == 15
    assert context["session_label"] == "asia"
    assert context["timeframe_seconds"] == 300
    assert context["seconds_since_last_trade"] == 1500.0
    assert context["bars_since_last_trade"] == 5.0
    assert context["loop_uptime_seconds"] == 2500.0


def test_remember_and_consume_close_reason_persists_recovery_meta():
    pending = {}
    merged = {}

    def _merge(position_id, meta):
        merged[position_id] = meta

    remember_close_reason(
        pending_reasons=pending,
        merge_recovery_meta=_merge,
        position_id=101,
        reason="supervisor_close",
        now_fn=lambda: 123.0,
    )

    assert pending[101] == "supervisor_close"
    assert merged[101] == {
        "pending_close_reason": "supervisor_close",
        "pending_close_reason_ts": 123.0,
    }

    assert consume_close_reason(
        pending_reasons=pending,
        load_recovery_row=lambda _pid: {},
        position_id=101,
        default="broker_close",
    ) == "supervisor_close"
    assert pending == {}


def test_consume_close_reason_falls_back_to_recovery_meta():
    reason = consume_close_reason(
        pending_reasons={},
        load_recovery_row=lambda _pid: {"recovery_meta": {"pending_close_reason": "timeout_close"}},
        position_id=202,
        default="broker_close",
    )

    assert reason == "timeout_close"


def test_serialize_close_verdict_handles_to_dict_failure():
    class _BadVerdict:
        def to_dict(self):
            raise RuntimeError("boom")

    assert serialize_close_verdict(_BadVerdict()) == {
        "allowed": False,
        "reason": "verdict_serialization_failed",
    }


def test_build_risk_state_with_policy_verdict_preserves_existing_state():
    verdict = SimpleNamespace(to_dict=lambda: {"allowed": True, "reason": "ok"})

    state = build_risk_state_with_policy_verdict({"kelly": {"fraction": 0.1}}, verdict)

    assert state == {
        "kelly": {"fraction": 0.1},
        "policy_verdict": {"allowed": True, "reason": "ok"},
    }


def test_build_risk_state_with_policy_verdict_handles_serialized_payloads():
    assert build_risk_state_with_policy_verdict(
        {"risk": "existing"},
        {"allowed": False, "reason": "blocked"},
        serialized=True,
    ) == {
        "risk": "existing",
        "policy_verdict": {"allowed": False, "reason": "blocked"},
    }
    assert build_risk_state_with_policy_verdict({}, {}, serialized=True) == {
        "policy_verdict": {"allowed": False, "reason": "missing_verdict"},
    }


def test_pending_open_attach_helpers_track_active_ids_and_cleanup():
    pending = {}

    remember_pending_open_attach(pending, 303, ttl_seconds=10.0, now_fn=lambda: 100.0)
    remember_pending_open_attach(pending, 0, ttl_seconds=10.0, now_fn=lambda: 100.0)

    assert pending == {303: 110.0}
    assert active_pending_open_attach_ids(pending, set(), now_fn=lambda: 105.0) == [303]
    assert active_pending_open_attach_ids(pending, {303}, now_fn=lambda: 105.0) == []
    assert pending == {}


def test_pending_open_attach_helpers_drop_expired_ids():
    pending = {101: 99.0, 202: 130.0}

    assert active_pending_open_attach_ids(pending, set(), now_fn=lambda: 100.0) == [202]
    assert pending == {202: 130.0}


def test_restore_attribution_for_positions_restores_missing_open_payloads():
    class _Engine:
        def __init__(self):
            self.restored = []

        def has_open(self, position_id):
            return position_id == 101

        def restore_open(self, position_id, payload):
            self.restored.append((position_id, payload))
            return True

    engine = _Engine()
    rows = {
        202: {"recovery_meta": {"trade_attribution": {"entry": "context"}}},
        303: {"recovery_meta": {}},
    }

    restored = restore_attribution_for_positions(
        engine,
        [
            {"position_id": 101},
            {"ticket": 202},
            SimpleNamespace(position_id=303),
            {"position_id": 0},
        ],
        load_recovery_row=lambda pid: rows.get(pid, {}),
    )

    assert restored == 2
    assert engine.restored == [(202, {"entry": "context"}), (303, {})]


def test_restore_attribution_for_positions_logs_and_continues_on_errors():
    class _Engine:
        def has_open(self, _position_id):
            return False

        def restore_open(self, _position_id, _payload):
            raise RuntimeError("restore failed")

    logged = []

    assert (
        restore_attribution_for_positions(
            _Engine(),
            [{"position_id": 404}],
            load_recovery_row=lambda _pid: {"recovery_meta": {"trade_attribution": {"x": 1}}},
            debug_log=lambda pid, exc: logged.append((pid, str(exc))),
        )
        == 0
    )
    assert logged == [(404, "restore failed")]


def test_enrich_positions_with_lifecycle_metrics_merges_display_fields_and_callbacks():
    calls = []

    def _coerce_positions(positions):
        calls.append(("coerce", list(positions)))
        return [
            {
                "position_id": 1,
                "symbol": "XAUUSD+",
                "pnl": 0.0,
                "pnl_state": "known",
                "unrealized_pnl": 0.0,
            }
        ]

    def _apply_unrealized(positions):
        calls.append(("pnl",))
        item = dict(positions[0])
        item["unrealized_pnl"] = 12.5
        return [item]

    def _holding(position, *, cfg, now_ts):
        calls.append(("holding", position["position_id"], cfg.name, now_ts))
        return {"holding_seconds": 30.0}

    def _path(position, *, cfg, now_ts, persist, broker, strategy_name):
        calls.append(("path", persist, broker, strategy_name))
        return {"path_mfe": 22.0}

    def _supervisor(position, *, cfg, now_ts, positions, persist, broker, strategy_name):
        calls.append(("supervisor", len(positions), persist, broker, strategy_name))
        return {
            "action": "hold",
            "action_label": "Hold",
            "summary_reason": "ok",
            "human_summary": "keep watching",
        }

    result = enrich_positions_with_lifecycle_metrics(
        [{"position_id": 1}],
        cfg=SimpleNamespace(name="cfg"),
        now_ts=123.0,
        persist=True,
        broker="ctrader",
        strategy_name="factor_v4",
        coerce_positions=_coerce_positions,
        apply_unrealized_pnl_fields_fn=_apply_unrealized,
        holding_summary_for_position=_holding,
        position_path_metrics_for_position=_path,
        evaluate_position_supervisor_for_position=_supervisor,
    )

    assert result == [
        {
            "position_id": 1,
            "symbol": "XAUUSD+",
            "pnl": 0.0,
            "pnl_state": "known",
            "unrealized_pnl": 12.5,
            "holding_seconds": 30.0,
            "path_mfe": 22.0,
            "supervisor": {
                "action": "hold",
                "action_label": "Hold",
                "summary_reason": "ok",
                "human_summary": "keep watching",
            },
            "supervisor_action": "hold",
            "supervisor_label": "Hold",
            "supervisor_reason": "ok",
            "supervisor_summary": "keep watching",
        }
    ]
    assert calls == [
        ("coerce", [{"position_id": 1}]),
        ("pnl",),
        ("holding", 1, "cfg", 123.0),
        ("path", True, "ctrader", "factor_v4"),
        ("supervisor", 1, True, "ctrader", "factor_v4"),
    ]


def test_current_regime_hint_from_composite_uses_first_available_key():
    assert current_regime_hint_from_composite({"regime_id": "trend", "regime": "range"}) == "trend"
    assert current_regime_hint_from_composite({"regime": "range"}) == "range"
    assert current_regime_hint_from_composite({"regime_state": 3}) == "3"
    assert current_regime_hint_from_composite({"regime_id": "", "regime": None}) == ""
    assert current_regime_hint_from_composite(SimpleNamespace(regime="ignored")) == ""


def test_current_regime_hint_from_composite_derives_context_state():
    assert current_regime_hint_from_composite({
        "context_state": {
            "trend_strength_state": "strong",
            "volatility_state": "normal",
        }
    }) == "trend=strong|volatility=normal"


def test_remember_and_consume_close_verdict_persists_payload():
    pending = {}
    merged = {}
    verdict = SimpleNamespace(to_dict=lambda: {"allowed": True, "reason": "ok"})

    remember_close_verdict(
        pending_verdicts=pending,
        merge_recovery_meta=lambda position_id, meta: merged.setdefault(position_id, meta),
        position_id=303,
        verdict=verdict,
        now_fn=lambda: 456.0,
    )

    assert pending[303] == {"allowed": True, "reason": "ok"}
    assert merged[303] == {
        "pending_close_verdict": {"allowed": True, "reason": "ok"},
        "pending_close_verdict_ts": 456.0,
    }
    assert consume_close_verdict(
        pending_verdicts=pending,
        load_recovery_row=lambda _pid: {},
        build_close_context=lambda **_kwargs: {},
        risk_evaluate=lambda *_args: SimpleNamespace(to_dict=lambda: {"allowed": False}),
        position_id=303,
        close_reason="supervisor_close",
    ) == {"allowed": True, "reason": "ok"}
    assert pending == {}


def test_consume_close_verdict_uses_recovery_meta_before_risk_fallback():
    result = consume_close_verdict(
        pending_verdicts={},
        load_recovery_row=lambda _pid: {"recovery_meta": {"pending_close_verdict": {"allowed": False, "reason": "cached"}}},
        build_close_context=lambda **_kwargs: {"unexpected": True},
        risk_evaluate=lambda *_args: SimpleNamespace(to_dict=lambda: {"allowed": True}),
        position_id=404,
        close_reason="broker_close",
    )

    assert result == {"allowed": False, "reason": "cached"}


def test_consume_close_verdict_falls_back_to_risk_policy():
    seen = {}

    def _build(**kwargs):
        seen["context_kwargs"] = kwargs
        return {"position_id": str(kwargs["position_id"]), "close_reason": kwargs["close_reason"]}

    def _risk(action, context):
        seen["risk"] = (action, context)
        return SimpleNamespace(to_dict=lambda: {"allowed": True, "reason": "fallback"})

    result = consume_close_verdict(
        pending_verdicts={},
        load_recovery_row=lambda _pid: {},
        build_close_context=_build,
        risk_evaluate=_risk,
        position_id=505,
        close_reason="broker_close",
    )

    assert result == {"allowed": True, "reason": "fallback"}
    assert seen["context_kwargs"] == {
        "position_id": 505,
        "close_reason": "broker_close",
        "mode": "live",
    }
    assert seen["risk"] == (
        "close_position",
        {"position_id": "505", "close_reason": "broker_close"},
    )


def test_forget_pending_close_state_clears_both_maps():
    reasons = {606: "broker_close"}
    verdicts = {606: {"allowed": True}}

    forget_pending_close_state(
        pending_reasons=reasons,
        pending_verdicts=verdicts,
        position_id=606,
    )

    assert reasons == {}
    assert verdicts == {}


def test_latest_close_evidence_prefers_newer_trace_over_ledger():
    ledger = {"event_type": "supervisor_tighten", "decision_ts": 100.0}
    trace = {"event_type": "legacy_awe_trailing", "decision_ts": 120.0}

    assert latest_close_evidence(ledger, trace) is trace
    assert latest_close_evidence(ledger, {"decision_ts": 90.0}) is ledger
    assert latest_close_evidence({}, trace) is trace


def test_normalize_supervisor_event_row_preserves_close_evidence_contract():
    row = {
        "decision_id": "decision-1",
        "event_type": "supervisor_close",
        "action_reason": "timeout",
        "action_json": (
            '{"supervisor_verdict":{"action":" close ","summary_reason":"timeout",'
            '"evidence":{"drawdown":1.2},"recommended_controls":{"mode":"full_exit"}}}'
        ),
        "risk_state_json": '{"allowed":true,"reason":"risk_reducing_action"}',
        "decision_ts": 95.25,
    }

    payload = normalize_supervisor_event_row(row, close_ts=100.0)

    assert payload == {
        "decision_id": "decision-1",
        "event_type": "supervisor_close",
        "action_reason": "timeout",
        "decision_ts": 95.25,
        "seconds_before_close": 4.75,
        "action": "close",
        "summary_reason": "timeout",
        "evidence": {"drawdown": 1.2},
        "recommended_controls": {"mode": "full_exit"},
        "risk_state": {"allowed": True, "reason": "risk_reducing_action"},
    }


def test_normalize_supervisor_event_row_handles_bad_json_defaults():
    payload = normalize_supervisor_event_row(
        {
            "decision_id": "decision-2",
            "event_type": "legacy_awe_trailing",
            "action_reason": "legacy",
            "action_json": "{bad",
            "risk_state_json": "[]",
            "decision_ts": 101.0,
        },
        close_ts=100.0,
    )

    assert payload["summary_reason"] == "legacy"
    assert payload["risk_state"] == {}
    assert payload["seconds_before_close"] == 0.0


def test_normalize_supervisor_event_row_accepts_mapping_style_db_rows():
    class Row:
        def __init__(self, data):
            self.data = data

        def __getitem__(self, key):
            return self.data[key]

    payload = normalize_supervisor_event_row(
        Row(
            {
                "decision_id": "decision-db",
                "event_type": "supervisor_tighten",
                "action_reason": "tighten",
                "action_json": '{"supervisor_verdict":{"action":"tighten"}}',
                "risk_state_json": "{}",
                "decision_ts": 90.0,
            }
        ),
        close_ts=100.0,
    )

    assert payload["decision_id"] == "decision-db"
    assert payload["event_type"] == "supervisor_tighten"
    assert payload["seconds_before_close"] == 10.0


def test_normalize_protection_trace_row_infers_timeout_source():
    row = {
        "trace_id": "trace-1",
        "decision_id": "decision-1",
        "action": "close",
        "summary_reason": "holding_timeout",
        "event_ts": 88.0,
        "verdict_json": (
            '{"evidence":{"protection_source":"holding_timeout"},'
            '"recommended_controls":{"close_reason":"holding_timeout"}}'
        ),
        "risk_verdict_json": '{"allowed":true}',
        "execution_json": '{"close_reason_source":"holding_timeout"}',
        "stage": "protection_arbitrated",
        "outcome": "applied",
    }

    payload = normalize_protection_trace_row(row, close_ts=100.0)

    assert payload == {
        "decision_id": "decision-1",
        "trace_id": "trace-1",
        "event_type": "holding_timeout",
        "action_reason": "holding_timeout",
        "decision_ts": 88.0,
        "seconds_before_close": 12.0,
        "action": "close",
        "summary_reason": "holding_timeout",
        "evidence": {"protection_source": "holding_timeout"},
        "recommended_controls": {"close_reason": "holding_timeout"},
        "risk_state": {"allowed": True},
        "execution": {"close_reason_source": "holding_timeout"},
        "stage": "protection_arbitrated",
        "outcome": "applied",
    }


def test_normalize_protection_trace_row_handles_bad_json_and_generic_event_type():
    payload = normalize_protection_trace_row(
        {
            "trace_id": "trace-2",
            "decision_id": "decision-2",
            "action": "reduce",
            "summary_reason": "partial",
            "event_ts": 105.0,
            "verdict_json": "{bad",
            "risk_verdict_json": "{bad",
            "execution_json": "[]",
            "stage": "execution_failed",
            "outcome": "failed",
        },
        close_ts=100.0,
    )

    assert payload["event_type"] == "supervisor_reduce"
    assert payload["evidence"] == {}
    assert payload["risk_state"] == {}
    assert payload["execution"] == {}
    assert payload["seconds_before_close"] == 0.0


def test_build_replayed_close_payloads_prefers_real_pnl_and_preserves_contracts():
    payload = build_replayed_close_payloads(
        position_id=301,
        position_state={
            "symbol": "EURUSD",
            "open_price": 1.2,
            "close_pnl": -5.0,
            "context_integrity": "complete",
        },
        real_pnl={
            "net": 12.5,
            "exec_price": 1.2345,
            "exec_timestamp": 456.7,
            "price_quality": "broker_reported",
        },
        strategy_name="factor_v4",
        now_ts=999.0,
        context_integrity_default="partial",
    )

    assert payload["total_pnl"] == 12.5
    assert payload["close_price"] == 1.2345
    assert payload["close_ts"] == 456.7
    assert payload["context_integrity"] == "complete"
    assert payload["recovery_meta"] == {"replayed_at": 999.0, "strategy_name": "factor_v4"}
    assert payload["decision"] == {
        "event_type": "close",
        "symbol": "EURUSD",
        "timeframe": "",
        "trade_id": "301",
        "position_id": "301",
        "decision_ts": 456.7,
        "portfolio_state": {},
        "action_score": 12.5,
        "action_reason": "restart_replay_close",
        "action_json": {
            "position_id": 301,
            "replayed": True,
            "close_reason": "restart_replay",
            "real_pnl": {
                "net": 12.5,
                "exec_price": 1.2345,
                "exec_timestamp": 456.7,
                "price_quality": "broker_reported",
            },
        },
    }
    assert payload["position_event"] == {
        "position_id": "301",
        "trade_id": "301",
        "symbol": "EURUSD",
        "event_type": "closed",
        "avg_price": 1.2345,
        "realized_pnl": 12.5,
        "details": {
            "replayed": True,
            "close_reason": "restart_replay",
            "real_pnl": {
                "net": 12.5,
                "exec_price": 1.2345,
                "exec_timestamp": 456.7,
                "price_quality": "broker_reported",
            },
        },
        "event_ts": 456.7,
    }
    assert payload["review"] == {
        "position_id": "301",
        "pnl": 12.5,
        "close_price": 1.2345,
        "close_ts": 456.7,
        "contributions": {},
        "attribution_integrity": "missing",
        "real_pnl": {
            "net": 12.5,
            "exec_price": 1.2345,
            "exec_timestamp": 456.7,
            "price_quality": "broker_reported",
        },
        "close_reason": "restart_replay",
        "context_integrity": "complete",
    }


def test_build_replayed_close_payloads_falls_back_to_recovery_state():
    payload = build_replayed_close_payloads(
        position_id=302,
        position_state={"open_price": 2301.5, "close_pnl": -7.25},
        real_pnl=None,
        strategy_name="",
        now_ts=123.0,
        context_integrity_default="partial",
    )

    assert payload["symbol"] == "XAUUSD+"
    assert payload["total_pnl"] == -7.25
    assert payload["close_price"] == 0.0
    assert payload["close_ts"] == 123.0
    assert payload["context_integrity"] == "partial"
    assert payload["decision"]["action_json"]["real_pnl"] == {}
    assert payload["position_event"]["details"]["real_pnl"] == {}
    assert payload["review"]["real_pnl"] is None


def test_classify_close_source_defaults_broker_close_to_external_source():
    result = classify_close_source_from_evidence(close_reason="broker_close", evidence={})

    assert result == {
        "close_reason_source": "external_broker_close",
        "inferred_close_supervisor": {},
    }


def test_classify_close_source_maps_supervisor_and_legacy_evidence():
    cases = [
        ("broker_close", "supervisor_tighten", "supervisor_tighten_stopout"),
        ("broker_close", "supervisor_reduce", "supervisor_reduce_partial_or_stopout"),
        ("broker_close", "supervisor_close", "supervisor_direct_close"),
        ("manual_close", "supervisor_close", "supervisor_direct_close"),
        ("broker_close", "legacy_awe_trailing", "legacy_awe_trailing_stopout"),
        ("broker_close", "holding_timeout", "holding_timeout"),
        ("restart_replay", "supervisor_close", "restart_replay"),
    ]

    for close_reason, event_type, expected in cases:
        result = classify_close_source_from_evidence(
            close_reason=close_reason,
            evidence={"event_type": event_type, "decision_ts": 1.0},
        )
        assert result["close_reason_source"] == expected
        assert result["inferred_close_supervisor"]["event_type"] == event_type


def test_classify_close_source_compacts_recursive_supervisor_evidence():
    evidence = {
        "event_type": "legacy_awe_trailing",
        "decision_ts": 1.0,
        "action": "tighten",
    }
    evidence["candidate"] = {"position": evidence}

    result = classify_close_source_from_evidence(
        close_reason="broker_close",
        evidence=evidence,
    )

    assert json.dumps(result, ensure_ascii=False)
    assert result["inferred_close_supervisor"] == {
        "event_type": "legacy_awe_trailing",
        "decision_ts": 1.0,
        "action": "tighten",
    }


def test_build_close_position_risk_context_payload_computes_holding_timeout_fields():
    payload = build_close_position_risk_context_payload(
        position_id=268,
        close_reason="holding_timeout",
        mode="live",
        broker="ctrader",
        symbol="XAUUSD+",
        entry_ts=1_000.0,
        entry_ts_source="decision_ledger",
        temporal_context={"decision_ts": 4_900.0, "timeframe_seconds": 300, "timeframe": "M5"},
        max_holding_bars=12,
    )

    assert payload == {
        "position_id": "268",
        "close_reason": "holding_timeout",
        "mode": "live",
        "broker": "ctrader",
        "symbol": "XAUUSD+",
        "entry_ts": 1_000.0,
        "entry_ts_source": "decision_ledger",
        "entry_ts_state": "known",
        "holding_seconds": 3_900.0,
        "holding_seconds_state": "known",
        "holding_timeout_fail_closed": False,
        "timeframe_seconds": 300,
        "max_holding_bars": 12,
        "max_holding_seconds": 3_600.0,
        "temporal_context": {"decision_ts": 4_900.0, "timeframe_seconds": 300, "timeframe": "M5"},
    }


def test_build_close_position_risk_context_payload_handles_missing_entry_and_disabled_timeout():
    payload = build_close_position_risk_context_payload(
        position_id=1,
        close_reason="broker_close",
        mode="snapshot",
        broker="",
        symbol="XAUUSD+",
        entry_ts=0.0,
        entry_ts_source="",
        temporal_context={"decision_ts": 100.0, "timeframe_seconds": 60},
        max_holding_bars=0,
    )

    assert payload["holding_seconds"] == 0.0
    assert payload["max_holding_seconds"] == 0.0
    assert payload["entry_ts_source"] == ""
    assert payload["entry_ts_state"] == "unknown"
    assert payload["holding_seconds_state"] == "unknown_infinite_stale"
    assert payload["holding_timeout_fail_closed"] is False


def test_missing_entry_timestamp_fails_closed_when_holding_timeout_is_enabled():
    payload = build_close_position_risk_context_payload(
        position_id=2,
        close_reason="holding_timeout",
        mode="live",
        broker="ctrader",
        symbol="XAUUSD+",
        entry_ts=0.0,
        entry_ts_source="",
        temporal_context={"decision_ts": 100.0, "timeframe_seconds": 300},
        max_holding_bars=12,
    )

    assert payload["entry_ts_state"] == "unknown"
    assert payload["holding_seconds_state"] == "unknown_infinite_stale"
    assert payload["holding_seconds"] == 3600.0
    assert payload["holding_timeout_fail_closed"] is True
    assert holding_timeout_is_expired(payload) is True

    summary = build_holding_summary_from_close_context(payload)
    assert summary["holding_timeout_exceeded"] is True
    assert summary["holding_timeout_fail_closed"] is True
    assert summary["holding_timeout_status"] == "expired_unknown_timestamp"


def test_build_holding_summary_from_close_context_reports_watch_and_remaining_time():
    summary = build_holding_summary_from_close_context(
        {
            "holding_seconds": 3000.0,
            "max_holding_seconds": 3600.0,
            "max_holding_bars": 12,
        }
    )

    assert summary["holding_seconds"] == 3000.0
    assert summary["holding_minutes"] == 50.0
    assert summary["timeout_enabled"] is True
    assert summary["max_holding_bars"] == 12
    assert summary["max_holding_seconds"] == 3600.0
    assert summary["holding_timeout_exceeded"] is False
    assert summary["holding_timeout_ratio"] == 0.8333
    assert summary["holding_timeout_status"] == "watch"
    assert summary["holding_timeout_remaining_seconds"] == 600.0


def test_build_holding_summary_from_close_context_reports_disabled_and_expired_states():
    disabled = build_holding_summary_from_close_context(
        {
            "holding_seconds": 120.0,
            "max_holding_seconds": 0.0,
            "max_holding_bars": 0,
        }
    )
    assert disabled["timeout_enabled"] is False
    assert disabled["holding_timeout_status"] == "disabled"
    assert disabled["holding_timeout_ratio"] == 0.0
    assert disabled["holding_timeout_remaining_seconds"] == 0.0

    expired = build_holding_summary_from_close_context(
        {
            "holding_seconds": 3600.0,
            "max_holding_seconds": 3600.0,
            "max_holding_bars": 12,
        }
    )
    assert expired["timeout_enabled"] is True
    assert expired["holding_timeout_exceeded"] is True
    assert expired["holding_timeout_status"] == "expired"
    assert expired["holding_timeout_remaining_seconds"] == 0.0


def test_holding_timeout_is_expired_requires_positive_limit_and_elapsed_holding():
    assert holding_timeout_is_expired({"holding_seconds": 3600.0, "max_holding_seconds": 3600.0}) is True
    assert holding_timeout_is_expired({"holding_seconds": 3599.0, "max_holding_seconds": 3600.0}) is False
    assert holding_timeout_is_expired({"holding_seconds": 3600.0, "max_holding_seconds": 0.0}) is False
    assert holding_timeout_is_expired({}) is False


def test_build_holding_timeout_verdict_payload_preserves_contract():
    payload = build_holding_timeout_verdict_payload(
        position_id=701,
        decision_ts=1234.5,
        holding_seconds=3900.0,
        max_holding_seconds=3600.0,
    )

    assert payload == {
        "position_id": "701",
        "decision_ts": 1234.5,
        "action": "close",
        "confidence": 1.0,
        "severity": "warn",
        "summary_reason": "holding_timeout",
        "human_summary": "holding timeout exceeded",
        "evidence": {
            "holding_seconds": 3900.0,
            "max_holding_seconds": 3600.0,
            "protection_source": "holding_timeout",
        },
        "recommended_controls": {
            "close_reason": "holding_timeout",
            "protection_mode": "full_exit",
        },
        "supervisor_template": {},
    }


def test_build_holding_timeout_trace_fields_preserves_close_contract():
    risk_verdict = {"allowed": True, "reason": "risk_reducing_action"}

    payload = build_holding_timeout_trace_fields(
        stage="protection_arbitrated",
        outcome="applied",
        decision_id="decision-1",
        risk_verdict=risk_verdict,
        execution_status="applied",
        execution_reason="close_position_success",
        execution={"close_reason_source": "holding_timeout"},
    )

    assert payload == {
        "stage": "protection_arbitrated",
        "outcome": "applied",
        "decision_id": "decision-1",
        "risk_action": "close_position",
        "risk_verdict": risk_verdict,
        "execution_status": "applied",
        "execution_reason": "close_position_success",
        "execution": {"close_reason_source": "holding_timeout"},
    }


def test_build_holding_timeout_trace_fields_uses_safe_defaults():
    payload = build_holding_timeout_trace_fields(
        stage="",
        outcome="",
        decision_id="",
        risk_verdict=None,
        execution_status="",
        execution_reason="",
    )

    assert payload == {
        "stage": "",
        "outcome": "",
        "decision_id": "",
        "risk_action": "close_position",
        "risk_verdict": {},
        "execution_status": "",
        "execution_reason": "",
        "execution": {},
    }


def test_build_holding_timeout_result_trace_fields_maps_all_outcomes():
    risk_verdict = {"allowed": False, "reason": "blocked"}

    blocked = build_holding_timeout_result_trace_fields(
        result="risk_rejected",
        decision_id="decision-1",
        risk_verdict=risk_verdict,
        execution_reason="blocked",
    )
    assert blocked["stage"] == "risk_rejected"
    assert blocked["outcome"] == "blocked"
    assert blocked["execution_status"] == "blocked"
    assert blocked["execution_reason"] == "blocked"

    exception = build_holding_timeout_result_trace_fields(
        result="exception",
        decision_id="decision-1",
        risk_verdict=risk_verdict,
        execution_reason="network_error",
    )
    assert exception["stage"] == "exception"
    assert exception["outcome"] == "failed"
    assert exception["execution_status"] == "exception"
    assert exception["execution_reason"] == "network_error"

    applied = build_holding_timeout_result_trace_fields(
        result="applied",
        decision_id="decision-1",
        risk_verdict={"allowed": True, "reason": "risk_reducing_action"},
    )
    assert applied["stage"] == "protection_arbitrated"
    assert applied["outcome"] == "applied"
    assert applied["execution_status"] == "applied"
    assert applied["execution_reason"] == "close_position_success"
    assert applied["execution"] == {"close_reason_source": "holding_timeout"}

    failed = build_holding_timeout_result_trace_fields(
        result="failed",
        decision_id="decision-1",
        risk_verdict={"allowed": True, "reason": "risk_reducing_action"},
        execution_reason="broker_rejected",
    )
    assert failed["stage"] == "execution_failed"
    assert failed["outcome"] == "failed"
    assert failed["execution_status"] == "failed"
    assert failed["execution_reason"] == "broker_rejected"


def test_build_position_supervisor_context_payload_preserves_supervisor_contract_fields():
    payload = build_position_supervisor_context_payload(
        position={
            "position_id": 9001,
            "symbol": "XAUUSD+",
            "direction": 1,
            "entry_price": 2300.0,
            "current_price": 2312.5,
            "volume": 1000,
            "open_time": 123.0,
            "profit": 42.5,
            "sl": 2290.0,
            "tp": 2320.0,
            "type": "buy",
            "max_holding_seconds": 3600.0,
            "holding_timeout_ratio": 0.5,
        },
        temporal_context={
            "temporal_context": {"timeframe": "M5", "decision_ts": 4000.0},
            "holding_seconds": 1500.0,
            "timeframe_seconds": 300,
        },
        position_metrics={
            "entry_regime": "trend",
            "current_regime": "range",
            "mfe": 50.0,
        },
        entry_decision_id="dec-1",
        risk_snapshot={"var": {"x": 1}},
        max_holding_bars=12,
        open_position_count=2,
        total_api_volume=3000.0,
        account={"equity": 10000.0},
        template_id="default",
        loop_running=True,
    )

    assert payload["position_supervisor_template"] == "default"
    assert payload["position"]["position_id"] == 9001
    assert payload["position"]["trade_id"] == "9001"
    assert payload["position"]["entry_price"] == 2300.0
    assert payload["position"]["current_price"] == 2312.5
    assert payload["position"]["stop_loss"] == 2290.0
    assert payload["position"]["take_profit"] == 2320.0
    assert payload["market"]["bid"] == 2312.5
    assert payload["market"]["ask"] == 2312.5
    assert payload["market"]["timeframe"] == "M5"
    assert payload["market"]["timeframe_seconds"] == 300
    assert payload["market"]["regime_state"] == "range"
    assert payload["risk"]["risk_snapshot"] == {"var": {"x": 1}}
    assert payload["risk"]["max_holding_bars"] == 12
    assert payload["risk"]["open_position_count"] == 2
    assert payload["risk"]["total_api_volume"] == 3000.0
    assert payload["risk"]["holding_timeout_ratio"] == 0.5
    assert payload["risk"]["mfe"] == 50.0
    assert payload["temporal_context"]["holding_minutes"] == 25.0
    assert payload["temporal_context"]["holding_bars"] == 5.0
    assert payload["market_space_context"]["distance_to_sl"] == 22.5
    assert payload["market_space_context"]["distance_to_tp"] == 7.5
    assert payload["entry_context"]["entry_decision_id"] == "dec-1"
    assert payload["entry_context"]["entry_regime"] == "trend"
    assert payload["runtime"]["loop_running"] is True
    assert payload["runtime"]["account"] == {"equity": 10000.0}


def test_build_position_supervisor_context_payload_uses_legacy_price_and_ticket_fallbacks():
    payload = build_position_supervisor_context_payload(
        position={
            "ticket": 55,
            "price_open": 2400.0,
            "price_current": 2398.0,
            "api_volume": 2000,
            "pnl": -3.0,
        },
        temporal_context={
            "holding_seconds": 120.0,
            "timeframe_seconds": 0,
            "timeframe": "M1",
        },
        position_metrics={},
        entry_decision_id="",
        risk_snapshot={},
        max_holding_bars=0,
        open_position_count=0,
        total_api_volume=0.0,
        account=None,
        template_id="",
        loop_running=False,
    )

    assert payload["position"]["position_id"] == 55
    assert payload["position"]["trade_id"] == "55"
    assert payload["position"]["symbol"] == "XAUUSD+"
    assert payload["position"]["entry_price"] == 2400.0
    assert payload["position"]["current_price"] == 2398.0
    assert payload["position"]["volume"] == 2000.0
    assert payload["position"]["unrealized_pnl"] == -3.0
    assert payload["market"]["timeframe"] == "M1"
    assert payload["temporal_context"]["holding_bars"] == 0.0
    assert payload["market_space_context"]["distance_to_sl"] == 0.0
    assert payload["market_space_context"]["distance_to_tp"] == 0.0
    assert payload["runtime"]["loop_running"] is False
    assert payload["runtime"]["account"] == {}


def test_build_position_supervisor_context_payload_uses_canonical_market_dimensions_and_unknowns():
    payload = build_position_supervisor_context_payload(
        position={
            "position_id": 905,
            "direction": 1,
            "entry_price": 2300.0,
            "current_price": 2310.0,
            "sl": 2290.0,
            "tp": 2340.0,
        },
        temporal_context={"holding_seconds": 600.0, "timeframe_seconds": 300},
        position_metrics={"entry_regime": "trend=strong", "current_regime": ""},
        entry_decision_id="entry-905",
        risk_snapshot={},
        market_context={
            "context_state": {
                "trend_strength_state": "strong",
                "trend_strength_score": 0.8,
                "volatility_state": "high",
                "volatility_score": 0.7,
                "event_window_state": "none",
                "event_window_score": 0.0,
                "session_state": "us",
            }
        },
        max_holding_bars=12,
        open_position_count=1,
        total_api_volume=100.0,
        account={},
        template_id="position_supervisor:default.v1",
        loop_running=True,
    )

    assert payload["market"]["regime_id"] == "trend=strong|volatility=high"
    assert payload["market"]["regime_confidence"] == 0.8
    assert payload["market"]["trend_strength_state"] == "strong"
    assert payload["market"]["volatility_state"] == "high"
    assert payload["market_space_context"]["state"] == "known"
    assert payload["market_space_context"]["atr_multiple_from_entry"] is None
    assert payload["market_space_context"]["range_location"] is None
    assert payload["market_space_context"]["structure_bias"] is None


def test_supervisor_recovery_projection_bounds_recursive_previous_verdict():
    previous = {"action": "hold", "summary_reason": "no_action", "evidence": {}}
    previous["evidence"]["supervisor_state"] = {"latest_supervisor": previous}

    meta = build_supervisor_recovery_meta(
        recovery_meta={"latest_supervisor": previous},
        verdict=previous,
    )

    json.dumps(meta)
    assert meta["latest_supervisor"] == {
        "action": "hold",
        "summary_reason": "no_action",
    }


def test_supervisor_context_drops_recursive_recovery_state_before_evaluation():
    supervisor_state = {"supervisor_posture": "range_capture"}
    supervisor_state["latest_supervisor"] = {
        "action": "hold",
        "evidence": {"supervisor_state": supervisor_state},
    }

    payload = build_position_supervisor_context_payload(
        position={"position_id": 1, "current_price": 2300.0},
        temporal_context={"holding_seconds": 0.0, "timeframe_seconds": 300},
        position_metrics={},
        entry_decision_id="",
        risk_snapshot={},
        supervisor_state=supervisor_state,
        max_holding_bars=0,
        open_position_count=1,
        total_api_volume=1.0,
        account={},
        template_id="",
        loop_running=True,
    )

    json.dumps(payload)
    assert payload["risk"]["supervisor_state"] == {
        "supervisor_posture": "range_capture",
        "latest_supervisor": {"action": "hold"},
    }


def test_build_position_supervisor_context_inputs_collects_live_defaults():
    position = {"position_id": 904}
    account = {"equity": 1000.0}
    risk_snapshot = {"kelly": {"fraction": 0.2}}

    inputs = build_position_supervisor_context_inputs(
        position=position,
        cfg=SimpleNamespace(risk_max_holding_bars=12, position_supervisor_template_id="tpl-1"),
        positions=[{"position_id": 904}, {"position_id": 905}],
        account=account,
        entry_decision_id="entry-904",
        risk_snapshot=risk_snapshot,
        total_api_volume=250.0,
        loop_running=False,
    )

    assert inputs == {
        "position": position,
        "entry_decision_id": "entry-904",
        "risk_snapshot": risk_snapshot,
        "market_context": {},
        "supervisor_state": {},
        "max_holding_bars": 12,
        "open_position_count": 2,
        "total_api_volume": 250.0,
        "account": account,
        "template_id": "tpl-1",
        "loop_running": False,
    }


def test_build_position_path_metrics_result_preserves_legacy_summary_fields():
    result = build_position_path_metrics_result(
        metrics={
            "time_in_profit_seconds": 12.3456789,
            "mfe": 10.0,
            "mae": -2.0,
        },
        entry_regime="trend",
        current_regime="range",
        entry_ts_source="decision_ledger",
    )

    assert result["time_in_profit_seconds"] == 12.3456789
    assert result["time_in_profit"] == 12.345679
    assert result["mfe"] == 10.0
    assert result["mae"] == -2.0
    assert result["entry_regime"] == "trend"
    assert result["current_regime"] == "range"
    assert result["entry_ts_source"] == "decision_ledger"


def test_build_position_path_recovery_meta_preserves_existing_meta_and_updates_path():
    next_meta = build_position_path_recovery_meta(
        recovery_meta={
            "existing": True,
            "position_path": {"old": 1},
            "entry_regime": "old_regime",
        },
        next_state={"mfe": 3.0, "mae": -1.0},
        entry_regime="trend",
        current_regime="range",
    )

    assert next_meta == {
        "existing": True,
        "position_path": {"mfe": 3.0, "mae": -1.0},
        "entry_regime": "trend",
        "current_regime": "range",
    }


def test_build_position_path_recovery_meta_omits_blank_regimes():
    next_meta = build_position_path_recovery_meta(
        recovery_meta=None,
        next_state={"mfe": 0.0},
        entry_regime="",
        current_regime="",
    )

    assert next_meta == {
        "position_path": {"mfe": 0.0},
    }


def test_build_position_path_metrics_update_wires_state_and_summary_payloads():
    calls = []

    def _normalize_path_state(raw):
        calls.append(("normalize", raw))
        return {"mfe": 1.0}

    def _update_position_path_metrics(**kwargs):
        calls.append(("update", kwargs))
        return (
            {"mfe": 4.0, "mae": -2.0},
            {
                "time_in_profit_seconds": 8.0,
                "mfe": 4.0,
                "mae": -2.0,
            },
        )

    update = build_position_path_metrics_update(
        recovery_meta={
            "position_path": {"mfe": 1.0},
            "entry_regime": "trend",
            "keep": True,
        },
        entry_context={"source": "decision_ledger"},
        current_pnl=3.5,
        now_ts=123.0,
        holding_seconds=60.0,
        max_holding_seconds=300.0,
        current_regime="range",
        normalize_path_state_fn=_normalize_path_state,
        update_position_path_metrics_fn=_update_position_path_metrics,
    )

    assert calls[0] == ("normalize", {"mfe": 1.0})
    assert calls[1] == (
        "update",
        {
            "previous_state": {"mfe": 1.0},
            "current_pnl": 3.5,
            "now_ts": 123.0,
            "holding_seconds": 60.0,
            "max_holding_seconds": 300.0,
            "entry_regime": "trend",
            "current_regime": "range",
        },
    )
    assert update["result"]["time_in_profit"] == 8.0
    assert update["result"]["entry_regime"] == "trend"
    assert update["result"]["current_regime"] == "range"
    assert update["result"]["entry_ts_source"] == "decision_ledger"
    assert update["next_meta"] == {
        "position_path": {"mfe": 4.0, "mae": -2.0},
        "entry_regime": "trend",
        "keep": True,
        "current_regime": "range",
    }


def test_build_position_path_metrics_inputs_normalizes_live_inputs_and_upsert_defaults():
    inputs = build_position_path_metrics_inputs(
        position={"ticket": 77, "profit": 12.5},
        recovery_row={
            "broker": "row_broker",
            "strategy_name": "row_strategy",
            "status": "recovered",
            "context_integrity": "partial",
            "recovery_meta": {"position_path": {"mfe": 1.0}},
        },
        entry_context={"source": "decision_ledger"},
        holding_summary={"holding_seconds": "60", "max_holding_seconds": 300.0},
        current_regime="range",
        current_pnl=12.5,
        now_ts=1234.5,
        broker="",
        strategy_name="",
        loop_strategy_name="loop_strategy",
        default_context_integrity="full",
    )

    assert inputs == {
        "position_id": 77,
        "recovery_meta": {"position_path": {"mfe": 1.0}},
        "entry_context": {"source": "decision_ledger"},
        "current_pnl": 12.5,
        "now_ts": 1234.5,
        "holding_seconds": 60.0,
        "max_holding_seconds": 300.0,
        "current_regime": "range",
        "upsert_defaults": {
            "broker": "row_broker",
            "strategy_name": "row_strategy",
            "status": "recovered",
            "context_integrity": "partial",
            "meta": {},
        },
    }


def test_build_position_path_metrics_inputs_prefers_explicit_upsert_values():
    inputs = build_position_path_metrics_inputs(
        position=SimpleNamespace(position_id=88),
        recovery_row=None,
        entry_context=None,
        holding_summary=None,
        current_regime="",
        current_pnl=0.0,
        now_ts=0.0,
        broker="ctrader",
        strategy_name="explicit",
        loop_strategy_name="loop",
        default_context_integrity="full",
    )

    assert inputs["position_id"] == 88
    assert inputs["entry_context"] == {}
    assert inputs["recovery_meta"] == {}
    assert inputs["holding_seconds"] == 0.0
    assert inputs["upsert_defaults"] == {
        "broker": "ctrader",
        "strategy_name": "explicit",
        "status": "open",
        "context_integrity": "full",
        "meta": {},
    }


def test_build_protection_candidate_verdict_payload_preserves_trace_contract():
    payload = build_protection_candidate_verdict_payload(
        position_id=704,
        decision_ts=1234.5,
        action="tighten",
        confidence=0.4,
        reason="legacy_awe_trailing",
        source="legacy_awe_trailing",
        evidence={
            "confidence": 0.4,
            "supervisor_template": {
                "schema_version": "position_supervisor_template.v1",
                "template_id": "tpl",
                "template_version": "v1",
                "template_role": "default",
                "thresholds": {"x": 1},
                "sl_policy": {"mode": "trail"},
                "tp_policy": {"mode": "hold"},
                "capture_policy": {"enabled": True},
            },
        },
        controls={"target_stop_loss": 4005.0},
        config_version=7,
        config_hash="abc",
        position_side="long",
    )

    assert payload["position_id"] == "704"
    assert payload["decision_ts"] == 1234.5
    assert payload["action"] == "tighten"
    assert payload["confidence"] == 0.4
    assert payload["summary_reason"] == "legacy_awe_trailing"
    assert payload["human_summary"] == "legacy_awe_trailing"
    assert payload["evidence"]["protection_source"] == "legacy_awe_trailing"
    assert payload["evidence"]["config_version"] == 7
    assert payload["evidence"]["config_hash"] == "abc"
    assert payload["recommended_controls"] == {"target_stop_loss": 4005.0}
    assert payload["supervisor_template"] == {
        "schema_version": "position_supervisor_template.v1",
        "template_id": "tpl",
        "template_version": "v1",
        "template_role": "default",
        "thresholds": {"x": 1},
        "sl_policy": {"mode": "trail"},
        "tp_policy": {"mode": "hold"},
        "capture_policy": {"enabled": True},
    }
    assert payload["requires_risk_verdict"] is True
    assert payload["action_label"] == "收紧保护"
    assert payload["position_side"] == "long"


def test_build_protection_candidate_verdict_payload_falls_back_to_source_and_action_label():
    payload = build_protection_candidate_verdict_payload(
        position_id=1,
        decision_ts=0.0,
        action="reduce",
        confidence=0.0,
        reason="",
        source="entry_protection_repair",
        evidence=None,
        controls=None,
        config_version=0,
        config_hash="",
        position_side="",
    )

    assert payload["summary_reason"] == "entry_protection_repair"
    assert payload["human_summary"] == "entry_protection_repair"
    assert payload["recommended_controls"] == {}
    assert payload["supervisor_template"] == {
        "schema_version": "",
        "template_id": "",
        "template_version": "",
        "template_role": "",
        "thresholds": {},
        "sl_policy": {},
        "tp_policy": {},
        "capture_policy": {},
    }
    assert payload["action_label"] == "reduce"


def test_build_protection_superseded_trace_fields_preserves_execution_shape():
    candidate_payload = {"position_id": 704, "source": "legacy_awe_trailing"}

    payload = build_protection_superseded_trace_fields(
        candidate_payload=candidate_payload,
        risk_action="tighten_position",
        reason="holding_timeout",
    )

    assert payload == {
        "stage": "protection_superseded",
        "outcome": "superseded",
        "risk_action": "tighten_position",
        "execution_status": "superseded",
        "execution_reason": "holding_timeout",
        "execution": {
            "candidate": candidate_payload,
            "superseded_by": "holding_timeout",
        },
    }


def test_build_protection_candidate_risk_context_payload_extends_close_context():
    close_context = {"position_id": "704", "close_reason": "legacy_awe_trailing"}
    position = {"position_id": 704, "symbol": "XAUUSD+"}
    evidence = {"confidence": 0.4}
    controls = {"target_stop_loss": 4005.0}

    payload = build_protection_candidate_risk_context_payload(
        close_context=close_context,
        position=position,
        action="tighten",
        confidence=0.4,
        reason="legacy_awe_trailing",
        evidence=evidence,
        controls=controls,
        loop_running=True,
        bridge_connected=False,
        source="legacy_awe_trailing",
    )

    assert payload["position_id"] == "704"
    assert payload["close_reason"] == "legacy_awe_trailing"
    assert payload["supervisor_action"] == "tighten"
    assert payload["supervisor_confidence"] == 0.4
    assert payload["supervisor_reason"] == "legacy_awe_trailing"
    assert payload["supervisor_evidence"] == evidence
    assert payload["recommended_controls"] == controls
    assert payload["loop_running"] is True
    assert payload["bridge_connected"] is False
    assert payload["protection_source"] == "legacy_awe_trailing"
    assert payload["position"] == position
    assert close_context == {"position_id": "704", "close_reason": "legacy_awe_trailing"}


def test_build_protection_candidate_risk_context_from_candidate_extracts_fields():
    candidate = SimpleNamespace(
        action="tighten",
        evidence={"confidence": 0.65},
        controls={"target_stop_loss": 4005.0},
        reason="legacy_awe_trailing",
        source="legacy_awe_trailing",
    )
    close_context = {"position_id": "704", "close_reason": "legacy_awe_trailing"}
    position = {"position_id": 704, "symbol": "XAUUSD+"}

    payload = build_protection_candidate_risk_context_from_candidate(
        close_context=close_context,
        position=position,
        candidate=candidate,
        loop_running=True,
        bridge_connected=False,
    )

    assert payload["position_id"] == "704"
    assert payload["close_reason"] == "legacy_awe_trailing"
    assert payload["supervisor_action"] == "tighten"
    assert payload["supervisor_confidence"] == 0.65
    assert payload["supervisor_reason"] == "legacy_awe_trailing"
    assert payload["supervisor_evidence"] == {"confidence": 0.65}
    assert payload["recommended_controls"] == {"target_stop_loss": 4005.0}
    assert payload["loop_running"] is True
    assert payload["bridge_connected"] is False
    assert payload["protection_source"] == "legacy_awe_trailing"
    assert payload["position"] == position


def test_build_protection_execution_trace_fields_preserves_execution_contract():
    execution = {"candidate": {"position_id": 704}, "sl_plan": {"allowed": False}}
    risk_verdict = {"allowed": False, "reason": "blocked"}

    payload = build_protection_execution_trace_fields(
        stage="execution_skipped",
        outcome="skipped",
        decision_id="dec-1",
        risk_action="tighten_position",
        risk_verdict=risk_verdict,
        execution_status="skipped",
        execution_reason="not_tightening",
        execution=execution,
    )

    assert payload == {
        "stage": "execution_skipped",
        "outcome": "skipped",
        "decision_id": "dec-1",
        "risk_action": "tighten_position",
        "risk_verdict": risk_verdict,
        "execution_status": "skipped",
        "execution_reason": "not_tightening",
        "execution": execution,
    }


def test_build_protection_execution_trace_fields_uses_safe_defaults():
    payload = build_protection_execution_trace_fields(
        stage="",
        outcome="",
        decision_id="",
        risk_action="",
        risk_verdict=None,
        execution_status="",
        execution_reason="",
        execution=None,
    )

    assert payload == {
        "stage": "",
        "outcome": "",
        "decision_id": "",
        "risk_action": "",
        "risk_verdict": {},
        "execution_status": "",
        "execution_reason": "",
        "execution": {},
    }


def test_build_protection_position_event_details_for_skip_apply_and_failure():
    sl_plan = {"allowed": False, "reason": "not_tightening"}
    controls = {"target_stop_loss": 4005.0}

    skipped = build_protection_position_event_details(
        event_type="amend_skipped",
        source="legacy_awe_trailing",
        action="tighten",
        reason="legacy_awe_trailing",
        risk_verdict_reason="risk_reducing_action",
        sl_plan=sl_plan,
        controls=controls,
    )
    applied = build_protection_position_event_details(
        event_type="tightened",
        source="legacy_awe_trailing",
        action="tighten",
        reason="legacy_awe_trailing",
        risk_verdict_reason="risk_reducing_action",
        sl_plan={"allowed": True},
        controls=controls,
        target_stop_loss_original=4005.0,
        target_stop_loss_sent=4006.0,
        target_take_profit_sent=4030.0,
    )
    failed = build_protection_position_event_details(
        event_type="amend_failed",
        source="legacy_awe_trailing",
        action="tighten",
        reason="legacy_awe_trailing",
        risk_verdict_reason="risk_reducing_action",
        sl_plan=sl_plan,
        controls=controls,
        failure_reason="amend_failed",
    )

    assert skipped == {
        "protection_source": "legacy_awe_trailing",
        "supervisor_action": "tighten",
        "supervisor_reason": "legacy_awe_trailing",
        "risk_verdict_reason": "risk_reducing_action",
        "skip_stage": "protection_arbitrated",
        "skip_reason": "not_tightening",
        "sl_plan": sl_plan,
        "applied_controls": controls,
    }
    assert applied == {
        "protection_source": "legacy_awe_trailing",
        "supervisor_action": "tighten",
        "supervisor_reason": "legacy_awe_trailing",
        "risk_verdict_reason": "risk_reducing_action",
        "close_reason_source": "legacy_awe_trailing",
        "applied_controls": {
            "target_stop_loss": 4005.0,
            "target_stop_loss_original": 4005.0,
            "target_stop_loss_sent": 4006.0,
            "target_take_profit_sent": 4030.0,
            "sl_plan": {"allowed": True},
        },
    }
    assert failed == {
        "protection_source": "legacy_awe_trailing",
        "supervisor_action": "tighten",
        "supervisor_reason": "legacy_awe_trailing",
        "risk_verdict_reason": "risk_reducing_action",
        "failure_stage": "protection_arbitrated",
        "failure_reason": "amend_failed",
        "sl_plan": sl_plan,
        "applied_controls": controls,
    }


def test_build_supervisor_decision_ledger_payload_preserves_contract():
    verdict = {"decision_ts": 123.4, "confidence": 0.8, "summary_reason": "tighten_sl"}
    risk_verdict = {"allowed": True, "reason": "risk_reducing_action"}

    payload = build_supervisor_decision_ledger_payload(
        position={"position_id": 99, "symbol": "XAUUSD+"},
        verdict=verdict,
        risk_state={"allowed": True},
        risk_verdict=risk_verdict,
        account={"balance": 10000.0, "equity": 10010.0},
        cfg=SimpleNamespace(timeframe="M5"),
        event_type="position_supervisor",
        tick=42,
        session_pnl=12.5,
        fallback_decision_ts=999.0,
    )

    assert payload == {
        "event_type": "position_supervisor",
        "symbol": "XAUUSD+",
        "timeframe": "M5",
        "trade_id": "99",
        "position_id": "99",
        "decision_ts": 123.4,
        "portfolio_state": {
            "balance": 10000.0,
            "equity": 10010.0,
            "session_pnl": 12.5,
        },
        "risk_state": {"allowed": True},
        "action_score": 0.8,
        "action_reason": "tighten_sl",
        "action_json": {
            "tick": 42,
            "supervisor_verdict": verdict,
            "risk_verdict": risk_verdict,
        },
    }


def test_build_supervisor_decision_ledger_payload_uses_safe_defaults():
    payload = build_supervisor_decision_ledger_payload(
        position={"ticket": 100},
        verdict={},
        risk_state={},
        risk_verdict=None,
        account=None,
        cfg=SimpleNamespace(),
        event_type="holding_timeout",
        tick=1,
        session_pnl=0.0,
        fallback_decision_ts=555.0,
    )

    assert payload["symbol"] == "XAUUSD+"
    assert payload["trade_id"] == "100"
    assert payload["position_id"] == "100"
    assert payload["decision_ts"] == 555.0
    assert payload["portfolio_state"] == {"balance": 0.0, "equity": 0.0, "session_pnl": 0.0}
    assert payload["action_score"] == 0.0
    assert payload["action_reason"] == "holding_timeout"
    assert payload["action_json"]["risk_verdict"] == {}


def test_build_supervisor_position_event_payload_preserves_contract():
    payload = build_supervisor_position_event_payload(
        position={
            "ticket": 77,
            "symbol": "EURUSD",
            "volume": 2000,
            "price_current": 1.2345,
        },
        event_type="tightened",
        details={"source": "position_supervisor"},
        realized_pnl=3.25,
    )

    assert payload == {
        "position_id": "77",
        "trade_id": "77",
        "symbol": "EURUSD",
        "event_type": "tightened",
        "net_volume": 2000.0,
        "avg_price": 1.2345,
        "realized_pnl": 3.25,
        "details": {"source": "position_supervisor"},
    }


def test_build_supervisor_trace_ledger_payload_preserves_contract():
    verdict = {
        "decision_ts": 123.4,
        "action": "tighten",
        "summary_reason": "profit_lock",
        "confidence": 0.75,
        "supervisor_template": {
            "template_id": "tpl",
            "template_version": "v1",
        },
    }
    risk_verdict = {"allowed": True, "reason": "risk_reducing_action"}
    execution = {"target_stop_loss_sent": 4005.0}

    payload = build_supervisor_trace_ledger_payload(
        position={
            "position_id": 88,
            "symbol": "XAUUSD+",
            "direction": 1,
            "entry_price": 4000.0,
            "current_price": 4010.0,
            "volume": 100.0,
            "sl": 3990.0,
            "tp": 4030.0,
            "profit": 12.5,
        },
        verdict=verdict,
        cfg=SimpleNamespace(timeframe="M5"),
        tick=7,
        stage="protection_arbitrated",
        outcome="applied",
        decision_id="decision-1",
        risk_action="tighten_position",
        risk_verdict=risk_verdict,
        execution_status="applied",
        execution_reason="amend_success",
        execution=execution,
        account={"equity": 10010.0, "balance": 10000.0},
        fallback_event_ts=999.0,
    )

    assert payload["position_id"] == "88"
    assert payload["decision_id"] == "decision-1"
    assert payload["trade_id"] == "88"
    assert payload["symbol"] == "XAUUSD+"
    assert payload["timeframe"] == "M5"
    assert payload["tick"] == 7
    assert payload["event_ts"] == 123.4
    assert payload["action"] == "tighten"
    assert payload["summary_reason"] == "profit_lock"
    assert payload["confidence"] == 0.75
    assert payload["template_id"] == "tpl"
    assert payload["template_version"] == "v1"
    assert payload["stage"] == "protection_arbitrated"
    assert payload["outcome"] == "applied"
    assert payload["risk_action"] == "tighten_position"
    assert payload["risk_allowed"] is True
    assert payload["risk_reason"] == "risk_reducing_action"
    assert payload["execution_status"] == "applied"
    assert payload["execution_reason"] == "amend_success"
    assert payload["context"] == {
        "schema_version": "position_supervisor_trace_context.v1",
        "position": {
            "position_id": "88",
            "symbol": "XAUUSD+",
            "direction": 1,
            "entry_price": 4000.0,
            "current_price": 4010.0,
            "volume": 100.0,
            "sl": 3990.0,
            "tp": 4030.0,
            "pnl": 12.5,
        },
        "account": {"equity": 10010.0, "balance": 10000.0},
        "tick": 7,
    }
    assert payload["verdict"] == verdict
    assert payload["verdict"] is not verdict
    assert payload["risk_verdict"] is risk_verdict
    assert payload["execution"] == {
        **execution,
        "execution_class": "observed",
        "is_real_execution": False,
        "requested_action": "tighten",
        "effective_action": "tighten",
        "recommended_action": "tighten",
    }


def test_build_supervisor_trace_ledger_payload_drops_recursive_context():
    payload = build_supervisor_trace_ledger_payload(
        position={"position_id": 92, "direction": 1},
        verdict={
            "action": "tighten",
            "summary_reason": "profit_lock",
            "evidence": {
                "protection_source": "position_supervisor",
                "supervisor_state": {"latest_supervisor": {"evidence": {"position": {}}}},
            },
            "recommended_controls": {
                "target_stop_loss": 4004.5,
                "position": {"supervisor": "recursive"},
            },
            "position": {"supervisor": "recursive"},
        },
        cfg=SimpleNamespace(timeframe="M5"),
        tick=1,
        stage="protection_arbitrated",
        outcome="observed",
        execution={"execution_class": "observed", "candidate": {"position": {}}},
    )

    assert payload["verdict"] == {
        "action": "tighten",
        "summary_reason": "profit_lock",
        "evidence": {"protection_source": "position_supervisor"},
        "recommended_controls": {"target_stop_loss": 4004.5},
    }
    assert payload["execution"] == {
        "execution_class": "observed",
        "is_real_execution": False,
        "requested_action": "tighten",
        "effective_action": "tighten",
        "recommended_action": "tighten",
    }


def test_supervisor_trace_contract_only_marks_executed_applied_as_real():
    base = {
        "position": {"position_id": 91, "direction": 1},
        "verdict": {"action": "tighten"},
        "cfg": SimpleNamespace(timeframe="M5"),
        "tick": 1,
    }
    applied = build_supervisor_trace_ledger_payload(
        **base,
        stage="executed",
        outcome="applied",
        execution_status="applied",
        execution={
            "is_real_execution": True,
            "broker_action_confirmed": True,
            "reconcile_confirmed": True,
        },
    )
    shadow = build_supervisor_trace_ledger_payload(
        **base,
        stage="canary_shadow",
        outcome="shadow",
        execution_status="shadow_only",
    )

    assert applied["execution"]["execution_class"] == "applied"
    assert applied["execution"]["is_real_execution"] is True
    assert shadow["execution"]["execution_class"] == "shadow"
    assert shadow["execution"]["is_real_execution"] is False


def test_supervisor_action_fingerprint_is_stable_and_persistently_matchable():
    fingerprint = build_supervisor_action_fingerprint(
        position_id=92,
        action="tighten",
        direction=1,
        controls={"target_take_profit": 4030, "target_stop_loss": 4004.5},
    )
    reordered = build_supervisor_action_fingerprint(
        position_id=92,
        action="tighten",
        direction=1,
        controls={"target_stop_loss": 4004.5, "target_take_profit": 4030},
    )

    assert fingerprint == reordered
    assert supervisor_noop_fingerprint_seen(
        recovery_meta={"last_supervisor_noop_fingerprint": fingerprint},
        fingerprint=reordered,
    ) is True


def test_build_supervisor_trace_ledger_payload_uses_safe_defaults():
    payload = build_supervisor_trace_ledger_payload(
        position={"ticket": 89, "price_open": 3990.0, "price_current": 4000.0, "api_volume": 50.0},
        verdict={},
        cfg=SimpleNamespace(),
        tick=0,
        stage="",
        outcome="",
        risk_verdict=None,
        fallback_event_ts=999.0,
    )

    assert payload["position_id"] == "89"
    assert payload["event_ts"] == 999.0
    assert payload["risk_allowed"] is False
    assert payload["risk_reason"] == ""
    assert payload["context"]["position"]["entry_price"] == 3990.0
    assert payload["context"]["position"]["current_price"] == 4000.0
    assert payload["context"]["position"]["volume"] == 50.0
    assert payload["context"]["account"] == {"equity": 0.0, "balance": 0.0}
    assert payload["risk_verdict"] == {}
    assert payload["execution"] == {
        "execution_class": "observed",
        "is_real_execution": False,
        "requested_action": "",
        "effective_action": "",
        "recommended_action": "",
    }


def test_build_recovered_open_ledger_payloads_preserves_open_repair_contract():
    payloads = build_recovered_open_ledger_payloads(
        position_id=268046003,
        recovery_row={
            "symbol": "XAUUSD+",
            "direction": 1,
            "open_price": 4015.92,
            "volume": 100.0,
            "first_seen_at": 1_782_373_400.0,
            "status": "open",
            "strategy_name": "smoke",
            "context_integrity": "partial",
        },
        broker="ctrader",
        close_ts=1_782_373_646.154,
        close_price=3980.89,
        risk_state={"risk": "snapshot"},
        real_pnl={"net": 36.52, "entry_price": 4015.92},
        close_reason="broker_close",
        fallback_strategy_name="factor_v4",
        context_integrity_default="partial",
        fallback_now_ts=1_782_373_700.0,
    )

    assert payloads["decision_payload"] == {
        "event_type": "open",
        "symbol": "XAUUSD+",
        "timeframe": "",
        "trade_id": "268046003",
        "position_id": "268046003",
        "decision_ts": 1_782_373_400.0,
        "portfolio_state": {},
        "risk_state": {"risk": "snapshot"},
        "action_score": 0.0,
        "action_reason": "live_close_open_repair",
        "action_json": {
            "position_id": 268046003,
            "broker": "ctrader",
            "strategy_name": "smoke",
            "price": 4015.92,
            "volume": 100.0,
            "direction": 1,
            "close_reason": "broker_close",
            "repair_source": "recovery_position_state",
            "context_integrity": "partial",
            "real_pnl": {"net": 36.52, "entry_price": 4015.92},
        },
    }
    assert payloads["position_event_payload"] == {
        "position_id": "268046003",
        "trade_id": "268046003",
        "symbol": "XAUUSD+",
        "event_type": "opened",
        "net_volume": 100.0,
        "avg_price": 4015.92,
        "details": {
            "repair_source": "recovery_position_state",
            "close_reason": "broker_close",
            "direction": 1,
        },
        "event_ts": 1_782_373_400.0,
    }
    assert payloads["recovery_state_payload"] == {
        "position_id": 268046003,
        "symbol": "XAUUSD+",
        "direction": 1,
        "open_price": 4015.92,
        "volume": 100.0,
    }
    assert payloads["recovery_state_kwargs"] == {
        "broker": "ctrader",
        "strategy_name": "smoke",
        "status": "open",
        "context_integrity": "partial",
    }
    assert payloads["recovery_state_meta"] == {"open_repaired_before_close": True}


def test_build_recovered_open_ledger_payloads_uses_safe_fallbacks():
    payloads = build_recovered_open_ledger_payloads(
        position_id=77,
        recovery_row={},
        broker="ctrader",
        close_ts=0.0,
        close_price=3980.89,
        risk_state=None,
        real_pnl={"entry_price": 3999.5},
        close_reason="manual_close",
        fallback_strategy_name="fallback_strategy",
        context_integrity_default="partial",
        fallback_now_ts=1000.0,
    )

    assert payloads["decision_payload"]["symbol"] == "XAUUSD+"
    assert payloads["decision_payload"]["decision_ts"] == 999.0
    assert payloads["decision_payload"]["risk_state"] == {}
    assert payloads["decision_payload"]["action_json"]["strategy_name"] == "fallback_strategy"
    assert payloads["decision_payload"]["action_json"]["price"] == 3999.5
    assert payloads["decision_payload"]["action_json"]["context_integrity"] == "partial"
    assert payloads["position_event_payload"]["avg_price"] == 3999.5
    assert payloads["recovery_state_kwargs"]["status"] == "open"
    assert payloads["recovery_state_kwargs"]["context_integrity"] == "partial"


def test_protection_candidate_supersede_reason_prioritizes_timeout():
    assert protection_candidate_supersede_reason(
        position_id=7,
        timeout_handled={7},
        protected_position_ids={7, 8},
    ) == "holding_timeout"
    assert protection_candidate_supersede_reason(
        position_id=8,
        timeout_handled={7},
        protected_position_ids={7, 8},
    ) == "position_supervisor"
    assert protection_candidate_supersede_reason(
        position_id=9,
        timeout_handled={7},
        protected_position_ids={7, 8},
    ) == ""


def test_build_position_protection_cycle_result_sorts_sets():
    result = build_position_protection_cycle_result(
        timeout_handled={5, 1},
        entry_repair_applied={4},
        supervisor_handled={3, 2},
        trailing_applied={9, 8},
        trailing_superseded={7, 6},
    )

    assert result == {
        "timeout": [1, 5],
        "entry_repair": [4],
        "supervisor": [2, 3],
        "trailing_applied": [8, 9],
        "trailing_superseded": [6, 7],
    }


def test_legacy_awe_trailing_atr_config_uses_conviction_bands():
    assert legacy_awe_trailing_atr_config(0.8) == {"trail_atr": 1.5, "activate_atr": 1.0}
    assert legacy_awe_trailing_atr_config(0.5) == {"trail_atr": 2.0, "activate_atr": 1.5}
    assert legacy_awe_trailing_atr_config(0.2) == {"trail_atr": 3.0, "activate_atr": 2.0}


def test_build_legacy_awe_trailing_update_emits_long_candidate_after_activation():
    update = build_legacy_awe_trailing_update(
        position={
            "position_id": 701,
            "symbol": "XAUUSD+",
            "direction": 1,
            "entry_price": 4000.0,
            "current_price_state": "known",
            "sl": 3990.0,
            "tp": 4030.0,
        },
        existing_state=None,
        current_price=4012.0,
        atr_price=5.0,
        conviction=0.8,
        config_version=7,
        config_hash="abc",
    )

    candidate = update["candidate"]
    assert update["activated_now"] is True
    assert update["state"]["best_price"] == 4012.0
    assert candidate["source"] == "legacy_awe_trailing"
    assert candidate["controls"]["target_stop_loss"] == 4004.5
    assert candidate["controls"]["target_take_profit"] == 4030.0
    assert candidate["evidence"]["trail_atr"] == 1.5
    assert candidate["evidence"]["activate_atr"] == 1.0
    assert candidate["config_version"] == 7
    assert candidate["config_hash"] == "abc"


def test_build_legacy_awe_trailing_update_emits_short_candidate_after_activation():
    update = build_legacy_awe_trailing_update(
        position={
            "position_id": 702,
            "symbol": "XAUUSD+",
            "direction": -1,
            "entry_price": 4000.0,
            "current_price_state": "known",
            "sl": 4010.0,
            "tp": 3970.0,
        },
        existing_state=None,
        current_price=3988.0,
        atr_price=5.0,
        conviction=0.8,
        config_version=8,
        config_hash="def",
    )

    candidate = update["candidate"]
    assert update["activated_now"] is True
    assert update["state"]["best_price"] == 3988.0
    assert candidate["controls"]["target_stop_loss"] == 3995.5
    assert candidate["controls"]["target_take_profit"] == 3970.0
    assert candidate["evidence"]["target_sl"] == 3995.5


def test_build_legacy_awe_trailing_update_keeps_state_without_candidate_before_activation():
    update = build_legacy_awe_trailing_update(
        position={
            "position_id": 703,
            "direction": 1,
            "entry_price": 4000.0,
            "current_price_state": "known",
            "sl": 3990.0,
        },
        existing_state=None,
        current_price=4003.0,
        atr_price=5.0,
        conviction=0.8,
        config_version=0,
        config_hash="",
    )

    assert update["activated_now"] is False
    assert update["state"]["activated"] is False
    assert update["candidate"] is None


def test_build_entry_protection_plan_payload_preserves_contract_and_rounding():
    payload = build_entry_protection_plan_payload(
        schema_version="entry_protection_plan.v1",
        position_id=88,
        direction=-1,
        entry_price=4012.456,
        target_stop_loss=4020.125,
        target_take_profit=3994.544,
        requested_volume=1000,
        actual_api_volume=990.5,
        tick=12,
        created_at=12345.6,
        config_version=9,
        config_hash="hash-1",
        status="failed",
        source="factor_v4_open",
        error="attach_failed",
    )

    assert payload == {
        "schema_version": "entry_protection_plan.v1",
        "position_id": 88,
        "source": "factor_v4_open",
        "status": "failed",
        "direction": -1,
        "entry_price": 4012.46,
        "target_stop_loss": 4020.12,
        "target_take_profit": 3994.54,
        "requested_volume": 1000.0,
        "actual_api_volume": 990.5,
        "tick": 12,
        "attempts": 0,
        "last_attempt_ts": 0.0,
        "last_error": "attach_failed",
        "created_at": 12345.6,
        "updated_at": 12345.6,
        "config_version": 9,
        "config_hash": "hash-1",
    }


def test_build_entry_protection_plan_payload_uses_safe_defaults():
    payload = build_entry_protection_plan_payload(
        schema_version="",
        position_id=1,
        direction=0,
        entry_price=0.0,
        target_stop_loss=0.0,
        target_take_profit=0.0,
        requested_volume=0.0,
        actual_api_volume=0.0,
        tick=0,
        created_at=0.0,
        config_version=0,
        config_hash="",
        status="",
        source="",
        error="",
    )

    assert payload["schema_version"] == ""
    assert payload["source"] == "factor_v4_open"
    assert payload["status"] == "pending"
    assert payload["created_at"] == 0.0
    assert payload["updated_at"] == 0.0
    assert payload["config_version"] == 0
    assert payload["config_hash"] == ""


def test_update_entry_protection_plan_payload_marks_failed_attempt():
    updated = update_entry_protection_plan_payload(
        plan={
            "status": "pending",
            "attempts": 1,
            "last_attempt_ts": 0.0,
            "last_error": "",
        },
        status="failed",
        updated_at=200.0,
        error="broker_reject",
        attempted=True,
    )

    assert updated["status"] == "failed"
    assert updated["updated_at"] == 200.0
    assert updated["attempts"] == 2
    assert updated["last_attempt_ts"] == 200.0
    assert updated["last_error"] == "broker_reject"


def test_update_entry_protection_plan_payload_marks_applied_and_records_prices():
    updated = update_entry_protection_plan_payload(
        plan={
            "status": "failed",
            "attempts": 2,
            "last_error": "old_error",
        },
        status="applied",
        updated_at=300.0,
        attempted=False,
        applied_sl=4014.949,
        applied_tp=3996.805,
    )

    assert updated["status"] == "applied"
    assert updated["updated_at"] == 300.0
    assert updated["attempts"] == 2
    assert updated["last_error"] == ""
    assert updated["applied_stop_loss"] == 4014.95
    assert updated["applied_take_profit"] == 3996.8


def test_update_entry_protection_plan_payload_ignores_empty_plan():
    assert update_entry_protection_plan_payload(
        plan={},
        status="applied",
        updated_at=1.0,
    ) == {}


def test_build_applied_entry_protection_plan_payload_preserves_plan_and_prices():
    applied = build_applied_entry_protection_plan_payload(
        plan={
            "schema_version": "entry_protection_plan.v1",
            "position_id": 268,
            "status": "pending",
            "last_error": "old",
            "target_stop_loss": 3998.123,
            "target_take_profit": 4028.987,
        },
        updated_at=456.7,
        applied_sl=3998.126,
        applied_tp=4028.984,
    )

    assert applied["schema_version"] == "entry_protection_plan.v1"
    assert applied["position_id"] == 268
    assert applied["status"] == "applied"
    assert applied["last_error"] == "old"
    assert applied["updated_at"] == 456.7
    assert applied["applied_stop_loss"] == 3998.13
    assert applied["applied_take_profit"] == 4028.98


def test_build_applied_entry_protection_plan_payload_ignores_empty_plan():
    assert build_applied_entry_protection_plan_payload(
        plan={},
        updated_at=1.0,
        applied_sl=3998.0,
        applied_tp=4028.0,
    ) == {}


def test_build_supervisor_tighten_sl_plan_clips_long_stop_below_bid():
    plan = build_supervisor_tighten_sl_plan(
        current_sl=3990.0,
        current_price=4010.0,
        direction=1,
        target_sl=4015.0,
        bid=4010.0,
        ask=4010.2,
        mid=4010.1,
        quote_age_seconds=0.0,
    )

    assert plan["allowed"] is True
    assert plan["reason"] == "ok"
    assert plan["reference_price"] == 4010.0
    assert plan["planned_sl"] == 4009.68
    assert plan["legal_boundary"] == 4009.6792


def test_build_supervisor_tighten_sl_plan_clips_short_stop_above_ask():
    plan = build_supervisor_tighten_sl_plan(
        current_sl=4020.0,
        current_price=4010.0,
        direction=-1,
        target_sl=4005.0,
        bid=4009.8,
        ask=4010.0,
        mid=4009.9,
        quote_age_seconds=0.0,
    )

    assert plan["allowed"] is True
    assert plan["reason"] == "ok"
    assert plan["reference_price"] == 4010.0
    assert plan["planned_sl"] == 4010.32
    assert plan["legal_boundary"] == 4010.3208


def test_build_supervisor_tighten_sl_plan_skips_when_not_more_protective():
    long_plan = build_supervisor_tighten_sl_plan(
        current_sl=4009.0,
        current_price=4010.0,
        direction=1,
        target_sl=4008.0,
        bid=4010.0,
        quote_age_seconds=0.0,
    )
    short_plan = build_supervisor_tighten_sl_plan(
        current_sl=4010.0,
        current_price=4010.0,
        direction=-1,
        target_sl=4012.0,
        ask=4010.0,
        quote_age_seconds=0.0,
    )

    assert long_plan["allowed"] is False
    assert long_plan["reason"] == "not_tightening_long_stop_loss"
    assert short_plan["allowed"] is False
    assert short_plan["reason"] == "not_tightening_short_stop_loss"


def test_build_supervisor_tighten_sl_plan_inputs_extracts_legacy_fields():
    inputs = build_supervisor_tighten_sl_plan_inputs(
        position={
            "stopLoss": 3990.0,
            "price_current": 4010.0,
            "type": "buy",
        },
        target_sl=4005.0,
        quote={"bid": 4010.1, "ask": 4010.3, "price": 4010.2},
    )

    assert {key: inputs[key] for key in ("current_sl", "current_price", "direction", "target_sl", "bid", "ask", "mid")} == {
        "current_sl": 3990.0,
        "current_price": 4010.0,
        "direction": 1,
        "target_sl": 4005.0,
        "bid": 4010.1,
        "ask": 4010.3,
        "mid": 4010.2,
    }
    assert inputs["min_stop_distance_points"] == 0.2
    assert inputs["quote_max_age_seconds"] == 10.0
    assert inputs["quote_age_seconds"] is None


def test_supervisor_tighten_rejects_quote_without_timestamp():
    inputs = build_supervisor_tighten_sl_plan_inputs(
        position={"sl": 3990.0, "current_price": 4010.0, "direction": 1},
        target_sl=4005.0,
        quote={"bid": 4010.0, "ask": 4010.2, "mid": 4010.1},
    )

    plan = build_supervisor_tighten_sl_plan(**inputs)

    assert plan["allowed"] is False
    assert plan["reason"] == "quote_timestamp_unknown"
    assert plan["quote_age_seconds"] is None


def test_target_tp_is_extension_uses_directional_progress():
    assert target_tp_is_extension(current_tp=4030.0, target_tp=4038.0, direction=1) is True
    assert target_tp_is_extension(current_tp=4030.0, target_tp=4029.0, direction=1) is False
    assert target_tp_is_extension(current_tp=3970.0, target_tp=3962.0, direction=-1) is True
    assert target_tp_is_extension(current_tp=3970.0, target_tp=3971.0, direction=-1) is False


def test_build_target_tp_extension_inputs_extracts_legacy_fields():
    assert build_target_tp_extension_inputs(
        position={"takeProfit": 4030.0, "side": "long"},
        target_tp=4038.0,
    ) == {
        "current_tp": 4030.0,
        "target_tp": 4038.0,
        "direction": 1,
    }


def test_target_tp_is_extension_handles_missing_current_and_unknown_direction():
    assert target_tp_is_extension(current_tp=0.0, target_tp=4038.0, direction=1) is True
    assert target_tp_is_extension(current_tp=4030.0, target_tp=0.0, direction=1) is False
    assert target_tp_is_extension(current_tp=4030.0, target_tp=4030.005, direction=0) is False
    assert target_tp_is_extension(current_tp=4030.0, target_tp=4030.02, direction=0) is True


def test_adjust_sl_plan_for_tp_only_protection_preserves_existing_sl_for_tp_repair():
    result = adjust_sl_plan_for_tp_only_protection(
        sl_plan={"allowed": False, "planned_sl": 0.0, "reason": "not_tightening"},
        source="entry_protection_repair",
        entry_protection_repair_source="entry_protection_repair",
        position_sl=3990.0,
        target_tp=4030.0,
        tp_extension_only=False,
    )

    assert result == {
        "planned_sl": 3990.0,
        "sl_plan": {
            "allowed": True,
            "planned_sl": 3990.0,
            "reason": "preserve_existing_stop_loss_for_tp_repair",
        },
    }


def test_adjust_sl_plan_for_tp_only_protection_preserves_existing_sl_for_tp_extension():
    result = adjust_sl_plan_for_tp_only_protection(
        sl_plan={"allowed": False, "planned_sl": 4005.0, "reason": "not_tightening"},
        source="legacy_awe_trailing",
        entry_protection_repair_source="entry_protection_repair",
        position_sl=3990.0,
        target_tp=4038.0,
        tp_extension_only=True,
    )

    assert result["planned_sl"] == 3990.0
    assert result["sl_plan"]["allowed"] is True
    assert result["sl_plan"]["reason"] == "preserve_existing_stop_loss_for_tp_extension"


def test_adjust_sl_plan_for_tp_only_protection_keeps_non_tp_plan_unchanged():
    blocked = {"allowed": False, "planned_sl": 4005.0, "reason": "not_tightening"}
    allowed = {"allowed": True, "planned_sl": 4006.0, "reason": "ok"}

    assert adjust_sl_plan_for_tp_only_protection(
        sl_plan=blocked,
        source="legacy_awe_trailing",
        entry_protection_repair_source="entry_protection_repair",
        position_sl=3990.0,
        target_tp=0.0,
        tp_extension_only=False,
    ) == {"planned_sl": 4005.0, "sl_plan": blocked}
    assert adjust_sl_plan_for_tp_only_protection(
        sl_plan=allowed,
        source="entry_protection_repair",
        entry_protection_repair_source="entry_protection_repair",
        position_sl=3990.0,
        target_tp=4030.0,
        tp_extension_only=True,
    ) == {"planned_sl": 4006.0, "sl_plan": allowed}


def test_build_protection_execution_plan_builds_tightening_plan():
    result = build_protection_execution_plan(
        position={
            "position_id": 7,
            "symbol": "XAUUSD+",
            "direction": 1,
            "sl": 3990.0,
            "tp": 4030.0,
            "current_price": 4010.0,
        },
        controls={"target_stop_loss": 4005.0, "target_take_profit": 4038.0},
        source="legacy_awe_trailing",
        entry_protection_repair_source="entry_protection_repair",
        quote={"bid": 4010.0, "ask": 4010.2, "price": 4010.1, "ts": time.time()},
    )

    assert result["target_sl"] == 4005.0
    assert result["target_tp"] == 4038.0
    assert result["position_sl"] == 3990.0
    assert result["position_tp"] == 4030.0
    assert result["current_tp"] == 4038.0
    assert result["planned_sl"] == 4005.0
    assert result["sl_plan"]["allowed"] is True
    assert result["sl_plan"]["reason"] == "ok"
    assert result["tp_extension_only"] is True


def test_build_protection_execution_plan_preserves_sl_for_tp_repair():
    result = build_protection_execution_plan(
        position={
            "position_id": 7,
            "symbol": "XAUUSD+",
            "direction": 1,
            "sl": 3990.0,
            "tp": 0.0,
            "current_price": 4010.0,
        },
        controls={"target_stop_loss": 0.0, "target_take_profit": 4038.0},
        source="entry_protection_repair",
        entry_protection_repair_source="entry_protection_repair",
        quote={"bid": 4010.0},
    )

    assert result["current_tp"] == 4038.0
    assert result["planned_sl"] == 3990.0
    assert result["sl_plan"]["allowed"] is True
    assert result["sl_plan"]["reason"] == "preserve_existing_stop_loss_for_tp_repair"
    assert result["tp_extension_only"] is True


def test_build_protection_execution_result_payloads_risk_rejected_trace_only():
    payload = build_protection_execution_result_payloads(
        result="risk_rejected",
        source="legacy_awe_trailing",
        action="tighten",
        reason="trail",
        risk_action="tighten_position",
        risk_verdict={"allowed": False, "reason": "blocked"},
        decision_id="dec-1",
        candidate_payload={"position_id": 7},
    )

    assert payload["position_event_type"] == ""
    assert payload["position_event_details"] == {}
    assert payload["trace_fields"] == {
        "stage": "risk_rejected",
        "outcome": "blocked",
        "decision_id": "dec-1",
        "risk_action": "tighten_position",
        "risk_verdict": {"allowed": False, "reason": "blocked"},
        "execution_status": "blocked",
        "execution_reason": "blocked",
        "execution": {"candidate": {"position_id": 7}},
    }


def test_build_protection_execution_result_payloads_skipped_event_and_trace():
    sl_plan = {"allowed": False, "reason": "not_tightening", "planned_sl": 4005.0}
    payload = build_protection_execution_result_payloads(
        result="skipped",
        source="legacy_awe_trailing",
        action="tighten",
        reason="trail",
        risk_action="tighten_position",
        risk_verdict={"allowed": True, "reason": "ok"},
        decision_id="dec-1",
        candidate_payload={"position_id": 7},
        sl_plan=sl_plan,
        controls={"target_stop_loss": 4005.0},
    )

    assert payload["position_event_type"] == "amend_skipped"
    assert payload["position_event_details"]["skip_reason"] == "not_tightening"
    assert payload["position_event_details"]["sl_plan"] is sl_plan
    assert payload["trace_fields"]["stage"] == "execution_skipped"
    assert payload["trace_fields"]["outcome"] == "skipped"
    assert payload["trace_fields"]["execution_status"] == "skipped"
    assert payload["trace_fields"]["execution_reason"] == "not_tightening"
    assert payload["trace_fields"]["execution"] == {
        "sl_plan": sl_plan,
        "candidate": {"position_id": 7},
    }


def test_build_protection_execution_result_payloads_applied_event_and_trace():
    sl_plan = {"allowed": True, "reason": "ok", "planned_sl": 4005.0}
    payload = build_protection_execution_result_payloads(
        result="applied",
        source="legacy_awe_trailing",
        action="tighten",
        reason="trail",
        risk_action="tighten_position",
        risk_verdict={"allowed": True, "reason": "ok"},
        decision_id="dec-1",
        candidate_payload={"position_id": 7},
        sl_plan=sl_plan,
        controls={"target_stop_loss": 4008.0},
        target_stop_loss_original=4008.0,
        target_stop_loss_sent=4005.0,
        target_take_profit_sent=4030.0,
    )

    assert payload["position_event_type"] == "tightened"
    details = payload["position_event_details"]
    assert details["close_reason_source"] == "legacy_awe_trailing"
    assert details["applied_controls"]["target_stop_loss_original"] == 4008.0
    assert details["applied_controls"]["target_stop_loss_sent"] == 4005.0
    assert details["applied_controls"]["target_take_profit_sent"] == 4030.0
    assert payload["trace_fields"]["stage"] == "protection_arbitrated"
    assert payload["trace_fields"]["execution_status"] == "applied"
    assert payload["trace_fields"]["execution"]["target_stop_loss_sent"] == 4005.0


def test_build_protection_execution_result_payloads_failed_event_and_trace():
    sl_plan = {"allowed": True, "reason": "ok", "planned_sl": 4005.0}
    payload = build_protection_execution_result_payloads(
        result="failed",
        source="legacy_awe_trailing",
        action="tighten",
        reason="trail",
        risk_action="tighten_position",
        risk_verdict={"allowed": True, "reason": "ok"},
        decision_id="dec-1",
        candidate_payload={"position_id": 7},
        sl_plan=sl_plan,
        controls={"target_stop_loss": 4008.0},
        failure_reason="broker_rejected",
    )

    assert payload["position_event_type"] == "amend_failed"
    assert payload["position_event_details"]["failure_reason"] == "broker_rejected"
    assert payload["trace_fields"]["stage"] == "execution_failed"
    assert payload["trace_fields"]["outcome"] == "failed"
    assert payload["trace_fields"]["execution_status"] == "failed"
    assert payload["trace_fields"]["execution_reason"] == "broker_rejected"


def test_build_supervisor_risk_context_payload_extends_close_context():
    close_context = {
        "close_reason": "supervisor_tighten",
        "mode": "live",
        "position_id": "old",
        "holding_seconds": 120.0,
    }
    position = {
        "position_id": 701,
        "symbol": "XAUUSD+",
        "direction": 1,
    }
    verdict = {
        "action": "tighten",
        "confidence": 0.72,
        "summary_reason": "profit_giveback_after_mfe",
        "evidence": {"giveback_ratio": 0.6},
        "decision_ts": 1234.0,
        "recommended_controls": {"target_stop_loss": 4005.0},
    }

    payload = build_supervisor_risk_context_payload(
        close_context=close_context,
        position=position,
        verdict=verdict,
    )

    assert payload["close_reason"] == "supervisor_tighten"
    assert payload["mode"] == "live"
    assert payload["holding_seconds"] == 120.0
    assert payload["position_id"] == "701"
    assert payload["position"] == position
    assert payload["supervisor_action"] == "tighten"
    assert payload["supervisor_confidence"] == 0.72
    assert payload["supervisor_reason"] == "profit_giveback_after_mfe"
    assert payload["supervisor_evidence"] == {"giveback_ratio": 0.6}
    assert payload["supervisor_decision_ts"] == 1234.0
    assert payload["recommended_controls"] == {"target_stop_loss": 4005.0}
    assert close_context["position_id"] == "old"


def test_build_supervisor_risk_context_payload_uses_safe_defaults():
    payload = build_supervisor_risk_context_payload(
        close_context={},
        position={"ticket": 55},
        verdict={},
    )

    assert payload["position_id"] == "55"
    assert payload["position"] == {"ticket": 55}
    assert payload["supervisor_action"] is None
    assert payload["supervisor_evidence"] == {}
    assert payload["recommended_controls"] == {}


def test_supervisor_risk_action_for_action_maps_supported_actions():
    assert supervisor_risk_action_for_action("tighten") == "tighten_position"
    assert supervisor_risk_action_for_action("reduce") == "reduce_position"
    assert supervisor_risk_action_for_action("close") == "close_position"
    assert supervisor_risk_action_for_action("hold") == ""
    assert supervisor_risk_action_for_action("") == ""


def test_build_supervisor_runtime_risk_evaluation_inputs_merges_runtime_flags():
    base_context = {"position_id": "701", "loop_running": False}

    payload = build_supervisor_runtime_risk_evaluation_inputs(
        action="tighten",
        risk_context=base_context,
        loop_running=True,
        bridge_connected=False,
    )

    assert payload == {
        "risk_action": "tighten_position",
        "risk_context": {
            "position_id": "701",
            "loop_running": True,
            "bridge_connected": False,
        },
    }
    assert base_context == {"position_id": "701", "loop_running": False}


def test_build_supervisor_tighten_execution_plan_preserves_live_plan_shape():
    plan = build_supervisor_tighten_execution_plan(
        position={
            "position_id": 701,
            "direction": 1,
            "sl": 3990.0,
            "tp": 4030.0,
            "current_price": 4010.0,
        },
        controls={"target_stop_loss": 4005.0, "target_take_profit": 4038.0},
        quote={"bid": 4010.0, "ask": 4010.2, "price": 4010.1, "ts": time.time()},
    )

    assert plan["target_sl"] == 4005.0
    assert plan["current_tp"] == 4030.0
    assert plan["target_tp"] == 4038.0
    assert plan["planned_tp"] == 4038.0
    assert plan["planned_sl"] == 4005.0
    assert plan["sl_plan"]["allowed"] is True
    assert plan["sl_plan"]["reason"] == "ok"


def test_build_supervisor_tighten_result_payloads_maps_skipped_applied_failed():
    verdict = {"summary_reason": "profit_giveback_after_mfe"}
    risk_verdict = {"allowed": True, "reason": "risk_reducing_action"}
    controls = {"target_stop_loss": 4005.0}
    sl_plan = {"allowed": False, "reason": "not_tightening", "planned_sl": 4004.0}

    skipped = build_supervisor_tighten_result_payloads(
        result="skipped",
        action="tighten",
        verdict=verdict,
        risk_action="tighten_position",
        risk_verdict=risk_verdict,
        decision_id="decision-1",
        controls=controls,
        sl_plan=sl_plan,
    )
    assert skipped["position_event_type"] == "amend_skipped"
    assert skipped["position_event_details"]["skip_stage"] == "supervisor_tighten_sltp"
    assert skipped["position_event_details"]["skip_reason"] == "not_tightening"
    assert skipped["trace_fields"]["stage"] == "execution_skipped"
    assert skipped["trace_fields"]["execution_reason"] == "not_tightening"

    applied = build_supervisor_tighten_result_payloads(
        result="applied",
        action="tighten",
        verdict=verdict,
        risk_action="tighten_position",
        risk_verdict=risk_verdict,
        decision_id="decision-1",
        controls=controls,
        sl_plan={"allowed": True, "reason": "ok", "planned_sl": 4005.0},
        target_sl=4006.0,
        planned_sl=4005.0,
        target_tp=4038.0,
        planned_tp=4038.0,
        current_tp=4030.0,
    )
    assert applied["position_event_type"] == "tightened"
    assert applied["position_event_details"]["applied_controls"]["target_stop_loss_original"] == 4006.0
    assert applied["position_event_details"]["applied_controls"]["target_take_profit_sent"] == 4038.0
    assert applied["trace_fields"]["stage"] == "executed"
    assert applied["trace_fields"]["execution_status"] == "applied"
    assert applied["trace_fields"]["execution"]["target_take_profit_changed"] is True

    failed = build_supervisor_tighten_result_payloads(
        result="failed",
        action="tighten",
        verdict=verdict,
        risk_action="tighten_position",
        risk_verdict=risk_verdict,
        decision_id="decision-1",
        controls=controls,
        sl_plan={"allowed": True, "reason": "ok", "planned_sl": 4005.0},
        failure_reason="broker_rejected",
    )
    assert failed["position_event_type"] == "amend_failed"
    assert failed["position_event_details"]["failure_stage"] == "supervisor_tighten_sltp"
    assert failed["position_event_details"]["failure_reason"] == "broker_rejected"
    assert failed["trace_fields"]["stage"] == "execution_failed"
    assert failed["trace_fields"]["execution_reason"] == "broker_rejected"


def test_build_supervisor_close_context_inputs_prefers_control_reason_and_defaults():
    position = {"ticket": 77}
    verdict = {
        "summary_reason": "profit_giveback_after_mfe",
        "recommended_controls": {"close_reason": "supervisor_reduce"},
    }

    inputs = build_supervisor_close_context_inputs(
        position=position,
        verdict=verdict,
        mode="supervisor",
        broker="",
    )

    assert inputs == {
        "position_id": 77,
        "close_reason": "supervisor_reduce",
        "mode": "supervisor",
        "broker": "ctrader",
        "symbol": "XAUUSD+",
        "position": position,
    }


def test_build_supervisor_close_context_inputs_falls_back_to_summary_and_symbol():
    position = {"position_id": 88, "symbol": "EURUSD+"}
    verdict = {"summary_reason": "hold_reason"}

    assert build_supervisor_close_context_inputs(position=position, verdict=verdict) == {
        "position_id": 88,
        "close_reason": "hold_reason",
        "mode": "live",
        "broker": "ctrader",
        "symbol": "EURUSD+",
        "position": position,
    }


def test_build_supervisor_recovery_meta_records_latest_and_applied_state():
    verdict = {
        "action": "tighten",
        "summary_reason": "profit_giveback_after_mfe",
    }
    meta = build_supervisor_recovery_meta(
        recovery_meta={"existing": True},
        verdict=verdict,
        action_applied="tighten",
        applied_ts=123.0,
    )

    assert meta == {
        "existing": True,
        "latest_supervisor": verdict,
        "latest_supervisor_source": "position_supervisor",
        "last_supervisor_applied_action": "tighten",
        "last_supervisor_applied_ts": 123.0,
        "last_supervisor_reason": "profit_giveback_after_mfe",
        "last_supervisor_applied_source": "position_supervisor",
    }


def test_build_supervisor_recovery_meta_omits_applied_fields_when_not_applied():
    verdict = {"action": "hold", "summary_reason": "no_action"}
    meta = build_supervisor_recovery_meta(
        recovery_meta=None,
        verdict=verdict,
    )

    assert meta == {
        "latest_supervisor": verdict,
        "latest_supervisor_source": "position_supervisor",
    }


def test_build_supervisor_recovery_meta_resets_adaptive_episode_on_posture_change_and_clear():
    adaptive_verdict = {
        "action": "tighten",
        "requested_action": "tighten",
        "action_fingerprint": "tighten:4005.0:4030.0",
        "summary_reason": "profit_giveback_after_mfe",
        "decision_ts": 100.0,
        "evidence": {
            "supervisor_posture": "range_capture",
            "closed_bar_key": "bar:42",
            "trigger_tags": ["profit_giveback_after_mfe"],
        },
        "execution_class": "observed",
    }
    meta = build_supervisor_recovery_meta(
        recovery_meta=None,
        verdict=adaptive_verdict,
    )
    assert meta["supervisor_trigger_episode"] == 1
    assert meta["supervisor_last_adaptive_fingerprint"] == "tighten:4005.0:4030.0"

    transition_meta = build_supervisor_recovery_meta(
        recovery_meta=meta,
        verdict={
            "action": "hold",
            "summary_reason": "transition_confirming",
            "decision_ts": 101.0,
            "evidence": {
                "supervisor_posture": "transition_confirming",
                "closed_bar_key": "bar:42",
                "trigger_tags": [],
            },
        },
    )
    assert "supervisor_last_adaptive_fingerprint" not in transition_meta
    assert transition_meta["supervisor_posture"] == "transition_confirming"

    reentered_meta = build_supervisor_recovery_meta(
        recovery_meta=transition_meta,
        verdict={**adaptive_verdict, "decision_ts": 102.0},
    )
    assert reentered_meta["supervisor_trigger_episode"] == 2
    assert reentered_meta["supervisor_last_adaptive_fingerprint"] == (
        "tighten:4005.0:4030.0"
    )


def test_build_protection_recovery_meta_records_source_and_applied_state():
    verdict = {
        "action": "tighten",
        "summary_reason": "legacy_awe_trailing",
    }
    meta = build_protection_recovery_meta(
        recovery_meta={"existing": True},
        verdict=verdict,
        source="legacy_awe_trailing",
        action_applied="tighten",
        applied_ts=456.0,
    )

    assert meta == {
        "existing": True,
        "latest_protection": verdict,
        "latest_protection_source": "legacy_awe_trailing",
        "last_protection_applied_action": "tighten",
        "last_protection_applied_ts": 456.0,
        "last_protection_reason": "legacy_awe_trailing",
        "last_protection_applied_source": "legacy_awe_trailing",
    }


def test_build_protection_recovery_meta_defaults_source_and_omits_applied_fields():
    verdict = {"action": "hold", "summary_reason": "no_action"}
    meta = build_protection_recovery_meta(
        recovery_meta=None,
        verdict=verdict,
        source="",
    )

    assert meta == {
        "latest_protection": verdict,
        "latest_protection_source": "position_protection",
    }


def test_build_supervisor_state_upsert_payload_merges_meta_and_row_defaults():
    verdict = {"action": "tighten", "summary_reason": "giveback"}

    payload = build_supervisor_state_upsert_payload(
        recovery_row={
            "broker": "row_broker",
            "strategy_name": "row_strategy",
            "status": "recovered",
            "context_integrity": "partial",
            "recovery_meta": {"existing": True},
        },
        verdict=verdict,
        broker="",
        strategy_name="",
        loop_strategy_name="loop_strategy",
        default_context_integrity="full",
        action_applied="tighten",
        applied_ts=789.0,
    )

    assert payload == {
        "broker": "row_broker",
        "strategy_name": "row_strategy",
        "status": "recovered",
        "context_integrity": "partial",
        "meta": {
            "existing": True,
            "latest_supervisor": verdict,
            "latest_supervisor_source": "position_supervisor",
            "last_supervisor_applied_action": "tighten",
            "last_supervisor_applied_ts": 789.0,
            "last_supervisor_reason": "giveback",
            "last_supervisor_applied_source": "position_supervisor",
        },
    }


def test_build_protection_state_upsert_payload_prefers_explicit_defaults():
    verdict = {"action": "tighten", "summary_reason": "legacy"}

    payload = build_protection_state_upsert_payload(
        recovery_row=None,
        verdict=verdict,
        source="legacy_awe_trailing",
        broker="ctrader",
        strategy_name="explicit",
        loop_strategy_name="loop_strategy",
        default_context_integrity="full",
        action_applied="tighten",
        applied_ts=456.0,
    )

    assert payload == {
        "broker": "ctrader",
        "strategy_name": "explicit",
        "status": "open",
        "context_integrity": "full",
        "meta": {
            "latest_protection": verdict,
            "latest_protection_source": "legacy_awe_trailing",
            "last_protection_applied_action": "tighten",
            "last_protection_applied_ts": 456.0,
            "last_protection_reason": "legacy",
            "last_protection_applied_source": "legacy_awe_trailing",
        },
    }


def test_build_recovery_upsert_defaults_preserves_row_fallbacks_and_meta():
    meta = {"latest_supervisor": {"action": "hold"}}

    payload = build_recovery_upsert_defaults(
        recovery_row={
            "broker": "row_broker",
            "strategy_name": "row_strategy",
            "status": "recovered",
            "context_integrity": "partial",
        },
        broker="",
        strategy_name="",
        loop_strategy_name="loop_strategy",
        default_context_integrity="full",
        meta=meta,
    )

    assert payload == {
        "broker": "row_broker",
        "strategy_name": "row_strategy",
        "status": "recovered",
        "context_integrity": "partial",
        "meta": meta,
    }


def test_build_recovery_upsert_defaults_prefers_explicit_values_and_safe_defaults():
    meta = {"latest_protection": {"action": "tighten"}}

    explicit = build_recovery_upsert_defaults(
        recovery_row={"strategy_name": "row_strategy"},
        broker="ctrader",
        strategy_name="explicit_strategy",
        loop_strategy_name="loop_strategy",
        default_context_integrity="full",
        meta=meta,
    )
    empty = build_recovery_upsert_defaults(
        recovery_row=None,
        broker="",
        strategy_name="",
        loop_strategy_name="",
        default_context_integrity="full",
        meta=meta,
    )

    assert explicit == {
        "broker": "ctrader",
        "strategy_name": "explicit_strategy",
        "status": "open",
        "context_integrity": "full",
        "meta": meta,
    }
    assert empty == {
        "broker": "ctrader",
        "strategy_name": "factor_v4",
        "status": "open",
        "context_integrity": "full",
        "meta": meta,
    }


def test_normalize_recovery_position_row_parses_meta_and_handles_bad_json():
    good = normalize_recovery_position_row(
        {"position_id": 1, "recovery_meta_json": '{"latest_supervisor":{"action":"hold"}}'}
    )
    bad = normalize_recovery_position_row({"position_id": 2, "recovery_meta_json": "{bad"})

    assert good["recovery_meta"] == {"latest_supervisor": {"action": "hold"}}
    assert bad["recovery_meta"] == {}
    assert normalize_recovery_position_row(None) == {}


def test_merge_recovery_meta_json_overlays_new_meta_safely():
    merged = merge_recovery_meta_json(
        '{"latest_supervisor":{"action":"hold"},"keep":true}',
        {"latest_supervisor": {"action": "close"}, "new": 1},
    )
    fallback = merge_recovery_meta_json("{bad", {"new": 2})

    assert merged == {
        "latest_supervisor": {"action": "close"},
        "keep": True,
        "new": 1,
    }
    assert fallback == {"new": 2}


def test_build_recovery_meta_update_payload_preserves_update_contract():
    payload = build_recovery_meta_update_payload(
        position_id=88,
        existing_meta_json='{"keep":true}',
        meta={"latest_protection": {"action": "tighten"}},
        now_ts=1234.5,
    )

    assert payload == {
        "position_id": 88,
        "recovery_meta": {"keep": True, "latest_protection": {"action": "tighten"}},
        "last_seen_at": 1234.5,
    }


def test_build_recovery_closed_update_payload_preserves_close_contract():
    payload = build_recovery_closed_update_payload(
        position_id=89,
        existing_meta_json='{"keep":true}',
        close_reason="restart_replay",
        close_pnl=-12.5,
        closed_at=4567.8,
        meta={"replayed_at": 4568.0},
    )

    assert payload == {
        "position_id": 89,
        "status": "closed_replayed",
        "closed_at": 4567.8,
        "close_reason": "restart_replay",
        "close_pnl": -12.5,
        "recovery_meta": {"keep": True, "replayed_at": 4568.0},
    }


def test_filter_removed_live_position_uses_position_id_and_ticket():
    positions = [
        {"position_id": 1, "symbol": "XAUUSD+"},
        {"ticket": 2, "symbol": "EURUSD"},
        {"position_id": 3, "symbol": "GBPUSD"},
    ]

    payload = filter_removed_live_position(positions, position_id=2)
    unchanged = filter_removed_live_position(positions, position_id=99)

    assert payload == {
        "position_id": 2,
        "positions": [
            {"position_id": 1, "symbol": "XAUUSD+"},
            {"position_id": 3, "symbol": "GBPUSD"},
        ],
        "removed": True,
    }
    assert unchanged == {
        "position_id": 99,
        "positions": positions,
        "removed": False,
    }


def test_recovery_position_id_helpers_compute_missing_and_active_ids():
    active_rows = [
        {"position_id": "10", "last_seen_at": 1000.0},
        {"position_id": 11, "last_seen_at": 1100.0},
        {"position_id": 0, "last_seen_at": 1200.0},
        {"position_id": "bad", "last_seen_at": 1300.0},
    ]

    assert recovery_active_position_ids(active_rows) == {10, 11}
    assert recovery_missing_position_ids(active_rows=active_rows, current_ids={11, 12}) == {10}


def test_recovery_replay_lookback_from_uses_oldest_replay_row_and_safe_fallback():
    active_rows = [
        {"position_id": 10, "last_seen_at": 1000.0},
        {"position_id": 11, "last_seen_at": 900.0},
        {"position_id": 12, "last_seen_at": "bad"},
    ]

    assert recovery_replay_lookback_from(
        active_rows=active_rows,
        replay_ids={10, 11},
        now_ts=2000.0,
        lookback_sec=300.0,
    ) == 600
    assert recovery_replay_lookback_from(
        active_rows=active_rows,
        replay_ids={12},
        now_ts=2000.0,
        lookback_sec=300.0,
    ) == 1700
    assert recovery_replay_lookback_from(
        active_rows=active_rows,
        replay_ids=set(),
        now_ts=200.0,
        lookback_sec=300.0,
    ) == 0


def test_supervisor_recently_applied_from_meta_checks_action_source_and_cooldown():
    meta = {
        "last_supervisor_applied_action": "tighten",
        "last_supervisor_applied_ts": 100.0,
        "last_supervisor_applied_source": "position_supervisor",
    }

    assert supervisor_recently_applied_from_meta(
        recovery_meta=meta,
        action="tighten",
        now_ts=150.0,
        cooldown_seconds=300.0,
    ) is True
    assert supervisor_recently_applied_from_meta(
        recovery_meta=meta,
        action="close",
        now_ts=150.0,
        cooldown_seconds=300.0,
    ) is False
    assert supervisor_recently_applied_from_meta(
        recovery_meta=meta,
        action="tighten",
        now_ts=450.0,
        cooldown_seconds=300.0,
    ) is False


def test_supervisor_recently_applied_from_meta_rejects_non_supervisor_sources():
    assert supervisor_recently_applied_from_meta(
        recovery_meta={
            "last_supervisor_applied_action": "tighten",
            "last_supervisor_applied_ts": 100.0,
            "last_supervisor_applied_source": "legacy_awe_trailing",
        },
        action="tighten",
        now_ts=150.0,
        cooldown_seconds=300.0,
    ) is False
    assert supervisor_recently_applied_from_meta(
        recovery_meta={
            "last_supervisor_applied_action": "tighten",
            "last_supervisor_applied_ts": 100.0,
            "last_supervisor_applied_source": "supervisor",
        },
        action="tighten",
        now_ts=150.0,
        cooldown_seconds=300.0,
    ) is True


def test_supervisor_reentry_key_normalizes_symbol_and_direction():
    assert supervisor_reentry_key("xau+usd", 1) == "XAUUSD:1"
    assert supervisor_reentry_key("", -3) == "XAUUSD:-1"
    assert supervisor_reentry_key("eurusd", 0) == "EURUSD:-1"


def test_supervisor_reentry_cooldown_seconds_uses_timeframe_fallback():
    assert supervisor_reentry_cooldown_seconds(
        cooldown_bars=3,
        timeframe="M5",
        timeframe_seconds=lambda _tf: 300,
    ) == 900.0
    assert supervisor_reentry_cooldown_seconds(
        cooldown_bars=2,
        timeframe="UNKNOWN",
        timeframe_seconds=lambda _tf: 0,
    ) == 600.0
    assert supervisor_reentry_cooldown_seconds(
        cooldown_bars=0,
        timeframe="M5",
        timeframe_seconds=lambda _tf: 300,
    ) == 0.0


def test_build_supervisor_reentry_block_payload_matches_live_shape():
    payload = build_supervisor_reentry_block_payload(
        symbol="XAUUSD",
        direction=-1,
        position_id=77,
        action="close",
        reason="thesis_broken",
        started_at=100.0,
        cooldown_seconds=900.0,
        current_price=2399.5,
        tick=12,
    )

    assert payload == {
        "active": True,
        "source": "position_supervisor",
        "symbol": "XAUUSD",
        "direction": -1,
        "position_id": 77,
        "action": "close",
        "reason": "thesis_broken",
        "started_at": 100.0,
        "expires_at": 1000.0,
        "cooldown_seconds": 900.0,
        "price": 2399.5,
        "tick": 12,
    }


def test_supervisor_reentry_block_view_returns_remaining_and_expires():
    block = {"expires_at": 160.0, "reason": "thesis_broken"}

    assert supervisor_reentry_block_view(block, now_ts=100.0) == {
        "expires_at": 160.0,
        "reason": "thesis_broken",
        "remaining_seconds": 60.0,
    }
    assert supervisor_reentry_block_view(block, now_ts=160.0) is None
    assert supervisor_reentry_block_view({}, now_ts=100.0) is None


def test_build_pending_supervisor_reentry_block_payload_matches_live_shape():
    assert build_pending_supervisor_reentry_block_payload(
        symbol="XAUUSD",
        direction=1,
        position_id=88,
        action="",
        reason="",
        thesis_status="broken",
        remaining_seconds=900.0,
    ) == {
        "active": True,
        "source": "pending_position_supervisor",
        "symbol": "XAUUSD",
        "direction": 1,
        "position_id": 88,
        "action": "unknown",
        "reason": "broken",
        "thesis_status": "broken",
        "remaining_seconds": 900.0,
    }
