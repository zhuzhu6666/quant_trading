from pathlib import Path

import pytest

from backend.services.live_safety_plane import LiveSafetyPlane, SafetyCandidate


def _reconcile(*position_ids: int, state: str = "fresh") -> dict:
    return {
        "success": state == "fresh",
        "state": state,
        "reconcile_id": "rec-1",
        "positions": [{"position_id": pid} for pid in position_ids],
    }


def test_reconcile_failure_blocks_new_risk_without_suppressing_future_safety():
    plane = LiveSafetyPlane(mode="enforce", clock=lambda: 100.0)
    executor_calls = []

    result = plane.run_cycle(
        reconcile_result=_reconcile(state="failed"),
        unknown_execution_count=0,
        candidate_provider=lambda _positions: [],
        executor=lambda candidate: executor_calls.append(candidate) or {"ok": True},
    )

    assert result.status == "reconciliation_failed"
    assert result.accepting_new_risk is False
    assert result.blockers == ("positions_reconciliation_failed",)
    assert executor_calls == []


def test_shadow_compares_candidates_but_never_executes():
    plane = LiveSafetyPlane(mode="shadow", clock=lambda: 100.0)
    calls = []
    candidate = SafetyCandidate(action="tighten", position_id=7, reason="trail")

    result = plane.run_cycle(
        reconcile_result=_reconcile(7),
        unknown_execution_count=0,
        candidate_provider=lambda _positions: [candidate],
        executor=lambda item: calls.append(item) or {"ok": True},
        legacy_candidates=[candidate],
    )

    assert result.status == "shadow"
    assert result.comparison["match"] is True
    assert result.executed == ()
    assert calls == []


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

    source = Path("backend/services/live_safety_plane.py").read_text(encoding="utf-8")
    forbidden = "market" + "_buy"
    forbidden_sell = "market" + "_sell"
    assert forbidden not in source
    assert forbidden_sell not in source
