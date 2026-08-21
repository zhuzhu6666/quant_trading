import time
from types import SimpleNamespace

from backend.services.live_risk_sizing import (
    floor_api_volume_to_step,
    should_full_close_untradeable_reduce,
)
from backend.services.live_position_lifecycle import (
    build_supervisor_tighten_result_payloads,
)
from backend.services.live_supervision_actions import (
    execute_supervisor_close_action,
    execute_supervisor_reduce_action,
    execute_supervisor_tighten_action,
    normalize_supervisor_reduce_verdict,
    plan_supervisor_reduce_action,
)


def _deps(calls):
    def _floor_api_volume_to_step(value, meta):
        min_volume = float((meta or {}).get("api_min_volume") or 1.0)
        floored = float(int(value or 0.0))
        return floored if floored >= min_volume else 0.0

    return {
        "floor_api_volume_to_step": _floor_api_volume_to_step,
        "should_full_close_untradeable_reduce": lambda **_kwargs: (False, "not_upgradeable"),
        "build_close_position_risk_context": lambda **kwargs: dict(kwargs),
        "risk_policy_evaluate": lambda _action, _context: SimpleNamespace(
            to_dict=lambda: {"allowed": True, "reason": "ok"}
        ),
        "log_supervisor_trace": lambda **kwargs: calls["traces"].append(kwargs),
        "remember_supervisor_state": lambda *args, **kwargs: calls["supervisor_state"].append((args, kwargs)),
        "remember_supervisor_reentry_block": lambda **kwargs: calls["reentry"].append(kwargs),
        "remember_close_reason": lambda *args: calls["close_reason"].append(args),
        "remember_close_verdict": lambda *args: calls["close_verdict"].append(args),
        "result_is_position_not_found": lambda _result: False,
        "retire_broker_missing_position": lambda *args, **kwargs: calls["retired"].append((args, kwargs)),
    }


def _execute_reduce(**kwargs):
    floor = kwargs.pop("floor_api_volume_to_step")
    should_close = kwargs.pop("should_full_close_untradeable_reduce")
    kwargs.pop("build_close_position_risk_context")
    kwargs.pop("risk_policy_evaluate")
    execution_plan = plan_supervisor_reduce_action(
        bridge=kwargs["bridge"],
        position=kwargs["position"],
        verdict=kwargs["verdict"],
        controls=kwargs["controls"],
        floor_api_volume_to_step=floor,
        should_full_close_untradeable_reduce=should_close,
    )
    execute_supervisor_reduce_action(
        **kwargs,
        execution_plan=execution_plan,
    )


def test_execute_supervisor_reduce_action_partial_success_logs_reduced_event():
    calls = {
        "traces": [],
        "supervisor_state": [],
        "reentry": [],
        "close_reason": [],
        "close_verdict": [],
        "retired": [],
        "logs": [],
        "ledger_events": [],
        "partial_accounting": [],
    }

    class _Bridge:
        _symbol_meta = {"api_min_volume": 1.0, "api_step_volume": 1.0}

        def close_position(self, pid, volume=None):
            calls["close_call"] = (pid, volume)
            return SimpleNamespace(success=True)

    class _Ledger:
        def log_position_event(self, **kwargs):
            calls["ledger_events"].append(kwargs)

    _execute_reduce(
        bridge=_Bridge(),
        position={"position_id": 7, "symbol": "XAUUSD+", "volume": 100.0, "current_price": 4010.0},
        verdict={"summary_reason": "trim_risk"},
        risk_action="reduce_position",
        risk_verdict={"allowed": True, "reason": "ok"},
        decision_id="decision-1",
        cfg=SimpleNamespace(),
        tick=9,
        acct={"equity": 10000.0},
        controls={"reduce_fraction": 0.25},
        log=lambda msg: calls["logs"].append(msg),
        ledger=_Ledger(),
        record_partial_close_execution=lambda **kwargs: calls["partial_accounting"].append(kwargs) or True,
        **_deps(calls),
    )

    assert calls["close_call"] == (7, 25.0)
    assert calls["ledger_events"][0]["event_type"] == "reduced"
    assert calls["ledger_events"][0]["net_volume"] == 75.0
    assert calls["ledger_events"][0]["realized_pnl"] == 0.0
    assert calls["ledger_events"][0]["details"]["realized_pnl_scope"] == "execution_detail_only"
    assert calls["partial_accounting"][0]["position_id"] == 7
    assert calls["partial_accounting"][0]["volume"] == 25.0
    assert calls["traces"][0]["execution"]["accounting_recorded"] is True
    assert calls["supervisor_state"][0][1]["action_applied"] == "reduce"
    assert calls["traces"][0]["execution_reason"] == "partial_close_success"
    assert calls["logs"] == ["tick 9: supervisor reduce pos=7 vol=25"]


def test_reduce_trace_is_real_only_after_fresh_volume_decrease_reconcile():
    calls = {
        "traces": [],
        "supervisor_state": [],
        "reentry": [],
        "close_reason": [],
        "close_verdict": [],
        "retired": [],
        "logs": [],
    }

    class _Bridge:
        _symbol_meta = {"api_min_volume": 1.0, "api_step_volume": 1.0}

        def close_position(self, pid, volume=None):
            calls["close_call"] = (pid, volume)
            return SimpleNamespace(success=True)

    _execute_reduce(
        bridge=_Bridge(),
        position={"position_id": 17, "symbol": "XAUUSD+", "volume": 100.0},
        verdict={"summary_reason": "trim_risk"},
        risk_action="reduce_position",
        risk_verdict={"allowed": True, "reason": "ok"},
        decision_id="decision-reduce-real",
        cfg=SimpleNamespace(),
        tick=9,
        acct=None,
        controls={"reduce_fraction": 0.25},
        log=calls["logs"].append,
        ledger=None,
        reconcile_positions=lambda _bridge: SimpleNamespace(
            status="fresh",
            observed_at=time.time(),
            reconcile_id="reconcile-reduce-real",
            positions=({"position_id": 17, "volume": 75.0},),
        ),
        **_deps(calls),
    )

    assert calls["close_call"] == (17, 25.0)
    assert calls["traces"][0]["stage"] == "executed"
    assert calls["traces"][0]["outcome"] == "applied"
    assert calls["traces"][0]["execution"]["is_real_execution"] is True
    assert calls["traces"][0]["execution"]["reduction_confirmed"] is True
    assert calls["traces"][0]["execution"]["position_volume_after"] == 75.0


def test_reduce_same_volume_reconcile_is_failed_not_applied():
    calls = {
        "traces": [],
        "supervisor_state": [],
        "reentry": [],
        "close_reason": [],
        "close_verdict": [],
        "retired": [],
        "logs": [],
    }

    class _Bridge:
        _symbol_meta = {"api_min_volume": 1.0, "api_step_volume": 1.0}

        def close_position(self, pid, volume=None):
            return SimpleNamespace(success=True)

    _execute_reduce(
        bridge=_Bridge(),
        position={"position_id": 18, "symbol": "XAUUSD+", "volume": 100.0},
        verdict={"summary_reason": "trim_risk"},
        risk_action="reduce_position",
        risk_verdict={"allowed": True, "reason": "ok"},
        decision_id="decision-reduce-unverified",
        cfg=SimpleNamespace(),
        tick=9,
        acct=None,
        controls={"reduce_fraction": 0.25},
        log=calls["logs"].append,
        ledger=None,
        reconcile_positions=lambda _bridge: SimpleNamespace(
            status="fresh",
            observed_at=time.time(),
            reconcile_id="reconcile-reduce-unverified",
            positions=({"position_id": 18, "volume": 100.0},),
        ),
        **_deps(calls),
    )

    assert calls["traces"][0]["stage"] == "execution_failed"
    assert calls["traces"][0]["outcome"] == "failed"
    assert calls["traces"][0]["execution_status"] == "reconcile_unverified"
    assert calls["traces"][0]["execution"].get("is_real_execution") is not True
    assert calls["supervisor_state"] == []
    assert calls["logs"] == []


def test_reduce_captures_deal_cursor_before_broker_rpc_and_passes_it_to_sync():
    calls = {
        "traces": [],
        "supervisor_state": [],
        "reentry": [],
        "close_reason": [],
        "close_verdict": [],
        "retired": [],
        "logs": [],
    }
    order: list[str] = []
    synced: list[dict] = []

    class _Bridge:
        _symbol_meta = {"api_min_volume": 1.0, "api_step_volume": 1.0}

        def close_position(self, pid, volume=None):
            order.append("broker_rpc")
            return SimpleNamespace(success=True, outcome="confirmed")

    cursor = {
        "status": "captured",
        "baseline_cursor_available": True,
        "baseline_deal_ids": [91],
        "baseline_closed_volume": 25.0,
    }

    _execute_reduce(
        bridge=_Bridge(),
        position={"position_id": 7, "symbol": "XAUUSD+", "volume": 100.0},
        verdict={"summary_reason": "trim_risk"},
        risk_action="reduce_position",
        risk_verdict={"allowed": True, "reason": "ok"},
        decision_id="decision-cursor",
        cfg=SimpleNamespace(),
        tick=9,
        acct=None,
        controls={"reduce_fraction": 0.25},
        log=calls["logs"].append,
        capture_partial_close_session_cursor=lambda **_kwargs: (
            order.append("capture_cursor") or cursor
        ),
        sync_partial_close_session_fact=lambda **kwargs: (
            order.append("sync_fact") or synced.append(kwargs) or True
        ),
        **_deps(calls),
    )

    assert order == ["capture_cursor", "broker_rpc", "sync_fact"]
    assert synced[0]["deal_cursor"] == cursor
    assert synced[0]["volume"] == 25.0
    assert calls["traces"][0]["execution"]["session_fact_recorded"] is True


def test_plan_supervisor_reduce_action_suppresses_untradeable_volume():
    calls = {
        "traces": [],
        "supervisor_state": [],
        "reentry": [],
        "close_reason": [],
        "close_verdict": [],
        "retired": [],
        "logs": [],
    }

    class _Bridge:
        _symbol_meta = {"api_min_volume": 50.0, "api_step_volume": 1.0}

        def close_position(self, *_args, **_kwargs):
            raise AssertionError("close should not be called")

    plan = plan_supervisor_reduce_action(
        bridge=_Bridge(),
        position={"position_id": 7, "volume": 100.0},
        verdict={"summary_reason": "trim_risk"},
        controls={"reduce_fraction": 0.1},
        floor_api_volume_to_step=_deps(calls)["floor_api_volume_to_step"],
        should_full_close_untradeable_reduce=(
            _deps(calls)["should_full_close_untradeable_reduce"]
        ),
    )

    assert plan["effective_action"] == "hold"
    assert plan["reason"] == "not_upgradeable"
    assert plan["reduce_volume"] == 0.0
    assert calls["logs"] == []


def test_minimum_reduce_upgrades_only_at_supervisor_near_stop_threshold():
    class _Bridge:
        _symbol_meta = {"api_min_volume": 100.0, "api_step_volume": 100.0}

    verdict = {
        "action": "reduce",
        "summary_reason": "profit_giveback_after_mfe",
        "evidence": {
            "current_pnl": -0.2,
            "giveback_ratio": 1.0,
            "stop_loss_progress": 0.82,
            "trigger_tags": ["profit_giveback_after_mfe"],
        },
        "recommended_controls": {"reduce_fraction": 0.5},
        "supervisor_template": {
            "thresholds": {"near_stop_loss_progress": 0.85},
        },
    }
    kwargs = {
        "bridge": _Bridge(),
        "position": {"position_id": 8, "symbol": "XAUUSD+", "volume": 100.0},
        "controls": verdict["recommended_controls"],
        "floor_api_volume_to_step": floor_api_volume_to_step,
        "should_full_close_untradeable_reduce": (
            should_full_close_untradeable_reduce
        ),
    }

    below_threshold = plan_supervisor_reduce_action(verdict=verdict, **kwargs)
    above_verdict = {
        **verdict,
        "evidence": {**verdict["evidence"], "stop_loss_progress": 0.86},
    }
    above_threshold = plan_supervisor_reduce_action(
        verdict=above_verdict,
        **kwargs,
    )
    normalized = normalize_supervisor_reduce_verdict(
        above_verdict,
        above_threshold,
    )

    assert below_threshold["effective_action"] == "hold"
    assert above_threshold["effective_action"] == "close"
    assert normalized["action"] == "close"
    assert normalized["requested_action"] == "reduce"
    assert normalized["effective_action"] == "close"
    assert normalized["recommended_controls"]["original_action"] == "reduce"
    assert normalized["recommended_controls"]["protection_mode"] == "full_exit"


def test_plan_supervisor_reduce_action_resolves_meta_before_partial_close():
    calls = {
        "traces": [],
        "supervisor_state": [],
        "reentry": [],
        "close_reason": [],
        "close_verdict": [],
        "retired": [],
        "logs": [],
    }

    class _Bridge:
        symbol = "XAUUSD"
        _symbol_meta = {}

        def _resolve_symbol_id(self):
            calls["resolved"] = True
            self._symbol_meta = {"api_min_volume": 100.0, "api_step_volume": 100.0}

        def close_position(self, *_args, **_kwargs):
            raise AssertionError("below-min partial close should not be sent")

    plan = plan_supervisor_reduce_action(
        bridge=_Bridge(),
        position={"position_id": 7, "symbol": "XAUUSD", "volume": 200.0},
        verdict={"summary_reason": "trim_risk"},
        controls={"reduce_fraction": 0.25},
        floor_api_volume_to_step=floor_api_volume_to_step,
        should_full_close_untradeable_reduce=(
            _deps(calls)["should_full_close_untradeable_reduce"]
        ),
    )

    assert calls["resolved"] is True
    assert plan["effective_action"] == "hold"
    assert plan["min_volume"] == 100.0
    assert plan["reduce_volume"] == 0.0
    assert calls["logs"] == []


def test_execute_supervisor_reduce_action_floors_partial_close_to_broker_step():
    calls = {
        "traces": [],
        "supervisor_state": [],
        "reentry": [],
        "close_reason": [],
        "close_verdict": [],
        "retired": [],
        "logs": [],
    }

    class _Bridge:
        _symbol_meta = {"api_min_volume": 100.0, "api_step_volume": 100.0}

        def close_position(self, pid, volume=None):
            calls["close_call"] = (pid, volume)
            return SimpleNamespace(success=True)

    _execute_reduce(
        bridge=_Bridge(),
        position={"position_id": 7, "symbol": "XAUUSD", "volume": 250.0},
        verdict={"summary_reason": "trim_risk"},
        risk_action="reduce_position",
        risk_verdict={"allowed": True, "reason": "ok"},
        decision_id="decision-1",
        cfg=SimpleNamespace(),
        tick=9,
        acct=None,
        controls={"reduce_fraction": 0.5},
        log=lambda msg: calls["logs"].append(msg),
        ledger=None,
        floor_api_volume_to_step=floor_api_volume_to_step,
        **{k: v for k, v in _deps(calls).items() if k != "floor_api_volume_to_step"},
    )

    assert calls["close_call"] == (7, 100.0)
    assert calls["traces"][0]["execution_reason"] == "partial_close_success"
    assert calls["logs"] == ["tick 9: supervisor reduce pos=7 vol=100"]


def test_execute_supervisor_tighten_action_success_records_trace_and_reentry():
    calls = {
        "traces": [],
        "events": [],
        "state": [],
        "reentry": [],
        "tracked": [],
        "retired": [],
        "logs": [],
    }

    class _Bridge:
        is_connected = True

        def get_spot_quote(self):
            return {"mid": 4010.0}

        def amend_position_sltp(self, pid, *, sl, tp):
            calls["amend"] = (pid, sl, tp)
            return SimpleNamespace(success=True)

        def reconcile_positions(self, *, force, allow_cache_fallback):
            calls["reconcile"] = (force, allow_cache_fallback)
            return SimpleNamespace(
                status="fresh",
                observed_at=time.time(),
                reconcile_id="reconcile-tighten-7",
                positions=({"position_id": 7, "sl": 4005.0, "tp": 4030.0},),
            )

    def build_plan(**_kwargs):
        return {
            "target_sl": 4005.0,
            "current_tp": 4020.0,
            "target_tp": 4030.0,
            "planned_tp": 4030.0,
            "planned_sl": 4005.0,
            "sl_plan": {"allowed": True},
        }

    def build_result_payloads(**kwargs):
        return {
            "position_event_type": "supervisor_tighten",
            "position_event_details": {"result": kwargs["result"]},
            "trace_fields": {
                "stage": "executed",
                "outcome": kwargs["result"],
                "execution_status": kwargs["result"],
                "execution_reason": "tighten_position_success",
            },
        }

    execute_supervisor_tighten_action(
        bridge=_Bridge(),
        position={"position_id": 7, "current_price": 4010.0},
        verdict={"summary_reason": "protect_profit"},
        risk_action="tighten_position",
        risk_verdict={"allowed": True, "reason": "ok"},
        decision_id="decision-tighten",
        cfg=SimpleNamespace(),
        tick=9,
        acct={"equity": 10000.0},
        controls={},
        log=calls["logs"].append,
        build_tighten_execution_plan=build_plan,
        build_tighten_result_payloads=build_result_payloads,
        log_supervisor_position_event=lambda **kwargs: calls["events"].append(kwargs),
        log_supervisor_trace=lambda **kwargs: calls["traces"].append(kwargs),
        remember_supervisor_state=lambda *args, **kwargs: calls["state"].append((args, kwargs)),
        remember_supervisor_reentry_block=lambda **kwargs: calls["reentry"].append(kwargs),
        track_local_sl_tp=lambda *args, **kwargs: calls["tracked"].append((args, kwargs)),
        result_is_position_not_found=lambda _result: False,
        retire_broker_missing_position=lambda *args, **kwargs: calls["retired"].append((args, kwargs)),
    )

    assert calls["amend"] == (7, 4005.0, 4030.0)
    assert calls["reconcile"] == (True, False)
    assert calls["tracked"] == [((7,), {"sl": 4005.0, "tp": 4030.0})]
    assert calls["events"][0]["event_type"] == "supervisor_tighten"
    assert calls["state"][0][1]["action_applied"] == "tighten"
    assert calls["reentry"][0]["action"] == "tighten"
    assert calls["logs"] == ["tick 9: supervisor tighten pos=7 sl->4005.00 tp->4030.00"]


def test_execute_supervisor_tighten_action_skip_when_plan_not_allowed():
    calls = {"traces": [], "events": [], "state": [], "logs": []}

    class _Bridge:
        def get_spot_quote(self):
            return {}

        def amend_position_sltp(self, *_args, **_kwargs):
            raise AssertionError("amend should not be called")

    execute_supervisor_tighten_action(
        bridge=_Bridge(),
        position={"position_id": 7},
        verdict={"summary_reason": "protect_profit"},
        risk_action="tighten_position",
        risk_verdict={"allowed": True, "reason": "ok"},
        decision_id="decision-tighten",
        cfg=SimpleNamespace(),
        tick=9,
        acct=None,
        controls={},
        log=calls["logs"].append,
        build_tighten_execution_plan=lambda **_kwargs: {"sl_plan": {"allowed": False, "reason": "not_tighter"}},
        build_tighten_result_payloads=lambda **kwargs: {
            "position_event_type": "supervisor_tighten_skipped",
            "position_event_details": {"result": kwargs["result"]},
            "trace_fields": {
                "stage": "execution_skipped",
                "outcome": "skipped",
                "execution_status": "skipped",
                "execution_reason": "not_tighter",
            },
        },
        log_supervisor_position_event=lambda **kwargs: calls["events"].append(kwargs),
        log_supervisor_trace=lambda **kwargs: calls["traces"].append(kwargs),
        remember_supervisor_state=lambda *args, **kwargs: calls["state"].append((args, kwargs)),
        remember_supervisor_reentry_block=lambda **_kwargs: None,
        track_local_sl_tp=lambda *_args, **_kwargs: None,
        result_is_position_not_found=lambda _result: False,
        retire_broker_missing_position=lambda *_args, **_kwargs: None,
    )

    assert calls["events"][0]["event_type"] == "supervisor_tighten_skipped"
    assert calls["state"][0][1]["broker"] == "ctrader"
    assert calls["logs"] == ["tick 9: supervisor tighten SKIP pos=7 reason=not_tighter"]


def test_execute_supervisor_tighten_action_rechecks_quote_before_amend():
    calls = {"traces": [], "events": [], "state": [], "logs": [], "quotes": 0}

    class _Bridge:
        def get_spot_quote(self):
            calls["quotes"] += 1
            return {"bid": 4010.0 if calls["quotes"] == 1 else 4004.0, "ts": time.time()}

        def amend_position_sltp(self, *_args, **_kwargs):
            raise AssertionError("invalidated stop must not reach broker")

    def build_plan(*, quote, policy, **_kwargs):
        assert policy["min_stop_distance_points"] == 0.5
        if quote["bid"] < 4005.0:
            return {
                "target_sl": 4005.0,
                "current_tp": 4020.0,
                "target_tp": 0.0,
                "planned_tp": 4020.0,
                "planned_sl": 0.0,
                "sl_plan": {"allowed": False, "reason": "not_tightening_long_stop_loss"},
            }
        return {
            "target_sl": 4005.0,
            "current_tp": 4020.0,
            "target_tp": 0.0,
            "planned_tp": 4020.0,
            "planned_sl": 4005.0,
            "sl_plan": {"allowed": True, "reason": "ok"},
        }

    execute_supervisor_tighten_action(
        bridge=_Bridge(),
        position={"position_id": 7, "current_price": 4010.0},
        verdict={"summary_reason": "protect_profit"},
        risk_action="tighten_position",
        risk_verdict={"allowed": True, "reason": "ok"},
        decision_id="decision-tighten",
        cfg=SimpleNamespace(supervisor_min_stop_distance_points=0.5),
        tick=9,
        acct=None,
        controls={"target_stop_loss": 4005.0},
        log=calls["logs"].append,
        build_tighten_execution_plan=build_plan,
        build_tighten_result_payloads=lambda **kwargs: {
            "position_event_type": "amend_skipped",
            "position_event_details": {"reason": kwargs["sl_plan"]["reason"]},
            "trace_fields": {
                "stage": "execution_skipped",
                "outcome": "skipped",
                "execution_status": "skipped",
                "execution_reason": kwargs["sl_plan"]["reason"],
            },
        },
        log_supervisor_position_event=lambda **kwargs: calls["events"].append(kwargs),
        log_supervisor_trace=lambda **kwargs: calls["traces"].append(kwargs),
        remember_supervisor_state=lambda *args, **kwargs: calls["state"].append((args, kwargs)),
        remember_supervisor_reentry_block=lambda **_kwargs: None,
        track_local_sl_tp=lambda *_args, **_kwargs: None,
        result_is_position_not_found=lambda _result: False,
        retire_broker_missing_position=lambda *_args, **_kwargs: None,
    )

    assert calls["quotes"] == 2
    assert calls["events"][0]["event_type"] == "amend_skipped"
    assert "not_tightening_long_stop_loss" in calls["logs"][0]


def test_execute_supervisor_close_action_success_remembers_reason_and_verdict():
    calls = {
        "traces": [],
        "state": [],
        "reentry": [],
        "close_reason": [],
        "close_verdict": [],
        "retired": [],
        "logs": [],
    }

    class _Bridge:
        def close_position(self, pid):
            calls["close_call"] = pid
            return SimpleNamespace(success=True)

    execute_supervisor_close_action(
        bridge=_Bridge(),
        position={"position_id": 7, "current_price": 4010.0},
        verdict={"summary_reason": "thesis_broken"},
        risk_action="close_position",
        risk_verdict={"allowed": True, "reason": "ok"},
        decision_id="decision-close",
        cfg=SimpleNamespace(),
        tick=9,
        acct=None,
        controls={"close_reason": "thesis_broken"},
        log=calls["logs"].append,
        log_supervisor_trace=lambda **kwargs: calls["traces"].append(kwargs),
        remember_supervisor_state=lambda *args, **kwargs: calls["state"].append((args, kwargs)),
        remember_supervisor_reentry_block=lambda **kwargs: calls["reentry"].append(kwargs),
        remember_close_reason=lambda *args: calls["close_reason"].append(args),
        remember_close_verdict=lambda *args: calls["close_verdict"].append(args),
        result_is_position_not_found=lambda _result: False,
        retire_broker_missing_position=lambda *args, **kwargs: calls["retired"].append((args, kwargs)),
    )

    assert calls["close_call"] == 7
    assert calls["close_reason"] == [(7, "thesis_broken")]
    assert calls["close_verdict"][0][1].to_dict() == {"allowed": True, "reason": "ok"}
    assert calls["state"][0][1]["action_applied"] == "close"
    assert calls["traces"][0]["execution_reason"] == "close_position_success"
    assert calls["logs"] == ["tick 9: supervisor close sent pos=7 reason=thesis_broken"]


def test_close_broker_success_survives_all_post_broker_audit_failures():
    broker_calls: list[int] = []
    failures: list[tuple[str, dict]] = []
    logs: list[str] = []

    class _Bridge:
        def close_position(self, pid):
            broker_calls.append(pid)
            return SimpleNamespace(success=True)

    def _fail(name):
        return lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(name))

    execute_supervisor_close_action(
        bridge=_Bridge(),
        position={"position_id": 71, "current_price": 4010.0},
        verdict={"summary_reason": "thesis_broken"},
        risk_action="close_position",
        risk_verdict={"allowed": True, "reason": "ok"},
        decision_id="decision-close-audit-failure",
        cfg=SimpleNamespace(),
        tick=10,
        acct=None,
        controls={"close_reason": "thesis_broken"},
        log=logs.append,
        log_supervisor_trace=_fail("trace unavailable"),
        remember_supervisor_state=_fail("state unavailable"),
        remember_supervisor_reentry_block=_fail("reentry unavailable"),
        remember_close_reason=_fail("reason unavailable"),
        remember_close_verdict=_fail("verdict unavailable"),
        result_is_position_not_found=lambda _result: False,
        retire_broker_missing_position=lambda *_args, **_kwargs: None,
        record_aux_failure=lambda event_type, **kwargs: failures.append((event_type, kwargs)),
    )

    assert broker_calls == [71]
    assert len(failures) == 5
    assert {item[1]["payload"]["stage"] for item in failures} == {
        "close_reason",
        "close_verdict",
        "supervisor_state",
        "reentry_block",
        "supervisor_trace",
    }
    assert logs[-1] == "tick 10: supervisor close sent pos=71 reason=thesis_broken"


def test_reduce_broker_success_survives_accounting_ledger_and_state_failures():
    broker_calls: list[tuple[int, float]] = []
    failures: list[tuple[str, dict]] = []
    logs: list[str] = []

    class _Bridge:
        _symbol_meta = {"api_min_volume": 1.0, "api_step_volume": 1.0}

        def close_position(self, pid, volume=None):
            broker_calls.append((pid, volume))
            return SimpleNamespace(success=True, price=4010.0)

    class _Ledger:
        def log_position_event(self, **_kwargs):
            raise RuntimeError("ledger unavailable")

    calls = {
        "traces": [],
        "supervisor_state": [],
        "reentry": [],
        "close_reason": [],
        "close_verdict": [],
        "retired": [],
        "logs": logs,
    }
    deps = _deps(calls)
    deps["remember_supervisor_state"] = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("state unavailable")
    )
    deps["log_supervisor_trace"] = lambda **_kwargs: (_ for _ in ()).throw(
        RuntimeError("trace unavailable")
    )

    _execute_reduce(
        bridge=_Bridge(),
        position={"position_id": 72, "symbol": "XAUUSD+", "volume": 100.0, "current_price": 4010.0},
        verdict={"summary_reason": "trim_risk"},
        risk_action="reduce_position",
        risk_verdict={"allowed": True, "reason": "ok"},
        decision_id="decision-reduce-audit-failure",
        cfg=SimpleNamespace(),
        tick=10,
        acct=None,
        controls={"reduce_fraction": 0.25},
        log=logs.append,
        ledger=_Ledger(),
        record_partial_close_execution=lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError("accounting unavailable")
        ),
        record_aux_failure=lambda event_type, **kwargs: failures.append((event_type, kwargs)),
        **deps,
    )

    assert broker_calls == [(72, 25.0)]
    assert {item[1]["payload"]["stage"] for item in failures} == {
        "partial_close_accounting",
        "position_event",
        "supervisor_state",
        "supervisor_trace",
    }
    assert logs[-1] == "tick 10: supervisor reduce pos=72 vol=25"


def test_tighten_broker_success_survives_projection_and_audit_failures():
    broker_calls: list[tuple[int, float, float]] = []
    failures: list[tuple[str, dict]] = []
    logs: list[str] = []

    class _Bridge:
        is_connected = True

        def get_spot_quote(self):
            return {"mid": 4010.0}

        def amend_position_sltp(self, pid, *, sl, tp):
            broker_calls.append((pid, sl, tp))
            return SimpleNamespace(success=True)

        def reconcile_positions(self, *, force, allow_cache_fallback):
            return SimpleNamespace(
                status="fresh",
                observed_at=time.time(),
                reconcile_id="reconcile-tighten-73",
                positions=({"position_id": 73, "sl": 4005.0, "tp": 4030.0},),
            )

    def _plan(**_kwargs):
        return {
            "target_sl": 4005.0,
            "current_tp": 4020.0,
            "target_tp": 4030.0,
            "planned_tp": 4030.0,
            "planned_sl": 4005.0,
            "sl_plan": {"allowed": True},
        }

    def _payloads(**kwargs):
        return {
            "position_event_type": "supervisor_tighten",
            "position_event_details": {"result": kwargs["result"]},
            "trace_fields": {
                "stage": "executed",
                "outcome": "applied",
                "execution_status": "applied",
                "execution_reason": "tighten_position_success",
            },
        }

    def _fail(name):
        return lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(name))

    execute_supervisor_tighten_action(
        bridge=_Bridge(),
        position={"position_id": 73, "current_price": 4010.0},
        verdict={"summary_reason": "protect_profit"},
        risk_action="tighten_position",
        risk_verdict={"allowed": True, "reason": "ok"},
        decision_id="decision-tighten-audit-failure",
        cfg=SimpleNamespace(),
        tick=10,
        acct=None,
        controls={"target_stop_loss": 4005.0},
        log=logs.append,
        build_tighten_execution_plan=_plan,
        build_tighten_result_payloads=_payloads,
        log_supervisor_position_event=_fail("event unavailable"),
        log_supervisor_trace=_fail("trace unavailable"),
        remember_supervisor_state=_fail("state unavailable"),
        remember_supervisor_reentry_block=_fail("reentry unavailable"),
        track_local_sl_tp=_fail("projection unavailable"),
        result_is_position_not_found=lambda _result: False,
        retire_broker_missing_position=lambda *_args, **_kwargs: None,
        record_aux_failure=lambda event_type, **kwargs: failures.append((event_type, kwargs)),
    )

    assert broker_calls == [(73, 4005.0, 4030.0)]
    assert {item[1]["payload"]["stage"] for item in failures} == {
        "track_local_sl_tp",
        "position_event",
        "supervisor_state",
        "reentry_block",
        "supervisor_trace",
    }
    assert logs[-1] == "tick 10: supervisor tighten pos=73 sl->4005.00 tp->4030.00"


def test_tighten_accepted_rpc_requires_matching_fresh_broker_projection():
    calls = {
        "tracked": [],
        "events": [],
        "traces": [],
        "state": [],
        "reentry": [],
        "fail_closed": [],
        "aux": [],
        "logs": [],
    }

    class _Bridge:
        is_connected = True

        def get_spot_quote(self):
            return {"mid": 4010.0}

        def amend_position_sltp(self, pid, *, sl, tp):
            calls["amend"] = (pid, sl, tp)
            return SimpleNamespace(success=True)

        def reconcile_positions(self, *, force, allow_cache_fallback):
            return SimpleNamespace(
                status="fresh",
                observed_at=time.time(),
                reconcile_id="reconcile-mismatch-74",
                positions=({"position_id": 74, "sl": 4000.0, "tp": 4030.0},),
            )

    def _plan(**_kwargs):
        return {
            "target_sl": 4005.0,
            "current_tp": 4020.0,
            "target_tp": 4030.0,
            "planned_tp": 4030.0,
            "planned_sl": 4005.0,
            "sl_plan": {"allowed": True},
        }

    def _payloads(**kwargs):
        return {
            "position_event_type": "supervisor_tighten_failed",
            "position_event_details": {
                "result": kwargs["result"],
                "failure_reason": kwargs.get("failure_reason"),
            },
            "trace_fields": {
                "stage": "execution_failed",
                "outcome": "failed",
                "execution_status": "failed",
                "execution_reason": kwargs.get("failure_reason"),
            },
        }

    execute_supervisor_tighten_action(
        bridge=_Bridge(),
        position={"position_id": 74, "current_price": 4010.0, "digits": 2},
        verdict={"summary_reason": "protect_profit"},
        risk_action="tighten_position",
        risk_verdict={"allowed": True, "reason": "ok"},
        decision_id="decision-tighten-mismatch",
        cfg=SimpleNamespace(),
        tick=11,
        acct=None,
        controls={"target_stop_loss": 4005.0},
        log=calls["logs"].append,
        build_tighten_execution_plan=_plan,
        build_tighten_result_payloads=_payloads,
        log_supervisor_position_event=lambda **kwargs: calls["events"].append(kwargs),
        log_supervisor_trace=lambda **kwargs: calls["traces"].append(kwargs),
        remember_supervisor_state=lambda *args, **kwargs: calls["state"].append((args, kwargs)),
        remember_supervisor_reentry_block=lambda **kwargs: calls["reentry"].append(kwargs),
        track_local_sl_tp=lambda *args, **kwargs: calls["tracked"].append((args, kwargs)),
        result_is_position_not_found=lambda _result: False,
        retire_broker_missing_position=lambda *_args, **_kwargs: None,
        record_aux_failure=lambda event_type, **kwargs: calls["aux"].append((event_type, kwargs)),
        persist_safety_fail_closed=lambda **kwargs: calls["fail_closed"].append(kwargs),
    )

    assert calls["amend"] == (74, 4005.0, 4030.0)
    assert calls["tracked"] == []
    assert calls["reentry"] == []
    assert not any(kwargs.get("action_applied") for _args, kwargs in calls["state"])
    assert calls["events"][0]["details"]["result"] == "failed"
    assert calls["traces"][0]["execution_reason"].endswith("stop_loss_mismatch")
    assert len(calls["fail_closed"]) == 1
    fail_closed = calls["fail_closed"][0]
    assert fail_closed["blockers"] == ("amend_projection_unverified",)
    assert fail_closed["source"] == "supervisor_tighten"
    assert fail_closed["error"] == "amend_projection_unverified:stop_loss_mismatch"
    assert fail_closed["metadata"]["position_id"] == 74
    assert fail_closed["metadata"]["verification"]["ok"] is False
    assert (
        fail_closed["metadata"]["verification"]["reason"]
        == "stop_loss_mismatch"
    )
    assert calls["aux"][0][0] == "supervisor_amend_projection_unverified"
    assert calls["logs"][-1].endswith(
        "supervisor tighten UNVERIFIED pos=74: amend_projection_unverified:stop_loss_mismatch"
    )


def test_tighten_accepted_rpc_with_fresh_position_absence_records_closed():
    calls = {
        "tracked": [],
        "events": [],
        "traces": [],
        "state": [],
        "reentry": [],
        "fail_closed": [],
        "aux": [],
        "logs": [],
        "published": [],
    }

    class _Bridge:
        is_connected = True

        def get_spot_quote(self):
            return {"mid": 4096.96}

        def amend_position_sltp(self, pid, *, sl, tp):
            calls["amend"] = (pid, sl, tp)
            return SimpleNamespace(success=True)

        def reconcile_positions(self, *, force, allow_cache_fallback):
            return SimpleNamespace(
                status="fresh",
                observed_at=time.time(),
                reconcile_id="reconcile-closed-75",
                positions=(),
            )

    def _plan(**_kwargs):
        return {
            "target_sl": 4097.11,
            "current_tp": 4087.16,
            "target_tp": 4087.16,
            "planned_tp": 4087.16,
            "planned_sl": 4097.29,
            "sl_plan": {"allowed": True},
        }

    execute_supervisor_tighten_action(
        bridge=_Bridge(),
        position={"position_id": 75, "current_price": 4097.13, "digits": 2},
        verdict={"summary_reason": "thesis_weakening"},
        risk_action="tighten_position",
        risk_verdict={"allowed": True, "reason": "risk_reducing_action"},
        decision_id="decision-tighten-closed",
        cfg=SimpleNamespace(),
        tick=12,
        acct=None,
        controls={
            "target_stop_loss": 4097.11,
            "target_take_profit": 4087.16,
        },
        log=calls["logs"].append,
        build_tighten_execution_plan=_plan,
        build_tighten_result_payloads=build_supervisor_tighten_result_payloads,
        log_supervisor_position_event=lambda **kwargs: calls["events"].append(kwargs),
        log_supervisor_trace=lambda **kwargs: calls["traces"].append(kwargs),
        remember_supervisor_state=lambda *args, **kwargs: calls["state"].append((args, kwargs)),
        remember_supervisor_reentry_block=lambda **kwargs: calls["reentry"].append(kwargs),
        track_local_sl_tp=lambda *args, **kwargs: calls["tracked"].append((args, kwargs)),
        result_is_position_not_found=lambda _result: False,
        retire_broker_missing_position=lambda *_args, **_kwargs: None,
        record_aux_failure=lambda event_type, **kwargs: calls["aux"].append((event_type, kwargs)),
        persist_safety_fail_closed=lambda **kwargs: calls["fail_closed"].append(kwargs),
        publish_fresh_positions=lambda projection: calls["published"].append(projection),
    )

    assert calls["amend"] == (75, 4097.29, 4087.16)
    assert calls["tracked"] == []
    assert calls["fail_closed"] == []
    assert calls["aux"] == []
    assert len(calls["published"]) == 1
    assert calls["events"][0]["event_type"] == "tightened_then_closed"
    assert calls["traces"][0]["execution_status"] == "applied"
    assert (
        calls["traces"][0]["execution_reason"]
        == "amend_accepted_position_closed"
    )
    assert calls["traces"][0]["execution"]["position_closed_after_amend"] is True
    assert calls["state"][0][1]["action_applied"] == "tighten"
    assert calls["reentry"][0]["action"] == "tighten"
    assert calls["logs"][-1].endswith("position_closed_after_amend")
