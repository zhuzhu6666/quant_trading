from __future__ import annotations

import json

from backend.services.live_safety_shadow_observation import (
    append_safety_shadow_observation,
    evaluate_safety_shadow_gate,
    read_safety_shadow_observations,
    safety_shadow_gate_status,
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
        "account_updated_at": at,
        "positions_updated_at": at,
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
    observations = [_observation(1000.0 + float(step * 30)) for step in range(2881)]
    result = evaluate_safety_shadow_gate(
        observations,
        required_hours=24,
        max_gap_sec=75,
        now=87400.0,
    )

    assert result["ok"] is True
    assert result["empty_account_window"] is True
    assert result["duration_sec"] == 86400.0


def test_shadow_mismatch_prevents_gate_even_after_duration():
    observations = [_observation(1000.0 + float(step * 30)) for step in range(2881)]
    observations[100] = _observation(4000.0, match=False)
    result = evaluate_safety_shadow_gate(observations, now=87400.0)

    assert result["ok"] is False
    assert "candidate_mismatch" in result["last_reset_reasons"]
    assert "duration_or_lifecycle_incomplete" in result["blockers"]


def test_complete_position_lifecycle_can_satisfy_gate_without_24_hours():
    observations = [
        _observation(1000.0),
        _observation(1030.0, positions=(42,)),
        _observation(1060.0, positions=(42,)),
        _observation(1090.0),
    ]
    result = evaluate_safety_shadow_gate(observations, now=1090.0)

    assert result["ok"] is True
    assert result["complete_lifecycle"] is True
    assert result["completed_position_ids"] == [42]


def test_startup_failure_resets_window_instead_of_poisoning_future_evidence():
    failed = _observation(1000.0)
    failed["reconciliation_state"] = "failed"
    failed["unknown_execution_count"] = 1
    observations = [failed] + [
        _observation(1030.0 + float(step * 30)) for step in range(2881)
    ]

    result = evaluate_safety_shadow_gate(observations, now=87430.0)

    assert result["ok"] is True
    assert result["unsafe_observation_count"] == 1
    assert result["continuous_observation_count"] == 2881


def test_gate_status_is_fail_closed_when_ledger_is_missing(tmp_path):
    result = safety_shadow_gate_status(path=tmp_path / "missing.jsonl", now=1000.0)

    assert result["ok"] is False
    assert result["status"] == "evidence_missing"
    assert result["blockers"] == ["shadow_observation_missing"]


def test_gate_status_reuses_parsed_ledger_until_file_changes(monkeypatch, tmp_path):
    from backend.services import live_safety_shadow_observation as module

    path = tmp_path / "observations.jsonl"
    path.write_text(json.dumps(_observation(1000.0)) + "\n", encoding="utf-8")
    original = module.read_safety_shadow_observations
    calls = []

    def counted(source):
        calls.append(source)
        return original(source)

    monkeypatch.setattr(module, "read_safety_shadow_observations", counted)
    first = safety_shadow_gate_status(path=path, now=1001.0)
    second = safety_shadow_gate_status(path=path, now=1002.0)
    path.write_text(
        path.read_text(encoding="utf-8") + json.dumps(_observation(1030.0)) + "\n",
        encoding="utf-8",
    )
    third = safety_shadow_gate_status(path=path, now=1031.0)

    assert first["observation_count"] == second["observation_count"] == 1
    assert third["observation_count"] == 2
    assert len(calls) == 2
