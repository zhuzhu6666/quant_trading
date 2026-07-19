from __future__ import annotations

import json

from backend.services.live_safety_shadow_observation import (
    append_safety_shadow_observation,
    evaluate_safety_shadow_gate,
    read_safety_shadow_observations,
    safety_shadow_observation_path,
)


def _observation(at: float, *, positions=(), match: bool = True) -> dict:
    return {
        "schema_version": "live_safety_shadow_observation.v1",
        "observed_at": at,
        "mode": "shadow",
        "effective_mode": "shadow",
        "status": "shadow",
        "reconciliation_state": "fresh",
        "position_ids": list(positions),
        "unknown_execution_count": 0,
        "forced_shadow": False,
        "comparison": {
            "independent": True,
            "match": match,
            "enforce_eligible": match,
            "duplicate": False,
            "position_conflict": False,
            "actual_recorded": bool(positions),
        },
    }


def test_append_shadow_observation_is_minimal_and_durable(monkeypatch, tmp_path):
    monkeypatch.setenv("QUANT_SAFETY_STATE_DIR", str(tmp_path))
    record = append_safety_shadow_observation(
        payload={
            **_observation(1.0),
            "heartbeat_at": 1.0,
            "reconcile_id": "rec-1",
            "candidates": [],
            "executed": [],
            "blockers": [],
        },
        generation_id="generation-1",
        broker="ctrader",
        tick=7,
    )

    assert record["mode"] == "shadow"
    assert record["reconcile_id"] == "rec-1"
    assert json.loads(safety_shadow_observation_path().read_text().strip())["tick"] == 7
    assert len(read_safety_shadow_observations()) == 1


def test_empty_account_gate_requires_continuous_24_hours():
    observations = [_observation(float(step * 30)) for step in range(2881)]
    result = evaluate_safety_shadow_gate(
        observations,
        required_hours=24,
        max_gap_sec=75,
        now=86400.0,
    )

    assert result["ok"] is True
    assert result["empty_account_window"] is True
    assert result["duration_sec"] == 86400.0


def test_shadow_mismatch_prevents_gate_even_after_duration():
    observations = [_observation(float(step * 30)) for step in range(2881)]
    observations[100] = _observation(3000.0, match=False)
    result = evaluate_safety_shadow_gate(observations, now=86400.0)

    assert result["ok"] is False
    assert "candidate_mismatch" in result["blockers"]


def test_complete_position_lifecycle_can_satisfy_gate_without_24_hours():
    observations = [
        _observation(0.0),
        _observation(30.0, positions=(42,)),
        _observation(60.0, positions=(42,)),
        _observation(90.0),
    ]
    result = evaluate_safety_shadow_gate(observations, now=90.0)

    assert result["ok"] is True
    assert result["complete_lifecycle"] is True
    assert result["completed_position_ids"] == [42]
