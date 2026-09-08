"""Supervisor SHADOW enrollment: birth, idempotency, isolation.

Covers FactorLifecycleService.register_supervisor_shadow: generated
supervisor templates get a durable SHADOW identity without leaking into
the factor alpha namespace (scoring, backpressure, admission).
"""

from pathlib import Path
import json
import sqlite3

from backend.services.factor_lifecycle_service import FactorLifecycleService

TID = "position_supervisor:auto_tighten.abc123def4.v1"
THASH = "f" * 64


def _service(tmp_path: Path) -> FactorLifecycleService:
    return FactorLifecycleService(tmp_path / "sup.sqlite")


def _enroll(svc: FactorLifecycleService) -> dict:
    return svc.register_supervisor_shadow(
        template_id=TID,
        template_hash=THASH,
        template_version="auto_tighten.abc123def4.v1",
        base_template_id="position_supervisor:default.v1",
        candidate_patch={
            "path": "thresholds.giveback_tighten_threshold",
            "base_value": 0.35,
            "candidate_value": 0.22,
            "regime_stratum": "*",
        },
        evidence_refs={"suggestion_id": "s1"},
    )


def test_supervisor_shadow_birth(tmp_path: Path):
    svc = _service(tmp_path)
    res = _enroll(svc)
    assert res["ok"] is True
    conn = sqlite3.connect(tmp_path / "sup.sqlite")
    try:
        row = conn.execute(
            "SELECT lifecycle_stage, origin, runtime_admission, artifact_hash,"
            " definition_fingerprint FROM factor_lifecycle_state"
            " WHERE factor_id=?",
            (TID,),
        ).fetchone()
        assert row is not None
        assert row[0] == "SHADOW"
        assert row[1] == "supervisor"
        assert row[4] == THASH
        patch_row = conn.execute(
            "SELECT patch_json FROM governance_mutation_intent"
            " WHERE mutation_id LIKE 'factor-lifecycle%' OR scope_type='factor'"
            " ORDER BY created_at DESC LIMIT 5"
        ).fetchall()
        found = False
        for (patch_json,) in patch_row:
            try:
                patch = json.loads(patch_json or "{}")
            except Exception:
                continue
            sig = patch.get("factor_signal_config") or {}
            entry = sig.get(TID)
            if isinstance(entry, dict):
                found = True
                assert entry.get("role") != "alpha"
                assert entry.get("enabled") is False
                assert "weight" not in entry
        assert found, "signal_config entry for supervisor template missing"
    finally:
        conn.close()


def test_supervisor_shadow_idempotent(tmp_path: Path):
    svc = _service(tmp_path)
    first = _enroll(svc)
    assert first["ok"] is True
    second = _enroll(svc)
    assert second["ok"] is True
    assert second["status"] == "already_shadow"
    conn = sqlite3.connect(tmp_path / "sup.sqlite")
    try:
        n = conn.execute(
            "SELECT count(*) FROM factor_lifecycle_state WHERE factor_id=?",
            (TID,),
        ).fetchone()[0]
        assert n == 1
    finally:
        conn.close()


def test_supervisor_shadow_rejects_non_auto(tmp_path: Path):
    svc = _service(tmp_path)
    res = svc.register_supervisor_shadow(
        template_id="position_supervisor:default.v1",
        template_hash=THASH,
    )
    assert res["ok"] is False


def test_supervisor_rows_excluded_from_factor_backpressure(tmp_path: Path):
    svc = _service(tmp_path)
    assert _enroll(svc)["ok"] is True
    conn = sqlite3.connect(tmp_path / "sup.sqlite")
    try:
        total = conn.execute(
            "SELECT count(*) FROM factor_lifecycle_state"
        ).fetchone()[0]
        scoped = conn.execute(
            "SELECT count(*) FROM factor_lifecycle_state"
            " WHERE origin IN ('dsl', 'shadow', 'discovered')"
                " AND lifecycle_stage NOT IN"
            " ('QUARANTINED', 'RETIRED')"
        ).fetchone()[0]
        assert total == 1
        assert scoped == 0
    finally:
        conn.close()
