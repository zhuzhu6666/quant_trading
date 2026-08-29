from __future__ import annotations

import json
import subprocess
import time

import pytest

from alpha.decision_policy import WeightDecision
from backend.core.db import STATE_DB_DDL, connect_sqlite
from backend.services.backend_readiness import BackendReadinessService
from backend.services.autonomous_evolution_runner import AutonomousEvolutionNurseryRunner
from backend.services.factor_weight_change import FactorWeightChangeService
from backend.services.release_control import ReleaseControlService
from backend.services.replay_harness import ReplayHarnessService
from backend.services.v15_phase0 import V15Phase0CompletionService


class _LargeWeightReductionPolicy:
    def decide(self, **_kwargs):
        return {
            "alpha_x": WeightDecision(
                factor="alpha_x",
                old_weight=1.0,
                new_weight=0.7,
                reason="test governed reduction",
                confidence=0.9,
            )
        }

    fast_decide = decide


def _repo_head() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        text=True,
    ).strip()


def _init_state(db_path, *, config_hash: str = "cfg-current") -> None:
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.execute(
            """
            INSERT INTO runtime_config_snapshot
            (config_hash, source, config_json, run_id, created_at)
            VALUES (?, 'test', '{}', 'snapshot-test', ?)
            """,
            (config_hash, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_replay(
    db_path,
    *,
    run_id: str,
    kind: str,
    created_at: float,
    config_hash: str = "cfg-current",
    grade: str = "A",
    status: str = "completed",
    code_version: str | None = None,
    dataset_hash: str = "dataset-hash",
    artifact_hash: str = "artifact-hash",
) -> None:
    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            """
            INSERT INTO replay_report
            (replay_run_id, scope_json, input_dataset_hash, runtime_config_hash,
             code_version, decision_count, matched_live_count, mismatch_count,
             metric_summary_json, replay_error, evidence_grade, artifact_path,
             artifact_hash, status, created_at)
            VALUES (?, ?, ?, ?, ?, 1, 1, 0, '{}', '', ?,
                    '/tmp/replay.json', ?, ?, ?)
            """,
            (
                run_id,
                json.dumps({"schema_version": "replay_scope.v1", "kind": kind}),
                dataset_hash,
                config_hash,
                _repo_head() if code_version is None else code_version,
                grade,
                artifact_hash,
                status,
                created_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_governance_replay_status_uses_full_evidence_not_newer_freshness(tmp_path):
    db_path = tmp_path / "state.db"
    _init_state(db_path)
    now = time.time()
    _insert_replay(
        db_path,
        run_id="full-evidence",
        kind="bar_replay_evidence",
        created_at=now - 10,
    )
    _insert_replay(
        db_path,
        run_id="newer-freshness",
        kind="bar_replay_freshness",
        created_at=now,
        grade="B",
    )

    result = ReplayHarnessService(db_path).status()

    assert result["ok"] is True
    assert result["status"] == "fresh"
    assert result["latest_report"]["replay_run_id"] == "full-evidence"
    assert result["latest_report"]["scope"]["kind"] == "bar_replay_evidence"


def test_governance_replay_status_rejects_old_runtime_config_binding(tmp_path):
    db_path = tmp_path / "state.db"
    _init_state(db_path, config_hash="cfg-current")
    _insert_replay(
        db_path,
        run_id="old-config-evidence",
        kind="bar_replay_evidence",
        created_at=time.time(),
        config_hash="cfg-old",
    )

    result = ReplayHarnessService(db_path).status()

    assert result["ok"] is False
    assert result["status"] == "degraded"
    assert "runtime_config_hash_mismatch" in result["blockers"]


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"dataset_hash": ""}, "input_dataset_hash_missing"),
        ({"artifact_hash": ""}, "artifact_hash_missing"),
        ({"code_version": ""}, "code_version_missing"),
        ({"code_version": "different-head"}, "code_version_mismatch"),
    ],
)
def test_governance_replay_status_rejects_incomplete_bindings(
    tmp_path,
    override,
    reason,
):
    db_path = tmp_path / "state.db"
    _init_state(db_path)
    _insert_replay(
        db_path,
        run_id="incomplete-binding",
        kind="bar_replay_evidence",
        created_at=time.time(),
        **override,
    )

    result = ReplayHarnessService(db_path).status()

    assert result["ok"] is False
    assert reason in result["blockers"]


def test_release_checklist_rejects_freshness_report_as_governance_evidence(tmp_path):
    db_path = tmp_path / "state.db"
    _init_state(db_path)
    _insert_replay(
        db_path,
        run_id="freshness-only",
        kind="bar_replay_freshness",
        created_at=time.time(),
        grade="B",
    )

    checklist = ReleaseControlService(db_path).build_checklist(
        readiness={"ready_for_release": True},
    )

    assert checklist["ok"] is False
    assert checklist["replay"]["ok"] is False
    assert checklist["replay"]["scope_kind"] == "bar_replay_evidence"


def test_release_status_requires_completed_current_config_binding(tmp_path):
    db_path = tmp_path / "state.db"
    _init_state(db_path)
    _insert_replay(
        db_path,
        run_id="full-evidence",
        kind="bar_replay_evidence",
        created_at=time.time(),
    )
    service = ReleaseControlService(db_path)
    started = service.start_release(
        release_class="test",
        readiness={"ready_for_release": True},
        run_id="release-current",
    )
    service.finish_release(
        started["run_id"],
        status="completed",
        readiness={"ready_for_release": True},
    )

    current = service.status()

    assert current["ok"] is True
    assert current["status"] == "completed"

    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            """
            INSERT INTO runtime_config_snapshot
            (config_hash, source, config_json, run_id, created_at)
            VALUES ('cfg-next', 'test', '{}', 'snapshot-next', ?)
            """,
            (time.time() + 1,),
        )
        conn.commit()
    finally:
        conn.close()

    drifted = service.status()

    assert drifted["ok"] is False
    assert drifted["status"] == "config_mismatch"
    assert "release_runtime_config_hash_mismatch" in drifted["blockers"]


def test_backend_readiness_does_not_treat_failed_release_run_as_ready(tmp_path):
    db_path = tmp_path / "state.db"
    _init_state(db_path)
    _insert_replay(
        db_path,
        run_id="full-evidence",
        kind="bar_replay_evidence",
        created_at=time.time(),
    )
    release = ReleaseControlService(db_path)
    started = release.start_release(
        release_class="test",
        readiness={"ready_for_release": True},
        run_id="release-failed",
    )
    release.finish_release(
        started["run_id"],
        status="failed",
        readiness={"ready_for_release": True},
    )

    readiness = BackendReadinessService(db_path=db_path).build()

    assert readiness["release"]["ok"] is False
    assert readiness["release"]["status"] == "failed"


def test_nursery_light_readiness_reuses_normalized_replay_and_release_status(tmp_path):
    db_path = tmp_path / "state.db"
    _init_state(db_path)
    _insert_replay(
        db_path,
        run_id="full-evidence",
        kind="bar_replay_evidence",
        created_at=time.time(),
    )
    release = ReleaseControlService(db_path)
    started = release.start_release(
        release_class="test",
        readiness={"ready_for_release": True},
        run_id="release-failed",
    )
    release.finish_release(
        started["run_id"],
        status="failed",
        readiness={"ready_for_release": True},
    )

    readiness = AutonomousEvolutionNurseryRunner(db_path).build_light_readiness()

    assert readiness["replay"]["ok"] is True
    assert readiness["replay"]["latest_report"]["replay_run_id"] == "full-evidence"
    assert readiness["release"]["ok"] is False
    assert readiness["release"]["status"] == "failed"
    assert "release_status_not_completed" in readiness["release"]["blockers"]


def test_factor_weight_execute_rejects_freshness_as_replay_evidence(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "state.db"
    _init_state(db_path)
    _insert_replay(
        db_path,
        run_id="freshness-only",
        kind="bar_replay_freshness",
        created_at=time.time(),
        grade="B",
    )
    service = FactorWeightChangeService(db_path)
    monkeypatch.setattr(
        service.admission,
        "evaluate",
        lambda **_kwargs: {"allowed": True, "status": "admitted"},
    )
    monkeypatch.setattr(
        "backend.services.factor_weight_change.ExperiencePriorService.priors",
        lambda _self: {},
    )

    result = service.execute(
        source="test_governed_weight",
        producer="test",
        run_id="run-freshness-only",
        actor="system:test",
        reason="replay contract test",
        factor_configs={"alpha_x": {"role": "alpha"}},
        current_weights={"alpha_x": 1.0},
        decision_policy=_LargeWeightReductionPolicy(),
        risk_check=lambda _plan: {"allowed": True},
    )

    assert result["status"] == "blocked_by_replay"
    assert result["applications"] == {}
    conn = connect_sqlite(db_path, read_only=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM learning_application_log").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM runtime_config_overlay").fetchone()[0] == 0
    finally:
        conn.close()


def test_v15_phase0_does_not_treat_failed_release_id_as_evidence():
    readiness = {
        "schema_version": "backend_readiness.v1",
        "v15": {
            "schema_version": "v15_readiness_contract.v1",
            "snapshot": {"ok": True, "config_hash": "cfg-current"},
            "control_plane_boundaries": {
                "runtime_overlay_is_source_of_truth": True,
                "runtime_snapshot_required_for_rollback": True,
                "risk_policy_service_required": True,
                "decision_policy_required_for_weight_writes": True,
                "models_shadow_or_advisory_only": True,
            },
        },
        "replay": {
            "ok": True,
            "schema_version": "replay_readiness.v1",
            "status": "fresh",
            "latest_report": {
                "replay_run_id": "full-evidence",
                "evidence_grade": "A",
            },
        },
        "release": {
            "ok": False,
            "schema_version": "release_readiness.v1",
            "status": "failed",
            "latest_release": {
                "run_id": "release-failed",
                "status": "failed",
            },
        },
        "autonomy_health": {
            "schema_version": "autonomy_health.v1",
            "posture": "full",
            "read_only": True,
        },
        "incident_control": {
            "schema_version": "runtime_incident_control.v1",
            "mode": "normal",
        },
    }

    result = V15Phase0CompletionService().build(readiness=readiness)

    assert result["operationally_ready"] is False
    assert "release_run_ledger_v1" in result["evidence_gaps"]
