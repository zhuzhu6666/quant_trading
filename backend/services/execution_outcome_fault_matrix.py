"""Durable, code-bound evidence for the broker execution outcome v2 matrix."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "execution_outcome_fault_matrix.v1"
REQUIRED_SCENARIOS: dict[str, tuple[str, ...]] = {
    "rpc_timeout_unknown": (
        "tests/test_ctrader_execution_outcome.py::test_timeout_does_not_guess_existing_same_direction_position",
    ),
    "delayed_receipt_recovery": (
        "tests/test_live_execution_recovery_gate.py::test_loop_recovers_delayed_fill_and_runs_safety_before_alpha",
        "tests/test_ctrader_execution_outcome.py::test_recovery_uses_client_order_identity_to_resolve_one_of_multiple_positions",
    ),
    "unknown_protobuf": (
        "tests/test_ctrader_execution_outcome.py::test_v2_unknown_protobuf_with_position_shaped_fields_is_not_a_broker_receipt",
    ),
    "amend_projection_not_applied": (
        "tests/test_ctrader_execution_outcome.py::test_amend_v2_requires_fresh_sltp_projection_ack",
    ),
    "restart_duplicate_prevention": (
        "tests/test_ctrader_execution_outcome.py::test_durable_unknown_close_blocks_resend_after_bridge_restart",
        "tests/test_ctrader_execution_outcome.py::test_pg_recovery_appends_explicit_local_unknown_resolution",
    ),
    "intent_commit_and_recovery_boundary": (
        "tests/test_ctrader_execution_outcome.py::test_market_order_persists_prepared_and_submitting_before_rpc_and_confirms_unique_diff",
        "tests/test_ctrader_execution_outcome.py::test_recovery_rejects_prepared_intent_that_never_reached_submitting",
    ),
    "risk_reduction_pg_independent": (
        "tests/test_ctrader_execution_outcome.py::test_close_v2_pg_intent_failure_does_not_block_risk_reduction",
        "tests/test_ctrader_execution_outcome.py::test_amend_v2_pg_intent_failure_still_confirms_fresh_broker_projection",
    ),
    "confirmed_open_post_fill_fail_closed": (
        "tests/test_live_open_entry_protection_barrier.py::test_submit_contains_confirmed_open_post_fill_exception",
        "tests/test_live_open_submission.py::test_confirmed_open_post_fill_and_reconcile_failure_stays_fail_closed",
    ),
}


def repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def fault_matrix_path() -> Path:
    configured = os.getenv("QUANT_EXECUTION_OUTCOME_FAULT_MATRIX_PATH", "").strip()
    return Path(configured) if configured else (
        repository_root()
        / "data/safety/execution_outcome_fault_matrix_attestations.jsonl"
    )


def required_nodeids() -> tuple[str, ...]:
    return tuple(nodeid for values in REQUIRED_SCENARIOS.values() for nodeid in values)


def binding_paths(*, root: Path | None = None) -> tuple[Path, ...]:
    root = root or repository_root()
    paths = {
        root / "execution/ctrader_bridge.py",
        root / "execution/broker_contract.py",
        root / "backend/services/broker_execution_intent.py",
        root / "backend/services/live_execution_recovery.py",
        root / "backend/services/live_open_submission.py",
        root / "backend/services/live_safety_state.py",
        root / "backend/services/live_service.py",
        root / "backend/services/phased_repair_release_gate.py",
        root / "scripts/execution_outcome_fault_matrix.py",
        Path(__file__).resolve(),
    }
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
            "blockers": ["execution_fault_matrix_attestation_missing"],
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
        blockers.append("execution_fault_matrix_attestation_unreadable")
    elif latest.get("record_hash") != _record_hash(latest):
        blockers.append("execution_fault_matrix_attestation_hash_invalid")
    else:
        if latest.get("schema_version") != SCHEMA_VERSION:
            blockers.append("execution_fault_matrix_attestation_schema_invalid")
        try:
            pytest_exit_code = int(latest.get("pytest_exit_code", -1))
        except (TypeError, ValueError):
            pytest_exit_code = -1
        if latest.get("status") != "passed" or pytest_exit_code != 0:
            blockers.append("execution_fault_matrix_not_passed")
        if set(latest.get("scenario_names") or []) != set(REQUIRED_SCENARIOS):
            blockers.append("execution_fault_matrix_scenarios_incomplete")
        if tuple(latest.get("nodeids") or ()) != required_nodeids():
            blockers.append("execution_fault_matrix_nodeids_incomplete")
        try:
            current_binding_hash = binding_hash(root=root)
        except Exception:
            current_binding_hash = ""
            blockers.append("execution_fault_matrix_code_binding_unavailable")
        if str(latest.get("binding_hash") or "") != current_binding_hash:
            blockers.append("execution_fault_matrix_code_binding_stale")
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
