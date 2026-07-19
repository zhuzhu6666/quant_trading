"""Read-only staged release preflight for the phased repair rollout."""
from __future__ import annotations

import subprocess
from typing import Any, Mapping


SCHEMA_VERSION = "phased_repair_release_preflight.v1"
REQUIRED_SERVICES = ("quant-backend.service", "quant-learning-worker.service")
JOB_WORKER_SERVICE = "quant-job-worker.service"
TARGET_EXPECTED_FLAGS = {
    "safety_enforce": {
        "live_safety_plane_v2_mode": "shadow",
        "live_generation_controller_v2_enabled": False,
        "ctrader_execution_outcome_v2_enabled": False,
        "governance_mutation_coordinator_v2_mode": "dual_record",
        "pg_job_queue_v2_enabled": False,
    },
    "generation_enable": {
        "live_safety_plane_v2_mode": "enforce",
        "live_generation_controller_v2_enabled": False,
        "ctrader_execution_outcome_v2_enabled": False,
        "governance_mutation_coordinator_v2_mode": "dual_record",
        "pg_job_queue_v2_enabled": False,
    },
    "execution_outcome_enable": {
        "live_safety_plane_v2_mode": "enforce",
        "live_generation_controller_v2_enabled": True,
        "ctrader_execution_outcome_v2_enabled": False,
        "governance_mutation_coordinator_v2_mode": "dual_record",
        "pg_job_queue_v2_enabled": False,
    },
    "governance_enforce": {
        "live_safety_plane_v2_mode": "enforce",
        "live_generation_controller_v2_enabled": True,
        "ctrader_execution_outcome_v2_enabled": True,
        "governance_mutation_coordinator_v2_mode": "dual_record",
        "pg_job_queue_v2_enabled": False,
    },
    "pg_job_queue_enable": {
        "live_safety_plane_v2_mode": "enforce",
        "live_generation_controller_v2_enabled": True,
        "ctrader_execution_outcome_v2_enabled": True,
        "governance_mutation_coordinator_v2_mode": "enforce",
        "pg_job_queue_v2_enabled": False,
    },
    "pg_job_queue_verify": {
        "live_safety_plane_v2_mode": "enforce",
        "live_generation_controller_v2_enabled": True,
        "ctrader_execution_outcome_v2_enabled": True,
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
    shadow_gate_required = target == "safety_enforce"
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
    execution_fault_matrix_required = target == "execution_outcome_enable"
    execution_fault_matrix_payload = (
        dict(execution_fault_matrix or {})
        if isinstance(execution_fault_matrix, Mapping)
        else {}
    )
    if (
        execution_fault_matrix_required
        and execution_fault_matrix_payload.get("ok") is not True
    ):
        blockers.append("execution_outcome_fault_matrix_incomplete")
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

    process_flags_required = target != "safety_enforce"
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


def evaluate_safety_enforce_preflight(**facts: Any) -> dict[str, Any]:
    """Backward-compatible evaluator for the first staged transition."""

    return evaluate_phased_release_preflight(target="safety_enforce", **facts)


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
        "live_generation_controller_v2_enabled": static.live_generation_controller_v2_enabled,
        "ctrader_execution_outcome_v2_enabled": static.ctrader_execution_outcome_v2_enabled,
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
    if target == "safety_enforce":
        from backend.services.live_safety_fault_matrix import fault_matrix_status

        safety_fault_matrix = fault_matrix_status()
    execution_fault_matrix: Mapping[str, Any] | None = None
    if target == "execution_outcome_enable":
        from backend.services.execution_outcome_fault_matrix import fault_matrix_status

        execution_fault_matrix = fault_matrix_status()
    return evaluate_phased_release_preflight(
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
        execution_fault_matrix=execution_fault_matrix,
        job_worker_capability=job_worker_capability,
    )


def collect_safety_enforce_preflight(
    *, required_hours: float = 24.0, max_gap_sec: float = 75.0
) -> dict[str, Any]:
    """Backward-compatible collector for the first staged transition."""

    return collect_phased_release_preflight(
        target="safety_enforce",
        required_hours=required_hours,
        max_gap_sec=max_gap_sec,
    )
