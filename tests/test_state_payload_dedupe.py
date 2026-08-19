from __future__ import annotations

import json

from backend.api import ops as ops_api
from backend.core.db import STATE_DB_DDL, connect_sqlite
from backend.services import mutation_audit
from backend.services.autonomous_learning import _upsert_sample, ensure_autonomous_learning_tables
from tests.canonical_fixture import seed_canonical_sqlite_file
from backend.services.evolution_ledger import (
    get_evolution_run,
    persist_runtime_config_snapshot,
    record_evolution_decision,
    start_evolution_run,
)
from backend.services.v16_brain_planning import BrainActionPlanEvaluatorService
from config.runtime_config import RuntimeConfig


def test_runtime_payload_is_interned_while_occurrences_remain(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    first = RuntimeConfig(shadow_vote_weight=0.11)
    second = RuntimeConfig(shadow_vote_weight=0.22)

    persist_runtime_config_snapshot(first, source="one", db_path=db_path)
    persist_runtime_config_snapshot(second, source="two", db_path=db_path)
    persist_runtime_config_snapshot(first, source="three", db_path=db_path)

    conn = connect_sqlite(db_path, read_only=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM runtime_config_snapshot").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM runtime_config_payload").fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM runtime_config_snapshot WHERE config_json='{}'"
        ).fetchone()[0] == 3
        assert conn.execute(
            "SELECT COUNT(DISTINCT payload_hash) FROM runtime_config_snapshot"
        ).fetchone()[0] == 2
    finally:
        conn.close()


def test_runtime_snapshot_mutation_id_is_idempotent(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    config = RuntimeConfig(shadow_vote_weight=0.17)
    first = persist_runtime_config_snapshot(
        config,
        source="mutation",
        run_id="run-1",
        mutation_id="mutation-1",
        db_path=db_path,
    )
    second = persist_runtime_config_snapshot(
        config,
        source="retry",
        run_id="run-2",
        mutation_id="mutation-1",
        db_path=db_path,
    )
    assert second["reused"] is True
    assert second["config_version"] == first["config_version"]
    assert second["source"] == "mutation"
    assert second["run_id"] == "run-1"


def test_eval_payload_is_interned_and_run_id_is_idempotent(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    service = BrainActionPlanEvaluatorService(db_path)
    item = {
        "eval_id": "eval-1",
        "plan_id": "plan-1",
        "snapshot_id": "snap-1",
        "action_type": "shadow_context_policy_review",
        "scope_type": "context_policy",
        "status": "comparable",
        "comparison_verdict": "supportive",
        "coverage_score": 0.9,
        "comparison": {"same": True, "score": 0.9},
        "evidence_refs": {"source": "test"},
        "boundary": {"read_only": True},
        "created_at": 100.0,
    }

    service._persist([item], evaluation_run_id="eval-run-1")
    service._persist([{**item, "eval_id": "eval-duplicate"}], evaluation_run_id="eval-run-1")
    service._persist([{**item, "eval_id": "eval-2", "created_at": 101.0}], evaluation_run_id="eval-run-2")

    conn = connect_sqlite(db_path, read_only=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM brain_action_plan_eval").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM brain_action_plan_eval_payload").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM brain_action_plan_eval WHERE comparison_json='{}'"
        ).fetchone()[0] == 2
        assert {
            row[0]
            for row in conn.execute(
                "SELECT eval_id FROM brain_action_plan_eval ORDER BY eval_id"
            ).fetchall()
        } == {"eval-1", "eval-2"}
    finally:
        conn.close()
    latest = service.latest_evals(limit=2)
    assert latest["evals"][0]["comparison"] == item["comparison"]


def test_brain_get_refresh_flag_is_read_only(monkeypatch) -> None:
    class FakePlanner:
        def latest_plans(self, *, limit):
            return {"ok": True, "plans": [], "limit": limit}

        def build_plans(self, **_kwargs):
            raise AssertionError("GET refresh must not persist action plans")

    class FakeEvaluator:
        def latest_evals(self, *, limit):
            return {"ok": True, "evals": [], "limit": limit}

        def evaluate_latest_plans(self, **_kwargs):
            raise AssertionError("GET refresh must not persist evaluations")

    monkeypatch.setattr(ops_api, "BrainActionPlannerService", FakePlanner)
    monkeypatch.setattr(ops_api, "BrainActionPlanEvaluatorService", FakeEvaluator)

    plans = ops_api.get_brain_action_plans(None, refresh=True, limit=3)
    evals = ops_api.get_brain_action_plan_evals(None, refresh=True, limit=3)
    assert plans["action_plans"]["limit"] == 3
    assert evals["action_plan_evals"]["limit"] == 3


def test_mutation_api_projection_keeps_canonical_lineage(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "state.db"
    canonical = record_evolution_decision(
        decision_type="factor_governance_autonomous",
        scope_type="factor",
        scope_key="alpha",
        action="update_weight",
        status="applied",
        result={"mutation": "committed"},
        db_path=db_path,
    )
    monkeypatch.setattr(mutation_audit, "STATE_DB", db_path)
    projection = mutation_audit.record_api_mutation(
        user="system:factor_governance",
        endpoint="test.endpoint",
        action="update_weight",
        status="applied",
        result={"decision_id": canonical},
        canonical_event_id=canonical,
    )

    conn = connect_sqlite(db_path, read_only=True)
    try:
        rows = conn.execute(
            """SELECT decision_id, canonical_event_id, projection_type
               FROM evolution_decision ORDER BY created_at, decision_id"""
        ).fetchall()
        assert len(rows) == 2
        assert rows[0][1] == canonical
        assert rows[0][2] == "canonical"
        assert rows[1][0] == projection
        assert rows[1][1] == canonical
        assert rows[1][2] == "api"
        assert conn.execute("SELECT COUNT(*) FROM mutation_payload").fetchone()[0] == 2
        api_payload = conn.execute(
            """SELECT p.before_json, p.after_json, p.result_json, p.evidence_json
               FROM evolution_decision d
               JOIN mutation_payload p ON p.payload_hash=d.payload_hash
               WHERE d.decision_id=?""",
            (projection,),
        ).fetchone()
        assert api_payload[0:3] == (
            "{}",
            "{}",
            json.dumps({"decision_id": canonical}, sort_keys=True),
        )
        assert json.loads(api_payload[3])["projection_mode"] == "canonical_reference"
    finally:
        conn.close()


def test_mutation_projection_reads_payload_without_event_json_copy(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    start_evolution_run(
        run_type="payload-test",
        db_path=db_path,
        run_id="run-1",
        config=RuntimeConfig(shadow_vote_weight=0.19),
    )
    record_evolution_decision(
        run_id="run-1",
        decision_type="factor_governance_autonomous",
        action="update_weight",
        status="applied",
        evidence={"evidence_id": "e-1"},
        result={"decision_id": "canonical-1"},
        db_path=db_path,
        decision_id="canonical-1",
    )
    conn = connect_sqlite(db_path, read_only=True)
    try:
        dj = json.loads(
            conn.execute(
                "SELECT decision_json FROM evolution_decision WHERE decision_id='canonical-1'"
            ).fetchone()[0]
        )
        # rich payload is interned in mutation_payload, not copied inline into decision_json
        assert "evidence" not in dj and "result" not in dj
        assert dj["action"] == "update_weight" and dj["status"] == "applied"
        pl = conn.execute(
            "SELECT p.evidence_json, p.result_json FROM evolution_decision d "
            "JOIN mutation_payload p ON p.payload_hash=d.payload_hash "
            "WHERE d.decision_id='canonical-1'"
        ).fetchone()
        assert json.loads(pl[0]) == {"evidence_id": "e-1"}
        assert json.loads(pl[1]) == {"decision_id": "canonical-1"}
    finally:
        conn.close()
    run = get_evolution_run("run-1", db_path=db_path)
    assert run["decisions"][0]["evidence"] == {"evidence_id": "e-1"}
    assert run["decisions"][0]["result"] == {"decision_id": "canonical-1"}
    assert run["decisions"][0]["action"] == "update_weight"
    assert run["decisions"][0]["status"] == "applied"


def test_api_projection_rehydrates_canonical_payload_on_read(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "state.db"
    start_evolution_run(
        run_type="projection-read-test",
        db_path=db_path,
        run_id="run-projection",
        config=RuntimeConfig(shadow_vote_weight=0.21),
    )
    canonical_before = {"factor_signal_config": {"rsi_14": {"weight": 0.25}}}
    canonical_after = {"factor_signal_config": {"rsi_14": {"weight": 0.3}}}
    canonical_result = {"mutation_id": "mutation-1", "precision_marker": 0.123456789}
    canonical = record_evolution_decision(
        run_id="run-projection",
        decision_type="factor_governance_autonomous",
        action="update_weight",
        status="applied",
        before=canonical_before,
        after=canonical_after,
        result=canonical_result,
        db_path=db_path,
        decision_id="canonical-projection",
    )
    monkeypatch.setattr(mutation_audit, "STATE_DB", db_path)
    projection = mutation_audit.record_api_mutation(
        user="system:factor_governance",
        endpoint="test.endpoint",
        action="update_weight",
        status="applied",
        before=canonical_before,
        after=canonical_after,
        result=canonical_result,
        canonical_event_id=canonical,
        run_id="run-projection",
    )

    run = get_evolution_run("run-projection", db_path=db_path)
    api_row = next(item for item in run["decisions"] if item["decision_id"] == projection)
    assert api_row["before"] == canonical_before
    assert api_row["after"] == canonical_after
    assert api_row["result"] == canonical_result
    assert api_row["canonical_event_id"] == canonical


def test_learning_sample_noop_does_not_update_timestamp(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    ensure_autonomous_learning_tables(db_path)
    seed_canonical_sqlite_file(db_path)
    item = {
        "sample_id": "sample-1",
        "sample_type": "shadow_open_decision",
        "source_table": "decision_ledger",
        "source_id": "decision-1",
        "decision_id": "decision-1",
        "label_status": "matured",
        "features": {"score": 1},
        "verdict": {"system_contamination": {"contaminated": False}},
        "label": {"outcome_label": "win"},
        "trace": {"decision_id": "decision-1"},
        "integrity": "full",
        "causal_level": "intervention_observed",
        "executable_governance_allowed": True,
    }
    conn = connect_sqlite(db_path)
    try:
        assert _upsert_sample(conn, item) is True
        conn.commit()
        before = conn.execute(
            "SELECT updated_at, content_fingerprint FROM training_sample_row WHERE sample_id='sample-1'"
        ).fetchone()
        assert _upsert_sample(conn, item) is False
        conn.commit()
        after = conn.execute(
            "SELECT updated_at, content_fingerprint FROM training_sample_row WHERE sample_id='sample-1'"
        ).fetchone()
        assert tuple(after) == tuple(before)
    finally:
        conn.close()


def test_compaction_script_dry_run_apply_verify_and_rollback(tmp_path, capsys, monkeypatch) -> None:
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.commit()
    finally:
        conn.close()
    persist_runtime_config_snapshot(RuntimeConfig(shadow_vote_weight=0.3), source="compact", db_path=db_path)

    from scripts import state_payload_compact

    rewrite_calls = []

    def unexpected_rewrite(*_args, **_kwargs):
        rewrite_calls.append(True)
        raise AssertionError("default --apply must not perform a physical rewrite")

    monkeypatch.setattr(state_payload_compact, "_rewrite_compacted_tables", unexpected_rewrite)

    assert state_payload_compact.main(["--dry-run", "--db-path", str(db_path)]) == 0
    capsys.readouterr()
    manifest = tmp_path / "compact.json"
    assert state_payload_compact.main(
        [
            "--apply",
            "--maintenance-id",
            "maintenance_test",
            "--manifest",
            str(manifest),
            "--db-path",
            str(db_path),
        ]
    ) == 0
    capsys.readouterr()
    assert rewrite_calls == []
    assert state_payload_compact.main(
        ["--verify", "--manifest", str(manifest), "--db-path", str(db_path)]
    ) == 0
    capsys.readouterr()

    conn = connect_sqlite(db_path, read_only=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM runtime_config_snapshot").fetchone()[0] == 1
        assert conn.execute("SELECT config_json FROM runtime_config_snapshot").fetchone()[0] == "{}"
        assert conn.execute("SELECT COUNT(*) FROM runtime_config_payload").fetchone()[0] == 1
    finally:
        conn.close()

    def explicit_rewrite(*_args, **_kwargs):
        rewrite_calls.append(True)
        return {"rewrite": "explicit_test_rewrite", "tables": []}

    monkeypatch.setattr(state_payload_compact, "_rewrite_compacted_tables", explicit_rewrite)
    assert state_payload_compact.main(
        [
            "--apply",
            "--rewrite",
            "--maintenance-id",
            "maintenance_test_reapply",
            "--rewrite-maintenance-id",
            "maintenance_test_rewrite",
            "--manifest",
            str(manifest),
            "--db-path",
            str(db_path),
        ]
    ) == 0
    capsys.readouterr()
    assert rewrite_calls == [True]

    assert state_payload_compact.main(
        [
            "--rollback",
            "--maintenance-id",
            "maintenance_test",
            "--db-path",
            str(db_path),
        ]
    ) == 0
    capsys.readouterr()
    conn = connect_sqlite(db_path, read_only=True)
    try:
        assert json.loads(conn.execute("SELECT config_json FROM runtime_config_snapshot").fetchone()[0])
    finally:
        conn.close()


def test_compaction_dry_run_respects_selected_target(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.commit()
    finally:
        conn.close()

    from scripts import state_payload_compact

    def unexpected_payload_scan(*_args, **_kwargs):
        raise AssertionError("supervisor_review dry-run must not scan payload domains")

    monkeypatch.setattr(state_payload_compact, "_domain_stats", unexpected_payload_scan)
    monkeypatch.setattr(
        state_payload_compact,
        "_supervisor_review_stats",
        lambda *_args, **_kwargs: {"estimated_temporary_space_bytes": 9},
    )
    monkeypatch.setattr(
        state_payload_compact,
        "_maintenance_preflight",
        lambda *_args, **_kwargs: {"ok": True},
    )

    conn = connect_sqlite(db_path)
    try:
        result = state_payload_compact._dry_run(
            conn,
            targets="supervisor_review",
        )
    finally:
        conn.close()

    assert result["targets"] == "supervisor_review"
    assert result["runtime_config_snapshot"] == {
        "status": "skipped",
        "reason": "target_not_selected",
    }
    assert result["supervisor_review"]["estimated_temporary_space_bytes"] == 9


def test_compaction_dry_run_bounds_recursive_paths_and_writes_manifest(tmp_path, capsys, monkeypatch) -> None:
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.commit()
    finally:
        conn.close()

    from scripts import state_payload_compact

    nested = {"leaf": True}
    for _ in range(30):
        nested = {"latest_supervisor": nested}
    depth, paths = state_payload_compact._walk_payload(nested)

    assert depth == 31
    assert paths
    assert all(len(path.split(".")) <= state_payload_compact.MAX_REPORTED_RECURSIVE_PATH_PARTS for path in paths)

    monkeypatch.setattr(
        state_payload_compact,
        "_dry_run",
        lambda *_args, **_kwargs: {"ok": True, "read_only": True, "targets": "supervisor_review"},
    )
    manifest = tmp_path / "supervisor-review-dry-run.json"
    assert state_payload_compact.main(
        [
            "--dry-run",
            "--targets",
            "supervisor_review",
            "--manifest",
            str(manifest),
            "--db-path",
            str(db_path),
        ]
    ) == 0
    capsys.readouterr()
    saved = json.loads(manifest.read_text(encoding="utf-8"))
    assert saved["mode"] == "dry_run"
    assert saved["targets"] == "supervisor_review"


def test_compactor_reports_and_preserves_audit_lineage(tmp_path, capsys) -> None:
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.commit()
    finally:
        conn.close()
    canonical = record_evolution_decision(
        run_id="audit-run",
        decision_type="factor_governance_autonomous",
        action="update_weight",
        status="applied",
        config_hash="cfg-1",
        evidence={"canonical": True},
        db_path=db_path,
        decision_id="canonical-audit",
    )
    record_evolution_decision(
        run_id="audit-run",
        decision_type="manual_api_mutation",
        action="update_weight",
        status="applied",
        config_hash="cfg-1",
        result={"decision_id": canonical},
        evidence={"endpoint": "/test"},
        db_path=db_path,
        decision_id="api-audit",
    )

    from scripts import state_payload_compact

    assert state_payload_compact.main(["--dry-run", "--db-path", str(db_path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["audit_double_write"]["linked"] == 1
    manifest = tmp_path / "audit-compact.json"
    assert state_payload_compact.main(
        [
            "--apply",
            "--maintenance-id",
            "maintenance_audit",
            "--manifest",
            str(manifest),
            "--db-path",
            str(db_path),
        ]
    ) == 0
    capsys.readouterr()
    assert state_payload_compact.main(
        ["--verify", "--manifest", str(manifest), "--db-path", str(db_path)]
    ) == 0
    verify = json.loads(capsys.readouterr().out)
    assert verify["metadata_unchanged"] is True
    conn = connect_sqlite(db_path, read_only=True)
    try:
        rows = conn.execute(
            "SELECT decision_id, canonical_event_id, projection_type FROM evolution_decision ORDER BY decision_id"
        ).fetchall()
        assert tuple(rows[0]) == ("api-audit", "canonical-audit", "api")
        assert tuple(rows[1]) == ("canonical-audit", "canonical-audit", "canonical")
    finally:
        conn.close()
