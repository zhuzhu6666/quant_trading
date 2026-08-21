from types import SimpleNamespace

from backend.services.live_safety_candidate_execution import (
    SafetyCandidateExecutionRuntime,
    execute_live_safety_candidate,
)
from backend.services.live_safety_planner import safety_candidate


def _runtime(
    *,
    timeout=None,
    trailing=None,
    verdict=None,
    supervision=None,
):
    return SafetyCandidateExecutionRuntime(
        enforce_holding_timeout=timeout or (lambda *_args, **_kwargs: set()),
        entry_protection_repair_source="entry_protection_repair",
        runtime_config_anchor=lambda: {
            "config_version": 7,
            "config_hash": "config-hash",
        },
        protection_candidate_cls=lambda **kwargs: SimpleNamespace(**kwargs),
        execute_protection_candidate=(
            trailing or (lambda *_args, **_kwargs: False)
        ),
        evaluate_position_supervisor=(
            verdict
            or (
                lambda *_args, **_kwargs: {
                    "action": "hold",
                    "recommended_controls": {},
                }
            )
        ),
        build_safety_candidate=safety_candidate,
        run_position_supervision=(
            supervision or (lambda *_args, **_kwargs: set())
        ),
    )


def _execute(candidate, runtime, *, pipeline=None):
    return execute_live_safety_candidate(
        candidate,
        bridge=SimpleNamespace(),
        positions=[{"position_id": 7, "symbol": "XAUUSD+"}],
        cfg=SimpleNamespace(),
        account={"equity": 10_000.0},
        pipeline=pipeline or {},
        tick=3,
        log=lambda _message: None,
        decision_ts=1_000.0,
        runtime=runtime,
    )


def test_missing_position_never_dispatches_candidate():
    candidate = safety_candidate(
        action="close",
        position_id=99,
        source="supervisor_close",
    )

    assert _execute(candidate, _runtime()) == {
        "ok": False,
        "status": "position_missing",
    }


def test_timeout_candidate_uses_only_holding_timeout_executor():
    candidate = safety_candidate(
        action="timeout",
        position_id=7,
        source="holding_timeout",
    )

    assert _execute(
        candidate,
        _runtime(timeout=lambda *_args, **_kwargs: {7}),
    ) == {"ok": True, "status": "dispatched"}


def test_supervisor_candidate_is_revalidated_before_execution():
    candidate = safety_candidate(
        action="close",
        position_id=7,
        source="supervisor_close",
        controls={"reason": "broken"},
    )

    result = _execute(
        candidate,
        _runtime(
            verdict=lambda *_args, **_kwargs: {
                "action": "hold",
                "recommended_controls": {},
            }
        ),
    )

    assert result["ok"] is False
    assert result["status"] == "candidate_changed_before_execution"
    assert result["observed_action"] == "hold"


def test_exact_supervisor_candidate_dispatches_reduction_once():
    controls = {"reason": "broken"}
    candidate = safety_candidate(
        action="close",
        position_id=7,
        source="supervisor_close",
        controls=controls,
    )
    calls = []

    def supervision(*_args, **kwargs):
        calls.append(kwargs)
        kwargs["candidate_recorder"](candidate)
        return {7}

    result = _execute(
        candidate,
        _runtime(
            verdict=lambda *_args, **_kwargs: {
                "action": "close",
                "recommended_controls": controls,
            },
            supervision=supervision,
        ),
    )

    assert result == {"ok": True, "status": "dispatched"}
    assert len(calls) == 1


def test_entry_protection_repair_uses_tightening_executor():
    candidate = safety_candidate(
        action="repair_entry_protection",
        position_id=7,
        source="entry_protection_repair",
        controls={"target_stop_loss": 2_300.0},
    )
    dispatched = []

    def trailing(protection, **_kwargs):
        dispatched.append(protection)
        return True

    result = _execute(candidate, _runtime(trailing=trailing))

    assert result == {"ok": True, "status": "dispatched"}
    assert dispatched[0].risk_action == "tighten_position"
    assert dispatched[0].config_hash == "config-hash"
