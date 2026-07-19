from __future__ import annotations

from types import SimpleNamespace
import threading

from backend.services.live_open_submission import (
    OpenSubmissionRuntime,
    submit_open_trade_candidate,
)


def _candidate(**overrides):
    values = {
        "volume": 100.0,
        "direction_name": "BUY",
        "nursery_reservation_id": "reservation-1",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _runtime(calls, **overrides):
    values = {
        "probe_final_admission": lambda **_kwargs: {"ok": True},
        "admission_lock": threading.Lock(),
        "open_trade_draining": lambda _stop: False,
        "persist_safety_fail_closed": lambda **kwargs: calls["latched"].append(
            kwargs
        ),
        "submit_order": lambda *_args: SimpleNamespace(
            success=True,
            position_id=7,
            intent_id="intent-7",
        ),
        "handle_order_success": lambda **kwargs: calls["success"].append(kwargs),
        "record_order_failure": lambda **kwargs: calls["failure"].append(kwargs),
        "reconcile_positions": lambda _bridge: {
            "success": True,
            "reconcile_id": "positions-1",
        },
        "publish_positions": lambda *args, **kwargs: calls["published"].append(
            (args, kwargs)
        ),
        "append_safety_outbox": lambda **kwargs: calls["outbox"].append(kwargs),
        "finalize_nursery_reservation": (
            lambda reservation_id, consumed: calls["nursery"].append(
                (reservation_id, consumed)
            )
        ),
        "now": iter((10.0, 11.0)).__next__,
    }
    values.update(overrides)
    return OpenSubmissionRuntime(**values)


def _calls():
    return {
        "latched": [],
        "success": [],
        "failure": [],
        "published": [],
        "outbox": [],
        "nursery": [],
    }


def _submit(runtime, logs, *, candidate=None):
    return submit_open_trade_candidate(
        bridge=object(),
        attr_engine=object(),
        broker="ctrader",
        cfg=object(),
        bar={},
        tick=3,
        account={},
        positions=[],
        composite=object(),
        gate_result=object(),
        candidate=candidate or _candidate(),
        current_price=2400.0,
        log=logs.append,
        runtime=runtime,
    )


def test_final_admission_failure_latches_and_never_calls_broker():
    calls = _calls()
    submits = []
    runtime = _runtime(
        calls,
        probe_final_admission=lambda **_kwargs: {
            "ok": False,
            "blockers": ["postgres_unavailable"],
            "postgres": {"error": "connection refused"},
        },
        submit_order=lambda *_args: submits.append(True),
    )

    result = _submit(runtime, [])

    assert result is False
    assert submits == []
    assert calls["latched"] == [
        {
            "blockers": ("postgres_unavailable",),
            "source": "final_open_admission",
            "error": "connection refused",
        }
    ]
    assert calls["nursery"] == [("reservation-1", False)]


def test_draining_rejects_before_broker_rpc():
    calls = _calls()
    submits = []
    runtime = _runtime(
        calls,
        open_trade_draining=lambda _stop: True,
        submit_order=lambda *_args: submits.append(True),
    )

    result = _submit(runtime, [])

    assert result is False
    assert submits == []
    assert calls["nursery"] == [("reservation-1", False)]


def test_broker_rpc_exception_is_a_consumed_attempt_not_a_resend_signal():
    calls = _calls()
    logs = []

    def timeout(*_args):
        raise TimeoutError("broker outcome unknown")

    result = _submit(_runtime(calls, submit_order=timeout), logs)

    assert result is True
    assert calls["nursery"] == [("reservation-1", False)]
    assert "broker outcome unknown" in logs[-1]


def test_confirmed_open_post_fill_and_reconcile_failure_stays_fail_closed():
    calls = _calls()
    logs = []

    def fail_post_fill(**_kwargs):
        raise RuntimeError("protection init failed")

    def fail_reconcile(_bridge):
        raise OSError("broker snapshot unavailable")

    result = _submit(
        _runtime(
            calls,
            handle_order_success=fail_post_fill,
            reconcile_positions=fail_reconcile,
        ),
        logs,
    )

    assert result is True
    assert calls["nursery"] == [("reservation-1", True)]
    assert calls["latched"][0]["blockers"] == (
        "confirmed_open_post_fill_processing_failed",
    )
    assert calls["published"] == []
    assert calls["outbox"][0]["payload"] == {
        "broker": "ctrader",
        "tick": 3,
        "position_id": 7,
        "intent_id": "intent-7",
        "reconcile_id": "",
        "reconcile_success": False,
    }
    assert "failed closed" in logs[-1]
