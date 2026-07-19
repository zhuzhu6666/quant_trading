from __future__ import annotations

import copy

import pytest

from backend.services.phased_repair_release_gate import (
    evaluate_phased_release_preflight,
    evaluate_safety_enforce_preflight,
)


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
    facts["flags"].update(flag_patch)
    facts["shadow_gate"] = {"ok": False, "status": "observation_stream_stale"}

    result = evaluate_phased_release_preflight(target=target, **facts)

    assert result["ok"] is True
    assert result["target"] == target
    assert result["checks"]["shadow_gate"]["required_for_target"] is False


def test_later_transition_preflight_rejects_skipped_predecessor():
    facts = _facts()
    facts["flags"].update(live_safety_plane_v2_mode="enforce")

    result = evaluate_phased_release_preflight(
        target="execution_outcome_enable", **facts
    )

    assert result["ok"] is False
    assert "static_rollout_flags_unexpected" in result["blockers"]


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
    facts["flags"].update(flag_patch)

    result = evaluate_phased_release_preflight(target=target, **facts)

    assert result["ok"] is False
    assert "static_rollout_flags_unexpected" in result["blockers"]


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
