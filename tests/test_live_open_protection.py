from __future__ import annotations

from types import SimpleNamespace

from backend.services.live_open_protection import (
    OpenProtectionRequest,
    OpenProtectionRuntime,
    attach_open_trade_protection,
)


def _request() -> OpenProtectionRequest:
    return OpenProtectionRequest(
        bridge=SimpleNamespace(_symbol_meta={"digits": 3}),
        attr_engine=object(),
        broker="ctrader",
        cfg=object(),
        bar={"time": 1.0},
        tick=9,
        position_id=501,
        actual_api_volume=100.0,
        requested_volume=100.0,
        base_requested_volume=120.0,
        fill_price=2400.0,
        current_price=2400.1,
        sl_price=2398.0,
        tp_price=2404.0,
        sl_dist=2.0,
        tp_dist=4.0,
        account={"equity": 1000.0},
        positions=[],
        composite=object(),
        gate_result=object(),
        candidate=SimpleNamespace(
            risk_verdict={"allowed": True},
            market_session={"status": "open"},
            event_sizing_context={"multiplier": 1.0},
            sizing_trace={"final": 100.0},
            direction_name="LONG",
        ),
        entry_protection_plan={"status": "pending"},
        log=lambda _message: None,
        submit_started_at=10.0,
        fill_received_at=11.0,
    )


def _runtime(events, **overrides) -> OpenProtectionRuntime:
    values = {
        "amend_position": (
            lambda **_kwargs: SimpleNamespace(success=True, comment="")
        ),
        "reconcile_positions": lambda _bridge: {
            "success": True,
            "reconcile_id": "positions-501",
        },
        "verify_projection": lambda *_args, **_kwargs: {"ok": True},
        "publish_projection": (
            lambda *args, **kwargs: events.append(("publish", args, kwargs))
        ),
        "release_pending_latch": (
            lambda *args, **kwargs: events.append(("release", args, kwargs))
        ),
        "record_success": (
            lambda **kwargs: events.append(("success", kwargs))
        ),
        "record_failure": (
            lambda **kwargs: events.append(("failure", kwargs))
        ),
        "record_aux_failure": (
            lambda *args, **kwargs: events.append(("aux", args, kwargs))
        ),
    }
    values.update(overrides)
    return OpenProtectionRuntime(**values)


def test_verified_projection_releases_latch_before_recording_success():
    events = []
    verification_calls = []
    runtime = _runtime(
        events,
        verify_projection=lambda projection, **kwargs: (
            verification_calls.append((projection, kwargs)) or {"ok": True}
        ),
    )

    attach_open_trade_protection(_request(), runtime=runtime)

    assert [event[0] for event in events] == [
        "publish",
        "release",
        "success",
    ]
    assert verification_calls[0][1]["precision"] == 3
    assert events[1][1] == (501,)
    assert events[2][1]["bridge"] is not None
    assert events[2][1]["entry_protection_plan"] == {"status": "pending"}


def test_amend_success_without_projection_ack_remains_unverified():
    events = []
    runtime = _runtime(
        events,
        verify_projection=lambda *_args, **_kwargs: {
            "ok": False,
            "reason": "stop_loss_mismatch",
        },
    )

    attach_open_trade_protection(_request(), runtime=runtime)

    assert [event[0] for event in events] == ["aux", "failure"]
    assert events[0][1] == ("entry_protection_projection_unverified",)
    failure = events[1][1]
    assert failure["status_error"] == (
        "entry_protection_projection_unverified:stop_loss_mismatch"
    )
    assert failure["market_session"] == {"status": "open"}


def test_broker_rejection_records_unverified_protection():
    events = []
    runtime = _runtime(
        events,
        amend_position=lambda **_kwargs: SimpleNamespace(
            success=False,
            comment="broker rejected amend",
        ),
    )

    attach_open_trade_protection(_request(), runtime=runtime)

    assert [event[0] for event in events] == ["failure"]
    failure = events[0][1]
    assert failure["status_error"] == "broker rejected amend"
    assert "AMEND FAILED" in failure["failure_log"]


def test_amend_exception_records_failure_without_session_freshness_claim():
    events = []

    def timeout(**_kwargs):
        raise TimeoutError("amend outcome unknown")

    attach_open_trade_protection(
        _request(),
        runtime=_runtime(events, amend_position=timeout),
    )

    assert [event[0] for event in events] == ["failure"]
    failure = events[0][1]
    assert failure["market_session"] is None
    assert failure["status_error"] == (
        "amend_exception:TimeoutError:amend outcome unknown"
    )
    assert failure["ledger_action_reason"] == "amend_exception:TimeoutError"
