from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from backend.services.governance_mutation_coordinator import (
    GovernanceMutationCoordinator,
    GovernanceMutationPlan,
)
from backend.services.governance_startup_recovery import (
    GovernanceStartupRecoveryService,
)
from backend.services.v16_command_gate import V16CommandGate


def _plan(mutation_id: str, target: str) -> GovernanceMutationPlan:
    return GovernanceMutationPlan(
        patch={"position_supervisor_template_id": target},
        source="pytest_crash_recovery",
        actor="system:pytest",
        action="switch_position_supervisor_template",
        control_surface="supervisor_template",
        scope_type="supervisor_template",
        scope_key="position_supervisor",
        idempotency_key=mutation_id,
        mutation_id=mutation_id,
    )


def test_kill9_startup_recovery_releases_stale_scope_and_expired_claim(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Persist the exact ownership state left when a process dies mid-mutation."""
    db_path = tmp_path / "crashed-process.db"
    coordinator = GovernanceMutationCoordinator(db_path)
    first = coordinator.reserve(_plan("mutation-before-kill", "template:a"))
    assert first["status"] == "reserved"

    # A second worker is blocked before startup recovery, even though the
    # original owner no longer exists.
    blocked = GovernanceMutationCoordinator(db_path).reserve(
        _plan("mutation-blocked", "template:b")
    )
    assert blocked["status"] == "scope_busy"

    V16CommandGate.ensure_finalize_schema(db_path)
    stale_at = time.time() - 3_600.0
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE governance_mutation_intent SET updated_at=? WHERE mutation_id=?",
            (stale_at, "mutation-before-kill"),
        )
        conn.execute(
            """INSERT INTO v16_brain_command
               (command_id, target_agent, scope_type, scope_key, action,
                decision, status, claim_status, claim_token, claimed_at,
                claim_expires_at, authority_issued_at, created_at, updated_at)
               VALUES ('claim-before-kill', 'factor_governance',
                       'factor_weight', 'alpha', 'update_weight', 'delegate',
                       'active', 'claimed', 'dead-worker-token', ?, ?, ?, ?, ?)""",
            (stale_at, stale_at + 30.0, stale_at, stale_at, stale_at),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setenv("QUANT_GOVERNANCE_INTENT_STALE_AFTER_SECONDS", "300")
    result = GovernanceStartupRecoveryService(db_path).run(
        process_role="learning_worker"
    )

    assert result["ok"] is True
    assert result["aborted_intent_count"] == 1
    assert result["released_claim_count"] == 1
    conn = sqlite3.connect(db_path)
    try:
        intent = conn.execute(
            "SELECT status FROM governance_mutation_intent WHERE mutation_id=?",
            ("mutation-before-kill",),
        ).fetchone()
        claim = conn.execute(
            """SELECT claim_status, claim_token, authority_issued_at
               FROM v16_brain_command WHERE command_id='claim-before-kill'"""
        ).fetchone()
    finally:
        conn.close()
    assert intent == ("aborted",)
    assert claim == ("available", "", stale_at)

    admitted = GovernanceMutationCoordinator(db_path).reserve(
        _plan("mutation-after-restart", "template:c")
    )
    assert admitted["status"] == "reserved"


def test_backend_and_learning_worker_both_wire_crash_recovery() -> None:
    backend_source = Path(
        "backend/services/backend_runtime_lifecycle.py"
    ).read_text(encoding="utf-8")
    worker_source = Path("scripts/learning_worker.py").read_text(encoding="utf-8")

    assert 'GovernanceStartupRecoveryService().run(process_role="backend")' in backend_source
    assert "GovernanceStartupRecoveryService().run(" in worker_source
    assert 'process_role="learning_worker"' in worker_source
