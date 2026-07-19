from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd

from backend.services.live_loop_tick_runtime import (
    LiveLoopTickRuntime,
    run_live_loop_tick_body,
)


class _Controller:
    def __init__(self):
        self.health = []
        self.heartbeats = []

    def status(self):
        return {"ready": True}

    def update_runtime_health(self, generation_id, **kwargs):
        self.health.append((generation_id, kwargs))

    def accepting_new_risk(self, _generation_id):
        return True

    def heartbeat(self, generation_id, kind):
        self.heartbeats.append((generation_id, kind))


class _Plane:
    def __init__(self):
        self.marked = []

    def alpha_due(self, *, closed_bar_id):
        return bool(closed_bar_id)

    def mark_alpha_run(self, *, closed_bar_id):
        self.marked.append(closed_bar_id)


def _frame():
    return pd.DataFrame(
        {
            "open": [2_400.0],
            "high": [2_405.0],
            "low": [2_395.0],
            "close": [2_402.0],
        },
        index=pd.DatetimeIndex([pd.Timestamp("2026-01-01T00:00:00Z")]),
    )


def _runtime(
    *,
    order=None,
    safety=None,
    reconcile_account=None,
    process_calls=None,
    state_updates=None,
    persisted=None,
    safety_error=None,
    phase2=True,
):
    order = order if order is not None else []
    process_calls = process_calls if process_calls is not None else []
    state_updates = state_updates if state_updates is not None else []
    persisted = persisted if persisted is not None else []
    safety = safety or {
        "ok": True,
        "accepting_new_risk": True,
        "blockers": [],
        "position_ids": [],
        "unknown_execution_count": 0,
        "reconciliation_state": "fresh",
    }
    bridge = SimpleNamespace(is_connected=True)
    controller = _Controller()
    plane = _Plane()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def run_safety(**_kwargs):
        order.append("safety")
        if safety_error is not None:
            raise safety_error
        return dict(safety)

    runtime = LiveLoopTickRuntime(
        phase2_active=lambda: phase2,
        legacy_tick_body=lambda **_kwargs: {"legacy": True},
        get_ctrader=lambda: (bridge, None, False),
        reconcile_positions=lambda _bridge: order.append("positions")
        or {"positions": []},
        run_safety_cycle=run_safety,
        persist_safety_fail_closed=lambda **kwargs: persisted.append(kwargs)
        or {"persisted": True},
        reconcile_account=lambda _bridge: order.append("account")
        or reconcile_account,
        reconcile_value=lambda payload, key, default=None: (
            payload.get(key, default)
            if isinstance(payload, dict)
            else getattr(payload, key, default)
        ),
        mark_account_reconcile_failed=lambda reason: order.append(
            f"account_failed:{reason}"
        ),
        live_state_update=lambda **kwargs: state_updates.append(kwargs),
        loop_controller=controller,
        set_loop_diagnostic=lambda *_args, **_kwargs: None,
        recover_execution_outcomes=lambda **kwargs: order.append("recovery")
        or (kwargs["safety_result"], True),
        attempt_startup_barrier=lambda **_kwargs: True,
        live_state_get=lambda key, default=None: {
            "trade_date": today,
            "session_state_status": "available",
            "circuit_breaker": False,
        }.get(key, default),
        bootstrap_position_recovery=lambda *_args, **_kwargs: True,
        loop_strategy_name="factor_v4",
        restore_session_state=lambda *_args, **_kwargs: True,
        evaluate_daily_drawdown=lambda: {"tripped": False},
        market_session_snapshot=lambda _bridge: {"status": "open_confirmed"},
        warmup_from_local_db=lambda *_args: _frame(),
        ensure_decision_bars_fresh=lambda **kwargs: kwargs["df_new"],
        get_safety_plane=lambda _generation_id: plane,
        process_tick=lambda *args, **kwargs: process_calls.append(
            (args, kwargs)
        ),
    )
    return runtime, controller, plane


def test_legacy_mode_delegates_without_touching_v2_safety():
    runtime, _controller, _plane = _runtime(phase2=False)

    assert run_live_loop_tick_body(
        broker="ctrader",
        bridge_cfg=SimpleNamespace(),
        timeframe="M5",
        tick=1,
        recovery_bootstrapped=False,
        stop_requested=lambda: False,
        log=lambda _message: None,
        runtime=runtime,
    ) == {"legacy": True}


def test_safety_exception_fails_closed_before_account_or_alpha():
    order = []
    persisted = []
    process_calls = []
    runtime, _controller, _plane = _runtime(
        order=order,
        persisted=persisted,
        process_calls=process_calls,
        safety_error=RuntimeError("protection unavailable"),
    )

    result = run_live_loop_tick_body(
        broker="ctrader",
        bridge_cfg=SimpleNamespace(),
        timeframe="M5",
        tick=2,
        recovery_bootstrapped=False,
        stop_requested=lambda: False,
        log=lambda _message: None,
        runtime=runtime,
    )

    assert order == ["positions", "safety"]
    assert result["wait_seconds"] == 5.0
    assert result["safety"]["accepting_new_risk"] is False
    assert persisted[0]["blockers"] == ("safety_cycle_exception",)
    assert process_calls == []


def test_account_failure_occurs_after_safety_and_blocks_alpha():
    order = []
    process_calls = []
    state_updates = []
    runtime, _controller, _plane = _runtime(
        order=order,
        reconcile_account=None,
        process_calls=process_calls,
        state_updates=state_updates,
    )

    result = run_live_loop_tick_body(
        broker="ctrader",
        bridge_cfg=SimpleNamespace(),
        timeframe="M5",
        tick=3,
        recovery_bootstrapped=False,
        stop_requested=lambda: False,
        log=lambda _message: None,
        runtime=runtime,
    )

    assert order == [
        "positions",
        "safety",
        "account",
        "account_failed:fresh_account_unavailable",
        "recovery",
    ]
    assert result["wait_seconds"] == 5.0
    assert any(item.get("accepting_new_risk") is False for item in state_updates)
    assert process_calls == []


def test_happy_path_runs_alpha_only_after_safety_account_and_recovery():
    order = []
    process_calls = []
    account = {
        "account": {"balance": 10_000.0},
        "observed_at": 100.0,
        "reconcile_id": "account-1",
    }
    runtime, _controller, plane = _runtime(
        order=order,
        reconcile_account=account,
        process_calls=process_calls,
    )

    result = run_live_loop_tick_body(
        broker="ctrader",
        bridge_cfg=SimpleNamespace(),
        timeframe="M5",
        tick=4,
        recovery_bootstrapped=True,
        stop_requested=lambda: False,
        log=lambda _message: None,
        runtime=runtime,
    )

    assert order == ["positions", "safety", "account", "recovery"]
    assert len(process_calls) == 1
    assert process_calls[0][1]["protection_already_run"] is True
    assert plane.marked
    assert result["wait_seconds"] == 10.0
