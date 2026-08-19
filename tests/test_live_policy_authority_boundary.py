from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from backend.core.db import STATE_DB_DDL, connect_sqlite
from backend.services import live_service
from backend.services.backend_readiness import BackendReadinessService
from backend.services.position_supervisor_governance import (
    materialize_position_supervisor_candidate_observations,
)


def _seed_candidate_observation_facts(db_path: Path) -> None:
    candidate_created_at = 1_700_000_000.0
    close_ts = candidate_created_at + 3600.0
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.execute(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence, status,
             reviewed_at, created_at)
            VALUES ('candidate_1', 'position_supervisor_template',
                    'position_supervisor:conservative.v1',
                    'switch_position_supervisor_template', 0.9, 'approved', ?, ?)
            """,
            (candidate_created_at, candidate_created_at),
        )
        conn.execute(
            """
            INSERT INTO trade_outcome_review
            (review_id, trade_id, position_id, entry_decision_id, exit_decision_id,
             pnl, mae, mfe, outcome_label, review_json, created_at)
            VALUES ('review_1', 'trade_1', 'position_1', 'entry_1', 'exit_1',
                    4.5, -2.0, 8.0, 'win', ?, ?)
            """,
            (
                json.dumps(
                    {
                        "position_id": "position_1",
                        "close_ts": close_ts,
                        "holding_seconds": 900.0,
                        "entry_price": 2300.0,
                        "close_price": 2304.0,
                        "giveback_ratio": 0.2,
                        "profit_capture_ratio": 0.7,
                        "holding_efficiency": 0.8,
                        "thesis_status": "intact",
                    }
                ),
                close_ts,
            ),
        )
        conn.execute(
            """
            INSERT INTO supervisor_counterfactual_review
            (counterfactual_id, review_id, trade_id, position_id, close_ts,
             evidence_json, created_at, updated_at)
            VALUES ('cf_1', 'review_1', 'trade_1', 'position_1', ?, ?, ?, ?)
            """,
            (
                close_ts,
                json.dumps(
                    {
                        "regime": "trend",
                        "maturity": {
                            "status": "governance_ready",
                            "governance_eligible": True,
                        },
                    }
                ),
                close_ts,
                close_ts,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_live_service_has_no_approved_supervisor_candidate_dependency() -> None:
    source = Path(live_service.__file__).read_text(encoding="utf-8")

    assert "latest_approved_position_supervisor_candidate" not in source
    assert "stage=\"canary_shadow\"" not in source
    assert "status='approved'" not in source


def test_frozen_live_supervision_only_evaluates_projected_template(monkeypatch) -> None:
    traces: list[dict] = []
    monkeypatch.setattr(
        live_service,
        "_log_supervisor_trace",
        lambda **kwargs: traces.append(kwargs),
    )
    # Isolate the shared recovery-store boundaries: the noop dedup is
    # persisted per position in state_v1, so a real/previous row for the
    # same deterministic hold fingerprint would suppress the evaluation
    # trace and the test would also upsert production state.
    monkeypatch.setattr(
        live_service,
        "_supervisor_noop_fingerprint_seen",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        live_service,
        "_remember_supervisor_noop",
        lambda *_args, **_kwargs: None,
    )
    cfg = SimpleNamespace(
        autonomy_mode="live_candidate",
        autonomy_expansion_frozen=True,
        timeframe="M5",
    )
    position = {
        "position_id": 42,
        "symbol": "XAUUSD+",
        "direction": 1,
        "entry_price": 2300.0,
        "current_price": 2301.0,
        "volume": 100.0,
    }
    verdict = {
        "action": "hold",
        "summary_reason": "no_change",
        "confidence": 0.8,
        "supervisor_template": {
            "template_id": "position_supervisor:default.v1",
            "template_version": "default.v1",
        },
    }

    handled = live_service._run_position_supervision(
        object(),
        [position],
        cfg=cfg,
        acct={"equity": 10_000.0, "balance": 10_000.0},
        tick=1,
        log=lambda *_args, **_kwargs: None,
        planned_verdicts={42: verdict},
    )

    assert handled == set()
    assert [item["stage"] for item in traces] == ["evaluated"]
    assert all(item.get("execution_status") != "shadow_only" for item in traces)


def test_learning_worker_materializes_bound_non_execution_candidate_trace(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    _seed_candidate_observation_facts(db_path)

    first = materialize_position_supervisor_candidate_observations(
        db_path=db_path,
        run_id="learning_run_1",
    )
    second = materialize_position_supervisor_candidate_observations(
        db_path=db_path,
        run_id="learning_run_2",
    )

    assert first["status"] == "completed"
    assert first["broker_mutation_allowed"] is False
    assert first["inserted"] == 1
    assert first["evaluated"] == 1
    assert second["inserted"] == 0
    assert second["existing"] == 1
    conn = connect_sqlite(db_path, read_only=True)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT stage, outcome, risk_allowed, execution_status,
                   execution_reason, trace_integrity, verdict_json, execution_json
            FROM position_supervisor_trace
            """
        ).fetchone()
    finally:
        conn.close()
    assert row["stage"] == "learning_shadow"
    assert row["outcome"] == "shadow"
    assert row["risk_allowed"] == 0
    assert row["execution_status"] == "observation_only"
    assert row["execution_reason"] == "learning_worker_candidate_replay:candidate_1"
    assert row["trace_integrity"] == "recovered"
    verdict = json.loads(row["verdict_json"])
    execution = json.loads(row["execution_json"])
    assert verdict["evidence"]["candidate_suggestion_id"] == "candidate_1"
    assert verdict["evidence"]["non_authoritative"] is True
    assert execution["broker_mutation_attempted"] is False


def test_learning_worker_skips_contaminated_candidate_review(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    _seed_candidate_observation_facts(db_path)
    conn = connect_sqlite(db_path)
    try:
        review = json.loads(
            conn.execute(
                "SELECT review_json FROM trade_outcome_review WHERE review_id='review_1'"
            ).fetchone()[0]
        )
        review["system_issue_context"] = {
            "system_contaminated": True,
            "contaminates_learning": True,
        }
        conn.execute(
            "UPDATE trade_outcome_review SET review_json=? WHERE review_id='review_1'",
            (json.dumps(review),),
        )
        conn.commit()
    finally:
        conn.close()

    result = materialize_position_supervisor_candidate_observations(
        db_path=db_path,
        run_id="learning_run_contaminated",
    )

    assert result["status"] == "completed"
    assert result["inserted"] == 0
    assert result["evaluated"] == 0
    conn = connect_sqlite(db_path, read_only=True)
    try:
        trace_count = conn.execute(
            "SELECT COUNT(*) FROM position_supervisor_trace"
        ).fetchone()[0]
    finally:
        conn.close()
    assert trace_count == 0


def test_learning_worker_filters_contamination_before_position_dedupe_and_limit(
    tmp_path,
) -> None:
    db_path = tmp_path / "state.db"
    _seed_candidate_observation_facts(db_path)
    contaminated_close_ts = 1_700_001_800.0
    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            """
            INSERT INTO trade_outcome_review
            (review_id, trade_id, position_id, entry_decision_id, exit_decision_id,
             pnl, mae, mfe, outcome_label, review_json, created_at)
            VALUES ('review_contaminated', 'trade_contaminated', 'position_1',
                    'entry_contaminated', 'exit_contaminated',
                    -1.0, -2.0, 3.0, 'loss', ?, ?)
            """,
            (
                json.dumps(
                    {
                        "position_id": "position_1",
                        "close_ts": contaminated_close_ts,
                        "system_issue_context": {
                            "system_contaminated": True,
                            "contaminates_learning": True,
                        },
                    }
                ),
                contaminated_close_ts,
            ),
        )
        conn.execute(
            """
            INSERT INTO supervisor_counterfactual_review
            (counterfactual_id, review_id, trade_id, position_id, close_ts,
             evidence_json, created_at, updated_at)
            VALUES ('cf_contaminated', 'review_contaminated',
                    'trade_contaminated', 'position_1', ?, ?, ?, ?)
            """,
            (
                contaminated_close_ts,
                json.dumps(
                    {
                        "maturity": {
                            "status": "governance_ready",
                            "governance_eligible": True,
                        }
                    }
                ),
                contaminated_close_ts,
                contaminated_close_ts,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    result = materialize_position_supervisor_candidate_observations(
        db_path=db_path,
        limit=1,
        run_id="learning_run_filter_before_limit",
    )

    assert result["inserted"] == 1
    assert result["evaluated"] == 1
    conn = connect_sqlite(db_path, read_only=True)
    try:
        row = conn.execute(
            "SELECT trade_id, verdict_json FROM position_supervisor_trace"
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "trade_1"
    assert json.loads(row[1])["evidence"]["counterfactual_id"] == "cf_1"


def test_readiness_ignores_legacy_canary_shadow_trace(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    _seed_candidate_observation_facts(db_path)
    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            """
            INSERT INTO position_supervisor_trace
            (trace_id, position_id, template_id, stage, outcome, event_ts, created_at)
            VALUES ('legacy_live_shadow', 'position_1',
                    'position_supervisor:conservative.v1',
                    'canary_shadow', 'shadow', 1700003600, 1700003600)
            """
        )
        conn.commit()
    finally:
        conn.close()

    status = BackendReadinessService(db_path=db_path)._learning_repair_status()

    assert status["checks"]["candidate_observation_available"] is False
    assert status["checks"]["canary_sample_count"] is False
    assert status["canary"]["shadow_position_count"] == 0
    assert status["ok"] is False
