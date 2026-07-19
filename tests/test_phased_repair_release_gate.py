from __future__ import annotations

import copy

import pytest

from backend.services.phased_repair_release_gate import evaluate_safety_enforce_preflight


def _facts() -> dict:
    return {
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
                },
            },
        },
    }


def test_safety_enforce_preflight_passes_only_with_complete_authoritative_facts():
    result = evaluate_safety_enforce_preflight(**_facts())

    assert result["ok"] is True
    assert result["status"] == "passed"
    assert result["blockers"] == []


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
