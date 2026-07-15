from __future__ import annotations

import json
import time

import pytest

from alpha.decision_policy import WeightDecision
from backend.core.db import STATE_DB_DDL, connect_sqlite
from backend.services.factor_weight_change import FactorWeightChangeService
from backend.services.learning_application_state import LearningApplicationStateService


class _Policy:
    def decide(self, **_kwargs):
        return {
            "alpha_x": WeightDecision(
                factor="alpha_x",
                old_weight=1.0,
                new_weight=0.7,
                reason="test governed change",
                confidence=0.9,
            )
        }

    fast_decide = decide


class _Mutation:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.calls = []

    def apply_patch(self, patch, **kwargs):
        self.calls.append((patch, kwargs))
        if self.fail:
            raise RuntimeError("mutation interrupted")
        return {
            "ok": True,
            "status": "applied",
            "snapshot": {"config_version": 7, "config_hash": "hash-7"},
        }


def _init(path):
    conn = connect_sqlite(path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.commit()
    finally:
        conn.close()


def _service(monkeypatch, tmp_path, mutation):
    path = tmp_path / "state.db"
    _init(path)
    service = FactorWeightChangeService(path)
    monkeypatch.setattr(service.admission, "evaluate", lambda **_kwargs: {"allowed": True, "status": "admitted"})
    monkeypatch.setattr(service, "_mutation_service", lambda: mutation)
    monkeypatch.setattr(
        service,
        "_replay_admission",
        lambda _decisions: {
            "required": True,
            "allowed": True,
            "max_delta": 0.3,
            "replay_run_id": "replay-test",
            "evidence_grade": "A",
        },
    )
    monkeypatch.setattr("backend.services.factor_weight_change.ExperiencePriorService.priors", lambda _self: {})
    return path, service


def test_weight_change_blocks_large_delta_without_replay(monkeypatch, tmp_path):
    mutation = _Mutation()
    _path, service = _service(monkeypatch, tmp_path, mutation)
    monkeypatch.setattr(
        service,
        "_replay_admission",
        lambda _decisions: {"required": True, "allowed": False, "max_delta": 0.3},
    )

    result = _execute(service)

    assert result["status"] == "blocked_by_replay"
    assert result["legacy_status"] == "blocked_by_replay_admission"
    assert result["applications"] == {}
    assert mutation.calls == []


def _execute(service):
    return service.execute(
        source="test_governed_weight",
        producer="test",
        run_id="run-1",
        actor="system:test",
        reason="state machine test",
        factor_configs={"alpha_x": {"role": "alpha"}},
        current_weights={"alpha_x": 1.0},
        decision_policy=_Policy(),
        risk_check=lambda _plan: {"allowed": True, "reason": "test"},
    )


def test_weight_change_prepares_before_mutation_and_enters_observation(monkeypatch, tmp_path):
    mutation = _Mutation()
    path, service = _service(monkeypatch, tmp_path, mutation)

    result = _execute(service)

    assert result["status"] == "applied"
    assert len(mutation.calls) == 1
    application_id = result["applications"]["alpha_x"]
    conn = connect_sqlite(path, read_only=True)
    try:
        app = conn.execute(
            "SELECT status, details_json FROM learning_application_log WHERE application_id=?",
            (application_id,),
        ).fetchone()
        effect = conn.execute(
            "SELECT status FROM learning_application_effect WHERE application_id=?",
            (application_id,),
        ).fetchone()
    finally:
        conn.close()
    assert app[0] == "applied"
    assert json.loads(app[1])["application_state"]["status"] == "applied"
    assert effect[0] == "observing"


def test_weight_change_marks_prepared_application_failed_when_mutation_interrupts(monkeypatch, tmp_path):
    path, service = _service(monkeypatch, tmp_path, _Mutation(fail=True))

    result = _execute(service)

    assert result["status"] == "governance_error"
    assert result["error_stage"] == "runtime_mutation"
    assert result["error_type"] == "RuntimeError"
    assert result["error"] == "mutation interrupted"

    conn = connect_sqlite(path, read_only=True)
    try:
        app = conn.execute("SELECT status FROM learning_application_log").fetchone()
        effect = conn.execute("SELECT status FROM learning_application_effect").fetchone()
    finally:
        conn.close()
    assert app[0] == "mutation_failed"
    assert effect[0] == "superseded"


def test_weight_change_releases_reservation_when_risk_check_crashes(monkeypatch, tmp_path):
    _path, service = _service(monkeypatch, tmp_path, _Mutation())
    released = []
    monkeypatch.setattr(service.admission, "release_reservations", lambda ids: released.extend(ids))

    result = service.execute(
        source="test_governed_weight",
        producer="test",
        run_id="run-risk-error",
        actor="system:test",
        reason="risk exception test",
        factor_configs={"alpha_x": {"role": "alpha"}},
        current_weights={"alpha_x": 1.0},
        decision_policy=_Policy(),
        risk_check=lambda _plan: (_ for _ in ()).throw(ConnectionError("risk unavailable")),
    )

    assert result["status"] == "governance_error"
    assert result["error_stage"] == "risk"
    assert result["error_type"] == "ConnectionError"
    assert len(released) == 1


def test_weight_change_reports_admission_infrastructure_error(monkeypatch, tmp_path):
    _path, service = _service(monkeypatch, tmp_path, _Mutation())
    monkeypatch.setattr(
        service.admission,
        "reserve_batch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ConnectionError("postgres unavailable")),
    )

    result = _execute(service)

    assert result["status"] == "governance_error"
    assert result["error_stage"] == "admission"
    assert result["error_type"] == "ConnectionError"


def test_production_system_weight_change_preflights_v16_before_reservation(monkeypatch, tmp_path):
    _path, service = _service(monkeypatch, tmp_path, _Mutation())
    monkeypatch.setenv("QUANT_ALLOW_PYTEST_STATE_OVERLAY_WRITE", "1")
    monkeypatch.setattr("backend.services.factor_weight_change.is_state_db_path", lambda _path: True)
    monkeypatch.setattr(
        "backend.services.v16_command_gate.V16CommandGate.authorize",
        lambda *_args, **_kwargs: {"allowed": False, "status": "v16_command_required"},
    )
    reserved = []
    monkeypatch.setattr(service.admission, "reserve_batch", lambda *_args, **_kwargs: reserved.append(True))

    result = _execute(service)

    assert result["status"] == "blocked_by_admission"
    assert result["admission_status"] == "blocked_v16_command_required"
    assert result["applications"] == {}
    assert reserved == []


def test_prepared_recovery_uses_runtime_snapshot_as_commit_fact(tmp_path):
    path = tmp_path / "state.db"
    _init(path)
    state = LearningApplicationStateService(path)
    application_id = state.prepare(
        scope_key="alpha_x",
        old_weight=1.0,
        new_weight=0.7,
        suggestion_ids=["suggestion-1"],
        cycle_ts=time.time() - 120,
        details={"run_id": "run-recovery", "mutation_source": "test-recovery"},
    )
    conn = connect_sqlite(path)
    try:
        conn.execute(
            """
            INSERT INTO runtime_config_snapshot
            (config_version, config_hash, config_json, source, run_id, created_at)
            VALUES (9, 'hash-9', '{}', 'test-recovery', 'run-recovery', ?)
            """,
            (time.time(),),
        )
        conn.commit()
    finally:
        conn.close()

    result = state.recover_prepared(grace_seconds=1)

    assert result["applied"] == 1
    conn = connect_sqlite(path, read_only=True)
    try:
        row = conn.execute(
            "SELECT status FROM learning_application_log WHERE application_id=?",
            (application_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "applied"
