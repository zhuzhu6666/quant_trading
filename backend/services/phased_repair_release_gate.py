"""Read-only release preflight for the canonical live/governance path."""
from __future__ import annotations

import json
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping

from backend.core.release_identity import collect_release_identity

SCHEMA_VERSION = "phased_repair_release_preflight.v1"
REQUIRED_SERVICES = ("quant-backend.service", "quant-learning-worker.service")
JOB_WORKER_SERVICE = "quant-job-worker.service"
BACKEND_SERVICE = "quant-backend.service"
REPO_ROOT = Path(__file__).resolve().parents[2]
RELEASE_PREFLIGHT_SCHEMA_VERSION = "release_preflight.v1"
TARGET_EXPECTED_FLAGS = {
    "supervisor_enforce": {
        "live_safety_plane_v2_mode": "enforce",
        "governance_mutation_coordinator_v2_mode": "dual_record",
        "pg_job_queue_v2_enabled": False,
    },
    "governance_enforce": {
        "live_safety_plane_v2_mode": "enforce",
        "governance_mutation_coordinator_v2_mode": "dual_record",
        "pg_job_queue_v2_enabled": False,
    },
    "pg_job_queue_enable": {
        "live_safety_plane_v2_mode": "enforce",
        "governance_mutation_coordinator_v2_mode": "enforce",
        "pg_job_queue_v2_enabled": False,
    },
    "pg_job_queue_verify": {
        "live_safety_plane_v2_mode": "enforce",
        "governance_mutation_coordinator_v2_mode": "enforce",
        "pg_job_queue_v2_enabled": True,
    },
}


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_zero_count(value: Any) -> bool:
    try:
        return value is not None and int(value) == 0
    except (TypeError, ValueError):
        return False


def _process_static_flags_check(
    raw: Any,
    *,
    expected_flags: Mapping[str, Any],
    required: bool,
) -> dict[str, Any]:
    projection = dict(raw or {}) if isinstance(raw, Mapping) else {}
    values = (
        dict(projection.get("values") or {})
        if isinstance(projection.get("values"), Mapping)
        else {}
    )
    from backend.core.static_feature_flags import static_feature_flags_fingerprint

    try:
        process_pid = int(projection.get("pid") or 0)
    except (TypeError, ValueError):
        process_pid = 0
    ok = bool(
        not required
        or (
            projection.get("schema_version") == "static_feature_flags.v1"
            and values == dict(expected_flags)
            and str(projection.get("fingerprint") or "")
            == static_feature_flags_fingerprint(values)
            and process_pid > 0
            and (_float_or_none(projection.get("process_started_at")) or 0.0) > 0.0
        )
    )
    return {
        "ok": ok,
        "required_for_target": required,
        "values": values,
        "fingerprint": projection.get("fingerprint"),
        "pid": projection.get("pid"),
        "process_started_at": projection.get("process_started_at"),
    }


def evaluate_phased_release_preflight(
    *,
    target: str,
    shadow_gate: Mapping[str, Any],
    flags: Mapping[str, Any],
    service_states: Mapping[str, str],
    latch_status: Mapping[str, Any],
    local_unknown_count: int | None,
    postgres_unknown_count: int | None,
    readiness_snapshot: Mapping[str, Any],
    job_worker_preflight: Mapping[str, Any] | None = None,
    governance_preflight: Mapping[str, Any] | None = None,
    safety_fault_matrix: Mapping[str, Any] | None = None,
    execution_fault_matrix: Mapping[str, Any] | None = None,
    job_worker_capability: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate authoritative facts before one staged static-flag transition."""

    blockers: list[str] = []
    if target not in TARGET_EXPECTED_FLAGS:
        return {
            "schema_version": SCHEMA_VERSION,
            "target": str(target or ""),
            "ok": False,
            "status": "blocked",
            "blockers": ["release_target_unknown"],
            "checks": {},
        }
    expected_flags = TARGET_EXPECTED_FLAGS[target]
    flag_mismatches = {
        key: {"expected": expected, "actual": flags.get(key)}
        for key, expected in expected_flags.items()
        if flags.get(key) != expected
    }
    if flag_mismatches:
        blockers.append("static_rollout_flags_unexpected")
    # Shadow continuity was a pre-cutover observation gate.  The canonical
    # supervisor path is already the active authority, so no target may
    # reintroduce a shadow/legacy transition.
    shadow_gate_required = False
    if shadow_gate_required and not bool(shadow_gate.get("ok")):
        blockers.append("safety_shadow_gate_incomplete")
    safety_fault_matrix_required = bool(
        shadow_gate_required and not shadow_gate.get("complete_lifecycle")
    )
    safety_fault_matrix_payload = (
        dict(safety_fault_matrix or {})
        if isinstance(safety_fault_matrix, Mapping)
        else {}
    )
    if (
        safety_fault_matrix_required
        and safety_fault_matrix_payload.get("ok") is not True
    ):
        blockers.append("safety_fault_matrix_incomplete")
    # Broker execution outcome is no longer a staged release target.  It is an
    # always-on prerequisite of the single mutation chain.
    execution_fault_matrix_required = False
    execution_fault_matrix_payload = (
        dict(execution_fault_matrix or {})
        if isinstance(execution_fault_matrix, Mapping)
        else {}
    )
    required_services = REQUIRED_SERVICES + (
        (JOB_WORKER_SERVICE,) if target == "pg_job_queue_verify" else ()
    )
    inactive_services = sorted(
        name for name in required_services if service_states.get(name) != "active"
    )
    if inactive_services:
        blockers.append("required_service_inactive")
    if bool(latch_status.get("active")) or str(latch_status.get("state") or "") == "error":
        blockers.append("no_new_risk_latch_active_or_unknown")
    if not _is_zero_count(local_unknown_count):
        blockers.append("local_execution_intent_unresolved_or_unknown")
    if not _is_zero_count(postgres_unknown_count):
        blockers.append("postgres_execution_intent_unresolved_or_unknown")

    readiness_payload = (
        dict(readiness_snapshot.get("payload") or {})
        if isinstance(readiness_snapshot.get("payload"), Mapping)
        else {}
    )
    readiness_age = _float_or_none(readiness_snapshot.get("age_seconds"))
    worker = (
        dict(readiness_payload.get("learning_worker") or {})
        if isinstance(readiness_payload.get("learning_worker"), Mapping)
        else {}
    )
    mutation_capability = (
        dict(worker.get("mutation_capability") or {})
        if isinstance(worker.get("mutation_capability"), Mapping)
        else {}
    )
    readiness_ok = bool(
        readiness_snapshot.get("ok")
        and readiness_age is not None
        and 0.0 <= readiness_age <= 180.0
        and readiness_payload.get("ready_for_release") is True
        and readiness_payload.get("ready_for_autonomous_mutation") is True
        and worker.get("fresh") is True
        and worker.get("config_hash_match") is True
        and worker.get("overlay_hash_match") is True
        and mutation_capability.get("status") == "available"
    )
    if not readiness_ok:
        blockers.append("release_readiness_unavailable_or_divergent")

    process_flags_required = True
    snapshot_meta = (
        dict(readiness_payload.get("snapshot") or {})
        if isinstance(readiness_payload.get("snapshot"), Mapping)
        else {}
    )
    backend_process_flags = _process_static_flags_check(
        snapshot_meta.get("process_static_feature_flags"),
        expected_flags=flags,
        required=process_flags_required,
    )
    if not backend_process_flags["ok"]:
        blockers.append("backend_process_static_flags_unconfirmed")

    learning_process_flags_required = target in {
        "governance_enforce",
        "pg_job_queue_enable",
        "pg_job_queue_verify",
    }
    learning_process_flags = _process_static_flags_check(
        worker.get("process_static_feature_flags"),
        expected_flags=flags,
        required=learning_process_flags_required,
    )
    if not learning_process_flags["ok"]:
        blockers.append("learning_worker_process_static_flags_unconfirmed")

    job_worker_preflight_required = target == "pg_job_queue_enable"
    job_worker_preflight_payload = (
        dict(job_worker_preflight or {})
        if isinstance(job_worker_preflight, Mapping)
        else {}
    )
    if (
        job_worker_preflight_required
        and job_worker_preflight_payload.get("ok") is not True
    ):
        blockers.append("pg_job_worker_preflight_unavailable")

    job_worker_capability_required = target == "pg_job_queue_verify"
    job_worker_capability_payload = (
        dict(job_worker_capability or {})
        if isinstance(job_worker_capability, Mapping)
        else {}
    )
    if (
        job_worker_capability_required
        and job_worker_capability_payload.get("ok") is not True
    ):
        blockers.append("pg_job_worker_capability_unavailable")

    governance_preflight_required = target in {
        "governance_enforce",
        "pg_job_queue_enable",
        "pg_job_queue_verify",
    }
    governance_preflight_payload = (
        dict(governance_preflight or {})
        if isinstance(governance_preflight, Mapping)
        else {}
    )
    if (
        governance_preflight_required
        and governance_preflight_payload.get("ok") is not True
    ):
        blockers.append("governance_integrity_preflight_unavailable")

    blockers = sorted(set(blockers))
    return {
        "schema_version": SCHEMA_VERSION,
        "target": target,
        "ok": not blockers,
        "status": "passed" if not blockers else "blocked",
        "blockers": blockers,
        "checks": {
            "shadow_gate": {
                **dict(shadow_gate),
                "required_for_target": shadow_gate_required,
            },
            "safety_fault_matrix": {
                **safety_fault_matrix_payload,
                "required_for_target": safety_fault_matrix_required,
            },
            "execution_fault_matrix": {
                **execution_fault_matrix_payload,
                "required_for_target": execution_fault_matrix_required,
            },
            "static_flags": {
                "ok": not flag_mismatches,
                "mismatches": flag_mismatches,
            },
            "services": {
                "ok": not inactive_services,
                "required": list(required_services),
                "states": dict(service_states),
                "inactive": inactive_services,
            },
            "safety_latch": {
                "ok": "no_new_risk_latch_active_or_unknown" not in blockers,
                "state": latch_status.get("state"),
                "cause_count": latch_status.get("cause_count"),
            },
            "execution_intents": {
                "ok": (
                    "local_execution_intent_unresolved_or_unknown" not in blockers
                    and "postgres_execution_intent_unresolved_or_unknown" not in blockers
                ),
                "local_unresolved_count": local_unknown_count,
                "postgres_unresolved_count": postgres_unknown_count,
            },
            "readiness": {
                "ok": readiness_ok,
                "snapshot_status": readiness_snapshot.get("status"),
                "age_seconds": readiness_age,
                "ready_for_release": readiness_payload.get("ready_for_release"),
                "ready_for_autonomous_mutation": readiness_payload.get(
                    "ready_for_autonomous_mutation"
                ),
                "worker_fresh": worker.get("fresh"),
                "config_hash_match": worker.get("config_hash_match"),
                "overlay_hash_match": worker.get("overlay_hash_match"),
                "mutation_capability_status": mutation_capability.get("status"),
            },
            "backend_process_static_flags": {
                **backend_process_flags,
            },
            "learning_worker_process_static_flags": {
                **learning_process_flags,
            },
            "job_worker_preflight": {
                **job_worker_preflight_payload,
                "required_for_target": job_worker_preflight_required,
            },
            "job_worker_capability": {
                **job_worker_capability_payload,
                "required_for_target": job_worker_capability_required,
            },
            "governance_preflight": {
                **governance_preflight_payload,
                "required_for_target": governance_preflight_required,
            },
        },
    }


def _service_states(*, target: str) -> dict[str, str]:
    states: dict[str, str] = {}
    names = REQUIRED_SERVICES + (
        (JOB_WORKER_SERVICE,) if target == "pg_job_queue_verify" else ()
    )
    for name in names:
        try:
            result = subprocess.run(
                ("systemctl", "is-active", name),
                check=False,
                capture_output=True,
                text=True,
                timeout=5.0,
            )
            states[name] = str(result.stdout or "unknown").strip() or "unknown"
        except Exception:
            states[name] = "unknown"
    return states


def _command_result(result: Any) -> tuple[int, str, str]:
    """Normalize subprocess results and small injected test doubles."""

    if isinstance(result, Mapping):
        return (
            int(result.get("returncode", 0) or 0),
            str(result.get("stdout") or ""),
            str(result.get("stderr") or ""),
        )
    return (
        int(getattr(result, "returncode", 0) or 0),
        str(getattr(result, "stdout", "") or ""),
        str(getattr(result, "stderr", "") or ""),
    )


def _run_readonly_command(
    command: tuple[str, ...],
    *,
    runner: Callable[..., Any],
    cwd: Path | None = None,
) -> dict[str, Any]:
    """Run one bounded read-only command through an injectable runner."""

    try:
        result = runner(
            command,
            cwd=str(cwd) if cwd is not None else None,
            check=False,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        returncode, stdout, stderr = _command_result(result)
        return {
            "returncode": returncode,
            "stdout": stdout,
            "stderr": stderr,
            "command": list(command),
        }
    except Exception as exc:
        return {
            "returncode": 1,
            "stdout": "",
            "stderr": f"{type(exc).__name__}:{exc}",
            "command": list(command),
        }


def _parse_systemd_properties(stdout: str) -> dict[str, str]:
    properties: dict[str, str] = {}
    for line in str(stdout or "").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            properties[key.strip()] = value.strip()
    return properties


def _default_health_reader(url: str) -> Mapping[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "quant-release-preflight/1"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=5.0) as response:
        body = response.read().decode("utf-8")
        payload = json.loads(body) if body else {}
        return {
            "status_code": int(getattr(response, "status", response.getcode())),
            "payload": payload if isinstance(payload, Mapping) else {},
        }


def _default_schema_status_reader() -> Mapping[str, Any]:
    from backend.core.db import get_state_pg_conn
    from backend.core.state_schema_migrations import (
        STATE_SCHEMA_MIN_VERSION,
        state_schema_status,
    )

    conn = get_state_pg_conn(read_only=True)
    try:
        return state_schema_status(conn, minimum_version=STATE_SCHEMA_MIN_VERSION)
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()


def collect_release_preflight(
    *,
    repo_root: str | Path = REPO_ROOT,
    backend_url: str = "http://127.0.0.1:8000/api/health",
    backend_service: str = BACKEND_SERVICE,
    runner: Callable[..., Any] = subprocess.run,
    health_reader: Callable[[str], Mapping[str, Any]] | None = None,
    schema_status_reader: Callable[[], Mapping[str, Any]] | None = None,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Collect a read-only release fact set without granting trade authority.

    ``repo_ready`` describes the checked-out source tree.  ``production_loaded``
    describes the independently running backend process.  A healthy HTTP
    response does not make either fact true by itself, and this contract never
    reports trading authorization.
    """

    root = Path(repo_root).expanduser().resolve()
    blockers: list[str] = []

    target_identity = collect_release_identity(root, runner=runner)
    reported_root = str(target_identity.get("reported_root") or "")
    head = str(target_identity.get("head") or "")
    status_output = str(target_identity.get("status_porcelain") or "")
    repo_clean = bool(target_identity.get("clean"))
    repo_ready = bool(target_identity.get("ok") and repo_clean)
    if not target_identity.get("ok"):
        blockers.append("repo_git_evidence_unavailable")
    if reported_root and Path(reported_root).resolve() != root:
        blockers.append("repo_worktree_root_mismatch")
    if not head:
        blockers.append("repo_head_missing")
    if not repo_clean:
        blockers.append("repo_worktree_dirty")
    if not str(target_identity.get("worktree_fingerprint") or ""):
        blockers.append("repo_worktree_fingerprint_missing")

    systemd_command = _run_readonly_command(
        (
            "systemctl",
            "show",
            backend_service,
            "--no-page",
            "--property=ActiveState,SubState,MainPID,ExecMainStartTimestamp,Restart,DropInPaths",
        ),
        runner=runner,
    )
    systemd = _parse_systemd_properties(str(systemd_command.get("stdout") or ""))
    try:
        main_pid = int(systemd.get("MainPID") or 0)
    except (TypeError, ValueError):
        main_pid = 0
    active = systemd.get("ActiveState") == "active" and systemd.get("SubState") == "running"
    restart_policy = str(systemd.get("Restart") or "")
    drop_in_paths = [item for item in str(systemd.get("DropInPaths") or "").split() if item]
    systemd_ok = bool(
        systemd_command.get("returncode") == 0
        and active
        and main_pid > 0
        and str(systemd.get("ExecMainStartTimestamp") or "")
        and "Restart" in systemd
        and "DropInPaths" in systemd
    )
    if systemd_command.get("returncode") != 0:
        blockers.append("systemd_properties_unavailable")
    if not active:
        blockers.append("backend_service_not_running")
    if main_pid <= 0:
        blockers.append("backend_main_pid_missing")
    if not str(systemd.get("ExecMainStartTimestamp") or ""):
        blockers.append("backend_start_time_missing")
    if "Restart" not in systemd or not restart_policy:
        blockers.append("systemd_restart_policy_unknown")
    elif restart_policy.lower() == "no":
        blockers.append("systemd_restart_policy_no")
    if "DropInPaths" not in systemd:
        blockers.append("systemd_dropin_paths_unknown")

    process_path = _run_readonly_command(
        ("readlink", "-f", f"/proc/{main_pid}/cwd"), runner=runner
    ) if main_pid > 0 else {
        "returncode": 1,
        "stdout": "",
        "stderr": "main_pid_missing",
        "command": ["readlink", "-f", "/proc/0/cwd"],
    }
    loaded_root = str(process_path.get("stdout") or "").strip()
    code_path_loaded = (
        process_path.get("returncode") == 0
        and bool(loaded_root)
        and Path(loaded_root).resolve() == root
    )
    if not code_path_loaded:
        blockers.append("backend_code_path_unconfirmed")

    health_payload: Mapping[str, Any] = {}
    health_status_code: int | None = None
    health_error = ""
    try:
        raw_health = (health_reader or _default_health_reader)(backend_url)
        health_status_code = int(raw_health.get("status_code") or 0)
        candidate = raw_health.get("payload")
        health_payload = candidate if isinstance(candidate, Mapping) else {}
    except Exception as exc:
        health_error = f"{type(exc).__name__}:{exc}"
    health_ok = bool(
        health_status_code == 200 and str(health_payload.get("status") or "") == "ok"
    )
    if not health_ok:
        blockers.append("api_health_unavailable_or_degraded")
    frozen_identity = health_payload.get("release_identity")
    frozen_identity = (
        dict(frozen_identity) if isinstance(frozen_identity, Mapping) else {}
    )
    target_head = str(target_identity.get("head") or "")
    target_fingerprint = str(target_identity.get("worktree_fingerprint") or "")
    frozen_head = str(frozen_identity.get("head") or "")
    frozen_fingerprint = str(frozen_identity.get("worktree_fingerprint") or "")
    frozen_root = str(frozen_identity.get("root") or "")
    try:
        frozen_pid = int(frozen_identity.get("pid") or 0)
    except (TypeError, ValueError):
        frozen_pid = 0
    identity_pid_match = bool(main_pid > 0 and frozen_pid == main_pid)
    release_identity_match = bool(
        frozen_identity.get("schema_version") == "process_release_identity.v1"
        and frozen_identity.get("ok") is True
        and frozen_root == str(root)
        and identity_pid_match
        and target_head
        and target_fingerprint
        and frozen_head == target_head
        and frozen_fingerprint == target_fingerprint
    )
    if not frozen_identity:
        blockers.append("backend_release_identity_missing")
    elif frozen_identity.get("schema_version") != "process_release_identity.v1":
        blockers.append("backend_release_identity_schema_unknown")
    elif not frozen_identity.get("ok"):
        blockers.append("backend_release_identity_invalid")
    elif frozen_root != str(root):
        blockers.append("backend_release_identity_root_mismatch")
    elif not identity_pid_match:
        blockers.append("backend_release_identity_pid_mismatch")
    elif frozen_head != target_head:
        blockers.append("backend_release_identity_head_mismatch")
    elif frozen_fingerprint != target_fingerprint:
        blockers.append("backend_release_identity_fingerprint_mismatch")
    uptime = _float_or_none(health_payload.get("uptime_seconds"))
    health_started_at = None
    if uptime is not None and uptime >= 0.0:
        health_started_at = float(now()) - uptime
    production_loaded = bool(
        systemd_ok and code_path_loaded and health_ok and release_identity_match
    )
    if not production_loaded:
        blockers.append("production_loaded_unconfirmed")

    schema: Mapping[str, Any]
    schema_error = ""
    try:
        schema = dict((schema_status_reader or _default_schema_status_reader)())
    except Exception as exc:
        schema = {"ok": False, "status": "unavailable"}
        schema_error = f"{type(exc).__name__}:{exc}"
    schema_ok = schema.get("ok") is True
    if not schema_ok:
        blockers.append("schema_status_unavailable_or_mismatched")

    blockers = sorted(set(blockers))
    return {
        "schema_version": RELEASE_PREFLIGHT_SCHEMA_VERSION,
        "read_only": True,
        "ok": not blockers,
        "status": "passed" if not blockers else "blocked",
        "blockers": blockers,
        "repo_ready": {
            "ok": repo_ready,
            "root": str(root),
            "reported_root": reported_root,
            "head": head,
            "clean": repo_clean,
            "status_porcelain": status_output,
            "worktree_fingerprint": target_fingerprint,
            "identity": target_identity,
        },
        "production_loaded": {
            "ok": production_loaded,
            "service": backend_service,
            "active": active,
            "main_pid": main_pid,
            "loaded_generation": (
                f"{main_pid}:{systemd.get('ExecMainStartTimestamp')}"
                if main_pid > 0 and systemd.get("ExecMainStartTimestamp")
                else ""
            ),
            "exec_main_start_timestamp": systemd.get("ExecMainStartTimestamp"),
            "restart_policy": restart_policy,
            "drop_in_paths": drop_in_paths,
            "systemd": systemd,
            "loaded_cwd": loaded_root,
            "code_path_loaded": code_path_loaded,
            "release_identity_match": release_identity_match,
            "release_identity_pid_match": identity_pid_match,
            "release_identity": frozen_identity,
            "health_started_at": health_started_at,
            "health_uptime_seconds": uptime,
            "health": {
                "ok": health_ok,
                "status_code": health_status_code,
                "status": health_payload.get("status"),
                "db": health_payload.get("db"),
                "ctrader": health_payload.get("ctrader"),
                "error": health_error,
            },
        },
        "schema": {**dict(schema), "error": schema_error},
        "trade_authorization": {
            "authorized": False,
            "status": "not_assessed",
            "reason": "release_preflight_is_not_trade_authority",
        },
    }


def collect_phased_release_preflight(
    *,
    target: str,
    required_hours: float = 24.0,
    max_gap_sec: float = 75.0,
) -> dict[str, Any]:
    """Collect authoritative local/PG facts and evaluate the release gate."""

    from backend.core.static_feature_flags import shared_static_feature_flags
    from backend.services.backend_readiness_snapshot import BackendReadinessSnapshotService
    from backend.services.broker_execution_intent import BrokerExecutionIntentStore
    from backend.services.live_safety_shadow_observation import safety_shadow_gate_status
    from backend.services.live_safety_state import (
        no_new_risk_latch_status,
        unresolved_broker_outcome_mutations,
    )
    from execution.broker_config import shared_broker_connection_config

    static = shared_static_feature_flags()
    flags = {
        "live_safety_plane_v2_mode": static.live_safety_plane_v2_mode,
        "governance_mutation_coordinator_v2_mode": (
            static.governance_mutation_coordinator_v2_mode
        ),
        "pg_job_queue_v2_enabled": static.pg_job_queue_v2_enabled,
    }
    try:
        local_unknown_count: int | None = len(unresolved_broker_outcome_mutations())
    except Exception:
        local_unknown_count = None
    broker = shared_broker_connection_config()
    try:
        postgres_unknown_count: int | None = BrokerExecutionIntentStore().unresolved_count(
            broker="ctrader",
            account_id=str(broker.account_id),
            symbol=str(broker.symbol),
        )
    except Exception:
        postgres_unknown_count = None
    job_worker_preflight: Mapping[str, Any] | None = None
    if target == "pg_job_queue_enable":
        from backend.jobs.release_preflight import (
            collect_persistent_job_worker_release_preflight,
        )

        job_worker_preflight = collect_persistent_job_worker_release_preflight()
    job_worker_capability: Mapping[str, Any] | None = None
    if target == "pg_job_queue_verify":
        from backend.jobs.release_preflight import (
            collect_persistent_job_worker_capability,
        )

        job_worker_capability = collect_persistent_job_worker_capability(
            expected_flags=flags,
        )
    governance_preflight: Mapping[str, Any] | None = None
    if target in {
        "governance_enforce",
        "pg_job_queue_enable",
        "pg_job_queue_verify",
    }:
        from backend.services.governance_release_preflight import (
            collect_governance_release_preflight,
        )

        governance_preflight = collect_governance_release_preflight()
    safety_fault_matrix: Mapping[str, Any] | None = None
    if target == "supervisor_enforce":
        from backend.services.live_safety_fault_matrix import fault_matrix_status

        safety_fault_matrix = fault_matrix_status()
    result = evaluate_phased_release_preflight(
        target=target,
        shadow_gate=safety_shadow_gate_status(
            required_hours=required_hours,
            max_gap_sec=max_gap_sec,
        ),
        flags=flags,
        service_states=_service_states(target=target),
        latch_status=no_new_risk_latch_status(fail_closed=True),
        local_unknown_count=local_unknown_count,
        postgres_unknown_count=postgres_unknown_count,
        readiness_snapshot=BackendReadinessSnapshotService().latest(),
        job_worker_preflight=job_worker_preflight,
        governance_preflight=governance_preflight,
        safety_fault_matrix=safety_fault_matrix,
        job_worker_capability=job_worker_capability,
    )
    release_runtime = collect_release_preflight()
    result.setdefault("checks", {})["release_runtime"] = release_runtime
    if not release_runtime.get("repo_ready", {}).get("ok"):
        result.setdefault("blockers", []).append("repo_ready_unconfirmed")
    if not release_runtime.get("production_loaded", {}).get("ok"):
        result.setdefault("blockers", []).append("production_loaded_unconfirmed")
    if not release_runtime.get("schema", {}).get("ok"):
        result.setdefault("blockers", []).append("schema_status_unavailable_or_mismatched")
    result["blockers"] = sorted(set(result.get("blockers") or []))
    result["ok"] = not result["blockers"]
    result["status"] = "passed" if result["ok"] else "blocked"
    return result
