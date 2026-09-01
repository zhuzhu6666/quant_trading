from types import SimpleNamespace

import pandas as pd

from backend.services.live_tick_pipeline import (
    build_close_decision_audit_meta,
    build_close_ledger_payloads,
    build_amend_failed_ledger_payloads,
    build_effective_event_sizing_payload,
    build_factor_bar,
    build_factor_snapshot_summary,
    build_factor_votes,
    guard_current_price_with_spot_quote,
    build_market_order_block,
    build_open_decision_audit_meta,
    build_open_ledger_payloads,
    build_open_order_preflight,
    build_order_failed_ledger_payloads,
    build_signal_log_suffix,
    build_skip_ledger_payload,
    build_trade_review_payload,
    collect_position_ids,
    normalize_live_positions_payload,
    resolve_closed_position_ids,
    resolve_open_protection_prices,
    resolve_order_fill_price,
    resolve_order_position_id,
    select_close_total_pnl,
)


def test_build_factor_bar_matches_live_pipeline_shape():
    df = pd.DataFrame(
        [{"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 9.0}],
        index=pd.to_datetime(["2026-07-05T00:00:00Z"]),
    )

    assert build_factor_bar(df.iloc[-1], df, "M5") == {
        "open": 1.0,
        "high": 2.0,
        "low": 0.5,
        "close": 1.5,
        "volume": 9.0,
        "time": 1783209600.0,
        "timeframe": "M5",
        "complete": True,
    }


def test_factor_snapshot_helpers_preserve_vote_and_summary_payloads():
    composite = SimpleNamespace(
        direction=1,
        score=0.123456,
        tactical_score=0.2,
        macro_score=-0.1,
        alpha_score=0.123456,
        n_active_factors=3,
        n_active_alpha_factors=2,
        n_available_factors=3,
        n_scoring_factors=2,
        n_contributing_factors=1,
        effective_alpha_factor_count=2,
        n_abstain_factors=1,
        composer_version="factor_roles.v2",
        context_state={"volatility_state": "normal"},
        redundancy_groups={"trend": ["trend", "mean"]},
        calibrated_confidence={
            "schema_version": "calibrated_signal_confidence.v1",
            "calibrated_probability": 0.61,
            "sizing_multiplier": 0.805,
        },
    )
    gate = SimpleNamespace(passed=True, reason="ok")

    votes = build_factor_votes(
        {"trend": 0.123456, "noise": "bad", "mean": -0.25},
        {"trend": 9.87654, "noise": 1.0},
        {"trend": "alpha", "noise": "context", "mean": "alpha"},
        {"trend": 0.5, "noise": 0.0, "mean": 0.2},
    )
    summary = build_factor_snapshot_summary(
        composite,
        gate,
        now=123.0,
        decision_bar_ts=120.0,
    )

    assert votes == {
        "trend": {
            "signal": 0.1235,
            "raw": 9.8765,
            "direction": 1,
            "role": "alpha",
            "used_in_score": True,
            "available": True,
            "abstained": False,
        },
        "noise": {
            "signal": None,
            "raw": 1.0,
            "direction": 0,
            "role": "context",
            "used_in_score": False,
            "available": False,
            "abstained": True,
        },
        "mean": {
            "signal": -0.25,
            "raw": None,
            "direction": -1,
            "role": "alpha",
            "used_in_score": True,
            "available": True,
            "abstained": False,
        },
    }
    assert summary == {
        "direction": 1,
        "score": 0.1235,
        "tactical_score": 0.2,
        "macro_score": -0.1,
        "alpha_score": 0.1235,
        "n_active": 3,
        "n_available": 3,
        "n_scoring": 2,
        "n_contributing": 1,
        "n_active_alpha": 2,
        "effective_alpha_factor_count": 2,
        "n_abstain": 1,
        "composer_version": "factor_roles.v2",
        "context_state": {"volatility_state": "normal"},
        "context_policy": {},
        "calibrated_confidence": {
            "schema_version": "calibrated_signal_confidence.v1",
            "calibrated_probability": 0.61,
            "sizing_multiplier": 0.805,
        },
        "redundancy_groups": {"trend": ["trend", "mean"]},
        "gate_passed": True,
        "gate_reason": "ok",
        "ts": 123.0,
        "decision_bar_ts": 120.0,
    }


def test_build_signal_log_suffix_only_emits_directional_signals():
    gate = SimpleNamespace(reason="passed")
    long_signal = SimpleNamespace(
        direction=1,
        score=0.7,
        tactical_score=0.2,
        macro_score=0.5,
        n_active_factors=4,
    )
    flat_signal = SimpleNamespace(
        direction=0,
        score=0.7,
        tactical_score=0.2,
        macro_score=0.5,
        n_active_factors=4,
    )

    assert build_signal_log_suffix(flat_signal, gate) == ""
    assert build_signal_log_suffix(long_signal, gate) == (
        " signal=LONG score=0.7000 tactical=0.2000 macro=0.5000 available=4 scoring=0 contributing=0 gate=passed"
    )


def test_normalize_live_positions_payload_handles_wrapped_and_object_positions():
    obj = SimpleNamespace(position_id=11, ticket=22)

    assert normalize_live_positions_payload({"positions": [{"position_id": 7}]}) == [
        {"position_id": 7}
    ]
    assert normalize_live_positions_payload(
        {"positions": [obj]},
        position_to_dict=lambda item: {"position_id": item.position_id},
    ) == [{"position_id": 11}]


def test_position_id_and_close_detection_helpers_preserve_cache_defer_behavior():
    current = collect_position_ids([{"position_id": "10"}, {"ticket": 12}, {"bad": 1}])

    initial_closed, initial_current, initial_deferred = resolve_closed_position_ids(
        previous_position_ids=set(),
        current_position_ids=current,
        positions_snapshot_ready=True,
    )
    deferred_closed, deferred_current, deferred = resolve_closed_position_ids(
        previous_position_ids={10, 12},
        current_position_ids=set(),
        positions_snapshot_ready=False,
    )
    closed, latest_current, latest_deferred = resolve_closed_position_ids(
        previous_position_ids={10, 12},
        current_position_ids={12},
        positions_snapshot_ready=True,
    )
    durable_closed, durable_current, durable_deferred = resolve_closed_position_ids(
        previous_position_ids=set(),
        tracked_position_ids={14},
        current_position_ids=set(),
        positions_snapshot_ready=True,
    )

    assert current == {10, 12}
    assert initial_closed == set()
    assert initial_current == {10, 12}
    assert initial_deferred is False
    assert deferred_closed == set()
    assert deferred_current == {10, 12}
    assert deferred is True
    assert closed == {10}
    assert latest_current == {12}
    assert latest_deferred is False
    assert durable_closed == {14}
    assert durable_current == set()
    assert durable_deferred is False


def test_close_total_pnl_prefers_real_then_factor_then_fallback():
    assert select_close_total_pnl(
        real_pnl={"net": -1.25},
        factor_contributions={"trend": 99.0},
        fallback_pnl=7.0,
    ) == -1.25
    assert select_close_total_pnl(
        real_pnl=None,
        factor_contributions={"trend": 1.25, "mean": -0.25},
        fallback_pnl=7.0,
    ) == 1.0
    assert select_close_total_pnl(
        real_pnl={},
        factor_contributions={},
        fallback_pnl=7.0,
    ) == 7.0


def test_close_payload_helpers_match_live_ledger_and_review_contracts():
    close_source = {
        "close_reason_source": "supervisor_inferred",
        "inferred_close_supervisor": {"action": "tighten"},
    }
    close_verdict = {"allowed": True, "reason": "ok"}
    real_pnl = {"net": 12.345, "commission": -0.2}
    contributions = {"trend": 10.0, "macro": 2.345}

    audit_meta = build_close_decision_audit_meta(
        position_id=268,
        total_pnl=12.345,
        current_price=3333.456,
        tick=9,
    )
    ledger_payloads = build_close_ledger_payloads(
        position_id=268,
        timeframe="M5",
        decision_ts=1000.0,
        close_ts=1001.0,
        account={"balance": 100.0, "equity": 111.0},
        session_pnl=3.0,
        risk_state={"risk": "state"},
        total_pnl=12.345,
        current_price=3333.456,
        tick=9,
        close_reason="broker_close",
        close_source=close_source,
        attribution_integrity="full",
        close_verdict=close_verdict,
        factor_contributions=contributions,
        real_pnl=real_pnl,
    )
    review_payload = build_trade_review_payload(
        position_id=268,
        total_pnl=12.345,
        current_price=3333.456,
        close_ts=1001.0,
        factor_contributions=contributions,
        exit_decision_id="decision-1",
        real_pnl=real_pnl,
        close_reason="broker_close",
        context_integrity="full",
        attribution_integrity="full",
        close_source=close_source,
    )

    assert audit_meta == {"position_id": 268, "pnl": 12.35, "price": 3333.46, "tick": 9}
    assert ledger_payloads["decision"]["event_type"] == "close"
    assert ledger_payloads["decision"]["trade_id"] == "268"
    assert ledger_payloads["decision"]["portfolio_state"] == {
        "balance": 100.0,
        "equity": 111.0,
        "session_pnl": 3.0,
    }
    assert ledger_payloads["decision"]["action_json"]["close_reason_source"] == "supervisor_inferred"
    assert ledger_payloads["decision"]["action_json"]["risk_verdict"] == close_verdict
    assert ledger_payloads["position_event"]["event_type"] == "closed"
    assert ledger_payloads["position_event"]["realized_pnl"] == 12.345
    assert ledger_payloads["position_event"]["details"]["factor_contributions"] == contributions
    assert review_payload == {
        "position_id": "268",
        "pnl": 12.345,
        "close_price": 3333.456,
        "close_ts": 1001.0,
        "contributions": contributions,
        "exit_decision_id": "decision-1",
        "real_pnl": real_pnl,
        "close_reason": "broker_close",
        "context_integrity": "full",
        "attribution_integrity": "full",
        "close_reason_source": "supervisor_inferred",
        "inferred_close_supervisor": {"action": "tighten"},
    }


def test_close_payload_helpers_accept_legacy_string_close_source():
    ledger_payloads = build_close_ledger_payloads(
        position_id=269,
        timeframe="M5",
        decision_ts=1000.0,
        close_ts=1001.0,
        account={},
        session_pnl=-1.0,
        risk_state={},
        total_pnl=-2.5,
        current_price=3330.0,
        tick=10,
        close_reason="broker_close",
        close_source="external_broker_close",
        attribution_integrity="full",
        close_verdict={},
        factor_contributions={},
        real_pnl={"net": -2.5},
    )
    review_payload = build_trade_review_payload(
        position_id=269,
        total_pnl=-2.5,
        current_price=3330.0,
        close_ts=1001.0,
        factor_contributions={},
        exit_decision_id="decision-2",
        real_pnl={"net": -2.5},
        close_reason="broker_close",
        context_integrity="full",
        attribution_integrity="full",
        close_source="external_broker_close",
    )

    assert ledger_payloads["decision"]["action_json"]["close_reason_source"] == "external_broker_close"
    assert ledger_payloads["decision"]["action_json"]["inferred_close_supervisor"] == {}
    assert ledger_payloads["position_event"]["details"]["close_reason_source"] == "external_broker_close"
    assert review_payload["close_reason_source"] == "external_broker_close"
    assert review_payload["inferred_close_supervisor"] == {}


def test_effective_event_sizing_payload_preserves_policy_candidate_when_below_min():
    result = build_effective_event_sizing_payload(
        base_volume=100.0,
        adjusted_volume=0.0,
        sizing_trace={"event_raw_api_volume": 20.0},
        sizing_block_reason="event_sizing_below_min",
        event_sizing_context={"enabled": True, "multiplier": 0.2},
    )

    assert result == {
        "volume": 100.0,
        "sizing_trace": {
            "event_raw_api_volume": 20.0,
            "event_policy_candidate_api_volume": 100.0,
        },
        "event_sizing_context": {
            "enabled": True,
            "multiplier": 0.2,
            "base_api_volume": 100.0,
            "raw_api_volume": 20.0,
            "adjusted_api_volume": 0.0,
            "effective_requested_api_volume": 100.0,
            "blocked_reason": "event_sizing_below_min",
        },
    }


def test_effective_event_sizing_payload_does_not_lift_non_positive_base_volume():
    result = build_effective_event_sizing_payload(
        base_volume=0.0,
        adjusted_volume=0.0,
        sizing_trace={
            "event_raw_api_volume": 0.0,
            "blocked_reason": "kelly_fraction_non_positive",
        },
        sizing_block_reason="kelly_fraction_non_positive",
        event_sizing_context={"enabled": True, "multiplier": 1.0},
    )

    assert result["volume"] == 0.0
    assert "event_policy_candidate_api_volume" not in result["sizing_trace"]
    assert result["event_sizing_context"]["effective_requested_api_volume"] == 0.0
    assert result["event_sizing_context"]["blocked_reason"] == "kelly_fraction_non_positive"


def test_spot_quote_guard_replaces_price_only_when_fresh_and_close():
    fresh = guard_current_price_with_spot_quote(
        current_price=100.0,
        get_spot_quote=lambda: {"mid": 101.0},
        quote_is_fresh=lambda quote: True,
    )
    stale = guard_current_price_with_spot_quote(
        current_price=100.0,
        get_spot_quote=lambda: {"mid": 101.0},
        quote_is_fresh=lambda quote: False,
    )
    far = guard_current_price_with_spot_quote(
        current_price=100.0,
        get_spot_quote=lambda: {"mid": 140.0},
        quote_is_fresh=lambda quote: True,
    )

    assert fresh == {"current_price": 101.0, "error": None}
    assert stale == {"current_price": 100.0, "error": None}
    assert far == {"current_price": 100.0, "error": None}


def test_spot_quote_guard_preserves_price_and_returns_error_on_exception():
    exc = RuntimeError("quote down")

    result = guard_current_price_with_spot_quote(
        current_price=100.0,
        get_spot_quote=lambda: (_ for _ in ()).throw(exc),
        quote_is_fresh=lambda quote: True,
    )

    assert result["current_price"] == 100.0
    assert result["error"] is exc


def test_open_order_preflight_uses_atr_distances_and_bridge_digits():
    calls: list[tuple[int, float, float, float, int]] = []

    def prices(direction, price, sl_dist, tp_dist, digits):
        calls.append((direction, price, sl_dist, tp_dist, digits))
        return round(price - sl_dist, digits), round(price + tp_dist, digits)

    result = build_open_order_preflight(
        direction=1,
        current_price=3333.333,
        atr_price=10.0,
        strategy_sl_atr=1.5,
        strategy_tp_atr=2.0,
        bridge_meta={"digits": 3},
        protection_prices=prices,
    )

    assert calls == [(1, 3333.333, 15.0, 20.0, 3)]
    assert result == {
        "direction_name": "LONG",
        "sl_dist": 15.0,
        "tp_dist": 20.0,
        "digits": 3,
        "sl_price": 3318.333,
        "tp_price": 3353.333,
    }


def test_open_order_preflight_preserves_percent_fallback_without_atr():
    def prices(direction, price, sl_dist, tp_dist, digits):
        return price + direction * sl_dist, price + direction * tp_dist

    result = build_open_order_preflight(
        direction=-1,
        current_price=100.0,
        atr_price=0.0,
        strategy_sl_atr=99.0,
        strategy_tp_atr=99.0,
        bridge_meta={},
        protection_prices=prices,
    )

    assert result == {
        "direction_name": "SHORT",
        "sl_dist": 2.0,
        "tp_dist": 3.0,
        "digits": 2,
        "sl_price": 98.0,
        "tp_price": 97.0,
    }


def test_order_success_helpers_require_broker_confirmed_position_id():
    assert resolve_order_fill_price(SimpleNamespace(price=0.0), current_price=3333.0) == 0.0
    assert resolve_order_fill_price(SimpleNamespace(price=3334.5), current_price=3333.0) == 3334.5
    assert resolve_order_position_id(SimpleNamespace(position_id=99), positions_before=[]) == 99
    assert resolve_order_position_id(
        SimpleNamespace(position_id=0),
        positions_before=[{"ticket": "123"}],
    ) == 0
    assert resolve_order_position_id(
        SimpleNamespace(position_id=0),
        positions_before=[SimpleNamespace(position_id=456)],
    ) == 0


def test_open_protection_prices_prefers_refreshed_position_open_price():
    calls: list[tuple[int, float, float, float, int]] = []

    def prices(direction, price, sl_dist, tp_dist, digits):
        calls.append((direction, price, sl_dist, tp_dist, digits))
        return price - sl_dist, price + tp_dist

    result = resolve_open_protection_prices(
        direction=1,
        fill_price=100.0,
        current_price=99.0,
        sl_dist=2.0,
        tp_dist=3.0,
        digits=2,
        position_id=7,
        refreshed_positions=[{"position_id": 6, "open": 101.0}, {"position_id": 7, "open": 102.0}],
        position_open_price=lambda position: position["open"],
        protection_prices=prices,
    )

    assert calls == [(1, 100.0, 2.0, 3.0, 2), (1, 102.0, 2.0, 3.0, 2)]
    assert result == {"reference_price": 102.0, "sl_price": 100.0, "tp_price": 105.0}


def test_open_protection_prices_uses_fill_price_when_refreshed_position_missing():
    def prices(direction, price, sl_dist, tp_dist, digits):
        return round(price - sl_dist, digits), round(price + tp_dist, digits)

    result = resolve_open_protection_prices(
        direction=1,
        fill_price=100.123,
        current_price=99.0,
        sl_dist=2.0,
        tp_dist=3.0,
        digits=2,
        position_id=7,
        refreshed_positions=[{"position_id": 8, "open": 102.0}],
        position_open_price=lambda position: position["open"],
        protection_prices=prices,
    )

    assert result == {"reference_price": 100.123, "sl_price": 98.12, "tp_price": 103.12}


def test_market_order_block_prefers_market_reason_then_risk_reason():
    risk_block = SimpleNamespace(allowed=False, reason="risk_policy_block")
    risk_ok = SimpleNamespace(allowed=True, reason="ok")

    market_blocked = build_market_order_block(
        market_session={"can_open_positions": False, "status": "closed", "reason": "weekend"},
        risk_verdict=risk_block,
    )
    risk_blocked = build_market_order_block(
        market_session={"can_open_positions": True},
        risk_verdict=risk_block,
    )
    allowed = build_market_order_block(
        market_session={"can_open_positions": True},
        risk_verdict=risk_ok,
    )

    assert market_blocked == {
        "market_block_reason": "market_session:closed:weekend",
        "order_blocked": True,
        "block_reason": "market_session:closed:weekend",
        "skip_stage": "market_session",
    }
    assert risk_blocked == {
        "market_block_reason": "",
        "order_blocked": True,
        "block_reason": "risk_policy_block",
        "skip_stage": "risk_policy",
    }
    assert allowed == {
        "market_block_reason": "",
        "order_blocked": False,
        "block_reason": "ok",
        "skip_stage": "risk_policy",
    }


def test_skip_ledger_payload_matches_live_contract():
    composite = SimpleNamespace(direction=1)
    gate = SimpleNamespace(passed=False, reason="blocked")
    cfg = SimpleNamespace(timeframe="M5", factor_signal_threshold=0.3)
    risk = SimpleNamespace(to_dict=lambda: {"allowed": False, "reason": "blocked"})

    payload = build_skip_ledger_payload(
        composite=composite,
        gate_result=gate,
        cfg=cfg,
        bar={"time": 1000.0},
        account={"balance": 10.0, "equity": 11.0},
        positions_before=[{"position_id": 1}],
        risk_state={"risk": "state"},
        risk_verdict=risk,
        block_reason="blocked",
        skip_stage="risk_policy",
        tick=4,
        sizing_trace={"base": 100},
        market_session={"can_open_positions": True},
        event_sizing_context={"enabled": True},
        learning_context={"entry_cluster": {"count": 1}},
        decision_ts_fallback=999.0,
    )

    assert payload["event_type"] == "skip"
    assert payload["timeframe"] == "M5"
    assert payload["decision_ts"] == 1000.0
    assert payload["portfolio_state"] == {"balance": 10.0, "equity": 11.0, "n_positions": 1}
    assert payload["action_reason"] == "blocked"
    assert payload["action_json"]["skip_stage"] == "risk_policy"
    assert payload["action_json"]["risk_verdict"] == {"allowed": False, "reason": "blocked"}
    assert payload["action_json"]["entry_cluster"] == {"count": 1}


def test_open_ledger_and_audit_payloads_match_live_contract():
    composite = SimpleNamespace(direction=-1)
    gate = SimpleNamespace(passed=True, reason="ok")
    cfg = SimpleNamespace(timeframe="M5", factor_signal_threshold=0.3)
    risk = SimpleNamespace(to_dict=lambda: {"allowed": True, "reason": "ok"})
    event_sizing = {"enabled": True, "effective_requested_api_volume": 200.0}
    sizing_trace = {"base_api_volume": 200.0}
    learning_context = {"entry_cluster": {"open_position_count_before": 0}}

    payloads = build_open_ledger_payloads(
        composite=composite,
        gate_result=gate,
        cfg=cfg,
        bar={"time": 1000.0},
        account={"balance": 10.0, "equity": 11.0},
        positions_before=[],
        session_pnl=1.5,
        risk_state={"risk": "state"},
        risk_verdict=risk,
        pid=99,
        requested_volume=200.0,
        base_requested_volume=100.0,
        actual_api_volume=200.0,
        current_price=3333.444,
        fill_price=3334.555,
        sl_price=3340.0,
        tp_price=3320.0,
        tick=5,
        event_sizing_context=event_sizing,
        sizing_trace=sizing_trace,
        learning_context=learning_context,
        decision_ts_fallback=999.0,
        event_ts=1001.0,
    )
    audit = build_open_decision_audit_meta(
        position_id=99,
        actual_api_volume=200.0,
        requested_volume=200.0,
        base_requested_volume=100.0,
        event_sizing_context=event_sizing,
        sizing_trace=sizing_trace,
        current_price=3333.444,
        sl_price=3340.0,
        tp_price=3320.0,
        tick=5,
    )

    assert payloads["decision"]["event_type"] == "open"
    assert payloads["decision"]["trade_id"] == "99"
    assert payloads["decision"]["portfolio_state"]["session_pnl"] == 1.5
    assert payloads["decision"]["action_json"]["base_requested_volume"] == 100.0
    assert payloads["decision"]["action_json"]["risk_verdict"] == {"allowed": True, "reason": "ok"}
    assert payloads["submitted_order"]["event_type"] == "submitted"
    assert payloads["filled_order"]["details"]["event_sizing"] == event_sizing
    assert payloads["position_event"]["details"]["sizing_trace"] == sizing_trace
    assert audit == {
        "position_id": 99,
        "volume": 200.0,
        "requested_volume": 200.0,
        "base_requested_volume": 100.0,
        "event_sizing": event_sizing,
        "sizing_trace": sizing_trace,
        "price": 3333.44,
        "sl": 3340.0,
        "tp": 3320.0,
        "tick": 5,
    }


def test_amend_failed_ledger_payloads_cover_comment_and_exception_shapes():
    composite = SimpleNamespace(direction=1)
    gate = SimpleNamespace(passed=True, reason="ok")
    cfg = SimpleNamespace(timeframe="M5")

    comment_payloads = build_amend_failed_ledger_payloads(
        composite=composite,
        gate_result=gate,
        cfg=cfg,
        bar={"time": 1000.0},
        account={"balance": 1.0, "equity": 2.0},
        positions_before=[{"position_id": 1}],
        risk_state={"risk": "state"},
        pid=99,
        requested_volume=200.0,
        fill_price=3334.5,
        sl_price=3320.0,
        tp_price=3360.0,
        actual_api_volume=200.0,
        tick=6,
        action_reason="bad sl",
        comment="bad sl",
        decision_ts_fallback=999.0,
    )
    error_payloads = build_amend_failed_ledger_payloads(
        composite=composite,
        gate_result=gate,
        cfg=cfg,
        bar={},
        account={},
        positions_before=[],
        risk_state={},
        pid=99,
        requested_volume=200.0,
        fill_price=3334.5,
        sl_price=3320.0,
        tp_price=3360.0,
        actual_api_volume=200.0,
        tick=6,
        action_reason="amend_exception:RuntimeError",
        error="boom",
        decision_ts_fallback=999.0,
    )

    assert comment_payloads["decision"]["event_type"] == "amend_failed"
    assert comment_payloads["decision"]["decision_ts"] == 1000.0
    assert comment_payloads["decision"]["portfolio_state"] == {
        "balance": 1.0,
        "equity": 2.0,
        "n_positions": 1,
    }
    assert comment_payloads["decision"]["action_json"]["skip_stage"] == "amend_sltp"
    assert comment_payloads["order_event"]["details"]["comment"] == "bad sl"
    assert error_payloads["decision"]["decision_ts"] == 999.0
    assert error_payloads["decision"]["action_json"]["error"] == "boom"
    assert error_payloads["order_event"]["details"]["error"] == "boom"


def test_order_failed_ledger_payloads_preserve_broker_failure_contract():
    composite = SimpleNamespace(direction=-1)
    gate = SimpleNamespace(passed=True, reason="ok")
    cfg = SimpleNamespace(timeframe="M5")

    payloads = build_order_failed_ledger_payloads(
        composite=composite,
        gate_result=gate,
        cfg=cfg,
        bar={"time": 1000.0},
        account={"balance": 10.0, "equity": 12.0},
        positions_before=[{"position_id": 1}, {"position_id": 2}],
        risk_state={"risk": "state"},
        requested_volume=300.0,
        current_price=3333.456,
        sl_price=3340.0,
        tp_price=3320.0,
        tick=7,
        error_code="REJECTED",
        comment="market closed",
        decision_ts_fallback=999.0,
    )

    assert payloads["decision"]["event_type"] == "order_failed"
    assert payloads["decision"]["action_reason"] == "REJECTED market closed"
    assert payloads["decision"]["portfolio_state"]["n_positions"] == 2
    assert payloads["decision"]["action_json"] == {
        "tick": 7,
        "skip_stage": "broker_order_failed",
        "requested_volume": 300.0,
        "price": 3333.46,
        "sl": 3340.0,
        "tp": 3320.0,
        "error_code": "REJECTED",
        "comment": "market closed",
    }
    assert payloads["order_event"] == {
        "event_type": "order_failed",
        "price": 3333.456,
        "volume": 300.0,
        "status": "failed",
        "details": {
            "tick": 7,
            "direction": -1,
            "error_code": "REJECTED",
            "comment": "market closed",
        },
    }
