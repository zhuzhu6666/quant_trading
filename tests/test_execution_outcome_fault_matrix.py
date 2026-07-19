from __future__ import annotations

from backend.services.execution_outcome_fault_matrix import (
    REQUIRED_SCENARIOS,
    SCHEMA_VERSION,
    append_fault_matrix_attestation,
    binding_hash,
    fault_matrix_status,
    repository_root,
    required_nodeids,
)


def _record(**patch):
    value = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": 1000.0,
        "duration_sec": 2.0,
        "status": "passed",
        "pytest_exit_code": 0,
        "scenario_names": sorted(REQUIRED_SCENARIOS),
        "nodeids": list(required_nodeids()),
        "binding_hash": binding_hash(root=repository_root()),
        "output_tail": "passed",
    }
    value.update(patch)
    return value


def test_execution_fault_matrix_requires_durable_attestation(tmp_path):
    result = fault_matrix_status(
        root=repository_root(),
        path=tmp_path / "missing.jsonl",
    )

    assert result["ok"] is False
    assert result["blockers"] == ["execution_fault_matrix_attestation_missing"]


def test_execution_fault_matrix_accepts_current_code_bound_pass(tmp_path):
    path = tmp_path / "faults.jsonl"
    appended = append_fault_matrix_attestation(_record(), path=path)

    result = fault_matrix_status(root=repository_root(), path=path)

    assert result["ok"] is True
    assert result["scenario_count"] == len(REQUIRED_SCENARIOS)
    assert appended["record_hash"]


def test_execution_fault_matrix_rejects_stale_binding(tmp_path):
    path = tmp_path / "faults.jsonl"
    append_fault_matrix_attestation(_record(binding_hash="stale"), path=path)

    result = fault_matrix_status(root=repository_root(), path=path)

    assert result["ok"] is False
    assert result["blockers"] == ["execution_fault_matrix_code_binding_stale"]


def test_latest_failed_execution_fault_matrix_supersedes_pass(tmp_path):
    path = tmp_path / "faults.jsonl"
    append_fault_matrix_attestation(_record(), path=path)
    append_fault_matrix_attestation(
        _record(status="failed", pytest_exit_code=1),
        path=path,
    )

    result = fault_matrix_status(root=repository_root(), path=path)

    assert result["ok"] is False
    assert result["blockers"] == ["execution_fault_matrix_not_passed"]


def test_malformed_latest_execution_attestation_is_fail_closed(tmp_path):
    path = tmp_path / "faults.jsonl"
    append_fault_matrix_attestation(_record(), path=path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{partial\n")

    result = fault_matrix_status(root=repository_root(), path=path)

    assert result["ok"] is False
    assert result["blockers"] == ["execution_fault_matrix_attestation_unreadable"]
