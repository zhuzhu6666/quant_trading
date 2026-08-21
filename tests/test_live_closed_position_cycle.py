from __future__ import annotations

from backend.services.live_closed_position_cycle import (
    ClosedPositionCycleRuntime,
    handle_closed_positions_after_tick,
)


def _runtime(calls, **overrides):
    values = {
        "authoritative_close_pnl": lambda value: bool(value),
        "defer_close": lambda *args, **kwargs: calls["deferred"].append(
            (args, kwargs)
        ),
        "update_live_state": lambda **kwargs: calls["states"].append(kwargs),
        "collect_attribution": lambda **_kwargs: {
            "total_pnl": 4.5,
            "close_ts": 100.0,
            "close_reason": "supervisor_close",
            "close_source": "supervisor",
            "close_verdict": {},
            "attribution_integrity": "full",
            "factor_contributions": {"trend": 0.4},
            "close_price": 2399.5,
        },
        "lookup_context_integrity": lambda _pid, default: default,
        "log_closed_position_ledger": lambda **_kwargs: ("exit-1", "full"),
        "run_closed_position_learning": lambda **_kwargs: None,
        "cleanup_closed_position": lambda **_kwargs: True,
        "record_aux_failure": lambda *args, **kwargs: calls["aux"].append(
            (args, kwargs)
        ),
        "mark_recovery_closed": lambda *args, **kwargs: calls["marked"].append(
            (args, kwargs)
        ),
        "reconcile_account": lambda _bridge: {
            "account": {"balance": 1000.0},
            "observed_at": 101.0,
            "reconcile_id": "acct-1",
        },
        "reconcile_value": lambda result, field, default: (
            result.get(field, default) if isinstance(result, dict) else default
        ),
        "restore_session_state": lambda *_args, **_kwargs: True,
        "release_close_latch": lambda *args: calls["released"].append(args),
        "trade_date": lambda: "2026-07-19",
        "now": lambda: 102.0,
        "full_context": "full",
    }
    values.update(overrides)
    return ClosedPositionCycleRuntime(**values)


def _calls():
    return {
        "deferred": [],
        "states": [],
        "aux": [],
        "marked": [],
        "released": [],
    }


def test_close_without_authoritative_deal_defers_every_consumer():
    calls = _calls()
    account_calls = []
    runtime = _runtime(
        calls,
        reconcile_account=lambda _bridge: account_calls.append(True),
    )

    handle_closed_positions_after_tick(
        closed_pids={7},
        real_pnls={},
        attr_engine=object(),
        bar={},
        cfg=object(),
        account={},
        broker="ctrader",
        tick=5,
        log=lambda _message: None,
        runtime=runtime,
        broker_open_position_ids=set(),
        bridge=object(),
        close_deal_cursors={7: {"from_ts": 90}},
    )

    assert account_calls == []
    assert calls["released"] == []
    assert calls["deferred"][0][0] == (7,)
    assert calls["deferred"][0][1]["recovery_evidence"] == {
        "pending_kind": "final_close",
        "from_ts": 90,
    }


def test_final_close_defer_strips_baseline_fields_from_cursor():
    """回归:final_close defer 不再把"库里已有 close deal"当作 baseline 写进 latch。

    生产死锁(2026-08-05):observed_close_cursor_out 的 baseline_deal_ids
    来自当前库里的 close deal 本身,若原样传入 recovery_evidence,
    _pending_close_cursor_overrides 读到被污染的 baseline 后
    observed_ids - baseline_ids 恒为空集,平仓确认永远无法完成。
    """
    calls = _calls()
    runtime = _runtime(
        calls,
        reconcile_account=lambda _bridge: None,
    )

    handle_closed_positions_after_tick(
        closed_pids={7},
        real_pnls={},
        attr_engine=object(),
        bar={},
        cfg=object(),
        account={},
        broker="ctrader",
        tick=5,
        log=lambda _message: None,
        runtime=runtime,
        broker_open_position_ids=set(),
        bridge=object(),
        close_deal_cursors={
            7: {
                "from_ts": 90,
                "baseline_cursor_available": True,
                "baseline_deal_ids": [327972131],
                "baseline_closed_volume": 100.0,
            }
        },
    )

    assert calls["deferred"][0][1]["recovery_evidence"] == {
        "pending_kind": "final_close",
        "from_ts": 90,
    }
    assert "baseline_deal_ids" not in calls["deferred"][0][1]["recovery_evidence"]
    assert "baseline_closed_volume" not in calls["deferred"][0][1]["recovery_evidence"]


def test_confirmed_close_rebuilds_session_before_releasing_cursor():
    calls = _calls()
    real_pnl = {"net": 4.5, "deal_ids": [88], "exec_timestamp": 100.0}

    handle_closed_positions_after_tick(
        closed_pids={8},
        real_pnls={8: real_pnl},
        attr_engine=object(),
        bar={},
        cfg=object(),
        account={},
        broker="ctrader",
        tick=6,
        log=lambda _message: None,
        runtime=_runtime(calls),
        broker_open_position_ids=set(),
        bridge=object(),
    )

    assert calls["states"][0]["session_state_status"] == "unavailable"
    assert calls["states"][0]["accepting_new_risk"] is False
    assert calls["states"][1]["account_reconcile_id"] == "acct-1"
    assert calls["released"] == [(8, real_pnl)]
    assert calls["deferred"] == []


def test_uncommitted_recovery_projection_stays_deferred_after_session_restore():
    calls = _calls()
    real_pnl = {"net": -2.0, "deal_ids": [89], "exec_timestamp": 100.0}

    handle_closed_positions_after_tick(
        closed_pids={9},
        real_pnls={9: real_pnl},
        attr_engine=object(),
        bar={},
        cfg=object(),
        account={},
        broker="ctrader",
        tick=7,
        log=lambda _message: None,
        runtime=_runtime(
            calls,
            cleanup_closed_position=lambda **_kwargs: False,
        ),
        broker_open_position_ids=set(),
        bridge=object(),
        close_deal_cursors={9: {"from_ts": 91}},
    )

    assert calls["released"] == []
    assert calls["deferred"][-1][1]["reason"] == (
        "post_close_session_projection_unavailable"
    )
    assert calls["deferred"][-1][1]["recovery_evidence"] == {
        "pending_kind": "final_close",
        "from_ts": 91,
        "confirmed_deal_ids": [89],
    }
    assert calls["states"][-1]["session_state_status"] == "unavailable"
    assert calls["aux"][-1][0] == (
        "post_close_session_projection_unavailable",
    )


def test_unknown_price_skips_price_audit_and_learning_but_keeps_recovery():
    calls = _calls()
    downstream = []
    runtime = _runtime(
        calls,
        collect_attribution=lambda **_kwargs: {
            "total_pnl": -2.5,
            "close_ts": 100.0,
            "close_reason": "broker_close",
            "close_source": "broker",
            "close_verdict": {},
            "attribution_integrity": "missing",
            "factor_contributions": {},
            "close_price": None,
        },
        log_closed_position_ledger=lambda **_kwargs: downstream.append("ledger"),
        run_closed_position_learning=lambda **_kwargs: downstream.append("learning"),
        cleanup_closed_position=lambda **_kwargs: downstream.append("cleanup") or True,
    )

    handle_closed_positions_after_tick(
        closed_pids={10},
        real_pnls={
            10: {
                "net": -2.5,
                "deal_ids": [90],
                "exec_timestamp": 100.0,
                "price_quality": "unknown",
            }
        },
        attr_engine=object(),
        bar={},
        cfg=object(),
        account={},
        broker="ctrader",
        tick=8,
        log=lambda _message: None,
        runtime=runtime,
        broker_open_position_ids=set(),
        bridge=object(),
    )

    assert downstream == ["cleanup"]
    assert calls["released"]
