from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.services.live_recovery_close import (
    MissingPositionRetirementRuntime,
    RecoveredCloseReplayRuntime,
    replay_recovered_close,
    retire_broker_missing_position,
)


def _replay_runtime(**overrides):
    values = {
        "authoritative_close_pnl": lambda value: bool(value),
        "defer_close": lambda *_args, **_kwargs: None,
        "build_payloads": lambda **_kwargs: {
            "total_pnl": -4.0,
            "close_ts": 100.0,
            "recovery_meta": {"deal_id": 9},
        },
        "mark_recovery_closed": lambda *_args, **_kwargs: None,
        "release_close_latch": lambda *_args, **_kwargs: None,
        "get_risk_state": lambda: {},
        "now": lambda: 101.0,
        "partial_context": "partial",
    }
    values.update(overrides)
    return RecoveredCloseReplayRuntime(**values)


def test_replay_defers_when_close_deal_is_not_authoritative():
    deferred = []
    result = replay_recovered_close(
        broker="ctrader",
        position_id=7,
        position_state={"position_id": 7},
        real_pnl=None,
        strategy_name="factor_v4",
        runtime=_replay_runtime(
            authoritative_close_pnl=lambda _value: False,
            defer_close=lambda *args, **kwargs: deferred.append((args, kwargs)),
        ),
    )

    assert result is False
    assert deferred == [
        (
            (7,),
            {
                "broker": "ctrader",
                "tick": 0,
                "reason": "restart_replay_close_deal_unavailable",
            },
        )
    ]


def test_replay_never_releases_cursor_before_projection_commit():
    order = []

    def fail_projection(*_args, **_kwargs):
        order.append("projection")
        raise RuntimeError("postgres unavailable")

    with pytest.raises(RuntimeError, match="postgres unavailable"):
        replay_recovered_close(
            broker="ctrader",
            position_id=8,
            position_state={"position_id": 8},
            real_pnl={"net": -4.0},
            strategy_name="factor_v4",
            runtime=_replay_runtime(
                mark_recovery_closed=fail_projection,
                release_close_latch=lambda *_args: order.append("release"),
            ),
        )

    assert order == ["projection"]


def test_replay_unknown_price_skips_audit_and_learning_but_commits_recovery():
    order = []

    class _Ledger:
        def log_decision(self, **_kwargs):
            order.append("ledger")

    class _Reviewer:
        def review_closed_trade(self, **_kwargs):
            order.append("review")

    result = replay_recovered_close(
        broker="ctrader",
        position_id=9,
        position_state={"position_id": 9},
        real_pnl={
            "net": -4.0,
            "exec_price": 4125.0,
            "price_quality": "unknown",
        },
        strategy_name="factor_v4",
        runtime=_replay_runtime(
            mark_recovery_closed=lambda *_args, **_kwargs: order.append(
                "recovery"
            ),
            release_close_latch=lambda *_args: order.append("release"),
            ledger=_Ledger(),
            trade_reviewer=_Reviewer(),
            experience_builder=object(),
            policy_suggester=object(),
        ),
    )

    assert result is True
    assert order == ["recovery", "release"]


def test_replay_preserves_durable_supervisor_close_reason():
    marks = []

    replay_recovered_close(
        broker="ctrader",
        position_id=10,
        position_state={
            "position_id": 10,
            "recovery_meta": {"pending_close_reason": "thesis_broken"},
        },
        real_pnl={
            "net": -4.0,
            "exec_price": 4125.0,
            "price_quality": "broker_reported",
        },
        strategy_name="factor_v4",
        runtime=_replay_runtime(
            build_payloads=lambda **kwargs: {
                "total_pnl": -4.0,
                "close_ts": 100.0,
                "recovery_meta": {
                    "close_reason": kwargs["resolved_close_reason"],
                    "close_reason_source": kwargs["close_reason_source"],
                },
                "decision": {
                    "event_type": "close",
                    "symbol": "XAUUSD+",
                    "timeframe": "",
                    "trade_id": "10",
                    "position_id": "10",
                    "decision_ts": 100.0,
                    "portfolio_state": {},
                    "action_score": -4.0,
                    "action_reason": "restart_replay_close",
                    "action_json": {},
                },
                "position_event": {
                    "position_id": "10",
                    "trade_id": "10",
                    "symbol": "XAUUSD+",
                    "event_type": "closed",
                    "avg_price": 4125.0,
                    "realized_pnl": -4.0,
                    "details": {},
                    "event_ts": 100.0,
                },
                "review": {
                    "position_id": "10",
                    "pnl": -4.0,
                    "close_price": 4125.0,
                    "close_ts": 100.0,
                    "contributions": {},
                    "attribution_integrity": "missing",
                    "real_pnl": {"net": -4.0},
                    "close_reason": "thesis_broken",
                    "close_reason_source": "supervisor_direct_close",
                    "context_integrity": "partial",
                },
            },
            mark_recovery_closed=lambda *_args, **kwargs: marks.append(kwargs),
        ),
    )

    assert marks[0]["close_reason"] == "thesis_broken"


class _Connection:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def _retirement_runtime(**overrides):
    connection = _Connection()
    values = {
        "read_positions": lambda _bridge: [],
        "normalize_position": lambda value: dict(value),
        "load_recovery_position": lambda position_id: {
            "position_id": position_id,
            "last_seen_at": 100.0,
            "volume": 75.0,
            "close_pnl": 0.0,
        },
        "open_prices": {},
        "get_state_connection": lambda: connection,
        "sync_close_deals_batch": lambda *_args, **_kwargs: {},
        "authoritative_close_pnl": lambda value: bool(value),
        "defer_close": lambda *_args, **_kwargs: None,
        "replay_close": lambda **_kwargs: True,
        "mark_recovery_closed": lambda *_args, **_kwargs: None,
        "remove_live_position_state": lambda _position_id: None,
        "now": lambda: 200.0,
        "replay_lookback_seconds": 60,
        "partial_context": "partial",
    }
    values.update(overrides)
    return MissingPositionRetirementRuntime(**values), connection


def test_retirement_rejects_position_still_present_at_broker():
    touched = []
    runtime, _connection = _retirement_runtime(
        read_positions=lambda _bridge: [{"position_id": 12}],
        load_recovery_position=lambda _position_id: touched.append("load"),
        sync_close_deals_batch=lambda *_args, **_kwargs: touched.append("sync"),
    )

    result = retire_broker_missing_position(
        object(),
        12,
        broker="ctrader",
        strategy_name="factor_v4",
        reason="POSITION_NOT_FOUND",
        runtime=runtime,
    )

    assert result is False
    assert touched == []


def test_retirement_defers_without_complete_authoritative_close_deal():
    deferred = []
    runtime, connection = _retirement_runtime(
        defer_close=lambda *args, **kwargs: deferred.append((args, kwargs)),
    )

    result = retire_broker_missing_position(
        object(),
        13,
        broker="ctrader",
        strategy_name="factor_v4",
        reason="POSITION_NOT_FOUND",
        runtime=runtime,
    )

    assert result is False
    assert connection.closed is True
    assert deferred[0][0] == (13,)
    assert deferred[0][1]["reason"] == (
        "broker_position_missing_close_deal_unavailable"
    )


def test_retirement_replays_then_marks_and_removes_missing_position():
    order = []
    sync_calls = []
    real_pnl = {
        "net": 6.5,
        "exec_timestamp": 150.0,
        "deal_id": 44,
        "source": "ctrader_deals",
    }
    runtime, connection = _retirement_runtime(
        sync_close_deals_batch=lambda *_args, **kwargs: (
            sync_calls.append(kwargs) or {14: real_pnl}
        ),
        replay_close=lambda **_kwargs: order.append("replay") or True,
        mark_recovery_closed=lambda *_args, **_kwargs: order.append(
            ("mark", _kwargs)
        ),
        remove_live_position_state=lambda position_id: order.append(
            ("remove", position_id)
        ),
    )
    messages = []

    result = retire_broker_missing_position(
        SimpleNamespace(),
        14,
        broker="ctrader",
        strategy_name="factor_v4",
        reason="POSITION_NOT_FOUND",
        runtime=runtime,
        log=messages.append,
    )

    assert result is True
    assert connection.closed is True
    assert sync_calls == [
        {
            "from_ts": 40,
            "max_rows": 200,
            "min_exec_timestamp_by_position": {14: 95.0},
            "required_closed_volume_delta_by_position": {14: 75.0},
            "baseline_close_cursor_by_position": {
                14: {
                    "baseline_cursor_available": True,
                    "baseline_deal_ids": [],
                    "baseline_closed_volume": 0.0,
                }
            },
        }
    ]
    assert order[0] == "replay"
    assert order[1][0] == "mark"
    assert order[1][1]["close_reason"] == "restart_replay"
    assert order[1][1]["meta"]["recovery_observation_reason"] == (
        "broker_position_not_found"
    )
    assert order[1][1]["close_pnl"] == pytest.approx(6.5)
    assert order[2] == ("remove", 14)
    assert messages == [
        "broker missing position retired pos=14: POSITION_NOT_FOUND"
    ]
