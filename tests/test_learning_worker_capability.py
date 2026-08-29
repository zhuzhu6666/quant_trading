from __future__ import annotations

import json
import sqlite3
import time

import pytest

from backend.services.learning_worker_capability import (
    LearningWorkerCapability,
    guarded_mutation_job,
)
from config import runtime_config as rc


def _capability_db(tmp_path):
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE runtime_kv (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at REAL NOT NULL)"
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def test_three_mutation_failures_open_only_mutation_circuit(tmp_path) -> None:
    cap = LearningWorkerCapability(db_path=_capability_db(tmp_path), boot_id="boot-test")
    cap.mark_ready(config_hash="cfg", overlay_hash="ovl", recovery_status="complete")

    cap.record_mutation_failure(job_name="governance", error="db down 1")
    cap.record_mutation_failure(job_name="governance", error="db down 2")
    assert cap.mutation_allowed() is True

    state = cap.record_mutation_failure(job_name="governance", error="db down 3")

    assert cap.mutation_allowed() is False
    assert state["mutation_capability"]["circuit_state"] == "open"
    assert state["mutation_capability"]["consecutive_failures"] == 3
    assert state["observation_capability"] == {
        "available": True,
        "status": "available",
    }
    assert state["research_capability"] == {
        "available": True,
        "status": "available",
    }


def test_mutation_success_resets_failures_but_does_not_reopen_latched_circuit(tmp_path) -> None:
    cap = LearningWorkerCapability(db_path=_capability_db(tmp_path))
    cap.mark_ready(config_hash="cfg", overlay_hash="", recovery_status="complete")
    cap.record_mutation_failure(job_name="governance", error="one")
    state = cap.record_mutation_success(job_name="governance")
    assert state["mutation_capability"]["consecutive_failures"] == 0

    for index in range(3):
        cap.record_mutation_failure(job_name="governance", error=f"failure-{index}")
    state = cap.record_mutation_success(job_name="late-inflight-success")
    assert state["mutation_capability"]["circuit_state"] == "open"
    assert state["mutation_capability"]["available"] is False


def test_operator_pause_skips_mutation_without_disabling_observation(tmp_path) -> None:
    cap = LearningWorkerCapability(db_path=_capability_db(tmp_path))
    cap.mark_ready(config_hash="cfg", overlay_hash="", recovery_status="complete")
    called = []
    rc.reset_for_tests()
    rc.patch({"governance_expansion_paused": True})
    try:
        result = guarded_mutation_job(
            cap,
            "factor_governance",
            lambda: called.append("mutation"),
            publish=False,
        )()
    finally:
        rc.reset_for_tests()

    assert result["status"] == "observation_only"
    assert result["reason"] == "governance_expansion_paused"
    assert called == []
    assert cap.snapshot()["observation_capability"]["available"] is True


def test_run_once_with_open_circuit_keeps_observation_but_skips_mutation(
    monkeypatch,
    tmp_path,
) -> None:
    import backend.runtime.evolution_orchestrator as evolution_module
    import backend.runtime.factor_governance_orchestrator as factor_module
    import backend.services.autonomous_evolution_runner as nursery_module
    import backend.services.autonomous_learning as autonomous_module
    import backend.services.supervisor_learning_scheduler as supervisor_module
    import scripts.learning_worker as worker

    cap = LearningWorkerCapability(db_path=_capability_db(tmp_path))
    cap.mark_ready(config_hash="cfg", overlay_hash="", recovery_status="complete")
    for index in range(3):
        cap.record_mutation_failure(job_name="dependency", error=f"failure-{index}")

    observations = []
    mutations = []
    monkeypatch.setattr(
        supervisor_module,
        "run_supervisor_learning_cycle",
        lambda **_kwargs: observations.append("supervisor") or {"ok": True},
    )

    def _run_learning(**kwargs):
        observations.append("autonomous_learning")
        assert kwargs["mutation_capability"] is False
        return {"ok": True, "status": "observation_only"}

    monkeypatch.setattr(autonomous_module, "run_autonomous_learning_cycle", _run_learning)
    monkeypatch.setattr(
        evolution_module,
        "scheduled_evolution_cycle",
        lambda: mutations.append("evolution"),
    )

    class _Nursery:
        def run_once(self, **_kwargs):
            mutations.append("nursery")
            return {"ok": True}

    monkeypatch.setattr(nursery_module, "AutonomousEvolutionNurseryRunner", _Nursery)

    worker._run_once(cap)

    assert observations == ["supervisor", "autonomous_learning"]
    assert mutations == []


def test_run_once_operator_pause_reaches_autonomous_learning_as_observation_only(
    monkeypatch,
    tmp_path,
) -> None:
    import backend.runtime.evolution_orchestrator as evolution_module
    import backend.runtime.factor_governance_orchestrator as factor_module
    import backend.services.autonomous_evolution_runner as nursery_module
    import backend.services.autonomous_learning as autonomous_module
    import backend.services.supervisor_learning_scheduler as supervisor_module
    import scripts.learning_worker as worker

    cap = LearningWorkerCapability(db_path=_capability_db(tmp_path))
    cap.mark_ready(config_hash="cfg", overlay_hash="", recovery_status="complete")
    mutations = []
    observed_capabilities = []
    monkeypatch.setattr(
        supervisor_module,
        "run_supervisor_learning_cycle",
        lambda **_kwargs: {"ok": True},
    )
    monkeypatch.setattr(
        autonomous_module,
        "run_autonomous_learning_cycle",
        lambda **kwargs: observed_capabilities.append(kwargs["mutation_capability"])
        or {"ok": True},
    )
    monkeypatch.setattr(
        evolution_module,
        "scheduled_evolution_cycle",
        lambda: mutations.append("evolution"),
    )

    class _Nursery:
        def run_once(self, **_kwargs):
            mutations.append("nursery")
            return {"ok": True}

    monkeypatch.setattr(nursery_module, "AutonomousEvolutionNurseryRunner", _Nursery)
    rc.reset_for_tests()
    rc.patch({"governance_expansion_paused": True})
    try:
        worker._run_once(cap)
    finally:
        rc.reset_for_tests()

    assert observed_capabilities == [False]
    assert mutations == []


def test_expected_policy_block_does_not_advance_or_reset_failure_counter(tmp_path) -> None:
    cap = LearningWorkerCapability(db_path=_capability_db(tmp_path))
    cap.mark_ready(config_hash="cfg", overlay_hash="", recovery_status="complete")
    cap.record_mutation_failure(job_name="governance", error="dependency failure")

    result = guarded_mutation_job(
        cap,
        "factor_governance",
        lambda: {"ok": False, "status": "blocked_v16_command_required"},
        publish=False,
    )()

    assert result["status"] == "blocked_v16_command_required"
    assert cap.snapshot()["mutation_capability"]["consecutive_failures"] == 1


def test_guarded_job_counts_result_and_projection_failure_as_one_attempt(
    monkeypatch,
    tmp_path,
) -> None:
    cap = LearningWorkerCapability(db_path=_capability_db(tmp_path))
    cap.mark_ready(config_hash="cfg", overlay_hash="", recovery_status="complete")
    monkeypatch.setattr(
        cap,
        "refresh_runtime_hashes",
        lambda: (_ for _ in ()).throw(RuntimeError("projection unavailable")),
    )
    monkeypatch.setattr(cap, "publish", lambda: cap.snapshot())

    guarded = guarded_mutation_job(
        cap,
        "governance",
        lambda: {"ok": False, "status": "mutation_failed"},
    )

    with pytest.raises(RuntimeError, match="projection unavailable"):
        guarded()

    state = cap.snapshot()["mutation_capability"]
    assert state["consecutive_failures"] == 1
    assert state["circuit_state"] == "closed"


def test_three_runtime_projection_refresh_failures_open_mutation_circuit(
    monkeypatch,
    tmp_path,
) -> None:
    cap = LearningWorkerCapability(db_path=_capability_db(tmp_path))
    cap.mark_ready(config_hash="cfg", overlay_hash="", recovery_status="complete")
    monkeypatch.setattr(
        cap,
        "refresh_runtime_hashes",
        lambda: (_ for _ in ()).throw(RuntimeError("config projection unavailable")),
    )

    for _ in range(3):
        with pytest.raises(RuntimeError, match="config projection unavailable"):
            cap.refresh_and_publish_heartbeat()

    state = cap.snapshot()
    assert state["mutation_capability"]["circuit_state"] == "open"
    assert state["mutation_capability"]["consecutive_failures"] == 3
    assert state["observation_capability"]["available"] is True


def test_three_capability_publish_failures_are_not_reset_between_heartbeats(
    monkeypatch,
    tmp_path,
) -> None:
    cap = LearningWorkerCapability(db_path=_capability_db(tmp_path))
    cap.mark_ready(config_hash="cfg", overlay_hash="", recovery_status="complete")
    monkeypatch.setattr(cap, "refresh_runtime_hashes", lambda: cap.snapshot())
    monkeypatch.setattr(
        cap,
        "publish",
        lambda: (_ for _ in ()).throw(RuntimeError("postgres publish unavailable")),
    )

    for _ in range(3):
        with pytest.raises(RuntimeError, match="postgres publish unavailable"):
            cap.refresh_and_publish_heartbeat()

    state = cap.snapshot()
    assert state["mutation_capability"]["circuit_state"] == "open"
    assert state["mutation_capability"]["consecutive_failures"] == 3


def test_successful_heartbeat_recovers_degraded_counter_before_circuit_opens(
    monkeypatch,
    tmp_path,
) -> None:
    cap = LearningWorkerCapability(db_path=_capability_db(tmp_path))
    cap.mark_ready(config_hash="cfg", overlay_hash="", recovery_status="complete")
    cap.record_mutation_failure(job_name="capability_heartbeat", error="temporary")
    monkeypatch.setattr(cap, "refresh_runtime_hashes", lambda: cap.snapshot())

    payload = cap.refresh_and_publish_heartbeat()

    assert payload["mutation_capability"]["status"] == "available"
    assert payload["mutation_capability"]["consecutive_failures"] == 0
    assert cap.mutation_allowed() is True


@pytest.mark.parametrize(
    ("failure_stage", "expected_stage"),
    [
        ("database", "database_or_schema"),
        ("governance_recovery", "recovery"),
        ("recovery", "recovery"),
        ("yaml", "yaml_config"),
        ("overlay", "runtime_overlay"),
    ],
)
def test_learning_worker_critical_boot_failures_propagate(
    monkeypatch,
    tmp_path,
    failure_stage: str,
    expected_stage: str,
) -> None:
    import backend.core.db as db
    import backend.core.logging as logging_module
    import backend.services.evolution_ledger as ledger
    import backend.services.governance_startup_recovery as governance_recovery_module
    import backend.services.learning_application_state as recovery_module
    import backend.services.runtime_config_startup as startup_module
    import scripts.learning_worker as worker

    cap = LearningWorkerCapability(db_path=_capability_db(tmp_path), boot_id="boot-fail")
    monkeypatch.setattr(logging_module, "setup_logging", lambda: None)
    monkeypatch.setattr(
        db,
        "init_all",
        (lambda: (_ for _ in ()).throw(RuntimeError("database failed")))
        if failure_stage == "database"
        else (lambda: None),
    )
    monkeypatch.setattr(ledger, "expire_stale_evolution_runs", lambda **_kwargs: {"expired_count": 0})
    monkeypatch.setattr(
        ledger,
        "recover_orphaned_evolution_runs",
        lambda **_kwargs: {"interrupted_count": 0, "items": []},
    )

    class _GovernanceRecovery:
        def run(self, **_kwargs):
            if failure_stage == "governance_recovery":
                return {"ok": False, "status": "crash_recovery_failed"}
            return {
                "ok": True,
                "aborted_intent_count": 0,
                "released_claim_count": 0,
            }

    monkeypatch.setattr(
        governance_recovery_module,
        "GovernanceStartupRecoveryService",
        _GovernanceRecovery,
    )

    class _Recovery:
        def recover_prepared(self):
            if failure_stage == "recovery":
                raise RuntimeError("recovery failed")
            return {"ok": True, "checked": 0}

    monkeypatch.setattr(recovery_module, "LearningApplicationStateService", _Recovery)

    def _load():
        if failure_stage == "yaml":
            raise ValueError("yaml failed")
        return rc.RuntimeConfig(), {}

    def _restore(*_args, **_kwargs):
        if failure_stage == "overlay":
            raise RuntimeError("overlay failed")
        return {
            "ok": True,
            "overlay": {"ok": True, "restored": False, "overlay_hash": ""},
            "snapshot": {"config_hash": "cfg"},
        }

    monkeypatch.setattr(startup_module, "load_yaml_runtime_config", _load)
    monkeypatch.setattr(startup_module, "restore_runtime_config_on_startup", _restore)

    with pytest.raises((RuntimeError, ValueError)):
        worker._bootstrap_runtime(cap)

    state = cap.snapshot()
    assert state["boot_status"] == "failed"
    assert state["boot_failure_stage"] == expected_stage
    assert state["mutation_capability"]["available"] is False


def test_learning_worker_projection_exposes_boot_and_hashes(tmp_path) -> None:
    from backend.services.backend_readiness import BackendReadinessService

    db_path = _capability_db(tmp_path)
    cap = LearningWorkerCapability(db_path=db_path, boot_id="boot-projection")
    cap.mark_ready(config_hash="cfg-a", overlay_hash="ovl-a", recovery_status="complete")
    cap.publish()

    status = BackendReadinessService(db_path=db_path)._learning_worker_capability_status(
        runtime_snapshot={"config_hash": "cfg-a"},
        runtime_overlay={"overlay_hash": "ovl-a"},
    )
    assert status["ok"] is True
    assert status["boot_id"] == "boot-projection"
    assert status["config_hash_match"] is True
    assert status["overlay_hash_match"] is True
    assert status["release_identity_match"] is True
    assert status["mutation_capability"]["available"] is True
    process_flags = status["process_static_feature_flags"]
    assert process_flags["schema_version"] == "static_feature_flags.v1"
    assert process_flags["values"]["governance_mutation_coordinator_v2_mode"] in {
        "off",
        "dual_record",
        "enforce",
    }
    assert process_flags["fingerprint"]
    assert process_flags["pid"] > 0
    assert process_flags["process_started_at"] > 0

    divergent = BackendReadinessService(db_path=db_path)._learning_worker_capability_status(
        runtime_snapshot={"config_hash": "cfg-b"},
        runtime_overlay={"overlay_hash": "ovl-a"},
    )
    assert divergent["ok"] is False
    assert divergent["mutation_capability"]["available"] is False
    assert divergent["mutation_capability"]["status"] == "config_hash_diverged"


def test_learning_worker_projection_rejects_release_identity_drift(tmp_path) -> None:
    from backend.core.release_identity import process_release_identity
    from backend.services.backend_readiness import BackendReadinessService
    from backend.services.learning_worker_capability import STATUS_KEY

    db_path = _capability_db(tmp_path)
    cap = LearningWorkerCapability(db_path=db_path, boot_id="boot-release-drift")
    cap.mark_ready(config_hash="cfg-a", overlay_hash="ovl-a", recovery_status="complete")
    cap.publish()

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT value_json FROM runtime_kv WHERE key=?",
            (STATUS_KEY,),
        ).fetchone()
        payload = json.loads(row[0])
        payload["release_identity"] = {
            **process_release_identity(),
            "head": "old-worker-head",
        }
        conn.execute(
            "UPDATE runtime_kv SET value_json=? WHERE key=?",
            (json.dumps(payload, sort_keys=True), STATUS_KEY),
        )
        conn.commit()
    finally:
        conn.close()

    status = BackendReadinessService(db_path=db_path)._learning_worker_capability_status(
        runtime_snapshot={"config_hash": "cfg-a"},
        runtime_overlay={"overlay_hash": "ovl-a"},
    )

    assert status["ok"] is False
    assert status["release_identity_match"] is False
    assert status["mutation_capability"]["available"] is False
    assert status["mutation_capability"]["status"] == "release_identity_diverged"


def test_learning_worker_projection_becomes_stale_after_75_seconds(tmp_path) -> None:
    from backend.services.backend_readiness import BackendReadinessService

    db_path = _capability_db(tmp_path)
    cap = LearningWorkerCapability(db_path=db_path)
    cap.mark_ready(config_hash="cfg", overlay_hash="", recovery_status="complete")
    cap.publish()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE runtime_kv SET updated_at=? WHERE key='learning_worker.capability.v2'",
            (time.time() - 76.0,),
        )
        conn.commit()
    finally:
        conn.close()

    status = BackendReadinessService(db_path=db_path)._learning_worker_capability_status(
        runtime_snapshot={"config_hash": "cfg"},
        runtime_overlay={"overlay_hash": ""},
    )
    assert status["state"] == "stale"
    assert status["mutation_capability"]["available"] is False
