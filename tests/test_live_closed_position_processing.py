from __future__ import annotations

from types import SimpleNamespace

from backend.services.live_closed_position_processing import (
    ClosedPositionProcessingRuntime,
    cleanup_closed_position,
    collect_closed_position_attribution,
    log_closed_position_ledger,
    run_closed_position_learning,
)


def _runtime(**overrides):
    values = {
        "consume_close_reason": lambda _pid, default: default,
        "consume_close_verdict": lambda _pid, reason: {"reason": reason},
        "classify_close_source": lambda *_args: "broker",
        "select_close_total_pnl": lambda **kwargs: float(
            (kwargs.get("real_pnl") or {}).get("net") or kwargs["fallback_pnl"]
        ),
        "open_api_volumes": {},
        "decision_log": None,
        "decision_log_run_id": "run-1",
        "safe_decision_log": lambda *_args, **_kwargs: None,
        "build_close_decision_audit_meta": lambda **kwargs: kwargs,
        "json_dumps": lambda value, **_kwargs: str(value),
        "ledger": None,
        "ensure_open_ledger": lambda *_args, **_kwargs: "",
        "lookup_context_integrity": lambda _pid, value: value,
        "build_close_ledger_payloads": lambda **_kwargs: {
            "decision": {},
            "position_event": {},
        },
        "get_session_pnl": lambda: 0.0,
        "risk_state_with_verdict": lambda value: value,
        "trade_reviewer": None,
        "experience_builder": None,
        "policy_suggester": None,
        "build_trade_review_payload": lambda **kwargs: kwargs,
        "mark_recovery_closed": lambda *_args, **_kwargs: None,
        "trailing_state": {},
        "entry_scores": {},
        "entry_decisions": {},
        "pending_open_attach_until": {},
        "now": lambda: 100.0,
        "debug": lambda *_args, **_kwargs: None,
        "info": lambda *_args, **_kwargs: None,
        "exception": lambda *_args, **_kwargs: None,
    }
    values.update(overrides)
    return ClosedPositionProcessingRuntime(**values)


def test_collect_attribution_consumes_close_context_and_original_volume():
    volumes = {7: 75.0}
    calls = []
    engine = SimpleNamespace(
        open_integrity=lambda _pid: "full",
        record_close=lambda *args, **kwargs: calls.append((args, kwargs))
        or {"trend": 0.5},
    )
    logs = []

    result = collect_closed_position_attribution(
        position_id=7,
        real_pnl={
            "net": 4.25,
            "exec_price": 2399.5,
            "exec_timestamp": 90.0,
            "price_quality": "broker_reported",
        },
        attr_engine=engine,
        tick=3,
        log=logs.append,
        runtime=_runtime(
            consume_close_reason=lambda _pid, _default: "supervisor_close",
            classify_close_source=lambda *_args: {"source": "supervisor"},
            open_api_volumes=volumes,
        ),
    )

    assert result["total_pnl"] == 4.25
    assert result["close_reason"] == "supervisor_close"
    assert result["attribution_integrity"] == "full"
    assert result["factor_contributions"] == {"trend": 0.5}
    assert result["close_source"] == {"source": "supervisor"}
    assert result["close_price"] == 2399.5
    assert calls[0][0] == (7,)
    assert calls[0][1]["close_price"] == 2399.5
    assert 7 not in volumes
    assert "factors=1" in logs[-1]


def test_unknown_close_price_keeps_money_pnl_but_discards_factor_attribution():
    calls = []
    engine = SimpleNamespace(
        open_integrity=lambda _pid: "full",
        record_close=lambda *_args, **_kwargs: calls.append("record"),
        discard_open=lambda pid: calls.append(("discard", pid)),
    )

    result = collect_closed_position_attribution(
        position_id=8,
        real_pnl={
            "net": -2.5,
            "exec_timestamp": 90.0,
            "price_quality": "unknown",
        },
        attr_engine=engine,
        tick=3,
        log=lambda _message: None,
        runtime=_runtime(),
    )

    assert result["total_pnl"] == -2.5
    assert result["factor_contributions"] == {}
    assert result["attribution_integrity"] == "missing"
    assert result["close_price"] is None
    assert calls == [("discard", 8)]


class _Ledger:
    def __init__(self):
        self.decisions = []
        self.events = []

    def log_decision(self, **kwargs):
        self.decisions.append(kwargs)
        return "exit-7"

    def log_position_event(self, **kwargs):
        self.events.append(kwargs)


def test_ledger_repair_refreshes_context_and_persists_both_records():
    ledger = _Ledger()
    runtime = _runtime(
        ledger=ledger,
        ensure_open_ledger=lambda *_args, **_kwargs: "entry-7",
        lookup_context_integrity=lambda _pid, _value: "full",
        build_close_ledger_payloads=lambda **_kwargs: {
            "decision": {"event_type": "close"},
            "position_event": {"event_type": "closed"},
        },
    )

    exit_id, integrity = log_closed_position_ledger(
        position_id=7,
        broker="ctrader",
        close_ts=90.0,
        current_price=2400.0,
        real_pnl={"net": 4.25},
        close_reason="supervisor_close",
        context_integrity="partial",
        cfg=SimpleNamespace(timeframe="M1"),
        bar={"time": 89.0},
        account={"balance": 1000.0},
        total_pnl=4.25,
        tick=3,
        close_source="supervisor",
        attribution_integrity="full",
        close_verdict={},
        factor_contributions={"trend": 0.5},
        runtime=runtime,
    )

    assert exit_id == "exit-7"
    assert integrity == "full"
    assert ledger.decisions == [{"event_type": "close"}]
    assert ledger.events == [{"event_type": "closed"}]


def test_rejected_learning_review_never_builds_or_suggests_experience():
    downstream = []
    runtime = _runtime(
        trade_reviewer=SimpleNamespace(
            review_closed_trade=lambda **_kwargs: {
                "accepted": False,
                "skip_reason": "unverified",
            }
        ),
        experience_builder=SimpleNamespace(
            build_from_review=lambda review: downstream.append(("build", review))
        ),
        policy_suggester=SimpleNamespace(
            suggest_from_experience=lambda value: downstream.append(
                ("suggest", value)
            )
        ),
    )

    run_closed_position_learning(
        position_id=7,
        total_pnl=4.25,
        current_price=2400.0,
        close_ts=90.0,
        factor_contributions={},
        exit_decision_id="exit-7",
        real_pnl={"net": 4.25},
        close_reason="broker_close",
        context_integrity="partial",
        attribution_integrity="missing",
        close_source="broker",
        runtime=runtime,
    )

    assert downstream == []


def test_cleanup_returns_projection_failure_but_always_clears_local_state():
    trailing = {7: {"sl": 2390.0}}
    scores = {7: 0.5}
    decisions = {7: "entry-7"}
    pending = {7: 120.0}

    def fail_projection(*_args, **_kwargs):
        raise RuntimeError("postgres unavailable")

    result = cleanup_closed_position(
        position_id=7,
        close_reason="broker_close",
        total_pnl=4.25,
        close_ts=90.0,
        real_pnl={"net": 4.25},
        factor_contributions={},
        runtime=_runtime(
            mark_recovery_closed=fail_projection,
            trailing_state=trailing,
            entry_scores=scores,
            entry_decisions=decisions,
            pending_open_attach_until=pending,
        ),
    )

    assert result is False
    assert trailing == {}
    assert scores == {}
    assert decisions == {}
    assert pending == {}
