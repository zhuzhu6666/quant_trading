from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from alpha.decision_policy import WeightDecision
from backend.core.db import STATE_DB_DDL, connect_sqlite
from backend.services.factor_weight_change import FactorWeightChangeService
from backend.services.learning_application_state import LearningApplicationStateService
from config import runtime_config


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


class _ExpansionPolicy:
    def decide(self, **_kwargs):
        return {
            "alpha_x": WeightDecision(
                factor="alpha_x",
                old_weight=0.7,
                new_weight=1.0,
                reason="test governed expansion",
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


def _coordinated_service(monkeypatch, tmp_path):
    path = tmp_path / "state-coordinated.db"
    _init(path)
    service = FactorWeightChangeService(path)
    monkeypatch.setattr(
        service.admission,
        "evaluate",
        lambda **_kwargs: {"allowed": True, "status": "admitted"},
    )
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
    monkeypatch.setattr(
        "backend.services.factor_weight_change.ExperiencePriorService.priors",
        lambda _self: {},
    )
    monkeypatch.setattr(
        "backend.core.static_feature_flags.shared_static_feature_flags",
        lambda: SimpleNamespace(
            governance_mutation_coordinator_v2_mode="dual_record"
        ),
    )
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

    result = service.execute(
        source="test_governed_weight",
        producer="test",
        run_id="run-expansion",
        actor="system:test",
        reason="v16 expansion preflight",
        factor_configs={"alpha_x": {"role": "alpha"}},
        current_weights={"alpha_x": 0.7},
        decision_policy=_ExpansionPolicy(),
        risk_check=lambda _plan: {"allowed": True, "reason": "test"},
    )

    assert result["status"] == "blocked_by_admission"
    assert result["admission_status"] == "blocked_v16_command_required"
    assert result["applications"] == {}
    assert reserved == []


def test_production_weight_tightening_does_not_require_v16_preflight(
    monkeypatch, tmp_path
):
    mutation = _Mutation()
    _path, service = _service(monkeypatch, tmp_path, mutation)
    monkeypatch.setenv("QUANT_ALLOW_PYTEST_STATE_OVERLAY_WRITE", "1")
    monkeypatch.setattr(
        "backend.services.factor_weight_change.is_state_db_path", lambda _path: True
    )
    monkeypatch.setattr(
        "backend.services.v16_command_gate.V16CommandGate.authorize",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("tightening must not depend on V16 availability")
        ),
    )

    result = _execute(service)

    assert result["status"] == "applied"
    assert len(mutation.calls) == 1


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


def test_coordinated_weight_change_atomically_binds_all_domain_facts(
    monkeypatch, tmp_path
):
    runtime_config.reset_for_tests()
    path, service = _coordinated_service(monkeypatch, tmp_path)

    result = _execute(service)

    assert result["status"] == "applied"
    assert result["atomic_domain_commit"] is True
    mutation_id = result["mutation"]["mutation_id"]
    application_id = result["applications"]["alpha_x"]
    conn = connect_sqlite(path, read_only=True)
    try:
        app = conn.execute(
            "SELECT status, mutation_id, details_json FROM learning_application_log "
            "WHERE application_id=?",
            (application_id,),
        ).fetchone()
        effect = conn.execute(
            "SELECT status, mutation_id FROM learning_application_effect "
            "WHERE application_id=?",
            (application_id,),
        ).fetchone()
        reservation = conn.execute(
            "SELECT status, application_id, mutation_id "
            "FROM learning_experiment_reservation"
        ).fetchone()
    finally:
        conn.close()
        runtime_config.reset_for_tests()
    assert tuple(app[:2]) == ("applied", mutation_id)
    assert json.loads(app[2])["application_state"]["atomic_commit"] is True
    assert tuple(effect) == ("observing", mutation_id)
    assert tuple(reservation) == ("consumed", application_id, mutation_id)


def test_coordinated_weight_domain_fault_rolls_back_every_fact(
    monkeypatch, tmp_path
):
    runtime_config.reset_for_tests()
    path, service = _coordinated_service(monkeypatch, tmp_path)
    original = service._write_atomic_domain

    def fail_after_domain_writes(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("fault_after_factor_domain_writes")

    monkeypatch.setattr(service, "_write_atomic_domain", fail_after_domain_writes)

    result = _execute(service)

    assert result["status"] == "governance_error"
    assert result["applications"] == {}
    conn = connect_sqlite(path, read_only=True)
    try:
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "learning_application_log",
                "learning_application_effect",
                "learning_experiment_reservation",
                "runtime_config_overlay",
                "runtime_config_snapshot",
            )
        }
        intent = conn.execute(
            "SELECT status FROM governance_mutation_intent"
        ).fetchone()[0]
    finally:
        conn.close()
        runtime_config.reset_for_tests()
    assert counts == {table: 0 for table in counts}
    assert intent == "aborted"


def test_coordinated_batch_admission_block_rolls_back_runtime_target(
    monkeypatch, tmp_path
):
    runtime_config.reset_for_tests()
    path, service = _coordinated_service(monkeypatch, tmp_path)
    monkeypatch.setenv("QUANT_LEARNING_MAX_ACTIVE_EXPERIMENTS", "1")
    conn = connect_sqlite(path)
    try:
        conn.execute(
            """
            INSERT INTO learning_application_log
            (application_id, cycle_ts, scope_type, scope_key, action, status, created_at)
            VALUES ('existing-app', 1.0, 'factor', 'existing-factor',
                    'update_weight', 'applied', 1.0)
            """
        )
        conn.execute(
            """
            INSERT INTO learning_application_effect
            (application_id, scope_type, scope_key, action, status, created_at, updated_at)
            VALUES ('existing-app', 'factor', 'existing-factor',
                    'update_weight', 'observing', 1.0, 1.0)
            """
        )
        conn.commit()
    finally:
        conn.close()

    result = _execute(service)

    assert result["status"] == "blocked_by_admission"
    assert result["applications"] == {}
    conn = connect_sqlite(path, read_only=True)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM learning_application_log"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM learning_experiment_reservation"
        ).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM runtime_config_overlay").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM runtime_config_snapshot").fetchone()[0] == 0
        assert conn.execute(
            "SELECT status FROM governance_mutation_intent"
        ).fetchone()[0] == "aborted"
    finally:
        conn.close()
        runtime_config.reset_for_tests()


def test_double_worker_same_scope_creates_one_atomic_weight_mutation(
    monkeypatch, tmp_path
):
    runtime_config.reset_for_tests()
    path, first_service = _coordinated_service(monkeypatch, tmp_path)
    second_service = FactorWeightChangeService(path)
    monkeypatch.setattr(
        second_service.admission,
        "evaluate",
        lambda **_kwargs: {"allowed": True, "status": "admitted"},
    )
    monkeypatch.setattr(
        second_service,
        "_replay_admission",
        lambda _decisions: {
            "required": True,
            "allowed": True,
            "max_delta": 0.3,
            "replay_run_id": "replay-test",
            "evidence_grade": "A",
        },
    )
    from backend.services.governance_mutation_coordinator import (
        GovernanceMutationCoordinator,
    )

    original_claim = GovernanceMutationCoordinator._claim_v16
    first_reserved = threading.Event()
    release_first = threading.Event()
    claim_lock = threading.Lock()
    claim_count = 0

    def hold_first_claim(self, plan, reserved):
        nonlocal claim_count
        with claim_lock:
            claim_count += 1
            current = claim_count
        if current == 1:
            first_reserved.set()
            if not release_first.wait(timeout=10):
                raise TimeoutError("double_worker_test_release_timeout")
        return original_claim(self, plan, reserved)

    monkeypatch.setattr(
        GovernanceMutationCoordinator, "_claim_v16", hold_first_claim
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(_execute, first_service)
        assert first_reserved.wait(timeout=10)
        second_future = pool.submit(_execute, second_service)
        second = second_future.result(timeout=10)
        release_first.set()
        first = first_future.result(timeout=10)

    results = [first, second]
    assert sum(item["status"] == "applied" for item in results) == 1
    assert sum(item["status"] == "governance_error" for item in results) == 1
    conn = connect_sqlite(path, read_only=True)
    try:
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "governance_mutation_intent",
                "runtime_config_snapshot",
                "learning_application_log",
                "learning_application_effect",
                "learning_experiment_reservation",
            )
        }
        statuses = conn.execute(
            "SELECT status FROM learning_application_log"
        ).fetchall()
    finally:
        conn.close()
        runtime_config.reset_for_tests()
    assert counts == {table: 1 for table in counts}
    assert [row[0] for row in statuses] == ["applied"]
