from __future__ import annotations

import copy

import pytest

from backend.core.static_feature_flags import static_feature_flags_fingerprint
from backend.services.phased_repair_release_gate import (
    collect_release_preflight,
    evaluate_phased_release_preflight,
)
from backend.core.release_identity import collect_release_identity


def _facts() -> dict:
    facts = {
        "shadow_gate": {"ok": True, "status": "passed"},
        "flags": {
            "live_safety_plane_v2_mode": "enforce",
            "governance_mutation_coordinator_v2_mode": "dual_record",
            "pg_job_queue_v2_enabled": False,
        },
        "service_states": {
            "quant-backend.service": "active",
            "quant-learning-worker.service": "active",
            "quant-job-worker.service": "active",
        },
        "latch_status": {"active": False, "state": "cleared", "cause_count": 0},
        "local_unknown_count": 0,
        "postgres_unknown_count": 0,
        "job_worker_preflight": {"ok": True, "status": "passed"},
        "governance_preflight": {"ok": True, "status": "passed"},
        "safety_fault_matrix": {"ok": True, "status": "passed"},
        "execution_fault_matrix": {"ok": True, "status": "passed"},
        "job_worker_capability": {"ok": True, "status": "passed"},
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


def _release_runner(root: str, *, dirty: str = "", restart: str = "on-failure", cwd: str | None = None):
    def runner(command, **_kwargs):
        command = tuple(command)
        if command[:3] == ("git", "rev-parse", "--show-toplevel"):
            return {"returncode": 0, "stdout": root}
        if command[:3] == ("git", "rev-parse", "HEAD"):
            return {"returncode": 0, "stdout": "abc123"}
        if command[:2] == ("git", "status"):
            return {"returncode": 0, "stdout": dirty}
        if command[:3] == ("git", "ls-files", "-z"):
            return {"returncode": 0, "stdout": ""}
        if command[:2] == ("systemctl", "show"):
            return {
                "returncode": 0,
                "stdout": "\n".join(
                    (
                        "ActiveState=active",
                        "SubState=running",
                        "MainPID=42",
                        "ExecMainStartTimestamp=Mon 2026-08-24 10:00:00 CST",
                        f"Restart={restart}",
                        "DropInPaths=/etc/systemd/system/quant-backend.service.d/override.conf",
                    )
                ),
            }
        if command[:2] == ("readlink", "-f"):
            return {"returncode": 0, "stdout": cwd or root}
        raise AssertionError(f"unexpected command: {command}")

    return runner


def _release_health(_url: str, identity: dict | None = None):
    public_identity = dict(identity or {})
    if public_identity:
        public_identity.setdefault("pid", 42)
        public_identity.setdefault("captured_at", 900.0)
    return {
        "status_code": 200,
        "payload": {
            "status": "ok",
            "db": "connected",
            "ctrader": "unknown",
            "uptime_seconds": 120.0,
            "release_identity": public_identity,
        },
    }


def test_release_preflight_separates_clean_repo_from_loaded_backend(tmp_path):
    root = str(tmp_path)
    runner = _release_runner(root)
    identity = collect_release_identity(root, runner=runner)
    result = collect_release_preflight(
        repo_root=root,
        runner=runner,
        health_reader=lambda url: _release_health(url, identity),
        schema_status_reader=lambda: {"ok": True, "current_version": 32},
        now=lambda: 1000.0,
    )

    assert result["ok"] is True
    assert result["repo_ready"]["ok"] is True
    assert result["repo_ready"]["head"] == "abc123"
    assert result["production_loaded"]["ok"] is True
    assert result["production_loaded"]["main_pid"] == 42
    assert result["production_loaded"]["health_started_at"] == 880.0
    assert result["production_loaded"]["drop_in_paths"]
    assert result["trade_authorization"]["authorized"] is False


def test_release_preflight_keeps_loaded_process_separate_from_dirty_repo(tmp_path):
    root = str(tmp_path)
    runner = _release_runner(root, dirty=" M backend/app.py\n")
    identity = collect_release_identity(root, runner=runner)
    result = collect_release_preflight(
        repo_root=root,
        runner=runner,
        health_reader=lambda url: _release_health(url, identity),
        schema_status_reader=lambda: {"ok": True},
    )

    assert result["ok"] is False
    assert result["repo_ready"]["ok"] is False
    assert result["production_loaded"]["ok"] is True
    assert "repo_worktree_dirty" in result["blockers"]


def test_release_preflight_blocks_restart_no_code_mismatch_health_and_schema(tmp_path):
    root = str(tmp_path)
    runner = _release_runner(root, restart="no", cwd="/srv/old_quant_trading")
    identity = collect_release_identity(root, runner=runner)
    result = collect_release_preflight(
        repo_root=root,
        runner=runner,
        health_reader=lambda _url: {
            **_release_health(_url, identity),
            "payload": {"status": "degraded", "uptime_seconds": 10, "release_identity": identity},
        },
        schema_status_reader=lambda: {"ok": False, "current_version": 31},
    )

    assert result["ok"] is False
    assert "systemd_restart_policy_no" in result["blockers"]
    assert "backend_code_path_unconfirmed" in result["blockers"]
    assert "api_health_unavailable_or_degraded" in result["blockers"]
    assert "schema_status_unavailable_or_mismatched" in result["blockers"]
    assert result["production_loaded"]["ok"] is False


def test_release_preflight_blocks_old_health_without_frozen_identity(tmp_path):
    root = str(tmp_path)
    runner = _release_runner(root)
    result = collect_release_preflight(
        repo_root=root,
        runner=runner,
        health_reader=lambda _url: {
            "status_code": 200,
            "payload": {"status": "ok", "uptime_seconds": 10},
        },
        schema_status_reader=lambda: {"ok": True},
    )

    assert result["ok"] is False
    assert "backend_release_identity_missing" in result["blockers"]
    assert result["production_loaded"]["ok"] is False


def test_release_preflight_blocks_frozen_identity_mismatch(tmp_path):
    root = str(tmp_path)
    runner = _release_runner(root)
    identity = collect_release_identity(root, runner=runner)
    stale_identity = {**identity, "head": "stale-head"}
    result = collect_release_preflight(
        repo_root=root,
        runner=runner,
        health_reader=lambda url: _release_health(url, stale_identity),
        schema_status_reader=lambda: {"ok": True},
    )

    assert result["ok"] is False
    assert "backend_release_identity_head_mismatch" in result["blockers"]
    assert result["production_loaded"]["release_identity_match"] is False


def test_release_preflight_blocks_health_from_another_pid(tmp_path):
    root = str(tmp_path)
    runner = _release_runner(root)
    identity = collect_release_identity(root, runner=runner)
    result = collect_release_preflight(
        repo_root=root,
        runner=runner,
        health_reader=lambda url: _release_health(
            url, {**identity, "pid": 99}
        ),
        schema_status_reader=lambda: {"ok": True},
    )

    assert result["ok"] is False
    assert "backend_release_identity_pid_mismatch" in result["blockers"]
    assert result["production_loaded"]["release_identity_pid_match"] is False


def test_supervisor_enforce_preflight_passes_with_canonical_authority():
    result = evaluate_phased_release_preflight(
        target="supervisor_enforce", **_facts()
    )

    assert result["ok"] is True
    assert result["status"] == "passed"
    assert result["blockers"] == []


@pytest.mark.parametrize(
    ("target", "flag_patch"),
    [
        (
            "governance_enforce",
            {
                "live_safety_plane_v2_mode": "enforce",
            },
        ),
        (
            "pg_job_queue_enable",
            {
                "live_safety_plane_v2_mode": "enforce",
                "governance_mutation_coordinator_v2_mode": "enforce",
            },
        ),
        (
            "pg_job_queue_verify",
            {
                "live_safety_plane_v2_mode": "enforce",
                "governance_mutation_coordinator_v2_mode": "enforce",
                "pg_job_queue_v2_enabled": True,
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
    _set_flags(
        facts,
        {"governance_mutation_coordinator_v2_mode": "enforce"},
    )

    result = evaluate_phased_release_preflight(target="governance_enforce", **facts)

    assert result["ok"] is False
    assert "static_rollout_flags_unexpected" in result["blockers"]


def test_later_transition_rejects_config_changed_without_backend_restart():
    facts = _facts()
    facts["flags"].update(governance_mutation_coordinator_v2_mode="enforce")

    result = evaluate_phased_release_preflight(
        target="governance_enforce", **facts
    )

    assert result["ok"] is False
    assert "backend_process_static_flags_unconfirmed" in result["blockers"]


def test_process_static_flag_projection_is_total_for_malformed_pid():
    facts = _facts()
    _set_flags(facts, {"governance_mutation_coordinator_v2_mode": "enforce"})
    facts["readiness_snapshot"]["payload"]["snapshot"][
        "process_static_feature_flags"
    ]["pid"] = "invalid"

    result = evaluate_phased_release_preflight(
        target="governance_enforce", **facts
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
        },
    )
    worker_projection["values"]["governance_mutation_coordinator_v2_mode"] = "enforce"
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
            "pg_job_queue_enable",
            {
                "live_safety_plane_v2_mode": "enforce",
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

    result = evaluate_phased_release_preflight(
        target="supervisor_enforce", **facts
    )

    assert result["ok"] is True
    assert result["checks"]["job_worker_preflight"]["required_for_target"] is False


def test_pg_job_queue_verify_requires_live_service_and_capability():
    facts = _facts()
    _set_flags(
        facts,
        {
            "live_safety_plane_v2_mode": "enforce",
            "governance_mutation_coordinator_v2_mode": "enforce",
            "pg_job_queue_v2_enabled": True,
        },
    )
    facts["service_states"]["quant-job-worker.service"] = "inactive"
    facts["job_worker_capability"] = {
        "ok": False,
        "status": "blocked",
        "blockers": ["persistent_job_worker_capability_stale"],
    }

    result = evaluate_phased_release_preflight(
        target="pg_job_queue_verify", **facts
    )

    assert result["ok"] is False
    assert "required_service_inactive" in result["blockers"]
    assert "pg_job_worker_capability_unavailable" in result["blockers"]
    assert result["checks"]["job_worker_preflight"]["required_for_target"] is False
    assert result["checks"]["job_worker_capability"]["required_for_target"] is True


def test_governance_transition_requires_integrity_preflight():
    facts = _facts()
    _set_flags(
        facts,
        {
            "live_safety_plane_v2_mode": "enforce",
            "governance_mutation_coordinator_v2_mode": "enforce",
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


def test_execution_fault_matrix_is_not_a_separate_release_transition():
    facts = _facts()
    facts["execution_fault_matrix"] = None

    result = evaluate_phased_release_preflight(
        target="supervisor_enforce", **facts
    )

    assert result["ok"] is True
    assert result["checks"]["execution_fault_matrix"]["required_for_target"] is False


def test_supervisor_transition_does_not_require_shadow_fault_matrix():
    facts = _facts()
    facts["safety_fault_matrix"] = {
        "ok": False,
        "status": "missing",
        "blockers": ["fault_matrix_attestation_missing"],
    }

    result = evaluate_phased_release_preflight(
        target="supervisor_enforce", **facts
    )

    assert result["ok"] is True
    assert result["checks"]["safety_fault_matrix"]["required_for_target"] is False


def test_complete_lifecycle_safety_transition_does_not_require_fault_matrix():
    facts = _facts()
    facts["shadow_gate"]["complete_lifecycle"] = True
    facts["safety_fault_matrix"] = None

    result = evaluate_phased_release_preflight(
        target="supervisor_enforce", **facts
    )

    assert result["ok"] is True
    assert result["checks"]["safety_fault_matrix"]["required_for_target"] is False


def test_unknown_release_target_is_total_and_fail_closed():
    result = evaluate_phased_release_preflight(target="unknown", **_facts())

    assert result["ok"] is False
    assert result["blockers"] == ["release_target_unknown"]


@pytest.mark.parametrize(
    ("mutation", "blocker"),
    [
        (
            lambda f: f["flags"].update(live_safety_plane_v2_mode="shadow"),
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
def test_supervisor_enforce_preflight_fails_closed_for_each_required_fact(mutation, blocker):
    facts = copy.deepcopy(_facts())
    mutation(facts)

    result = evaluate_phased_release_preflight(
        target="supervisor_enforce", **facts
    )

    assert result["ok"] is False
    assert blocker in result["blockers"]
