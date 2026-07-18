import inspect
from pathlib import Path
from types import SimpleNamespace

from backend.services.live_loop_v2 import LiveSafetyCycleRuntime, run_live_safety_cycle
from backend.services.live_legacy_safety_preview import preview_legacy_safety_candidates
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


def test_pure_planner_covers_all_safety_stages_in_legacy_priority_order():
    positions = [
        {"position_id": 1, "direction": 1, "sl": 90.0, "tp": 120.0},
        {"position_id": 2, "direction": 1, "sl": 0.0, "tp": 0.0},
        {"position_id": 3, "direction": 1, "sl": 90.0, "tp": 120.0},
        {"position_id": 4, "direction": 1, "sl": 90.0, "tp": 120.0},
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
        ("trailing", 4),
    ]
    selected_priorities = [
        item["priority"] for item in plan.arbitration if item["decision"] == "selected"
    ]
    assert selected_priorities == [10, 20, 30, 50]
    assert len([item for item in plan.arbitration if item["decision"] == "superseded"]) == 3
    assert all(len(item.fingerprint) == 64 for item in plan.candidates)

    legacy_preview = preview_legacy_safety_candidates(
        positions=positions,
        cfg=SimpleNamespace(),
        account={},
        current_price=110.0,
        atr_price=2.0,
        runtime=_planner_runtime(),
        planned_at=1000.0,
    )
    assert [item.fingerprint for item in legacy_preview.candidates] == [
        item.fingerprint for item in plan.candidates
    ]


class _Bridge:
    def unresolved_execution_intent_count(self):
        return 0


def _loop_runtime(*, plane, plan, legacy_result, calls):
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
        plan_legacy_candidates=lambda **_kwargs: plan,
        execute_safety_candidate=lambda candidate, **_kwargs: (
            calls.append(("v2_execute", candidate)) or {"ok": True, "status": "dispatched"}
        ),
        run_position_protection_cycle=lambda *_args, **_kwargs: (
            calls.append(("legacy", _kwargs)) or legacy_result
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


def test_shadow_compares_independent_plan_to_actual_legacy_arbitration():
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
            legacy_result={
                "safety_candidates": [candidate.__dict__.copy()],
                "safety_arbitration": [],
            },
            calls=calls,
        ),
        reconcile_result=_reconcile(),
    )

    assert len([item for item in calls if item[0] == "legacy"]) == 1
    assert payload["legacy_authoritative"] is True
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
            legacy_result={},
            calls=calls,
        ),
        reconcile_result={**_reconcile(), "positions": []},
    )

    assert payload["comparison"]["independent"] is True
    assert payload["comparison"]["match"] is True
    assert payload["accepting_new_risk"] is True
    assert not [item for item in calls if item[0] in {"legacy", "v2_execute"}]


def test_shadow_mismatch_is_fail_closed_while_legacy_still_executes():
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
            legacy_result={"safety_candidates": [legacy.__dict__.copy()]},
            calls=calls,
        ),
        reconcile_result=_reconcile(),
    )

    assert len([item for item in calls if item[0] == "legacy"]) == 1
    assert payload["comparison"]["independent"] is True
    assert payload["comparison"]["match"] is False
    assert payload["comparison"]["enforce_eligible"] is False
    assert "safety_candidate_mismatch" in payload["blockers"]
    assert payload["accepting_new_risk"] is False


def test_shadow_planner_exception_falls_back_to_legacy_and_stays_fail_closed():
    legacy = safety_candidate(action="close", position_id=7, source="supervisor_close")
    calls = []
    runtime = _loop_runtime(
        plane=LiveSafetyPlane(mode="shadow", clock=lambda: 100.0),
        plan=SafetyPlan(candidates=(), arbitration=(), planned_at=100.0),
        legacy_result={"safety_candidates": [legacy.__dict__.copy()]},
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

    assert len([item for item in calls if item[0] == "legacy"]) == 1
    assert payload["legacy_authoritative"] is True
    assert payload["comparison"]["independent"] is False
    assert payload["comparison"]["enforce_eligible"] is False
    assert "safety_candidate_planner_failed" in payload["blockers"]
    assert payload["accepting_new_risk"] is False


def test_enforce_mismatch_forces_shadow_and_executes_legacy_exactly_once():
    v2 = safety_candidate(action="tighten", position_id=7, source="supervisor_tighten")
    legacy = safety_candidate(action="close", position_id=7, source="supervisor_close")
    calls = []
    runtime = _loop_runtime(
        plane=LiveSafetyPlane(mode="enforce", clock=lambda: 100.0),
        plan=SafetyPlan(candidates=(v2,), arbitration=(), planned_at=100.0),
        legacy_result={},
        calls=calls,
    )
    runtime = LiveSafetyCycleRuntime(
        **{
            **runtime.__dict__,
            "plan_safety_candidates": lambda **_kwargs: (
                calls.append(("v2_plan", {}))
                or SafetyPlan(candidates=(v2,), arbitration=(), planned_at=100.0)
            ),
            "plan_legacy_candidates": lambda **_kwargs: (
                calls.append(("legacy_preview", {}))
                or SafetyPlan(candidates=(legacy,), arbitration=(), planned_at=100.0)
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

    mutation_calls = [item[0] for item in calls if item[0] in {"legacy", "v2_execute"}]
    assert mutation_calls == ["legacy"]
    assert [item[0] for item in calls].index("persist_fail_closed") < [
        item[0] for item in calls
    ].index("legacy")
    assert [item[0] for item in calls].index("v2_plan") < [
        item[0] for item in calls
    ].index("persist_fail_closed")
    assert [item[0] for item in calls].index("legacy_preview") < [
        item[0] for item in calls
    ].index("persist_fail_closed")
    assert payload["status"] == "forced_shadow"
    assert payload["effective_mode"] == "shadow"
    assert payload["forced_shadow"] is True
    assert payload["legacy_authoritative"] is True
    assert payload["legacy_fail_closed_fallback"] is True
    assert payload["comparison"]["independent"] is True
    assert payload["comparison"]["match"] is False
    assert "safety_candidate_mismatch" in payload["blockers"]
    assert "safety_v2_forced_shadow" in payload["blockers"]
    assert payload["accepting_new_risk"] is False


def test_enforce_planner_exception_falls_back_to_legacy_without_v2_mutation():
    candidate = safety_candidate(action="close", position_id=7, source="supervisor_close")
    calls = []
    runtime = _loop_runtime(
        plane=LiveSafetyPlane(mode="enforce", clock=lambda: 100.0),
        plan=SafetyPlan(candidates=(candidate,), arbitration=(), planned_at=100.0),
        legacy_result={},
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

    assert [item[0] for item in calls if item[0] in {"legacy", "v2_execute"}] == [
        "legacy"
    ]
    assert payload["status"] == "forced_shadow"
    assert "safety_candidate_planner_failed" in payload["blockers"]
    assert "safety_v2_forced_shadow" in payload["blockers"]
    assert payload["accepting_new_risk"] is False


def test_enforce_legacy_preview_exception_keeps_legacy_executor_authoritative():
    candidate = safety_candidate(action="close", position_id=7, source="supervisor_close")
    calls = []
    runtime = _loop_runtime(
        plane=LiveSafetyPlane(mode="enforce", clock=lambda: 100.0),
        plan=SafetyPlan(candidates=(candidate,), arbitration=(), planned_at=100.0),
        legacy_result={},
        calls=calls,
    )
    runtime = LiveSafetyCycleRuntime(
        **{
            **runtime.__dict__,
            "plan_legacy_candidates": lambda **_kwargs: (_ for _ in ()).throw(
                RuntimeError("legacy preview unavailable")
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

    assert [item[0] for item in calls if item[0] in {"legacy", "v2_execute"}] == [
        "legacy"
    ]
    assert payload["status"] == "forced_shadow"
    assert "legacy_safety_preview_failed" in payload["blockers"]
    assert "safety_v2_forced_shadow" in payload["blockers"]
    assert payload["accepting_new_risk"] is False


def test_enforce_fallback_persistence_failure_does_not_suppress_legacy_protection():
    v2 = safety_candidate(action="tighten", position_id=7, source="supervisor_tighten")
    legacy = safety_candidate(action="close", position_id=7, source="supervisor_close")
    calls = []
    runtime = _loop_runtime(
        plane=LiveSafetyPlane(mode="enforce", clock=lambda: 100.0),
        plan=SafetyPlan(candidates=(v2,), arbitration=(), planned_at=100.0),
        legacy_result={},
        calls=calls,
    )
    runtime = LiveSafetyCycleRuntime(
        **{
            **runtime.__dict__,
            "plan_legacy_candidates": lambda **_kwargs: SafetyPlan(
                candidates=(legacy,), arbitration=(), planned_at=100.0
            ),
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

    assert [item[0] for item in calls if item[0] in {"legacy", "v2_execute"}] == [
        "legacy"
    ]
    assert "safety_forced_shadow_persistence_failed" in payload["blockers"]
    assert payload["accepting_new_risk"] is False
    assert payload["protection"]["ok"] is True


def test_enforce_forced_shadow_is_sticky_and_never_mixes_authority():
    now = [100.0]
    plane = LiveSafetyPlane(mode="enforce", clock=lambda: now[0])
    v2 = safety_candidate(action="tighten", position_id=7, source="supervisor_tighten")
    legacy = safety_candidate(action="close", position_id=7, source="supervisor_close")
    current_v2 = [v2]
    current_legacy = [legacy]
    calls = []
    runtime = _loop_runtime(
        plane=plane,
        plan=SafetyPlan(candidates=(), arbitration=(), planned_at=100.0),
        legacy_result={"safety_candidates": [legacy.__dict__.copy()]},
        calls=calls,
    )
    runtime = LiveSafetyCycleRuntime(
        **{
            **runtime.__dict__,
            "plan_safety_candidates": lambda **_kwargs: SafetyPlan(
                candidates=tuple(current_v2), arbitration=(), planned_at=now[0]
            ),
            "plan_legacy_candidates": lambda **_kwargs: SafetyPlan(
                candidates=tuple(current_legacy), arbitration=(), planned_at=now[0]
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
    current_v2[:] = [legacy]
    now[0] = 105.0
    second = run_live_safety_cycle(
        bridge=_Bridge(),
        broker="ctrader",
        tick=2,
        log=lambda _message: None,
        runtime=runtime,
        reconcile_result={**_reconcile(), "observed_at": now[0]},
    )

    assert first["forced_shadow"] is True
    assert second["forced_shadow"] is True
    assert second["comparison"]["match"] is True
    assert [item[0] for item in calls if item[0] == "legacy"] == ["legacy", "legacy"]
    assert not [item for item in calls if item[0] == "v2_execute"]


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
            legacy_result={},
            calls=calls,
        ),
        reconcile_result=_reconcile(),
    )

    assert payload["comparison"]["match"] is True
    assert payload["comparison"]["duplicate"] is True
    assert payload["comparison"]["position_conflict"] is True
    assert payload["comparison"]["enforce_eligible"] is False
    assert [item[0] for item in calls if item[0] in {"legacy", "v2_execute"}] == [
        "legacy"
    ]
    assert "safety_candidate_duplicate" in payload["blockers"]
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
    monkeypatch.setattr(live_service, "_generation_controller_enabled", lambda: False)
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


def test_live_service_enforce_mismatch_uses_persisted_legacy_fallback_exactly_once(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("QUANT_SAFETY_STATE_DIR", str(tmp_path))
    v2 = safety_candidate(action="tighten", position_id=7, source="supervisor_tighten")
    legacy = safety_candidate(action="close", position_id=7, source="supervisor_close")
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
        "_preview_legacy_live_safety_candidates",
        lambda **_kwargs: SafetyPlan(
            candidates=(legacy,), arbitration=(), planned_at=100.0
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
        lambda *_args, **_kwargs: calls.append("legacy") or {},
    )

    payload = live_service._run_live_safety_cycle(
        bridge=_Bridge(),
        broker="ctrader",
        tick=1,
        log=lambda _message: None,
        reconcile_result=_reconcile(),
    )

    assert calls == ["legacy"]
    assert payload["forced_shadow"] is True
    assert payload["legacy_fail_closed_fallback"] is True
    assert payload["accepting_new_risk"] is False
    assert safety_v2_forced_shadow_status()["active"] is True

    release_no_new_risk_latch_cause(
        cause="safety_v2_forced_shadow",
        cause_id="candidate_comparison",
        reason="reviewed",
        actor="operator:test",
    )


def test_enforce_match_dispatches_only_v2_candidates_serially():
    candidates = (
        safety_candidate(action="close", position_id=7, source="supervisor_close"),
        safety_candidate(action="trailing", position_id=8, source="legacy_awe_trailing"),
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
            legacy_result={},
            calls=calls,
        ),
        reconcile_result=reconcile,
    )

    assert [item[1].position_id for item in calls if item[0] == "v2_execute"] == [7, 8]
    assert not [item for item in calls if item[0] == "legacy"]
    assert payload["comparison"]["enforce_eligible"] is True
    assert payload["legacy_authoritative"] is False
    assert payload["status"] == "completed"


def test_safety_planner_module_has_no_broker_mutation_or_entry_order_surface():
    for path in (
        "backend/services/live_safety_planner.py",
        "backend/services/live_legacy_safety_preview.py",
    ):
        source = Path(path).read_text(encoding="utf-8")
        for forbidden in (
            "market" + "_buy",
            "market" + "_sell",
            "close_" + "position(",
            "amend_" + "position_sltp(",
        ):
            assert forbidden not in source

    dispatcher = inspect.getsource(live_service._execute_live_safety_candidate)
    for forbidden in ("market" + "_buy", "market" + "_sell", "open_trade"):
        assert forbidden not in dispatcher
