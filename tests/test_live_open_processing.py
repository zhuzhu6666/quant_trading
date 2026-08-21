from __future__ import annotations

from types import SimpleNamespace

from backend.services.live_open_processing import (
    AmendFailureRequest,
    AmendFailureRuntime,
    AmendedOpenSuccessRequest,
    AmendedOpenSuccessRuntime,
    FilledOpenRequest,
    FilledOpenRuntime,
    record_amend_failure_after_fill,
    record_amended_open_success_context,
    record_filled_position_open_context,
)


def _filled_request() -> FilledOpenRequest:
    return FilledOpenRequest(
        attr_engine=object(),
        broker="ctrader",
        cfg=object(),
        bar={"time": 1.0},
        tick=7,
        pid=501,
        actual_api_volume=100.0,
        requested_volume=100.0,
        base_requested_volume=120.0,
        fill_price=2400.0,
        current_price=2400.1,
        sl_price=2398.0,
        tp_price=2404.0,
        acct={"equity": 1000.0},
        pos=[],
        composite=SimpleNamespace(direction=1),
        gate_result=object(),
        risk_verdict={"allowed": True},
        market_session={"status": "open"},
        event_sizing_context={"multiplier": 1.0},
        sizing_trace={"final": 100.0},
        sl_dist=2.0,
        tp_dist=4.0,
        bridge=object(),
    )


def test_filled_open_ledger_failure_does_not_block_recovery_persist():
    recovery = []
    debug = []
    context_calls = []

    def build_context(**kwargs):
        context_calls.append(kwargs)
        return {"context": len(context_calls)}

    def fail_ledger(**_kwargs):
        raise RuntimeError("ledger unavailable")

    result = record_filled_position_open_context(
        _filled_request(),
        runtime=FilledOpenRuntime(
            ledger_available=True,
            record_attribution=lambda **_kwargs: {"attr": "open"},
            build_learning_context=build_context,
            log_ledger=fail_ledger,
            upsert_recovery=lambda **kwargs: recovery.append(kwargs),
            debug=lambda *args: debug.append(args),
        ),
    )

    assert result == ""
    assert len(context_calls) == 2
    assert "sizing_trace" in context_calls[0]
    assert "sizing_trace" not in context_calls[1]
    assert recovery[0]["entry_decision_id"] == ""
    assert recovery[0]["trade_attribution_payload"] == {"attr": "open"}
    assert "ledger open persist failed" in debug[0][0]


def _success_request(logs) -> AmendedOpenSuccessRequest:
    filled = _filled_request()
    return AmendedOpenSuccessRequest(
        attr_engine=filled.attr_engine,
        bridge=filled.bridge,
        broker=filled.broker,
        cfg=filled.cfg,
        bar=filled.bar,
        tick=filled.tick,
        pid=filled.pid,
        actual_api_volume=filled.actual_api_volume,
        requested_volume=filled.requested_volume,
        base_requested_volume=float(filled.base_requested_volume or 0.0),
        fill_price=filled.fill_price,
        current_price=filled.current_price,
        sl_price=filled.sl_price,
        tp_price=filled.tp_price,
        sl_dist=filled.sl_dist,
        tp_dist=filled.tp_dist,
        acct=filled.acct,
        pos=filled.pos,
        composite=filled.composite,
        gate_result=filled.gate_result,
        risk_verdict=filled.risk_verdict,
        market_session=filled.market_session or {},
        event_sizing_context=filled.event_sizing_context or {},
        sizing_trace=filled.sizing_trace or {},
        entry_protection_plan={"status": "verified"},
        direction_name="LONG",
        log=logs.append,
        submit_started_at=10.0,
        fill_received_at=11.0,
    )


def test_amended_success_preserves_processing_order_and_context():
    events = []
    logs = []

    record_amended_open_success_context(
        _success_request(logs),
        runtime=AmendedOpenSuccessRuntime(
            mark_local_state=lambda **_kwargs: events.append("local"),
            record_execution_quality=lambda **_kwargs: events.append("quality"),
            record_attribution=lambda **_kwargs: (
                events.append("attribution") or {"attr": "verified"}
            ),
            build_learning_context=lambda **_kwargs: (
                events.append("learning") or {"learning": True}
            ),
            log_ledger=lambda **_kwargs: (
                events.append("ledger") or "decision-501"
            ),
            upsert_recovery=lambda **kwargs: events.append(
                ("recovery", kwargs["entry_decision_id"])
            ),
        ),
    )

    assert events == [
        "local",
        "quality",
        "attribution",
        "learning",
        "ledger",
        ("recovery", "decision-501"),
    ]
    assert logs == []


def test_amended_success_aux_failure_is_logged_after_local_safety_state():
    events = []
    logs = []

    def fail_attribution(**_kwargs):
        events.append("attribution")
        raise RuntimeError("audit unavailable")

    record_amended_open_success_context(
        _success_request(logs),
        runtime=AmendedOpenSuccessRuntime(
            mark_local_state=lambda **_kwargs: events.append("local"),
            record_execution_quality=lambda **_kwargs: events.append("quality"),
            record_attribution=fail_attribution,
            build_learning_context=lambda **_kwargs: {},
            log_ledger=lambda **_kwargs: "",
            upsert_recovery=lambda **_kwargs: None,
        ),
    )

    assert events == ["local", "quality", "attribution"]
    assert "audit unavailable" in logs[0]


def _failure_request(logs) -> AmendFailureRequest:
    success = _success_request(logs)
    return AmendFailureRequest(
        attr_engine=success.attr_engine,
        bridge=success.bridge,
        broker=success.broker,
        cfg=success.cfg,
        bar=success.bar,
        tick=success.tick,
        pid=success.pid,
        actual_api_volume=success.actual_api_volume,
        requested_volume=success.requested_volume,
        base_requested_volume=success.base_requested_volume,
        fill_price=success.fill_price,
        current_price=success.current_price,
        sl_price=success.sl_price,
        tp_price=success.tp_price,
        sl_dist=success.sl_dist,
        tp_dist=success.tp_dist,
        acct=success.acct,
        pos=success.pos,
        composite=success.composite,
        gate_result=success.gate_result,
        risk_verdict=success.risk_verdict,
        market_session=success.market_session,
        event_sizing_context=success.event_sizing_context,
        sizing_trace=success.sizing_trace,
        status_error="projection mismatch",
        ledger_action_reason="projection_unverified",
        ledger_comment="accepted",
        failure_log="amend failed closed",
        log=logs.append,
    )


def test_amend_failure_latches_before_recovery_and_audit():
    events = []
    logs = []

    def latch(**_kwargs):
        events.append("latch")

    record_amend_failure_after_fill(
        _failure_request(logs),
        runtime=AmendFailureRuntime(
            persist_fail_closed=latch,
            record_aux_failure=lambda *_args, **_kwargs: events.append("aux"),
            record_filled_context=lambda _request: events.append("filled") or "",
            update_plan_status=lambda *_args, **_kwargs: events.append("status"),
            ledger_available=True,
            build_failed_payloads=lambda **_kwargs: {
                "decision": {"kind": "amend_failed"},
                "order_event": {"status": "failed"},
            },
            get_risk_state=lambda: {"status": "known"},
            log_composite_decision=lambda **_kwargs: (
                events.append("decision") or "decision-failed-501"
            ),
            log_order_event=lambda **_kwargs: events.append("order"),
            debug=lambda *_args: events.append("debug"),
            now=lambda: 12.0,
        ),
    )

    assert events == ["latch", "filled", "status", "decision", "order"]
    assert logs == ["amend failed closed"]


def test_latch_persistence_failure_records_aux_and_continues_recovery():
    events = []

    def fail_latch(**_kwargs):
        events.append("latch")
        raise OSError("latch unavailable")

    record_amend_failure_after_fill(
        _failure_request([]),
        runtime=AmendFailureRuntime(
            persist_fail_closed=fail_latch,
            record_aux_failure=lambda *_args, **_kwargs: events.append("aux"),
            record_filled_context=lambda _request: events.append("filled") or "",
            update_plan_status=lambda *_args, **_kwargs: events.append("status"),
            ledger_available=False,
            build_failed_payloads=lambda **_kwargs: {},
            get_risk_state=lambda: {},
            log_composite_decision=lambda **_kwargs: "",
            log_order_event=lambda **_kwargs: None,
            debug=lambda *_args: None,
            now=lambda: 12.0,
        ),
    )

    assert events == ["latch", "aux", "filled", "status"]
