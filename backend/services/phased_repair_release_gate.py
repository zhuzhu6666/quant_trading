"""Read-only staged release preflight for the phased repair rollout."""
from __future__ import annotations

import subprocess
from typing import Any, Mapping


SCHEMA_VERSION = "phased_repair_release_preflight.v1"
REQUIRED_SERVICES = ("quant-backend.service", "quant-learning-worker.service")


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


def evaluate_safety_enforce_preflight(
    *,
    shadow_gate: Mapping[str, Any],
    flags: Mapping[str, Any],
    service_states: Mapping[str, str],
    latch_status: Mapping[str, Any],
    local_unknown_count: int | None,
    postgres_unknown_count: int | None,
    readiness_snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """Evaluate facts needed before changing Safety v2 shadow to enforce."""

    blockers: list[str] = []
    expected_flags = {
        "live_safety_plane_v2_mode": "shadow",
        "live_generation_controller_v2_enabled": False,
        "ctrader_execution_outcome_v2_enabled": False,
        "governance_mutation_coordinator_v2_mode": "dual_record",
        "pg_job_queue_v2_enabled": False,
    }
    flag_mismatches = {
        key: {"expected": expected, "actual": flags.get(key)}
        for key, expected in expected_flags.items()
        if flags.get(key) != expected
    }
    if flag_mismatches:
        blockers.append("static_rollout_flags_unexpected")
    if not bool(shadow_gate.get("ok")):
        blockers.append("safety_shadow_gate_incomplete")
    inactive_services = sorted(
        name for name in REQUIRED_SERVICES if service_states.get(name) != "active"
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

    blockers = sorted(set(blockers))
    return {
        "schema_version": SCHEMA_VERSION,
        "target": "safety_enforce",
        "ok": not blockers,
        "status": "passed" if not blockers else "blocked",
        "blockers": blockers,
        "checks": {
            "shadow_gate": dict(shadow_gate),
            "static_flags": {
                "ok": not flag_mismatches,
                "mismatches": flag_mismatches,
            },
            "services": {
                "ok": not inactive_services,
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
        },
    }


def _service_states() -> dict[str, str]:
    states: dict[str, str] = {}
    for name in REQUIRED_SERVICES:
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


def collect_safety_enforce_preflight(
    *, required_hours: float = 24.0, max_gap_sec: float = 75.0
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
    return evaluate_safety_enforce_preflight(
        shadow_gate=safety_shadow_gate_status(
            required_hours=required_hours,
            max_gap_sec=max_gap_sec,
        ),
        flags=flags,
        service_states=_service_states(),
        latch_status=no_new_risk_latch_status(fail_closed=True),
        local_unknown_count=local_unknown_count,
        postgres_unknown_count=postgres_unknown_count,
        readiness_snapshot=BackendReadinessSnapshotService().latest(),
    )
