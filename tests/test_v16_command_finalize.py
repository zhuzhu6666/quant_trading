from __future__ import annotations

import time

from backend.services._brain_helpers import connect, execute
from backend.services.v16_command_gate import V16CommandGate


def _command(db_path, *, command_id="command-1", evidence_fingerprint="evidence-1"):
    V16CommandGate.ensure_finalize_schema(db_path)
    conn = connect(db_path)
    now = time.time()
    execute(
        conn,
        """INSERT INTO v16_brain_command
           (command_id, target_agent, scope_type, scope_key, action, decision,
            status, evidence_json, delegation_json, evidence_fingerprint,
            created_at, updated_at)
           VALUES (?, 'factor_governance', 'factor_weight', 'alpha',
                   'update_weight', 'delegate', 'active', '{}', '{}', ?, ?, ?)""",
        (command_id, evidence_fingerprint, now, now),
    )
    conn.commit()
    conn.close()


def test_v16_apply_count_increments_only_once_at_finalize(tmp_path):
    db_path = tmp_path / "state.db"
    _command(db_path)
    claim = V16CommandGate.claim(
        db_path,
        target_agent="factor_governance",
        scope_type="factor_weight",
        scope_key="alpha",
        action="update_weight",
        evidence_fingerprint="evidence-1",
    )
    assert claim["allowed"] is True

    conn = connect(db_path, read_only=True)
    assert execute(conn, "SELECT apply_count FROM v16_brain_command").fetchone()[0] == 0
    conn.close()

    first = V16CommandGate.finalize(
        db_path,
        command_id="command-1",
        claim_token=claim["claim_token"],
        mutation_id="mutation-1",
        config_hash="config-1",
        domain_hash="domain-1",
    )
    second = V16CommandGate.finalize(
        db_path,
        command_id="command-1",
        claim_token=claim["claim_token"],
        mutation_id="mutation-1",
        config_hash="config-1",
        domain_hash="domain-1",
    )
    assert first["status"] == "v16_command_finalized"
    assert second["status"] == "v16_command_already_finalized"

    conn = connect(db_path, read_only=True)
    row = execute(
        conn,
        """SELECT claim_status, apply_count, finalized_mutation_id,
                  finalized_config_hash, finalized_domain_hash
           FROM v16_brain_command""",
    ).fetchone()
    conn.close()
    assert tuple(row) == ("finalized", 1, "mutation-1", "config-1", "domain-1")


def test_v16_finalize_rejects_different_mutation_binding(tmp_path):
    db_path = tmp_path / "state.db"
    _command(db_path)
    claim = V16CommandGate.claim(
        db_path,
        target_agent="factor_governance",
        scope_type="factor_weight",
        scope_key="alpha",
        action="update_weight",
        evidence_fingerprint="evidence-1",
    )
    V16CommandGate.finalize(
        db_path,
        command_id="command-1",
        claim_token=claim["claim_token"],
        mutation_id="mutation-1",
        config_hash="config-1",
        domain_hash="domain-1",
    )
    rejected = V16CommandGate.finalize(
        db_path,
        command_id="command-1",
        claim_token=claim["claim_token"],
        mutation_id="mutation-2",
        config_hash="config-2",
        domain_hash="domain-2",
    )
    assert rejected["allowed"] is False
    assert rejected["status"] == "v16_command_finalized_for_other_mutation"


def _issued_command(db_path, *, issued_at: float, command_id: str) -> None:
    V16CommandGate.ensure_finalize_schema(db_path)
    conn = connect(db_path)
    execute(
        conn,
        """INSERT INTO v16_brain_command
           (command_id, target_agent, scope_type, scope_key, action, decision,
            status, evidence_json, delegation_json, authority_issued_at,
            created_at, updated_at)
           VALUES (?, 'factor_governance', 'factor_weight', 'alpha',
                   'update_weight', 'delegate', 'active', '{}', '{}', ?, ?, ?)""",
        (command_id, issued_at, issued_at, issued_at),
    )
    conn.commit()
    conn.close()


def test_release_cannot_refresh_stale_v16_authority(tmp_path, monkeypatch):
    import backend.services.v16_command_gate as gate_module

    db_path = tmp_path / "release-stale.db"
    _issued_command(db_path, issued_at=1_000.0, command_id="release-stale")
    monkeypatch.setenv("QUANT_V16_COMMAND_MAX_AGE_SECONDS", "60")
    monkeypatch.setattr(gate_module.time, "time", lambda: 1_010.0)
    claim = V16CommandGate.claim(
        db_path,
        target_agent="factor_governance",
        scope_type="factor_weight",
        scope_key="alpha",
        action="update_weight",
        command_id="release-stale",
        claim_ttl_seconds=120.0,
    )
    assert claim["allowed"] is True

    # The claim is still live, but the underlying delegation is now stale.
    # release() may update operational updated_at; it must not renew authority.
    monkeypatch.setattr(gate_module.time, "time", lambda: 1_070.0)
    released = V16CommandGate.release(
        db_path,
        command_id="release-stale",
        claim_token=claim["claim_token"],
        reason="worker_retry",
    )
    assert released["allowed"] is True
    assert V16CommandGate.authorize(
        db_path,
        target_agent="factor_governance",
        scope_type="factor_weight",
        scope_key="alpha",
        action="update_weight",
        command_id="release-stale",
        max_age_seconds=60.0,
    )["allowed"] is False
    assert V16CommandGate.claim(
        db_path,
        target_agent="factor_governance",
        scope_type="factor_weight",
        scope_key="alpha",
        action="update_weight",
        command_id="release-stale",
    )["allowed"] is False

    conn = connect(db_path, read_only=True)
    row = execute(
        conn,
        "SELECT authority_issued_at, updated_at FROM v16_brain_command",
    ).fetchone()
    conn.close()
    assert tuple(row) == (1_000.0, 1_070.0)


def test_expired_claim_recovery_cannot_refresh_stale_v16_authority(
    tmp_path, monkeypatch
):
    import backend.services.v16_command_gate as gate_module

    db_path = tmp_path / "expired-stale.db"
    _issued_command(db_path, issued_at=2_000.0, command_id="expired-stale")
    monkeypatch.setenv("QUANT_V16_COMMAND_MAX_AGE_SECONDS", "60")
    monkeypatch.setattr(gate_module.time, "time", lambda: 2_010.0)
    claim = V16CommandGate.claim(
        db_path,
        target_agent="factor_governance",
        scope_type="factor_weight",
        scope_key="alpha",
        action="update_weight",
        command_id="expired-stale",
        claim_ttl_seconds=120.0,
    )
    assert claim["allowed"] is True

    recovery = V16CommandGate.recover_expired_claims(db_path, now=2_140.0)
    assert recovery["ok"] is True
    assert recovery["released_count"] == 1
    monkeypatch.setattr(gate_module.time, "time", lambda: 2_140.0)
    retry = V16CommandGate.claim(
        db_path,
        target_agent="factor_governance",
        scope_type="factor_weight",
        scope_key="alpha",
        action="update_weight",
        command_id="expired-stale",
    )
    assert retry["allowed"] is False

    conn = connect(db_path, read_only=True)
    row = execute(
        conn,
        """SELECT claim_status, authority_issued_at, updated_at
           FROM v16_brain_command""",
    ).fetchone()
    conn.close()
    assert row[0] == "available"
    assert tuple(row) == ("available", 2_000.0, 2_140.0)


def test_v16_claim_accepts_cycle_command_for_narrow_action(tmp_path):
    """cycle 级 broad 命令可被 update_weight claim(authorize/claim 一致)。

    回归:生产命令 action='factor_governance_cycle'(scope_type='factor_weight'),
    而 FactorWeightChangeService.execute 用 action='update_weight' claim。
    修复前 claim 在 action 不匹配时直接 continue,导致命令永远无法消费
    (apply_count=0 / aborted:v16_command_required 死锁)。
    """
    db_path = tmp_path / "cycle-claim.db"
    V16CommandGate.ensure_finalize_schema(db_path)
    conn = connect(db_path)
    now = time.time()
    execute(
        conn,
        """INSERT INTO v16_brain_command
           (command_id, target_agent, scope_type, scope_key, action, decision,
            status, evidence_json, delegation_json, created_at, updated_at)
           VALUES (?, 'factor_governance', 'factor_weight', 'alpha_weight_policy',
                   'factor_governance_cycle', 'delegate', 'active', '{}', '{}', ?, ?)""",
        ("cycle-cmd-1", now, now),
    )
    conn.commit()
    conn.close()

    claim = V16CommandGate.claim(
        db_path,
        target_agent="factor_governance",
        scope_type="factor_weight",
        scope_key="fib_rejection_confirmation",
        action="update_weight",
    )
    assert claim["allowed"] is True
    assert claim["command_id"] == "cycle-cmd-1"
