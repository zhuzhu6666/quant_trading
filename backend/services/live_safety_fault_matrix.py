"""Durable, code-bound evidence for the Safety v2 fault-injection matrix."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "live_safety_fault_matrix.v1"
REQUIRED_SCENARIOS: dict[str, tuple[str, ...]] = {
    "closed_bar_factor_circuit": (
        "tests/test_live_safety_plane.py::test_full_cycle_cadence_and_alpha_require_new_closed_bar",
        "tests/test_live_factor_bootstrap.py::test_primary_factor_initialization_failure_returns_no_alpha_pipeline",
        "tests/test_live_generation_integration.py::test_phase2_circuit_blocks_alpha_only_after_safety",
    ),
    "postgres_loss_reduction_continues": (
        "tests/test_live_open_admission.py::test_runtime_postgres_failure_latches_no_new_risk_and_skips_open_rpc",
        "tests/test_live_risk_reduction.py::test_close_context_postgres_failure_records_outbox_and_continues",
    ),
    "reconcile_failure": (
        "tests/test_live_generation_integration.py::test_failed_position_reconcile_blocks_open_but_cached_position_protection_continues",
    ),
    "spot_stale": (
        "tests/test_live_open_admission.py::test_final_open_admission_fails_closed_for_each_authority",
    ),
    "order_timeout_delayed_unknown": (
        "tests/test_ctrader_execution_outcome.py::test_timeout_does_not_guess_existing_same_direction_position",
        "tests/test_live_execution_recovery_gate.py::test_loop_recovers_delayed_fill_and_runs_safety_before_alpha",
        "tests/test_ctrader_execution_outcome.py::test_v2_unknown_protobuf_with_position_shaped_fields_is_not_a_broker_receipt",
    ),
    "amend_projection_ack": (
        "tests/test_ctrader_execution_outcome.py::test_amend_v2_requires_fresh_sltp_projection_ack",
    ),
    "emergency_reconcile_and_audit": (
        "tests/test_live_emergency_safety.py::test_emergency_requires_fresh_pre_reconcile",
        "tests/test_live_emergency_safety.py::test_emergency_post_reconcile_failure_never_reports_success",
        "tests/test_live_emergency_safety.py::test_emergency_pg_and_audit_failures_do_not_change_broker_result",
    ),
    "stop_open_rpc_draining": (
        "tests/test_live_generation_integration.py::test_stop_waits_for_admitted_open_rpc_then_keeps_generation_draining",
    ),
    "session_cache_missing": (
        "tests/test_live_generation_integration.py::test_session_restore_queries_deals_even_when_runtime_cache_is_missing",
    ),
    "partial_close_remains_open": (
        "tests/test_live_session_restore.py::test_partial_close_legs_aggregate_by_position_and_open_position_is_excluded",
        "tests/test_ctrader_execution_outcome.py::test_recovery_requires_fresh_expected_partial_close_volume",
    ),
    "safety_outbox_failure": (
        "tests/test_live_emergency_safety.py::test_latch_and_outbox_persistence_failure_still_allows_emergency_close",
        "tests/test_live_risk_reduction.py::test_safety_outbox_failure_never_changes_risk_reduction_result",
    ),
    "safety_heartbeat_stale": (
        "tests/test_live_loop_controller.py::test_stale_safety_heartbeat_degrades_generation_and_blocks_new_risk",
    ),
}


def repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def fault_matrix_path() -> Path:
    configured = os.getenv("QUANT_SAFETY_FAULT_MATRIX_PATH", "").strip()
    return Path(configured) if configured else (
        repository_root() / "data/safety/safety_fault_matrix_attestations.jsonl"
    )


def required_nodeids() -> tuple[str, ...]:
    return tuple(nodeid for values in REQUIRED_SCENARIOS.values() for nodeid in values)


def binding_paths(*, root: Path | None = None) -> tuple[Path, ...]:
    root = root or repository_root()
    paths = set((root / "backend/services").glob("live*.py"))
    paths.update(
        {
            root / "execution/ctrader_bridge.py",
            root / "execution/broker_contract.py",
            root / "risk/runtime_policy.py",
            Path(__file__).resolve(),
        }
    )
    paths.update(root / nodeid.split("::", 1)[0] for nodeid in required_nodeids())
    return tuple(sorted(path for path in paths if path.is_file()))


def binding_hash(*, root: Path | None = None, paths: Sequence[Path] | None = None) -> str:
    root = (root or repository_root()).resolve()
    selected = tuple(paths or binding_paths(root=root))
    digest = hashlib.sha256()
    for path in sorted(selected):
        resolved = path.resolve()
        try:
            label = resolved.relative_to(root).as_posix()
        except ValueError:
            label = resolved.as_posix()
        digest.update(label.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(resolved.read_bytes()).digest())
        digest.update(b"\0")
    return digest.hexdigest()


def _record_hash(record: Mapping[str, Any]) -> str:
    payload = {key: value for key, value in record.items() if key != "record_hash"}
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def append_fault_matrix_attestation(
    record: Mapping[str, Any],
    *,
    path: Path | None = None,
) -> dict[str, Any]:
    target = path or fault_matrix_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(record)
    payload["record_hash"] = _record_hash(payload)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
    with target.open("a", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    return payload


def run_fault_matrix(
    *,
    root: Path | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    root = (root or repository_root()).resolve()
    started_at = time.time()
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *required_nodeids()],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
    record = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": time.time(),
        "duration_sec": time.time() - started_at,
        "status": "passed" if result.returncode == 0 else "failed",
        "pytest_exit_code": int(result.returncode),
        "scenario_names": sorted(REQUIRED_SCENARIOS),
        "nodeids": list(required_nodeids()),
        "binding_hash": binding_hash(root=root),
        "output_tail": output[-4000:],
    }
    return append_fault_matrix_attestation(record, path=path)


def fault_matrix_status(
    *,
    root: Path | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    root = (root or repository_root()).resolve()
    target = path or fault_matrix_path()
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {
            "ok": False,
            "status": "missing",
            "blockers": ["fault_matrix_attestation_missing"],
            "path": str(target),
        }
    latest: dict[str, Any] = {}
    latest_line = next((line for line in reversed(lines) if line.strip()), "")
    if latest_line:
        try:
            candidate = json.loads(latest_line)
        except (TypeError, ValueError, json.JSONDecodeError):
            candidate = None
        if isinstance(candidate, dict):
            latest = candidate
    blockers: list[str] = []
    if not latest:
        blockers.append("fault_matrix_attestation_unreadable")
    elif latest.get("record_hash") != _record_hash(latest):
        blockers.append("fault_matrix_attestation_hash_invalid")
    else:
        if latest.get("schema_version") != SCHEMA_VERSION:
            blockers.append("fault_matrix_attestation_schema_invalid")
        try:
            pytest_exit_code = int(latest.get("pytest_exit_code", -1))
        except (TypeError, ValueError):
            pytest_exit_code = -1
        if latest.get("status") != "passed" or pytest_exit_code != 0:
            blockers.append("fault_matrix_not_passed")
        if set(latest.get("scenario_names") or []) != set(REQUIRED_SCENARIOS):
            blockers.append("fault_matrix_scenarios_incomplete")
        if tuple(latest.get("nodeids") or ()) != required_nodeids():
            blockers.append("fault_matrix_nodeids_incomplete")
        try:
            current_binding_hash = binding_hash(root=root)
        except Exception:
            current_binding_hash = ""
            blockers.append("fault_matrix_code_binding_unavailable")
        if str(latest.get("binding_hash") or "") != current_binding_hash:
            blockers.append("fault_matrix_code_binding_stale")
    blockers = sorted(set(blockers))
    return {
        "ok": not blockers,
        "status": "passed" if not blockers else "blocked",
        "blockers": blockers,
        "path": str(target),
        "generated_at": latest.get("generated_at"),
        "duration_sec": latest.get("duration_sec"),
        "binding_hash": latest.get("binding_hash"),
        "scenario_count": len(latest.get("scenario_names") or []),
        "pytest_exit_code": latest.get("pytest_exit_code"),
    }
