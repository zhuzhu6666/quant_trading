from types import SimpleNamespace

import pytest

from backend.services.live_factor_state import (
    commit_ready_factor_decision,
    resolve_decision_bar_progress,
)


def _decision_frame():
    composite = SimpleNamespace(
        direction=1,
        score=0.7,
        tactical_score=0.6,
        macro_score=0.1,
        alpha_score=0.7,
        n_active_factors=2,
        n_active_alpha_factors=1,
        effective_alpha_factor_count=1,
        n_abstain_factors=0,
        composer_version="factor_roles.v2",
        context_state={},
        context_policy={},
        redundancy_groups={},
        factor_roles={"trend": "alpha", "volatility": "context"},
        active_weights={"trend": 0.5, "volatility": 0.0},
    )
    return SimpleNamespace(
        factor_values={"trend": 2.5, "volatility": 1.2},
        signals={"trend": 0.8, "volatility": -0.2},
        composite=composite,
        gate_result=SimpleNamespace(passed=True, reason="passed"),
    )


def test_resolve_decision_bar_progress_normalizes_and_detects_duplicate():
    progress = resolve_decision_bar_progress({"time": "123.5"}, 123.5)
    malformed = resolve_decision_bar_progress({"time": "bad"}, object())

    assert progress.bar_ts == 123.5
    assert progress.last_processed_ts == 123.5
    assert progress.already_processed is True
    assert malformed.bar_ts == 0.0
    assert malformed.last_processed_ts == 0.0
    assert malformed.already_processed is False


def test_commit_ready_factor_decision_updates_state_and_snapshot_contract():
    frame = _decision_frame()
    pipeline = {}
    state_updates = []
    snapshots = []

    committed = commit_ready_factor_decision(
        decision_frame=frame,
        progress=resolve_decision_bar_progress({"time": 321.0}, 0.0),
        pipeline=pipeline,
        update_live_state=lambda **changes: state_updates.append(changes),
        set_factor_snapshot=lambda votes, summary: snapshots.append((votes, summary)),
        tick=7,
        log=lambda _message: None,
        now=lambda: 999.0,
    )

    assert pipeline["last_factor_values"] == frame.factor_values
    assert pipeline["last_factor_values"] is not frame.factor_values
    assert state_updates == [{"last_processed_decision_bar_ts": 321.0}]
    votes, summary = snapshots[0]
    assert votes["trend"] == {
        "signal": 0.8,
        "raw": 2.5,
        "direction": 1,
        "role": "alpha",
        "used_in_score": True,
        "available": True,
        "abstained": False,
    }
    assert votes["volatility"]["direction"] == 0
    assert summary["ts"] == 999.0
    assert summary["gate_reason"] == "passed"
    assert committed.factor_values is frame.factor_values
    assert committed.composite is frame.composite
    assert committed.gate_result is frame.gate_result


def test_commit_ready_factor_decision_keeps_snapshot_failure_non_fatal():
    frame = _decision_frame()
    pipeline = {}
    state_updates = []
    logs = []

    def _fail_snapshot(_votes, _summary):
        raise RuntimeError("snapshot unavailable")

    committed = commit_ready_factor_decision(
        decision_frame=frame,
        progress=resolve_decision_bar_progress({"time": 654.0}, 0.0),
        pipeline=pipeline,
        update_live_state=lambda **changes: state_updates.append(changes),
        set_factor_snapshot=_fail_snapshot,
        tick=9,
        log=logs.append,
        now=lambda: 1000.0,
    )

    assert committed.signals is frame.signals
    assert pipeline["last_factor_values"] == frame.factor_values
    assert state_updates == [{"last_processed_decision_bar_ts": 654.0}]
    assert logs == [
        "tick 9: factor votes save failed (non-fatal): snapshot unavailable"
    ]


def test_commit_ready_factor_decision_propagates_state_update_failure_before_snapshot():
    frame = _decision_frame()
    pipeline = {}
    snapshots = []

    def _fail_state_update(**_changes):
        raise RuntimeError("state unavailable")

    with pytest.raises(RuntimeError, match="state unavailable"):
        commit_ready_factor_decision(
            decision_frame=frame,
            progress=resolve_decision_bar_progress({"time": 777.0}, 0.0),
            pipeline=pipeline,
            update_live_state=_fail_state_update,
            set_factor_snapshot=lambda votes, summary: snapshots.append((votes, summary)),
            tick=10,
            log=lambda _message: None,
        )

    assert pipeline["last_factor_values"] == frame.factor_values
    assert snapshots == []


def test_commit_ready_factor_decision_with_zero_bar_ts_skips_state_update():
    frame = _decision_frame()
    pipeline = {}
    state_updates = []
    snapshots = []

    commit_ready_factor_decision(
        decision_frame=frame,
        progress=resolve_decision_bar_progress({"time": 0.0}, 12.0),
        pipeline=pipeline,
        update_live_state=lambda **changes: state_updates.append(changes),
        set_factor_snapshot=lambda votes, summary: snapshots.append((votes, summary)),
        tick=11,
        log=lambda _message: None,
        now=lambda: 1001.0,
    )

    assert state_updates == []
    assert len(snapshots) == 1
    assert snapshots[0][1]["ts"] == 1001.0
