from __future__ import annotations

import copy

import pytest

from backend.core.static_feature_flags import static_feature_flags_fingerprint
from backend.services.phased_repair_release_gate import (
    evaluate_phased_release_preflight,
    evaluate_safety_enforce_preflight,
)


def _facts() -> dict:
    facts = {
        "shadow_gate": {"ok": True, "status": "passed"},
        "flags": {
            "live_safety_plane_v2_mode": "shadow",
            "live_generation_controller_v2_enabled": False,
            "ctrader_execution_outcome_v2_enabled": False,
            "governance_mutation_coordinator_v2_mode": "dual_record",
            "pg_job_queue_v2_enabled": False,
        },
        "service_states": {
            "quant-backend.service": "active",
            "quant-learning-worker.service": "active",
        },
        "latch_status": {"active": False, "state": "cleared", "cause_count": 0},
        "local_unknown_count": 0,
        "postgres_unknown_count": 0,
        "job_worker_preflight": {"ok": True, "status": "passed"},
        "governance_preflight": {"ok": True, "status": "passed"},
        "safety_fault_matrix": {"ok": True, "status": "passed"},
        "readiness_snapshot": {
            "ok": True,
            "status": "available",
            "age_seconds": 10.0,
            "payload": {
                "ready_for_release": True,
                "ready_for_autonomous_mutation": True,
                "learning_worker": {
                    "fresh": True,
                    "config_hash_match": True,
                    "overlay_hash_match": True,
                    "mutation_capability": {"status": "available"},
                    "process_static_feature_flags": {
                        "schema_version": "static_feature_flags.v1",
                        "values": {},
                        "fingerprint": "",
                        "pid": 456,
                        "process_started_at": 901.0,
                    },
                },
                "snapshot": {
                    "process_static_feature_flags": {
                        "schema_version": "static_feature_flags.v1",
                        "values": {},
                        "fingerprint": "",
                        "pid": 123,
                        "process_started_at": 900.0,
                    }
                },
            },
        },
    }
    _set_flags(facts, {})
    return facts


def _set_flags(facts: dict, patch: dict) -> None:
    facts["flags"].update(patch)
    projection = facts["readiness_snapshot"]["payload"]["snapshot"][
        "process_static_feature_flags"
    ]
    projection["values"] = dict(facts["flags"])
    projection["fingerprint"] = static_feature_flags_fingerprint(projection["values"])
    worker_projection = facts["readiness_snapshot"]["payload"]["learning_worker"][
        "process_static_feature_flags"
    ]
    worker_projection["values"] = dict(facts["flags"])
    worker_projection["fingerprint"] = static_feature_flags_fingerprint(
        worker_projection["values"]
    )


def test_safety_enforce_preflight_passes_only_with_complete_authoritative_facts():
    result = evaluate_safety_enforce_preflight(**_facts())

    assert result["ok"] is True
    assert result["status"] == "passed"
    assert result["blockers"] == []


@pytest.mark.parametrize(
    ("target", "flag_patch"),
    [
        (
            "generation_enable",
            {"live_safety_plane_v2_mode": "enforce"},
        ),
        (
            "execution_outcome_enable",
            {
                "live_safety_plane_v2_mode": "enforce",
                "live_generation_controller_v2_enabled": True,
            },
        ),
        (
            "governance_enforce",
            {
                "live_safety_plane_v2_mode": "enforce",
                "live_generation_controller_v2_enabled": True,
                "ctrader_execution_outcome_v2_enabled": True,
            },
        ),
        (
            "pg_job_queue_enable",
            {
                "live_safety_plane_v2_mode": "enforce",
                "live_generation_controller_v2_enabled": True,
                "ctrader_execution_outcome_v2_enabled": True,
                "governance_mutation_coordinator_v2_mode": "enforce",
            },
        ),
    ],
)
def test_later_transition_preflights_require_exact_predecessor_flags(
    target, flag_patch
):
    facts = _facts()
    _set_flags(facts, flag_patch)
    facts["shadow_gate"] = {"ok": False, "status": "observation_stream_stale"}

    result = evaluate_phased_release_preflight(target=target, **facts)

    assert result["ok"] is True
    assert result["target"] == target
    assert result["checks"]["shadow_gate"]["required_for_target"] is False


def test_later_transition_preflight_rejects_skipped_predecessor():
    facts = _facts()
    _set_flags(facts, {"live_safety_plane_v2_mode": "enforce"})

    result = evaluate_phased_release_preflight(
        target="execution_outcome_enable", **facts
    )

    assert result["ok"] is False
    assert "static_rollout_flags_unexpected" in result["blockers"]


def test_later_transition_rejects_config_changed_without_backend_restart():
    facts = _facts()
    facts["flags"].update(live_safety_plane_v2_mode="enforce")

    result = evaluate_phased_release_preflight(
        target="generation_enable", **facts
    )

    assert result["ok"] is False
    assert "backend_process_static_flags_unconfirmed" in result["blockers"]


def test_process_static_flag_projection_is_total_for_malformed_pid():
    facts = _facts()
    _set_flags(facts, {"live_safety_plane_v2_mode": "enforce"})
    facts["readiness_snapshot"]["payload"]["snapshot"][
        "process_static_feature_flags"
    ]["pid"] = "invalid"

    result = evaluate_phased_release_preflight(
        target="generation_enable", **facts
    )

    assert result["ok"] is False
    assert "backend_process_static_flags_unconfirmed" in result["blockers"]


def test_governance_transition_rejects_learning_worker_not_restarted():
    facts = _facts()
    worker_projection = facts["readiness_snapshot"]["payload"]["learning_worker"][
        "process_static_feature_flags"
    ]
    _set_flags(
        facts,
        {
            "live_safety_plane_v2_mode": "enforce",
            "live_generation_controller_v2_enabled": True,
            "ctrader_execution_outcome_v2_enabled": True,
        },
    )
    worker_projection["values"]["ctrader_execution_outcome_v2_enabled"] = False
    worker_projection["fingerprint"] = static_feature_flags_fingerprint(
        worker_projection["values"]
    )

    result = evaluate_phased_release_preflight(
        target="governance_enforce", **facts
    )

    assert result["ok"] is False
    assert "learning_worker_process_static_flags_unconfirmed" in result["blockers"]


@pytest.mark.parametrize(
    ("target", "flag_patch"),
    [
        (
            "governance_enforce",
            {
                "live_safety_plane_v2_mode": "enforce",
                "live_generation_controller_v2_enabled": True,
            },
        ),
        (
            "pg_job_queue_enable",
            {
                "live_safety_plane_v2_mode": "enforce",
                "live_generation_controller_v2_enabled": True,
                "ctrader_execution_outcome_v2_enabled": True,
            },
        ),
    ],
)
def test_final_transition_preflights_reject_skipped_predecessor(target, flag_patch):
    facts = _facts()
    _set_flags(facts, flag_patch)

    result = evaluate_phased_release_preflight(target=target, **facts)

    assert result["ok"] is False
    assert "static_rollout_flags_unexpected" in result["blockers"]


def test_pg_job_queue_transition_requires_worker_preflight():
    facts = _facts()
    _set_flags(
        facts,
        {
            "live_safety_plane_v2_mode": "enforce",
            "live_generation_controller_v2_enabled": True,
            "ctrader_execution_outcome_v2_enabled": True,
            "governance_mutation_coordinator_v2_mode": "enforce",
        },
    )
    facts["job_worker_preflight"] = {
        "ok": False,
        "status": "blocked",
        "blockers": ["persistent_job_active_lease_exists_before_enable"],
    }

    result = evaluate_phased_release_preflight(
        target="pg_job_queue_enable", **facts
    )

    assert result["ok"] is False
    assert "pg_job_worker_preflight_unavailable" in result["blockers"]
    assert result["checks"]["job_worker_preflight"]["required_for_target"] is True


def test_non_queue_transition_does_not_require_worker_preflight():
    facts = _facts()
    facts["job_worker_preflight"] = None

    result = evaluate_safety_enforce_preflight(**facts)

    assert result["ok"] is True
    assert result["checks"]["job_worker_preflight"]["required_for_target"] is False


def test_governance_transition_requires_integrity_preflight():
    facts = _facts()
    _set_flags(
        facts,
        {
            "live_safety_plane_v2_mode": "enforce",
            "live_generation_controller_v2_enabled": True,
            "ctrader_execution_outcome_v2_enabled": True,
        },
    )
    facts["governance_preflight"] = {
        "ok": False,
        "status": "blocked",
        "blockers": ["governance_mutation_in_flight"],
    }

    result = evaluate_phased_release_preflight(
        target="governance_enforce", **facts
    )

    assert result["ok"] is False
    assert "governance_integrity_preflight_unavailable" in result["blockers"]
    assert result["checks"]["governance_preflight"]["required_for_target"] is True


def test_execution_transition_does_not_require_governance_integrity_preflight():
    facts = _facts()
    _set_flags(
        facts,
        {
            "live_safety_plane_v2_mode": "enforce",
            "live_generation_controller_v2_enabled": True,
        },
    )
    facts["governance_preflight"] = None

    result = evaluate_phased_release_preflight(
        target="execution_outcome_enable", **facts
    )

    assert result["ok"] is True
    assert result["checks"]["governance_preflight"]["required_for_target"] is False


def test_empty_account_safety_transition_requires_fault_matrix():
    facts = _facts()
    facts["safety_fault_matrix"] = {
        "ok": False,
        "status": "missing",
        "blockers": ["fault_matrix_attestation_missing"],
    }

    result = evaluate_safety_enforce_preflight(**facts)

    assert result["ok"] is False
    assert "safety_fault_matrix_incomplete" in result["blockers"]
    assert result["checks"]["safety_fault_matrix"]["required_for_target"] is True


def test_complete_lifecycle_safety_transition_does_not_require_fault_matrix():
    facts = _facts()
    facts["shadow_gate"]["complete_lifecycle"] = True
    facts["safety_fault_matrix"] = None

    result = evaluate_safety_enforce_preflight(**facts)

    assert result["ok"] is True
    assert result["checks"]["safety_fault_matrix"]["required_for_target"] is False


def test_unknown_release_target_is_total_and_fail_closed():
    result = evaluate_phased_release_preflight(target="unknown", **_facts())

    assert result["ok"] is False
    assert result["blockers"] == ["release_target_unknown"]


@pytest.mark.parametrize(
    ("mutation", "blocker"),
    [
        (lambda f: f["shadow_gate"].update(ok=False), "safety_shadow_gate_incomplete"),
        (
            lambda f: f["flags"].update(live_generation_controller_v2_enabled=True),
            "static_rollout_flags_unexpected",
        ),
        (
            lambda f: f["service_states"].update({"quant-backend.service": "failed"}),
            "required_service_inactive",
        ),
        (
            lambda f: f["latch_status"].update(active=True, state="active"),
            "no_new_risk_latch_active_or_unknown",
        ),
        (
            lambda f: f.update(local_unknown_count=None),
            "local_execution_intent_unresolved_or_unknown",
        ),
        (
            lambda f: f.update(postgres_unknown_count=1),
            "postgres_execution_intent_unresolved_or_unknown",
        ),
        (
            lambda f: f["readiness_snapshot"].update(age_seconds=181.0),
            "release_readiness_unavailable_or_divergent",
        ),
        (
            lambda f: f["readiness_snapshot"]["payload"]["learning_worker"].update(
                config_hash_match=False
            ),
            "release_readiness_unavailable_or_divergent",
        ),
        (
            lambda f: f["readiness_snapshot"].update(age_seconds="invalid"),
            "release_readiness_unavailable_or_divergent",
        ),
        (
            lambda f: f.update(local_unknown_count="invalid"),
            "local_execution_intent_unresolved_or_unknown",
        ),
        (
            lambda f: f["readiness_snapshot"]["payload"]["learning_worker"].update(
                mutation_capability="invalid"
            ),
            "release_readiness_unavailable_or_divergent",
        ),
    ],
)
def test_safety_enforce_preflight_fails_closed_for_each_required_fact(mutation, blocker):
    facts = copy.deepcopy(_facts())
    mutation(facts)

    result = evaluate_safety_enforce_preflight(**facts)

    assert result["ok"] is False
    assert blocker in result["blockers"]
