import inspect
from pathlib import Path
from types import SimpleNamespace

from backend.services.live_loop_v2 import LiveSafetyCycleRuntime, run_live_safety_cycle
from backend.services import live_service
from backend.services.live_safety_plane import LiveSafetyPlane
from backend.services.live_safety_planner import (
    SafetyPlan,
    SafetyPlannerRuntime,
    plan_live_safety_candidates,
    safety_candidate,
)
from backend.services.live_safety_state import (
    activate_no_new_risk_latch,
    release_no_new_risk_latch_cause,
    safety_v2_forced_shadow_status,
)


def _planner_runtime():
    plans = {
        2: {
            "schema_version": "entry_protection_plan.v1",
            "direction": 1,
            "target_stop_loss": 99.0,
            "target_take_profit": 120.0,
        }
    }

    def timeout_context(position, _cfg, _now):
        return {
            "holding_seconds": 120.0 if position["position_id"] == 1 else 5.0,
            "max_holding_seconds": 60.0,
        }

    def supervisor(position, *_args):
        if position["position_id"] == 3:
            return {
                "action": "reduce",
                "recommended_controls": {
                    "reduce_fraction": 0.5,
                    "close_reason": "supervisor_reduce",
                },
            }
        return {"action": "hold"}

    def trailing(position, _state, _price, _atr, _conviction):
        return {
            "candidate": {
                "controls": {
                    "target_stop_loss": 105.0 + position["position_id"],
                    "target_take_profit": 120.0,
                    "close_reason": "legacy_awe_trailing",
                    "protection_mode": "legacy_awe_trailing_stop",
                }
            }
        }

    return SafetyPlannerRuntime(
        build_timeout_context=timeout_context,
        load_entry_protection_plan=lambda pid: plans.get(pid, {}),
        evaluate_supervisor=supervisor,
        build_trailing_update=trailing,
        trailing_state=lambda _pid: {},
        composite_conviction=lambda: 0.7,
        clock=lambda: 1000.0,
    )


def test_pure_planner_excludes_retired_legacy_trailing_candidates():
    positions = [
        {"position_id": 1, "direction": 1, "sl": 90.0, "tp": 120.0, "current_price_state": "known", "pnl_state": "known"},
        {"position_id": 2, "direction": 1, "sl": 0.0, "tp": 0.0, "current_price_state": "known", "pnl_state": "known"},
        {"position_id": 3, "direction": 1, "sl": 90.0, "tp": 120.0, "current_price_state": "known", "pnl_state": "known"},
        {"position_id": 4, "direction": 1, "sl": 90.0, "tp": 120.0, "current_price_state": "known", "pnl_state": "known"},
    ]

    plan = plan_live_safety_candidates(
        positions=positions,
        cfg=SimpleNamespace(),
        account={},
        current_price=110.0,
        atr_price=2.0,
        runtime=_planner_runtime(),
        planned_at=1000.0,
    )

    assert [(item.action, item.position_id) for item in plan.candidates] == [
        ("timeout", 1),
        ("repair_entry_protection", 2),
        ("reduce", 3),
    ]
    selected_priorities = [
        item["priority"] for item in plan.arbitration if item["decision"] == "selected"
    ]
    assert selected_priorities == [10, 20, 30]
    assert len([item for item in plan.arbitration if item["decision"] == "superseded"]) == 0
    assert all(len(item.fingerprint) == 64 for item in plan.candidates)


def test_pure_planner_does_not_select_wall_clock_timeout_during_market_closure():
    runtime = SafetyPlannerRuntime(
        build_timeout_context=lambda *_args: {
            "holding_seconds": 3600.0,
            "holding_seconds_state": "known",
            "max_holding_seconds": 1800.0,
            "market_time_budget": {
                "market_open_holding_seconds": 900.0,
                "market_closed_pending": True,
                "timeout_on_market_time": False,
            },
        },
        load_entry_protection_plan=lambda _pid: {},
        evaluate_supervisor=lambda *_args: {"action": "hold"},
    )

    plan = plan_live_safety_candidates(
        positions=[{"position_id": 91}],
        cfg=SimpleNamespace(),
        account={},
        current_price=100.0,
        atr_price=1.0,
        runtime=runtime,
        planned_at=1000.0,
    )

    assert plan.candidates == ()


class _Bridge:
    def unresolved_execution_intent_count(self):
        return 0


def _loop_runtime(*, plane, plan, supervisor_result, calls):
    return LiveSafetyCycleRuntime(
        get_safety_plane=lambda _owner: plane,
        explicit_position_reconcile=lambda _bridge: {},
        publish_fresh_positions=lambda result, **_kwargs: list(result["positions"]),
        get_live_state=lambda key, default=None, **_kwargs: (
            {"balance": 1000.0} if key == "account" else default
        ),
        update_live_state=lambda **payload: calls.append(("state", payload)),
        runtime_config=lambda: SimpleNamespace(),
        safety_reference_price=lambda _bridge, _positions: 100.0,
        factor_pipeline={"last_factor_values": {"atr_ratio": 0.01}},
        plan_safety_candidates=lambda **_kwargs: plan,
        execute_safety_candidate=lambda candidate, **_kwargs: (
            calls.append(("v2_execute", candidate)) or {"ok": True, "status": "dispatched"}
        ),
        run_position_protection_cycle=lambda *_args, **_kwargs: (
            calls.append(("supervisor_cycle", _kwargs)) or supervisor_result
        ),
        persist_safety_fail_closed=lambda **payload: (
            calls.append(("persist_fail_closed", payload))
            or {"status": "no_new_risk_latched"}
        ),
        controller=SimpleNamespace(),
    )


def _reconcile():
    return {
        "status": "fresh",
        "success": True,
        "reconcile_id": "r1",
        "observed_at": 100.0,
        "positions": [{"position_id": 7}],
    }


def test_shadow_uses_single_governed_supervisor_authority():
    candidate = safety_candidate(
        action="tighten",
        position_id=7,
        source="supervisor_tighten",
        controls={"target_stop_loss": 99.0},
    )
    plan = SafetyPlan(candidates=(candidate,), arbitration=(), planned_at=100.0)
    calls = []

    payload = run_live_safety_cycle(
        bridge=_Bridge(),
        broker="ctrader",
        tick=1,
        log=lambda _message: None,
        runtime=_loop_runtime(
            plane=LiveSafetyPlane(mode="shadow", clock=lambda: 100.0),
            plan=plan,
            supervisor_result={
                "safety_candidates": [candidate.__dict__.copy()],
                "safety_arbitration": [],
            },
            calls=calls,
        ),
        reconcile_result=_reconcile(),
    )

    assert len([item for item in calls if item[0] == "supervisor_cycle"]) == 1
    assert payload["supervisor_executor_authoritative"] is True
    assert payload["planner"]["broker_mutation"] is False
    assert payload["comparison"]["independent"] is True
    assert payload["comparison"]["match"] is True
    assert payload["comparison"]["enforce_eligible"] is True
    assert len(payload["comparison"]["fingerprint"]) == 64
    assert payload["accepting_new_risk"] is True


def test_shadow_empty_account_still_runs_two_pure_plans_without_mutation():
    calls = []
    plan = SafetyPlan(candidates=(), arbitration=(), planned_at=100.0)
    payload = run_live_safety_cycle(
        bridge=_Bridge(),
        broker="ctrader",
        tick=1,
        log=lambda _message: None,
        runtime=_loop_runtime(
            plane=LiveSafetyPlane(mode="shadow", clock=lambda: 100.0),
            plan=plan,
            supervisor_result={},
            calls=calls,
        ),
        reconcile_result={**_reconcile(), "positions": []},
    )

    assert payload["comparison"]["independent"] is True
    assert payload["comparison"]["match"] is True
    assert payload["accepting_new_risk"] is True
    assert not [
        item for item in calls if item[0] in {"supervisor_cycle", "v2_execute"}
    ]


def test_shadow_ignores_retired_legacy_projection_and_runs_supervisor_cycle():
    v2 = safety_candidate(action="tighten", position_id=7, source="supervisor_tighten")
    legacy = safety_candidate(action="close", position_id=7, source="supervisor_close")
    calls = []

    payload = run_live_safety_cycle(
        bridge=_Bridge(),
        broker="ctrader",
        tick=1,
        log=lambda _message: None,
        runtime=_loop_runtime(
            plane=LiveSafetyPlane(mode="shadow", clock=lambda: 100.0),
            plan=SafetyPlan(candidates=(v2,), arbitration=(), planned_at=100.0),
            supervisor_result={"safety_candidates": [legacy.__dict__.copy()]},
            calls=calls,
        ),
        reconcile_result=_reconcile(),
    )

    assert len([item for item in calls if item[0] == "supervisor_cycle"]) == 1
    assert payload["comparison"]["independent"] is True
    assert payload["comparison"]["match"] is True
    assert payload["comparison"]["enforce_eligible"] is True
    assert payload["comparison"]["authority"] == "supervisor_executor"
    assert "safety_candidate_mismatch" not in payload["blockers"]
    assert payload["accepting_new_risk"] is True


def test_shadow_planner_exception_keeps_supervisor_cycle_but_blocks_admission():
    legacy = safety_candidate(action="close", position_id=7, source="supervisor_close")
    calls = []
    runtime = _loop_runtime(
        plane=LiveSafetyPlane(mode="shadow", clock=lambda: 100.0),
        plan=SafetyPlan(candidates=(), arbitration=(), planned_at=100.0),
        supervisor_result={"safety_candidates": [legacy.__dict__.copy()]},
        calls=calls,
    )
    runtime = LiveSafetyCycleRuntime(
        **{
            **runtime.__dict__,
            "plan_safety_candidates": lambda **_kwargs: (_ for _ in ()).throw(
                RuntimeError("planner unavailable")
            ),
        }
    )

    payload = run_live_safety_cycle(
        bridge=_Bridge(),
        broker="ctrader",
        tick=1,
        log=lambda _message: None,
        runtime=runtime,
        reconcile_result=_reconcile(),
    )

    assert len([item for item in calls if item[0] == "supervisor_cycle"]) == 1
    assert payload["supervisor_executor_authoritative"] is True
    assert payload["comparison"]["independent"] is True
    assert payload["comparison"]["enforce_eligible"] is True
    assert "safety_candidate_planner_failed" in payload["blockers"]
    assert payload["accepting_new_risk"] is False


def test_enforce_single_authority_executes_candidate_once():
    v2 = safety_candidate(action="tighten", position_id=7, source="supervisor_tighten")
    calls = []
    runtime = _loop_runtime(
        plane=LiveSafetyPlane(mode="enforce", clock=lambda: 100.0),
        plan=SafetyPlan(candidates=(v2,), arbitration=(), planned_at=100.0),
        supervisor_result={},
        calls=calls,
    )
    runtime = LiveSafetyCycleRuntime(
        **{
            **runtime.__dict__,
            "plan_safety_candidates": lambda **_kwargs: (
                calls.append(("v2_plan", {}))
                or SafetyPlan(candidates=(v2,), arbitration=(), planned_at=100.0)
            ),
        }
    )

    payload = run_live_safety_cycle(
        bridge=_Bridge(),
        broker="ctrader",
        tick=1,
        log=lambda _message: None,
        runtime=runtime,
        reconcile_result=_reconcile(),
    )

    mutation_calls = [item[0] for item in calls if item[0] in {"supervisor_cycle", "v2_execute"}]
    assert mutation_calls == ["v2_execute"]
    assert "persist_fail_closed" not in [item[0] for item in calls]
    assert payload["status"] == "completed"
    assert payload["effective_mode"] == "enforce"
    assert payload["forced_shadow"] is False
    assert payload["supervisor_executor_authoritative"] is True
    assert payload["comparison"]["independent"] is True
    assert payload["comparison"]["match"] is True
    assert "safety_candidate_mismatch" not in payload["blockers"]
    assert payload["accepting_new_risk"] is True


def test_enforce_planner_exception_blocks_without_fallback_mutation():
    candidate = safety_candidate(action="close", position_id=7, source="supervisor_close")
    calls = []
    runtime = _loop_runtime(
        plane=LiveSafetyPlane(mode="enforce", clock=lambda: 100.0),
        plan=SafetyPlan(candidates=(candidate,), arbitration=(), planned_at=100.0),
        supervisor_result={},
        calls=calls,
    )
    runtime = LiveSafetyCycleRuntime(
        **{
            **runtime.__dict__,
            "plan_safety_candidates": lambda **_kwargs: (_ for _ in ()).throw(
                RuntimeError("planner unavailable")
            ),
        }
    )

    payload = run_live_safety_cycle(
        bridge=_Bridge(),
        broker="ctrader",
        tick=1,
        log=lambda _message: None,
        runtime=runtime,
        reconcile_result=_reconcile(),
    )

    assert [item[0] for item in calls if item[0] in {"supervisor_cycle", "v2_execute"}] == []
    assert "persist_fail_closed" not in [item[0] for item in calls]
    assert payload["status"] == "completed"
    assert payload["forced_shadow"] is False
    assert "safety_candidate_planner_failed" in payload["blockers"]
    assert payload["accepting_new_risk"] is False


def test_enforce_uses_the_single_live_safety_authority():
    candidate = safety_candidate(action="close", position_id=7, source="supervisor_close")
    calls = []
    runtime = _loop_runtime(
        plane=LiveSafetyPlane(mode="enforce", clock=lambda: 100.0),
        plan=SafetyPlan(candidates=(candidate,), arbitration=(), planned_at=100.0),
        supervisor_result={},
        calls=calls,
    )
    runtime = LiveSafetyCycleRuntime(
        **{
            **runtime.__dict__,
        }
    )

    payload = run_live_safety_cycle(
        bridge=_Bridge(),
        broker="ctrader",
        tick=1,
        log=lambda _message: None,
        runtime=runtime,
        reconcile_result=_reconcile(),
    )

    assert [item[0] for item in calls if item[0] in {"supervisor_cycle", "v2_execute"}] == [
        "v2_execute"
    ]
    assert payload["status"] == "completed"
    assert payload["forced_shadow"] is False
    assert "legacy_safety_preview_failed" not in payload["blockers"]
    assert "safety_v2_forced_shadow" not in payload["blockers"]
    assert payload["accepting_new_risk"] is True


def test_enforce_does_not_call_retired_fallback_persistence():
    v2 = safety_candidate(action="tighten", position_id=7, source="supervisor_tighten")
    calls = []
    runtime = _loop_runtime(
        plane=LiveSafetyPlane(mode="enforce", clock=lambda: 100.0),
        plan=SafetyPlan(candidates=(v2,), arbitration=(), planned_at=100.0),
        supervisor_result={},
        calls=calls,
    )
    runtime = LiveSafetyCycleRuntime(
        **{
            **runtime.__dict__,
            "persist_safety_fail_closed": lambda **_kwargs: (_ for _ in ()).throw(
                OSError("disk unavailable")
            ),
        }
    )

    payload = run_live_safety_cycle(
        bridge=_Bridge(),
        broker="ctrader",
        tick=1,
        log=lambda _message: None,
        runtime=runtime,
        reconcile_result=_reconcile(),
    )

    assert [item[0] for item in calls if item[0] in {"supervisor_cycle", "v2_execute"}] == [
        "v2_execute"
    ]
    assert "safety_forced_shadow_persistence_failed" not in payload["blockers"]
    assert payload["accepting_new_risk"] is True
    assert payload["protection"]["status"] == "v2_enforced"


def test_enforce_single_authority_never_mixes_legacy_projection():
    now = [100.0]
    plane = LiveSafetyPlane(mode="enforce", clock=lambda: now[0])
    v2 = safety_candidate(action="tighten", position_id=7, source="supervisor_tighten")
    current_v2 = [v2]
    calls = []
    runtime = _loop_runtime(
        plane=plane,
        plan=SafetyPlan(candidates=(), arbitration=(), planned_at=100.0),
        supervisor_result={},
        calls=calls,
    )
    runtime = LiveSafetyCycleRuntime(
        **{
            **runtime.__dict__,
            "plan_safety_candidates": lambda **_kwargs: SafetyPlan(
                candidates=tuple(current_v2), arbitration=(), planned_at=now[0]
            ),
        }
    )

    first = run_live_safety_cycle(
        bridge=_Bridge(),
        broker="ctrader",
        tick=1,
        log=lambda _message: None,
        runtime=runtime,
        reconcile_result={**_reconcile(), "observed_at": now[0]},
    )
    current_v2[:] = [v2]
    now[0] = 105.0
    second = run_live_safety_cycle(
        bridge=_Bridge(),
        broker="ctrader",
        tick=2,
        log=lambda _message: None,
        runtime=runtime,
        reconcile_result={**_reconcile(), "observed_at": now[0]},
    )

    assert first["forced_shadow"] is False
    assert second["forced_shadow"] is False
    assert second["comparison"]["match"] is True
    assert [item[0] for item in calls if item[0] == "v2_execute"] == [
        "v2_execute", "v2_execute"
    ]
    assert not [item for item in calls if item[0] == "supervisor_cycle"]


def test_enforce_duplicate_candidates_cannot_pass_set_like_comparison():
    candidate = safety_candidate(action="close", position_id=7, source="supervisor_close")
    duplicate_plan = SafetyPlan(
        candidates=(candidate, candidate), arbitration=(), planned_at=100.0
    )
    calls = []

    payload = run_live_safety_cycle(
        bridge=_Bridge(),
        broker="ctrader",
        tick=1,
        log=lambda _message: None,
        runtime=_loop_runtime(
            plane=LiveSafetyPlane(mode="enforce", clock=lambda: 100.0),
            plan=duplicate_plan,
            supervisor_result={},
            calls=calls,
        ),
        reconcile_result=_reconcile(),
    )

    assert payload["comparison"]["match"] is True
    assert payload["comparison"]["duplicate"] is True
    assert payload["comparison"]["position_conflict"] is True
    assert payload["comparison"]["enforce_eligible"] is False
    assert [
        item[0] for item in calls if item[0] in {"supervisor_cycle", "v2_execute"}
    ] == ["supervisor_cycle"]
    assert "safety_candidate_duplicate" in payload["blockers"]
    assert payload["supervisor_executor_authoritative"] is True
    assert payload["accepting_new_risk"] is False


def test_forced_shadow_authority_survives_plane_reconstruction(monkeypatch, tmp_path):
    monkeypatch.setenv("QUANT_SAFETY_STATE_DIR", str(tmp_path))
    activate_no_new_risk_latch(
        reason="safety_v2_forced_shadow",
        actor="system:test",
        metadata={"blockers": ["safety_v2_forced_shadow"]},
    )
    persisted = safety_v2_forced_shadow_status()
    assert persisted["active"] is True

    monkeypatch.setattr(
        live_service,
        "_phase2_feature_flags",
        lambda: SimpleNamespace(live_safety_plane_v2_mode="enforce"),
    )
    monkeypatch.setattr(live_service, "_live_safety_plane", None)
    monkeypatch.setattr(live_service, "_live_safety_plane_owner", "")
    restored = live_service._get_live_safety_plane("generation-restarted")

    assert restored.mode == "enforce"
    assert restored.effective_mode == "shadow"
    assert restored.forced_shadow is True

    release_no_new_risk_latch_cause(
        cause="safety_v2_forced_shadow",
        cause_id="candidate_comparison",
        reason="reviewed",
        actor="operator:test",
    )
    assert safety_v2_forced_shadow_status()["active"] is False


def test_existing_plane_observes_forced_shadow_written_by_another_process(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("QUANT_SAFETY_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        live_service,
        "_phase2_feature_flags",
        lambda: SimpleNamespace(live_safety_plane_v2_mode="enforce"),
    )
    monkeypatch.setattr(live_service, "_live_safety_plane", None)
    monkeypatch.setattr(live_service, "_live_safety_plane_owner", "")
    existing = live_service._get_live_safety_plane("generation-live")
    assert existing.forced_shadow is False

    activate_no_new_risk_latch(
        reason="safety_v2_forced_shadow",
        actor="system:other-process",
        metadata={"blockers": ["safety_v2_forced_shadow"]},
    )
    observed = live_service._get_live_safety_plane("generation-live")

    assert observed is existing
    assert observed.forced_shadow is True
    assert observed.effective_mode == "shadow"

    release_no_new_risk_latch_cause(
        cause="safety_v2_forced_shadow",
        cause_id="candidate_comparison",
        reason="reviewed",
        actor="operator:test",
    )


def test_production_forced_shadow_persistence_records_dedicated_safety_cause(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("QUANT_SAFETY_STATE_DIR", str(tmp_path))
    state_updates = []
    monkeypatch.setattr(
        live_service,
        "_live_state_update",
        lambda **payload: state_updates.append(payload),
    )

    result = live_service._persist_safety_fail_closed(
        blockers=["safety_candidate_mismatch", "safety_v2_forced_shadow"],
        source="safety_v2_forced_shadow",
    )
    status = safety_v2_forced_shadow_status()

    assert result["status"] == "no_new_risk_latched"
    assert status["active"] is True
    assert status["reason"] == "safety_v2_forced_shadow"
    assert any(update.get("accepting_new_risk") is False for update in state_updates)

    release_no_new_risk_latch_cause(
        cause="safety_v2_forced_shadow",
        cause_id="candidate_comparison",
        reason="reviewed",
        actor="operator:test",
    )
    assert safety_v2_forced_shadow_status()["active"] is False


def test_live_service_enforce_uses_single_supervisor_executor_exactly_once(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("QUANT_SAFETY_STATE_DIR", str(tmp_path))
    v2 = safety_candidate(action="tighten", position_id=7, source="supervisor_tighten")
    calls = []
    monkeypatch.setattr(
        live_service,
        "_get_live_safety_plane",
        lambda _generation_id="": LiveSafetyPlane(mode="enforce", clock=lambda: 100.0),
    )
    monkeypatch.setattr(
        live_service,
        "_publish_fresh_position_reconcile",
        lambda result, **_kwargs: list(result["positions"]),
    )
    monkeypatch.setattr(
        live_service,
        "_live_state_get",
        lambda key, default=None, **_kwargs: (
            {"balance": 1000.0} if key == "account" else default
        ),
    )
    monkeypatch.setattr(live_service, "_live_state_update", lambda **_payload: None)
    monkeypatch.setattr(live_service, "_safety_reference_price", lambda *_args: 100.0)
    monkeypatch.setattr(
        live_service,
        "_plan_live_safety_candidates",
        lambda **_kwargs: SafetyPlan(
            candidates=(v2,), arbitration=(), planned_at=100.0
        ),
    )
    monkeypatch.setattr(
        live_service,
        "_execute_live_safety_candidate",
        lambda *_args, **_kwargs: calls.append("v2") or {"ok": True},
    )
    monkeypatch.setattr(
        live_service,
        "_run_position_protection_cycle",
        lambda *_args, **_kwargs: calls.append("supervisor_cycle") or {},
    )

    payload = live_service._run_live_safety_cycle(
        bridge=_Bridge(),
        broker="ctrader",
        tick=1,
        log=lambda _message: None,
        reconcile_result=_reconcile(),
    )

    assert calls == ["v2"]
    assert payload["forced_shadow"] is False
    assert payload["supervisor_execution_path"] == "safety_candidate_executor"
    assert payload["accepting_new_risk"] is True
    assert safety_v2_forced_shadow_status()["active"] is False


def test_enforce_match_dispatches_only_v2_candidates_serially():
    candidates = (
        safety_candidate(action="close", position_id=7, source="supervisor_close"),
        safety_candidate(action="reduce", position_id=8, source="supervisor_reduce"),
    )
    calls = []
    reconcile = {
        **_reconcile(),
        "positions": [{"position_id": 7}, {"position_id": 8}],
    }
    plan = SafetyPlan(candidates=candidates, arbitration=(), planned_at=100.0)

    payload = run_live_safety_cycle(
        bridge=_Bridge(),
        broker="ctrader",
        tick=1,
        log=lambda _message: None,
        runtime=_loop_runtime(
            plane=LiveSafetyPlane(mode="enforce", clock=lambda: 100.0),
            plan=plan,
            supervisor_result={},
            calls=calls,
        ),
        reconcile_result=reconcile,
    )

    assert [item[1].position_id for item in calls if item[0] == "v2_execute"] == [7, 8]
    assert not [item for item in calls if item[0] == "supervisor_cycle"]
    assert payload["comparison"]["enforce_eligible"] is True
    assert payload["status"] == "completed"


def test_safety_planner_module_has_no_broker_mutation_or_entry_order_surface():
    for path in ("backend/services/live_safety_planner.py",):
        source = Path(path).read_text(encoding="utf-8")
        for forbidden in (
            "market" + "_buy",
            "market" + "_sell",
            "close_" + "position(",
            "amend_" + "position_sltp(",
        ):
            assert forbidden not in source

    dispatcher = Path(
        "backend/services/live_safety_candidate_execution.py"
    ).read_text(encoding="utf-8")
    for forbidden in ("market" + "_buy", "market" + "_sell", "open_trade"):
        assert forbidden not in dispatcher
