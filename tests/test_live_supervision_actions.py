from types import SimpleNamespace

from backend.services.live_risk_sizing import floor_api_volume_to_step
from backend.services.live_supervision_actions import (
    execute_supervisor_close_action,
    execute_supervisor_reduce_action,
    execute_supervisor_tighten_action,
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
    }

    class _Bridge:
        _symbol_meta = {"api_min_volume": 1.0, "api_step_volume": 1.0}

        def close_position(self, pid, volume=None):
            calls["close_call"] = (pid, volume)
            return SimpleNamespace(success=True)

    class _Ledger:
        def log_position_event(self, **kwargs):
            calls["ledger_events"].append(kwargs)

    execute_supervisor_reduce_action(
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
        **_deps(calls),
    )

    assert calls["close_call"] == (7, 25.0)
    assert calls["ledger_events"][0]["event_type"] == "reduced"
    assert calls["ledger_events"][0]["net_volume"] == 75.0
    assert calls["supervisor_state"][0][1]["action_applied"] == "reduce"
    assert calls["traces"][0]["execution_reason"] == "partial_close_success"
    assert calls["logs"] == ["tick 9: supervisor reduce pos=7 vol=25"]


def test_execute_supervisor_reduce_action_invalid_volume_skips_when_not_upgradeable():
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

    execute_supervisor_reduce_action(
        bridge=_Bridge(),
        position={"position_id": 7, "volume": 100.0},
        verdict={"summary_reason": "trim_risk"},
        risk_action="reduce_position",
        risk_verdict={"allowed": True, "reason": "ok"},
        decision_id="decision-1",
        cfg=SimpleNamespace(),
        tick=9,
        acct=None,
        controls={"reduce_fraction": 0.1},
        log=lambda msg: calls["logs"].append(msg),
        ledger=None,
        **_deps(calls),
    )

    assert calls["traces"][0]["stage"] == "execution_skipped"
    assert calls["traces"][0]["execution_reason"] == "invalid_reduce_volume"
    assert calls["traces"][0]["execution"]["fallback_skip_reason"] == "not_upgradeable"
    assert calls["logs"] == []


def test_execute_supervisor_reduce_action_resolves_meta_before_partial_close():
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

    execute_supervisor_reduce_action(
        bridge=_Bridge(),
        position={"position_id": 7, "symbol": "XAUUSD", "volume": 200.0},
        verdict={"summary_reason": "trim_risk"},
        risk_action="reduce_position",
        risk_verdict={"allowed": True, "reason": "ok"},
        decision_id="decision-1",
        cfg=SimpleNamespace(),
        tick=9,
        acct=None,
        controls={"reduce_fraction": 0.25},
        log=lambda msg: calls["logs"].append(msg),
        ledger=None,
        floor_api_volume_to_step=floor_api_volume_to_step,
        **{k: v for k, v in _deps(calls).items() if k != "floor_api_volume_to_step"},
    )

    assert calls["resolved"] is True
    assert calls["traces"][0]["stage"] == "execution_skipped"
    assert calls["traces"][0]["execution"]["min_volume"] == 100.0
    assert calls["traces"][0]["execution"]["reduce_volume"] == 0.0
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

    execute_supervisor_reduce_action(
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
        def get_spot_quote(self):
            return {"mid": 4010.0}

        def amend_position_sltp(self, pid, *, sl, tp):
            calls["amend"] = (pid, sl, tp)
            return SimpleNamespace(success=True)

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
