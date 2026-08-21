from pathlib import Path

import pytest

from backend.services.live_safety_plane import LiveSafetyPlane, SafetyCandidate


def _reconcile(*position_ids: int, state: str = "fresh") -> dict:
    return {
        "success": state == "fresh",
        "state": state,
        "reconcile_id": "rec-1",
        "observed_at": 100.0,
        "positions": [{"position_id": pid} for pid in position_ids],
    }


def test_reconcile_failure_blocks_new_risk_without_suppressing_future_safety():
    plane = LiveSafetyPlane(mode="enforce", clock=lambda: 100.0)
    executor_calls = []

    result = plane.run_cycle(
        reconcile_result={
            "success": False,
            "state": "failed",
            "reconcile_id": "rec-failed",
            "positions": [{"position_id": 7}],
        },
        unknown_execution_count=0,
        candidate_provider=lambda _positions: [SafetyCandidate(action="close", position_id=7)],
        executor=lambda candidate: executor_calls.append(candidate) or {"ok": True},
    )

    assert result.status == "reconciliation_failed"
    assert result.accepting_new_risk is False
    assert result.blockers == ("positions_reconciliation_failed",)
    assert [item.position_id for item in executor_calls] == [7]


@pytest.mark.parametrize("state", ["cache", "event", "unknown"])
def test_non_fresh_reconcile_never_authorizes_safety_or_new_risk(state):
    plane = LiveSafetyPlane(mode="enforce", clock=lambda: 100.0)
    result = plane.run_cycle(
        reconcile_result={
            "success": True,
            "state": state,
            "reconcile_id": "rec-stale",
            "positions": [],
        },
        unknown_execution_count=0,
        candidate_provider=lambda _positions: [],
        executor=lambda _candidate: {"ok": True},
    )

    assert result.status == "reconciliation_failed"
    assert result.accepting_new_risk is False


@pytest.mark.parametrize(
    ("reconcile_state", "observed_at"),
    [
        ("fresh", 0.0),
        ("fresh", 79.9),
        ("fresh", "invalid"),
        (None, 100.0),
    ],
)
def test_incomplete_fresh_contract_remains_fail_closed(reconcile_state, observed_at):
    plane = LiveSafetyPlane(mode="enforce", clock=lambda: 100.0)
    reconcile = {
        "success": True,
        "reconcile_id": "rec-unproven",
        "observed_at": observed_at,
        "positions": [],
    }
    if reconcile_state is not None:
        reconcile["state"] = reconcile_state
    result = plane.run_cycle(
        reconcile_result=reconcile,
        unknown_execution_count=0,
        candidate_provider=lambda _positions: [],
        executor=lambda _candidate: {"ok": True},
    )

    assert result.status == "reconciliation_failed"
    assert result.accepting_new_risk is False
    assert result.blockers == ("positions_reconciliation_failed",)


def test_shadow_compares_candidates_but_never_executes():
    plane = LiveSafetyPlane(mode="shadow", clock=lambda: 100.0)
    calls = []
    candidate = SafetyCandidate(action="tighten", position_id=7, reason="trail")

    result = plane.run_cycle(
        reconcile_result=_reconcile(7),
        unknown_execution_count=0,
        candidate_provider=lambda _positions: [candidate],
        executor=lambda item: calls.append(item) or {"ok": True},
    )

    assert result.status == "shadow"
    assert result.comparison["match"] is True
    assert result.executed == ()
    assert calls == []


def test_shadow_mismatch_remains_fail_closed_during_heartbeat_interval():
    now = [100.0]
    plane = LiveSafetyPlane(mode="shadow", clock=lambda: now[0])
    v2 = SafetyCandidate(action="tighten", position_id=7, fingerprint="v2")
    first = plane.run_cycle(
        reconcile_result=_reconcile(7),
        unknown_execution_count=0,
        candidate_provider=lambda _positions: [v2, v2],
        executor=lambda _candidate: {"ok": True},
    )
    now[0] = 102.0
    heartbeat = plane.run_cycle(
        reconcile_result=_reconcile(7),
        unknown_execution_count=0,
        candidate_provider=lambda _positions: (_ for _ in ()).throw(
            AssertionError("heartbeat must not replan")
        ),
        executor=lambda _candidate: {"ok": True},
    )

    assert first.accepting_new_risk is False
    assert heartbeat.status == "heartbeat"
    assert heartbeat.comparison["match"] is True
    assert heartbeat.comparison["duplicate"] is True
    assert "safety_candidate_duplicate" in heartbeat.blockers
    assert heartbeat.accepting_new_risk is False


def test_enforce_comparison_error_forces_shadow_before_executor():
    plane = LiveSafetyPlane(mode="enforce", clock=lambda: 100.0)
    calls = []

    result = plane.run_cycle(
        reconcile_result=_reconcile(7),
        unknown_execution_count=0,
        candidate_provider=lambda _positions: [
            {"action": "market_buy", "position_id": 7}
        ],
        executor=lambda candidate: calls.append(candidate) or {"ok": True},
    )

    assert calls == []
    assert result.status == "forced_shadow"
    assert result.effective_mode == "shadow"
    assert result.forced_shadow is True
    assert "safety_candidate_comparison_error" in result.blockers
    assert "safety_v2_forced_shadow" in result.blockers
    assert result.accepting_new_risk is False


def test_enforce_executes_only_risk_reducing_candidates_and_reports_partial():
    plane = LiveSafetyPlane(mode="enforce", clock=lambda: 100.0)

    result = plane.run_cycle(
        reconcile_result=_reconcile(7, 8),
        unknown_execution_count=0,
        candidate_provider=lambda _positions: [
            SafetyCandidate(action="close", position_id=7),
            SafetyCandidate(action="repair_entry_protection", position_id=8),
        ],
        executor=lambda item: {"ok": item.position_id == 7},
    )

    assert result.status == "partial"
    assert result.accepting_new_risk is False
    assert len(result.executed) == 2
    assert result.blockers == ("safety_action_failed",)


def test_unknown_execution_blocks_new_risk_while_safety_actions_continue():
    plane = LiveSafetyPlane(mode="enforce", clock=lambda: 100.0)
    calls = []

    result = plane.run_cycle(
        reconcile_result=_reconcile(7),
        unknown_execution_count=1,
        candidate_provider=lambda _positions: [SafetyCandidate(action="tighten", position_id=7)],
        executor=lambda item: calls.append(item) or {"ok": True},
    )

    assert len(calls) == 1
    assert result.status == "completed"
    assert result.accepting_new_risk is False
    assert result.blockers == ("unknown_execution",)


def test_full_cycle_cadence_and_alpha_require_new_closed_bar():
    now = [100.0]
    plane = LiveSafetyPlane(mode="shadow", clock=lambda: now[0])
    assert plane.full_cycle_due(has_positions=True, unknown_execution_count=0) is True
    plane.run_cycle(
        reconcile_result=_reconcile(7),
        unknown_execution_count=0,
        candidate_provider=lambda _positions: [],
        executor=lambda _item: {"ok": True},
    )
    now[0] = 104.0
    assert plane.full_cycle_due(has_positions=True, unknown_execution_count=0) is False
    now[0] = 105.0
    assert plane.full_cycle_due(has_positions=True, unknown_execution_count=0) is True

    assert plane.alpha_due(closed_bar_id="bar-1") is True
    plane.mark_alpha_run(closed_bar_id="bar-1")
    now[0] = 200.0
    assert plane.alpha_due(closed_bar_id="bar-1") is False
    assert plane.alpha_due(closed_bar_id="bar-2") is True


def test_safety_plane_rejects_entry_order_actions_and_has_no_entry_api_symbols():
    with pytest.raises(ValueError, match="unsafe_safety_plane_action"):
        SafetyCandidate(action="market_buy", position_id=7)
    with pytest.raises(ValueError, match="unsafe_safety_plane_action"):
        SafetyCandidate(action="emergency_close", position_id=7)

    forbidden_symbols = {
        "market" + "_buy",
        "market" + "_sell",
        "_send_market_order",
        "_submit_open_trade_order",
    }
    for relative_path in (
        "backend/services/live_safety_plane.py",
        "backend/services/live_safety_planner.py",
        "backend/services/live_emergency.py",
        "backend/services/live_loop_v2.py",
    ):
        source = Path(relative_path).read_text(encoding="utf-8")
        assert forbidden_symbols.isdisjoint(source.split())
        for symbol in forbidden_symbols:
            assert symbol not in source
