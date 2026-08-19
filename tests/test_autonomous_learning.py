import json
import sqlite3
import time
from pathlib import Path

from backend.core.db import STATE_DB_DDL
from backend.services import autonomous_learning as al
from backend.services import evolution_ledger
from backend.services.evolution_ledger import expire_stale_evolution_runs, start_evolution_run
from backend.services.v16_command_gate import V16CommandGate
from config import runtime_config as rc


def _v16_supervisor_bridge_evidence(candidate_id: str) -> dict:
    return {
        "candidate_id": candidate_id,
        "source_agent": "v16_brain",
        "bridge": {"command_owner": "v16_brain"},
        "governance_eligibility": {
            "governance_eligible": True,
            "governance_eligibility_version": "governance_eligibility.v1",
            "governance_eligibility_fingerprint": f"eligible-{candidate_id}",
        },
        "replay_summary": {"sample_count": 8},
        "counterfactual_summary": {"total": 12},
    }


def _seed_v16_supervisor_command(
    db_path, *, candidate_id: str, evidence_fingerprint: str = ""
) -> None:
    V16CommandGate.ensure_finalize_schema(db_path)
    now = time.time()
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO v16_brain_command
            (command_id, candidate_id, target_agent, scope_type, scope_key, action,
             decision, status, evidence_json, delegation_json, claim_status,
             evidence_fingerprint, authority_issued_at, created_at, updated_at)
            VALUES (?, ?, 'position_supervisor_governance', 'supervisor_template',
                    'position_supervisor', 'switch_position_supervisor_template',
                    'delegate', 'delegated_to_specialist', '{}', '{}', 'available', ?, ?, ?, ?)
            """,
            (
                f"v16_supervisor_{candidate_id}",
                candidate_id,
                evidence_fingerprint,
                now,
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _create_sample_db(path):
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE decision_ledger (
            decision_id TEXT PRIMARY KEY,
            trade_id TEXT DEFAULT '',
            position_id TEXT DEFAULT '',
            event_type TEXT NOT NULL,
            symbol TEXT DEFAULT '',
            timeframe TEXT DEFAULT '',
            decision_ts REAL NOT NULL DEFAULT 0.0,
            regime_id TEXT DEFAULT '',
            regime_confidence REAL DEFAULT 0.0,
            portfolio_state_json TEXT DEFAULT '{}',
            risk_state_json TEXT DEFAULT '{}',
            policy_version TEXT DEFAULT '',
            factor_set_version TEXT DEFAULT '',
            action_score REAL DEFAULT 0.0,
            action_reason TEXT DEFAULT '',
            action_json TEXT DEFAULT '{}',
            created_at REAL NOT NULL DEFAULT 0.0
        );
        CREATE TABLE trade_outcome_review (
            review_id TEXT PRIMARY KEY,
            trade_id TEXT DEFAULT '',
            position_id TEXT DEFAULT '',
            entry_decision_id TEXT DEFAULT '',
            exit_decision_id TEXT DEFAULT '',
            entry_quality REAL DEFAULT 0.0,
            hold_quality REAL DEFAULT 0.0,
            exit_quality REAL DEFAULT 0.0,
            regime_fit_score REAL DEFAULT 0.0,
            execution_quality REAL DEFAULT 0.0,
            pnl REAL DEFAULT 0.0,
            mae REAL DEFAULT 0.0,
            mfe REAL DEFAULT 0.0,
            outcome_label TEXT DEFAULT '',
            failure_tags_json TEXT DEFAULT '[]',
            summary_text TEXT DEFAULT '',
            review_json TEXT DEFAULT '{}',
            created_at REAL NOT NULL DEFAULT 0.0
        );
        CREATE TABLE supervisor_counterfactual_review (
            counterfactual_id TEXT PRIMARY KEY,
            review_id TEXT DEFAULT '',
            trade_id TEXT DEFAULT '',
            position_id TEXT NOT NULL,
            close_ts REAL NOT NULL DEFAULT 0.0,
            close_reason TEXT DEFAULT '',
            supervisor_event_type TEXT DEFAULT '',
            supervisor_reason TEXT DEFAULT '',
            label TEXT DEFAULT '',
            confidence REAL DEFAULT 0.0,
            horizons_json TEXT DEFAULT '[]',
            evidence_json TEXT DEFAULT '{}',
            created_at REAL NOT NULL DEFAULT 0.0,
            updated_at REAL NOT NULL DEFAULT 0.0
        );
        CREATE TABLE position_supervisor_trace (
            trace_id TEXT PRIMARY KEY,
            decision_id TEXT DEFAULT '',
            position_id TEXT NOT NULL,
            trade_id TEXT DEFAULT '',
            symbol TEXT DEFAULT '',
            timeframe TEXT DEFAULT '',
            tick INTEGER DEFAULT 0,
            event_ts REAL NOT NULL DEFAULT 0.0,
            action TEXT DEFAULT '',
            summary_reason TEXT DEFAULT '',
            confidence REAL DEFAULT 0.0,
            template_id TEXT DEFAULT '',
            template_version TEXT DEFAULT '',
            stage TEXT DEFAULT '',
            outcome TEXT DEFAULT '',
            risk_action TEXT DEFAULT '',
            risk_allowed INTEGER DEFAULT 0,
            risk_reason TEXT DEFAULT '',
            execution_status TEXT DEFAULT '',
            execution_reason TEXT DEFAULT '',
            context_json TEXT DEFAULT '{}',
            verdict_json TEXT DEFAULT '{}',
            risk_verdict_json TEXT DEFAULT '{}',
            execution_json TEXT DEFAULT '{}',
            created_at REAL NOT NULL DEFAULT 0.0
        );
        """
    )
    risk_verdict = {"allowed": False, "reason": "max_positions_reached"}
    conn.execute(
        """
        INSERT INTO decision_ledger
        (decision_id, event_type, symbol, timeframe, decision_ts, action_reason,
         action_score, portfolio_state_json, risk_state_json, action_json, created_at)
        VALUES ('dec_skip', 'skip', 'XAUUSD+', 'M5', 100.0, 'max_positions_reached',
                0.71, ?, ?, ?, 100.0)
        """,
        (
            json.dumps({"n_positions": 1}),
            json.dumps({"policy_verdict": risk_verdict}),
            json.dumps({"skip_stage": "risk_policy", "risk_verdict": risk_verdict}),
        ),
    )
    conn.execute(
        """
        INSERT INTO decision_ledger
        (decision_id, trade_id, position_id, event_type, symbol, timeframe,
         decision_ts, action_reason, action_score, portfolio_state_json,
         risk_state_json, action_json, created_at)
        VALUES ('dec_open', 'p1', 'p1', 'open', 'XAUUSD+', 'M5',
                90.0, 'executed', -0.62, '{"n_positions": 0}',
                '{"policy_verdict": {"allowed": true}}', ?, 90.0)
        """,
        (
            json.dumps(
                {
                    "direction": -1,
                    "score": -0.62,
                    "entry_cluster": {"same_direction_open_count_before": 2},
                    "same_direction_open_count": 2,
                    "recent_same_direction_entries": {"15m": 2},
                    "portfolio_exposure": {"same_direction_open_count_after": 3},
                }
            ),
        ),
    )
    conn.execute(
        """
        INSERT INTO decision_ledger
        (decision_id, trade_id, position_id, event_type, symbol, timeframe,
         decision_ts, action_reason, action_score, action_json, created_at)
        VALUES ('dec_sup', 'p1', 'p1', 'supervisor_tighten', 'XAUUSD+', 'M5',
                120.0, 'thesis_weakening', 0.66, ?, 120.0)
        """,
        (
            json.dumps(
                {
                    "supervisor_verdict": {
                        "action": "tighten",
                        "summary_reason": "thesis_weakening",
                        "evidence": {"giveback_ratio": 0.5},
                    }
                }
            ),
        ),
    )
    conn.execute(
        """
        INSERT INTO position_supervisor_trace
        (trace_id, decision_id, position_id, trade_id, symbol, timeframe, tick,
         event_ts, action, summary_reason, confidence, template_id,
         template_version, stage, outcome, risk_action, risk_allowed,
         risk_reason, execution_status, execution_reason, context_json,
         verdict_json, risk_verdict_json, execution_json, created_at)
        VALUES ('trace1', 'dec_sup', 'p1', 'p1', 'XAUUSD+', 'M5', 7,
                121.0, 'tighten', 'thesis_weakening', 0.66,
                'position_supervisor:default.v1', 'default.v1',
                'executed', 'applied', 'tighten_position', 1,
                'risk_reducing_action', 'applied', 'amend_position_sltp_success',
                ?, ?, ?, ?, 121.0)
        """,
        (
            json.dumps({"position": {"position_id": "p1", "pnl": 0.2}}),
            json.dumps({"action": "tighten", "summary_reason": "thesis_weakening"}),
            json.dumps({"allowed": True, "reason": "risk_reducing_action"}),
            json.dumps({"target_stop_loss_sent": 4000.0}),
        ),
    )
    review = {
        "symbol": "XAUUSD+",
        "timeframe": "M5",
        "close_ts": 180.0,
        "close_reason_source": "supervisor_tighten_stopout",
        "attribution_integrity": "recovered",
        "execution_quality_state": "replay_verified",
        "execution_quality_evidence": {
            "schema_version": "execution_quality_evidence.v2",
            "evidence_state": "replay_verified",
            "replay_verified": True,
        },
    }
    conn.execute(
        """
        INSERT INTO trade_outcome_review
        (review_id, trade_id, position_id, entry_decision_id, exit_decision_id,
         entry_quality, hold_quality, exit_quality, pnl, mae, mfe, outcome_label,
         failure_tags_json, review_json, created_at)
        VALUES ('rev1', 'p1', 'p1', 'dec_open', 'dec_sup', 0.4, 0.5, 0.6,
                -1.2, 1.5, 0.1, 'bad_loss', '["exit"]', ?, 180.0)
        """,
        (json.dumps(review),),
    )
    conn.execute(
        """
        INSERT INTO supervisor_counterfactual_review
        (counterfactual_id, review_id, trade_id, position_id, close_ts,
         close_reason, supervisor_event_type, supervisor_reason, label,
         confidence, horizons_json, evidence_json, created_at, updated_at)
        VALUES ('scf1', 'rev1', 'p1', 'p1', 180.0, 'broker_close',
                'supervisor_tighten', 'thesis_weakening', 'premature_tighten',
                0.78, '[{"horizon_minutes": 60, "matured": true}]',
                '{"advisory_only": true, "maturity": {"status": "governance_ready", "governance_eligible": true}}',
                181.0, 181.0)
        """
    )
    conn.commit()
    conn.close()


def _seed_unusable_counterfactuals_ahead_of_clean_evidence(path):
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            """
            INSERT INTO trade_outcome_review
            (review_id, trade_id, position_id, entry_decision_id, exit_decision_id,
             pnl, review_json, created_at)
            VALUES ('rev_contaminated', 'p1', 'p1', 'dec_open', 'dec_sup',
                    -1.2, ?, 180.0)
            """,
            (
                json.dumps(
                    {
                        "close_ts": 180.0,
                        "system_issue_context": {
                            "contaminates_learning": True,
                            "labels": ["market_data_stale"],
                        },
                    }
                ),
            ),
        )
        rows = [
            (
                "scf_missing_review",
                "rev_missing",
                "correct_stop",
                json.dumps(
                    {
                        "maturity": {
                            "status": "governance_ready",
                            "governance_eligible": True,
                        }
                    }
                ),
                600.0,
            ),
            (
                "scf_contaminated",
                "rev_contaminated",
                "correct_stop",
                json.dumps(
                    {
                        "maturity": {
                            "status": "governance_ready",
                            "governance_eligible": True,
                        }
                    }
                ),
                500.0,
            ),
            (
                "scf_invalidated",
                "rev1",
                "correct_stop",
                json.dumps(
                    {
                        "evidence_invalidated": True,
                        "invalidation_reason": "late_evidence_rejected",
                        "maturity": {
                            "status": "governance_ready",
                            "governance_eligible": True,
                        },
                    }
                ),
                400.0,
            ),
        ]
        conn.executemany(
            """
            INSERT INTO supervisor_counterfactual_review
            (counterfactual_id, review_id, trade_id, position_id, close_ts,
             close_reason, supervisor_event_type, supervisor_reason, label,
             confidence, horizons_json, evidence_json, created_at, updated_at)
            VALUES (?, ?, 'p1', 'p1', 180.0, 'broker_close',
                    'supervisor_tighten', 'thesis_weakening', ?, 0.95,
                    '[]', ?, ?, ?)
            """,
            [(counterfactual_id, review_id, label, evidence, updated_at, updated_at)
             for counterfactual_id, review_id, label, evidence, updated_at in rows],
        )
        conn.commit()
    finally:
        conn.close()


def test_materialize_autonomous_learning_samples_from_existing_evidence(tmp_path):
    db_path = tmp_path / "state.db"
    _create_sample_db(db_path)

    result = al.materialize_autonomous_learning_samples(db_path=db_path, limit=20)

    assert result["counts"]["risk_rejection"] == 1
    assert result["counts"]["supervisor_trajectory"] == 1
    assert result["counts"]["supervisor_execution_trace"] == 1
    assert result["counts"]["trade_review_outcome"] == 1
    assert result["counts"]["post_close_counterfactual"] == 1

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            """
            SELECT sample_type, label_status, integrity, train_weight, evidence_contract_json,
                   config_version, config_hash, evolution_run_id
            FROM training_sample_row
            ORDER BY sample_type
            """
        ).fetchall()
        events = conn.execute("SELECT event_type FROM evolution_events").fetchall()
        runs = conn.execute("SELECT run_type, status FROM evolution_run").fetchall()
    finally:
        conn.close()

    sample_types = {row[0] for row in rows}
    assert "risk_rejection" in sample_types
    assert "supervisor_trajectory" in sample_types
    assert "supervisor_execution_trace" in sample_types
    assert "trade_review_outcome" in sample_types
    assert "post_close_counterfactual" in sample_types
    open_sample = [row for row in rows if row[0] == "shadow_open_decision" and row[1] == "matured"][0]
    assert open_sample[1] == "matured"
    open_contract = json.loads(open_sample[4])
    assert open_contract["model_ready"] is True
    assert "supervised_training" in open_contract["allowed_uses"]
    supervisor_trace = [row for row in rows if row[0] == "supervisor_execution_trace"][0]
    assert supervisor_trace[1] == "pending"
    trace_contract = json.loads(supervisor_trace[4])
    assert trace_contract["causal_level"] == "observational"
    assert trace_contract["model_ready"] is False
    assert "supervised_training" not in trace_contract["allowed_uses"]
    assert supervisor_trace[5] > 0
    assert supervisor_trace[6]
    assert supervisor_trace[7]
    recovered_review = [row for row in rows if row[0] == "trade_review_outcome"][0]
    assert recovered_review[2] == "recovered"
    assert recovered_review[3] == 0.5
    assert json.loads(recovered_review[4])["schema_version"] == "learning_evidence_contract.v1"
    counterfactual_sample = [row for row in rows if row[0] == "post_close_counterfactual"][0]
    counterfactual_contract = json.loads(counterfactual_sample[4])
    assert counterfactual_contract["model_ready"] is True
    assert "counterfactual_training" in counterfactual_contract["allowed_uses"]
    assert "supervised_training" in counterfactual_contract["allowed_uses"]

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO training_sample_row
            (sample_id, sample_type, source_table, source_id, label_status,
             integrity, train_weight, features_json, verdict_json, label_json,
             trace_json, created_at, updated_at, evidence_contract_json,
             config_version, config_hash, evolution_run_id)
            VALUES ('stale_cf_sample', 'post_close_counterfactual',
                    'supervisor_counterfactual_review', 'missing_cf', 'matured',
                    'full', 1.0, '{}', '{}', '{}', '{}', 1.0, 1.0, '{}',
                    1, 'hash', 'run')
            """
        )
        conn.commit()
    finally:
        conn.close()

    al.materialize_autonomous_learning_samples(db_path=db_path, limit=20)

    conn = sqlite3.connect(str(db_path))
    try:
        stale_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM training_sample_row s
            LEFT JOIN supervisor_counterfactual_review cf ON cf.counterfactual_id=s.source_id
            WHERE s.sample_type='post_close_counterfactual'
              AND cf.counterfactual_id IS NULL
            """
        ).fetchone()[0]
    finally:
        conn.close()
    assert stale_count == 0
    assert ("autonomous_learning_samples",) in events
    assert ("autonomous_learning_samples", "completed") in runs


def test_open_consumer_eligibility_does_not_require_factor_attribution():
    item = {
        "sample_type": "shadow_open_decision",
        "source_table": "decision_ledger",
        "source_id": "dec_open_consumer_scope",
        "sample_id": "als_open_consumer_scope",
        "config_hash": "cfg-current",
        "label_status": "matured",
        "integrity": "missing",
        "train_weight": 1.0,
        "trace": {
            "decision_id": "dec_open_consumer_scope",
            "position_id": "position-open-consumer-scope",
        },
        "features": {
            "entry_cluster": {"schema_version": "entry_cluster_context.v1", "direction": 1},
            "market_micro_context": {
                "schema_version": "market_micro_context.v1",
                "bid": 4000.0,
                "ask": 4000.2,
                "mid": 4000.1,
                "spread": 0.2,
                "signal_price": 4000.1,
                "quote_fresh": True,
            },
            "bar_context": {"schema_version": "entry_bar_context.v1", "complete": True},
            "execution_context": {
                "requested_volume": 100.0,
                "actual_api_volume": 100.0,
                "signal_price": 4000.1,
                "fill_price": 4000.2,
            },
            "decision_quality_context": {
                "schema_version": "decision_quality_context.v1",
                "composer_version": "factor_roles.v2",
                "factor_roles": {"rsi": "alpha"},
                "n_active_alpha_factors": 1,
            },
            "event_context": {"multiplier": 1.0},
            "data_quality_context": {
                "schema_version": "entry_data_quality_context.v1",
                "quote_fresh": True,
            },
        },
        "label": {
            "label": "open_outcome",
            "open_target_v2": {
                "schema_version": "open_target.v2",
                "financial_label": "profit",
                "execution_evidence_state": "full",
                "trainable": True,
                "contaminated": False,
            },
        },
        "verdict": {},
        "executable_governance_allowed": True,
    }

    _, contract, _ = al._build_sample_evidence_contract(item)

    assert contract["model_ready"] is False
    assert "supervised_training" not in contract["allowed_uses"]
    consumer = contract["consumer_eligibility"]["open_quality_lightgbm"]
    assert consumer["model_ready"] is True
    assert consumer["allowed_uses"] == ["supervised_training"]


def test_counterfactual_materialization_filters_before_limit(tmp_path):
    db_path = tmp_path / "state.db"
    _create_sample_db(db_path)
    _seed_unusable_counterfactuals_ahead_of_clean_evidence(db_path)

    result = al.materialize_autonomous_learning_samples(db_path=db_path, limit=1)

    assert result["counts"]["post_close_counterfactual"] == 1
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            """
            SELECT source_id
            FROM training_sample_row
            WHERE sample_type='post_close_counterfactual'
            """
        ).fetchall()
    finally:
        conn.close()
    assert rows == [("scf1",)]


def test_supervisor_trace_uses_one_canonical_review_and_missing_review_fails_closed(tmp_path):
    db_path = tmp_path / "state.db"
    _create_sample_db(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO trade_outcome_review
            (review_id, trade_id, position_id, review_json, created_at)
            VALUES ('rev_old_contaminated', 'p1', 'p1', ?, 100.0)
            """,
            (
                json.dumps(
                    {
                        "system_issue_context": {
                            "contaminates_learning": True,
                            "labels": ["market_data_stale"],
                        }
                    }
                ),
            ),
        )
        conn.execute(
            """
            INSERT INTO position_supervisor_trace
            (trace_id, position_id, trade_id, event_ts, action, outcome,
             verdict_json, created_at)
            VALUES ('trace_missing_review', 'p_missing', 'p_missing', 130.0,
                    'close', 'executed', '{"action":"close"}', 130.0)
            """
        )
        conn.commit()
    finally:
        conn.close()

    materialized = al.materialize_autonomous_learning_samples(db_path=db_path, limit=20)
    assert materialized["counts"]["supervisor_execution_trace"] == 2

    matured = al.mature_position_supervisor_traces(db_path=db_path, limit=20)
    # A6: traces with executed outcome now mature observationally even without
    # a counterfactual review, so pending count is 0 (both traces matured).
    assert matured["matured"] >= 1
    conn = sqlite3.connect(str(db_path))
    try:
        rows = {
            row[0]: row[1:]
            for row in conn.execute(
                """
                    SELECT source_id, train_weight, features_json, trace_json
                FROM training_sample_row
                WHERE sample_type='supervisor_execution_trace'
                """
            ).fetchall()
        }
    finally:
        conn.close()
    assert json.loads(rows["trace1"][2])["source_review_id"] == "rev1"
    # A6: trace_missing_review is now matured observationally (not pending),
    # but still contaminated because it has no source review.
    assert rows["trace_missing_review"][0] == 0.0
    contamination = json.loads(rows["trace_missing_review"][1])["system_contamination"]
    assert contamination["contaminated"] is True
    assert contamination["reason"] == "canonical_source_review_missing"


def test_expire_stale_evolution_runs_marks_only_old_running_rows(tmp_path):
    db_path = tmp_path / "state.db"
    stale = start_evolution_run(run_type="autonomous_learning_samples", db_path=db_path)
    fresh = start_evolution_run(run_type="demo_autonomy_apply", db_path=db_path)
    old_ts = time.time() - 7200
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("UPDATE evolution_run SET started_at=? WHERE run_id=?", (old_ts, stale["run_id"]))
        conn.commit()
    finally:
        conn.close()

    result = expire_stale_evolution_runs(db_path=db_path, max_age_sec=3600)

    assert result["expired_count"] == 1
    conn = sqlite3.connect(str(db_path))
    try:
        rows = dict(conn.execute("SELECT run_id, status FROM evolution_run").fetchall())
    finally:
        conn.close()
    assert rows[stale["run_id"]] == "expired"
    assert rows[fresh["run_id"]] == "running"


def test_start_evolution_run_does_not_return_snapshot_run_id(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(
        evolution_ledger,
        "current_runtime_config_snapshot",
        lambda **_kwargs: {
            "run_id": "snapshot_owner",
            "config_version": 7,
            "config_hash": "cfg_hash",
        },
    )

    run = start_evolution_run(run_type="factor_governance_autonomous", db_path=db_path)

    assert run["run_id"].startswith("evorun_")
    assert run["run_id"] != "snapshot_owner"
    conn = sqlite3.connect(str(db_path))
    try:
        stored = conn.execute("SELECT run_id FROM evolution_run").fetchone()[0]
    finally:
        conn.close()
    assert stored == run["run_id"]


def test_materialize_autonomous_learning_orders_decisions_by_event_time(tmp_path):
    db_path = tmp_path / "state.db"
    _create_sample_db(db_path)
    risk_verdict = {"allowed": False, "reason": "test"}
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO decision_ledger
            (decision_id, event_type, symbol, timeframe, decision_ts, action_reason,
             action_score, portfolio_state_json, risk_state_json, action_json, created_at)
            VALUES ('dec_old_replay', 'skip', 'XAUUSD+', 'M5', 50.0, 'old_replay',
                    0.1, '{}', ?, ?, 5000.0)
            """,
            (
                json.dumps({"policy_verdict": risk_verdict}),
                json.dumps({"skip_stage": "risk_policy", "risk_verdict": risk_verdict}),
            ),
        )
        conn.execute(
            """
            INSERT INTO decision_ledger
            (decision_id, event_type, symbol, timeframe, decision_ts, action_reason,
             action_score, portfolio_state_json, risk_state_json, action_json, created_at)
            VALUES ('dec_new_event', 'skip', 'XAUUSD+', 'M5', 500.0, 'new_event',
                    0.1, '{}', ?, ?, 10.0)
            """,
            (
                json.dumps({"policy_verdict": risk_verdict}),
                json.dumps({"skip_stage": "risk_policy", "risk_verdict": risk_verdict}),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    al.materialize_autonomous_learning_samples(db_path=db_path, limit=1)

    conn = sqlite3.connect(str(db_path))
    try:
        ids = {
            row[0]
            for row in conn.execute(
                """
                SELECT source_id
                FROM training_sample_row
                WHERE source_table='decision_ledger'
                """
            ).fetchall()
        }
    finally:
        conn.close()

    assert "dec_new_event" in ids
    assert "dec_old_replay" not in ids


def test_repair_evidence_contracts_removes_pending_supervised_training(tmp_path):
    db_path = tmp_path / "state.db"
    _create_sample_db(db_path)
    al.materialize_autonomous_learning_samples(db_path=db_path, limit=20)

    conn = sqlite3.connect(str(db_path))
    try:
        bad_contract = {
            "schema_version": "learning_evidence_contract.v1",
            "allowed_uses": ["audit", "explainability", "supervised_training"],
            "model_ready": False,
        }
        conn.execute(
            """
            UPDATE training_sample_row
            SET evidence_contract_json=?
            WHERE sample_type='supervisor_execution_trace'
            """,
            (json.dumps(bad_contract),),
        )
        conn.commit()
    finally:
        conn.close()

    before = al.validate_evidence_contract_health(db_path=db_path)
    assert before["counts"]["non_matured_allows_supervised_training"] == 1

    result = al.repair_evidence_contracts(db_path=db_path)

    assert result["repaired"] >= 1
    after = al.validate_evidence_contract_health(db_path=db_path)
    assert after["counts"]["non_matured_allows_supervised_training"] == 0
    conn = sqlite3.connect(str(db_path))
    try:
        decision = conn.execute(
            """
            SELECT decision_type, decision_json
            FROM evolution_decision
            WHERE decision_type='repair_evidence_contracts'
            """
        ).fetchone()
    finally:
        conn.close()
    assert decision is not None
    assert decision[0] == "repair_evidence_contracts"
    assert json.loads(decision[1])["status"] == "completed"


def test_entry_cluster_governance_materializes_policy_suggestion(tmp_path):
    db_path = tmp_path / "state.db"
    al.ensure_autonomous_learning_tables(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        now = time.time()
        contract = {
            "schema_version": "learning_evidence_contract.v1",
            "allowed_uses": ["audit", "explainability", "supervised_training"],
            "model_ready": True,
            "quality": {"executable_governance_allowed": True},
        }
        for idx in range(3):
            features = {
                "action": {"same_direction_open_count": 2},
                "entry_cluster": {
                    "same_direction_open_count_before": 2,
                    "pyramid_depth": 2,
                    "recent_same_direction_entries": {"within_5m": idx + 1},
                },
            }
            label = {
                "label": "open_outcome",
                "outcome_label": "bad_loss",
                "pnl": -8.0 - idx,
                "failure_tags": ["entry_cluster_risk"],
                "open_target_v2": {
                    "schema_version": "open_target.v2",
                    "objective": "profitable_open_outcome",
                    "financial_label": "loss",
                    "legacy_outcome_label": "bad_loss",
                    "execution_evidence_state": "full",
                    "contaminated": False,
                    "trainable": True,
                },
            }
            conn.execute(
                """
                INSERT INTO training_sample_row
                (sample_id, sample_type, source_table, source_id, decision_id,
                 label_status, integrity, train_weight, event_ts, features_json,
                 verdict_json, label_json, trace_json, evidence_contract_json,
                 created_at, updated_at)
                VALUES (?, 'shadow_open_decision', 'decision_ledger', ?, ?,
                        'matured', 'full', 1.0, ?, ?, '{}', ?, ?, ?, ?, ?)
                """,
                (
                    f"open_cluster_{idx}",
                    f"dec_cluster_{idx}",
                    f"dec_cluster_{idx}",
                    now + idx,
                    json.dumps(features),
                    json.dumps(label),
                    json.dumps({"decision_id": f"dec_cluster_{idx}", "position_id": f"p{idx}"}),
                    json.dumps(contract),
                    now,
                    now,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    al.repair_evidence_contracts(db_path=db_path)
    result = al.materialize_entry_cluster_governance_suggestions(db_path=db_path, min_samples=3, min_bad_rate=0.5)

    assert result["suggestions"] == 1
    conn = sqlite3.connect(str(db_path))
    try:
        stats = conn.execute(
            """
            SELECT sample_count, bad_loss_count, recommended_action
            FROM experience_pattern_stats
            WHERE scope_type='entry_cluster' AND scope_key='same_direction_ge_2'
            """
        ).fetchone()
        suggestion = conn.execute(
            """
            SELECT scope_type, scope_key, action, status
            FROM policy_suggestion
            WHERE scope_type='entry_cluster'
            """
        ).fetchone()
    finally:
        conn.close()
    assert stats == (3, 3, "increase_same_direction_cooldown")
    assert suggestion == ("entry_cluster", "same_direction_ge_2", "increase_same_direction_cooldown", "proposed")


def test_event_window_governance_materializes_policy_suggestion(tmp_path):
    db_path = tmp_path / "state.db"
    al.ensure_autonomous_learning_tables(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        now = time.time()
        contract = {
            "schema_version": "learning_evidence_contract.v1",
            "allowed_uses": ["audit", "explainability", "supervised_training"],
            "model_ready": True,
            "quality": {"executable_governance_allowed": True},
        }
        for idx in range(3):
            features = {
                "event_context": {
                    "schema_version": al.EVENT_WINDOW_CONTEXT_SCHEMA_VERSION,
                    "event_type": "NFP",
                    "event": "Non-Farm Employment Change",
                    "event_importance": 3,
                    "window_bucket": "pre_0_15m",
                    "multiplier": 0.5,
                    "hours_until_event": 0.10 + idx * 0.01,
                }
            }
            label = {
                "label": "open_outcome",
                "outcome_label": "bad_loss",
                "pnl": -7.0 - idx,
                "failure_tags": ["event_window_bad_entry"],
                "open_target_v2": {
                    "schema_version": "open_target.v2",
                    "objective": "profitable_open_outcome",
                    "financial_label": "loss",
                    "legacy_outcome_label": "bad_loss",
                    "execution_evidence_state": "full",
                    "contaminated": False,
                    "trainable": True,
                },
            }
            conn.execute(
                """
                INSERT INTO training_sample_row
                (sample_id, sample_type, source_table, source_id, decision_id,
                 label_status, integrity, train_weight, event_ts, features_json,
                 verdict_json, label_json, trace_json, evidence_contract_json,
                 created_at, updated_at)
                VALUES (?, 'shadow_open_decision', 'decision_ledger', ?, ?,
                        'matured', 'full', 1.0, ?, ?, '{}', ?, ?, ?, ?, ?)
                """,
                (
                    f"open_event_{idx}",
                    f"dec_event_{idx}",
                    f"dec_event_{idx}",
                    now + idx,
                    json.dumps(features),
                    json.dumps(label),
                    json.dumps({"decision_id": f"dec_event_{idx}", "position_id": f"ep{idx}"}),
                    json.dumps(contract),
                    now,
                    now,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    al.repair_evidence_contracts(db_path=db_path)
    result = al.materialize_event_window_governance_suggestions(db_path=db_path, min_samples=3, min_bad_rate=0.5)

    assert result["suggestions"] == 1
    conn = sqlite3.connect(str(db_path))
    try:
        stats = conn.execute(
            """
            SELECT sample_count, bad_loss_count, recommended_action
            FROM experience_pattern_stats
            WHERE scope_type='event_window' AND scope_key='NFP:pre_0_15m'
            """
        ).fetchone()
        suggestion = conn.execute(
            """
            SELECT scope_type, scope_key, action, status
            FROM policy_suggestion
            WHERE scope_type='event_window'
            """
        ).fetchone()
    finally:
        conn.close()
    assert stats == (3, 3, "tighten_event_window_sizing")
    assert suggestion == ("event_window", "NFP:pre_0_15m", "tighten_event_window_sizing", "proposed")


def test_entry_quality_governance_materializes_policy_suggestions(tmp_path):
    db_path = tmp_path / "state.db"
    al.ensure_autonomous_learning_tables(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        now = time.time()
        contract = {
            "schema_version": "learning_evidence_contract.v1",
            "allowed_uses": ["audit", "explainability", "supervised_training"],
            "model_ready": True,
            "quality": {"executable_governance_allowed": True},
        }
        for idx in range(24):
            low_score = idx < 12
            bad = low_score or idx < 14
            failure_tags = ["weak_signal_overtraded"] if bad else []
            if idx < 3:
                failure_tags.extend(
                    [
                        "factor_conflict",
                        "conflicting_factor_entry",
                    ]
                )
            review = {
                "entry_score": 0.32 if low_score else 0.60,
                "worst_factor": "real_yield_chg" if idx < 3 else "",
                "primary_responsibility": "factor_conflict" if idx < 3 else "signal_quality",
                "failure_tags": failure_tags,
            }
            label = {
                "outcome_label": "bad_loss" if bad else "good_win",
                "pnl": -4.0 - idx if bad else 5.0 + idx,
                "failure_tags": review["failure_tags"],
            }
            conn.execute(
                """
                INSERT INTO training_sample_row
                (sample_id, sample_type, source_table, source_id, decision_id,
                 label_status, integrity, train_weight, event_ts, features_json,
                 verdict_json, label_json, trace_json, evidence_contract_json,
                 created_at, updated_at)
                VALUES (?, 'trade_review_outcome', 'trade_outcome_review', ?, ?,
                        'matured', 'full', 1.0, ?, ?, '{}', ?, ?, ?, ?, ?)
                """,
                (
                    f"review_entry_quality_{idx}",
                    f"review_entry_quality_{idx}",
                    f"dec_entry_quality_{idx}",
                    now + idx,
                    json.dumps({"review": review}),
                    json.dumps(label),
                    json.dumps({"review_id": f"review_entry_quality_{idx}", "position_id": f"eq{idx}"}),
                    json.dumps(contract),
                    now,
                    now,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    al.repair_evidence_contracts(db_path=db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence, reason,
             evidence_json, status, governance_eligible,
             governance_eligibility_version, governance_eligibility_fingerprint,
             governance_ineligible_reason, created_at)
            VALUES ('legacy_ineligible_weak_signal', 'entry_quality', 'weak_signal',
                    'raise_weak_signal_threshold', 0.8, 'legacy placeholder', '{}',
                    'proposed', 0, 'legacy', '', 'legacy_placeholder', ?)
            """,
            (time.time() - 100.0,),
        )
        conn.commit()
    finally:
        conn.close()
    result = al.materialize_entry_quality_governance_suggestions(db_path=db_path, min_samples=3, min_bad_rate=0.5)

    assert result["suggestions"] == 3
    conn = sqlite3.connect(str(db_path))
    try:
        suggestions = conn.execute(
            """
            SELECT scope_type, scope_key, action, status
            FROM policy_suggestion
            WHERE scope_type='entry_quality'
            ORDER BY scope_key, action
            """
        ).fetchall()
        legacy_status = conn.execute(
            "SELECT status FROM policy_suggestion WHERE suggestion_id='legacy_ineligible_weak_signal'"
        ).fetchone()[0]
        weak = conn.execute(
            """
            SELECT suggestion_id, governance_eligible,
                   governance_eligibility_fingerprint
            FROM policy_suggestion
            WHERE scope_type='entry_quality' AND scope_key='weak_signal'
              AND status='proposed'
            """
        ).fetchone()
    finally:
        conn.close()
    assert legacy_status == "invalidated_evidence"
    assert weak[0] != "legacy_ineligible_weak_signal"
    assert weak[1] == 1
    assert weak[2]
    evidence = json.loads(
        sqlite3.connect(str(db_path)).execute(
            "SELECT evidence_json FROM policy_suggestion WHERE suggestion_id=?",
            (weak[0],),
        ).fetchone()[0]
    )
    assert evidence["schema_version"] == "entry_quality_governance_evidence.v2"
    assert evidence["sample_count"] == 24
    assert evidence["bad_count"] == 14
    assert evidence["win_count"] == 10
    assert evidence["recommended_controls"]["min_abs_signal_score"] == 0.35
    assert evidence["recommended_controls"]["strong_signal_override"] == 0.70
    assert ("entry_quality", "weak_signal", "raise_weak_signal_threshold", "proposed") in suggestions
    assert ("entry_quality", "factor_conflict", "require_factor_agreement", "proposed") in suggestions
    assert ("entry_quality", "real_yield_chg", "suppress_recent_worst_factor", "proposed") in suggestions
    repeated = al.materialize_entry_quality_governance_suggestions(
        db_path=db_path, min_samples=3, min_bad_rate=0.5
    )
    assert repeated["suggestions"] == 0


def test_entry_quality_observational_factor_does_not_penalize_non_entry_responsibility(tmp_path):
    db_path = tmp_path / "state.db"
    al.ensure_autonomous_learning_tables(db_path)
    contract = {
        "schema_version": "learning_evidence_contract.v1",
        "allowed_uses": ["audit", "explainability", "supervised_training"],
        "model_ready": True,
        "quality": {"executable_governance_allowed": True},
    }
    responsibilities = ["exit", "holding", "data_quality", "parameter"]
    conn = sqlite3.connect(str(db_path))
    try:
        now = time.time()
        for idx in range(8):
            responsibility = responsibilities[idx % len(responsibilities)]
            review = {
                "entry_score": 0.31,
                "worst_factor": "engulfing",
                "largest_contribution_factor": "engulfing",
                "primary_responsibility": responsibility,
                "factor_attribution": {
                    "largest_contribution_factor": "engulfing",
                    "causal_level": "observational",
                    "causal_claim": False,
                },
                "failure_tags": ["factor_conflict", "conflicting_factor_entry"],
            }
            label = {
                "outcome_label": "bad_loss",
                "pnl": -5.0,
                "failure_tags": review["failure_tags"],
            }
            conn.execute(
                """
                INSERT INTO training_sample_row
                (sample_id, sample_type, source_table, source_id, decision_id,
                 position_id, label_status, integrity, train_weight, event_ts,
                 features_json, verdict_json, label_json, trace_json,
                 evidence_contract_json, created_at, updated_at)
                VALUES (?, 'trade_review_outcome', 'trade_outcome_review', ?, ?, ?,
                        'matured', 'full', 1.0, ?, ?, '{}', ?, ?, ?, ?, ?)
                """,
                (
                    f"observational_factor_{idx}",
                    f"review_observational_factor_{idx}",
                    f"decision_observational_factor_{idx}",
                    f"position_observational_factor_{idx}",
                    now + idx,
                    json.dumps({"review": review}),
                    json.dumps(label),
                    json.dumps({"position_id": f"position_observational_factor_{idx}"}),
                    json.dumps(contract),
                    now,
                    now,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    al.repair_evidence_contracts(db_path=db_path)
    al.materialize_entry_quality_governance_suggestions(
        db_path=db_path,
        min_samples=3,
        min_bad_rate=0.5,
    )

    conn = sqlite3.connect(str(db_path))
    try:
        suppressed = conn.execute(
            """
            SELECT COUNT(*)
            FROM policy_suggestion
            WHERE scope_type='entry_quality'
              AND action='suppress_recent_worst_factor'
              AND status IN ('proposed', 'approved', 'applied')
            """
        ).fetchone()[0]
    finally:
        conn.close()
    assert suppressed == 0


def test_event_window_governance_ignores_legacy_gradient_samples(tmp_path):
    db_path = tmp_path / "state.db"
    al.ensure_autonomous_learning_tables(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        now = time.time()
        contract = {
            "schema_version": "learning_evidence_contract.v1",
            "allowed_uses": ["audit", "explainability", "supervised_training"],
            "model_ready": True,
            "quality": {"executable_governance_allowed": True},
        }
        for idx in range(3):
            features = {
                "event_context": {
                    "event_type": "NFP",
                    "event": "Non-Farm Employment Change",
                    "event_importance": 3,
                    "window_bucket": "pre_0_4h",
                    "multiplier": 0.2,
                    "hours_until_event": 1.5 + idx * 0.1,
                }
            }
            label = {
                "label": "open_outcome",
                "outcome_label": "bad_loss",
                "pnl": -7.0 - idx,
                "failure_tags": ["event_window_bad_entry"],
            }
            conn.execute(
                """
                INSERT INTO training_sample_row
                (sample_id, sample_type, source_table, source_id, decision_id,
                 label_status, integrity, train_weight, event_ts, features_json,
                 verdict_json, label_json, trace_json, evidence_contract_json,
                 created_at, updated_at)
                VALUES (?, 'shadow_open_decision', 'decision_ledger', ?, ?,
                        'matured', 'full', 1.0, ?, ?, '{}', ?, ?, ?, ?, ?)
                """,
                (
                    f"legacy_event_{idx}",
                    f"legacy_dec_event_{idx}",
                    f"legacy_dec_event_{idx}",
                    now + idx,
                    json.dumps(features),
                    json.dumps(label),
                    json.dumps({"decision_id": f"legacy_dec_event_{idx}", "position_id": f"lep{idx}"}),
                    json.dumps(contract),
                    now,
                    now,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    result = al.materialize_event_window_governance_suggestions(db_path=db_path, min_samples=3, min_bad_rate=0.5)

    assert result["bucket_count"] == 0
    assert result["suggestions"] == 0


def test_backfill_trade_review_close_sources_from_protection_trace(tmp_path):
    db_path = tmp_path / "state.db"
    _create_sample_db(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        review = {
            "symbol": "XAUUSD+",
            "timeframe": "M5",
            "close_ts": 180.0,
            "close_reason": "broker_close",
            "attribution_integrity": "recovered",
        }
        conn.execute(
            """
            UPDATE trade_outcome_review
            SET review_json=?
            WHERE review_id='rev1'
            """,
            (json.dumps(review),),
        )
        conn.commit()
    finally:
        conn.close()

    result = al.backfill_trade_review_close_sources(db_path=db_path, limit=20)

    assert result["updated"] == 1
    assert result["by_source"]["supervisor_tighten_stopout"] == 1
    conn = sqlite3.connect(str(db_path))
    try:
        raw = conn.execute("SELECT review_json FROM trade_outcome_review WHERE review_id='rev1'").fetchone()[0]
        decision = conn.execute(
            """
            SELECT decision_type, decision_json
            FROM evolution_decision
            WHERE decision_type='backfill_close_sources'
            """
        ).fetchone()
    finally:
        conn.close()
    repaired = json.loads(raw)
    assert repaired["close_reason_source"] == "supervisor_tighten_stopout"
    assert repaired["inferred_close_supervisor"]["event_type"] == "supervisor_tighten"
    assert decision is not None
    assert decision[0] == "backfill_close_sources"
    assert json.loads(decision[1])["status"] == "completed"


def test_backfill_trade_review_integrity_markers_prevents_legacy_full_training(tmp_path):
    db_path = tmp_path / "state.db"
    _create_sample_db(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        review = {
            "symbol": "XAUUSD+",
            "timeframe": "M5",
            "close_ts": 180.0,
            "close_reason_source": "external_broker_close",
        }
        conn.execute(
            """
            UPDATE trade_outcome_review
            SET review_json=?
            WHERE review_id='rev1'
            """,
            (json.dumps(review),),
        )
        conn.commit()
    finally:
        conn.close()

    result = al.backfill_trade_review_integrity_markers(db_path=db_path, limit=20)
    al.materialize_autonomous_learning_samples(db_path=db_path, limit=20)

    assert result["updated"] == 1
    conn = sqlite3.connect(str(db_path))
    try:
        review_raw = conn.execute("SELECT review_json FROM trade_outcome_review WHERE review_id='rev1'").fetchone()[0]
        sample = conn.execute(
            """
            SELECT integrity, train_weight, evidence_contract_json
            FROM training_sample_row
            WHERE sample_type='trade_review_outcome' AND source_id='rev1'
            """
        ).fetchone()
        decision = conn.execute(
            """
            SELECT decision_type, decision_json
            FROM evolution_decision
            WHERE decision_type='backfill_review_integrity'
            """
        ).fetchone()
    finally:
        conn.close()
    review = json.loads(review_raw)
    contract = json.loads(sample[2])
    assert review["attribution_integrity"] == "missing"
    assert sample[0] == "missing"
    assert sample[1] == 0.0
    assert contract["model_ready"] is False
    assert "supervised_training" not in contract["allowed_uses"]
    assert decision is not None
    assert decision[0] == "backfill_review_integrity"
    assert json.loads(decision[1])["status"] == "completed"


def test_backfill_trade_review_timing_marks_system_contamination(tmp_path):
    db_path = tmp_path / "state.db"
    _create_sample_db(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS order_lifecycle_event (
                event_id TEXT PRIMARY KEY,
                decision_id TEXT DEFAULT '',
                trade_id TEXT DEFAULT '',
                order_id TEXT DEFAULT '',
                broker_order_id TEXT DEFAULT '',
                event_type TEXT NOT NULL,
                event_ts REAL NOT NULL DEFAULT 0.0,
                price REAL DEFAULT 0.0,
                volume REAL DEFAULT 0.0,
                status TEXT DEFAULT '',
                details_json TEXT DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS factor_contribution_review (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                review_id TEXT NOT NULL,
                trade_id TEXT DEFAULT '',
                factor TEXT NOT NULL,
                entry_contribution REAL DEFAULT 0.0,
                hold_contribution REAL DEFAULT 0.0,
                exit_contribution REAL DEFAULT 0.0,
                net_contribution REAL DEFAULT 0.0,
                confidence REAL DEFAULT 0.0,
                notes TEXT DEFAULT ''
            );
            """
        )
        risk_verdict = {
            "allowed": True,
            "reason": "ok",
            "audit_payload": {
                "temporal_context": {
                    "evaluated_at": 700.0,
                    "timeframe": "M5",
                    "timeframe_seconds": 300,
                },
                "state": {
                    "runtime_health_snapshot": {
                        "data_lag_seconds": 610.0,
                        "raw": {
                            "sync_health": {
                                "fresh": True,
                                "stale": False,
                                "degraded": False,
                            }
                        },
                    }
                },
            },
        }
        action = {
            "direction": -1,
            "risk_verdict": risk_verdict,
            "data_quality_context": {"quote_fresh": True},
            "market_session": {"market_data_age_seconds": 610.0},
        }
        conn.execute(
            """
            UPDATE decision_ledger
            SET decision_ts=90.0, action_json=?, risk_state_json=?
            WHERE decision_id='dec_open'
            """,
            (json.dumps(action), json.dumps({"policy_verdict": risk_verdict})),
        )
        review = json.loads(conn.execute("SELECT review_json FROM trade_outcome_review WHERE review_id='rev1'").fetchone()[0])
        review["close_ts"] = 900.0
        conn.execute(
            """
            UPDATE trade_outcome_review
            SET review_json=?, created_at=900.0
            WHERE review_id='rev1'
            """,
            (json.dumps(review),),
        )
        conn.execute(
            """
            INSERT INTO order_lifecycle_event
            (event_id, decision_id, trade_id, event_type, event_ts, price, volume, status)
            VALUES ('sub1', 'dec_open', 'p1', 'submitted', 701.0, 4000.0, 100.0, 'submitted')
            """
        )
        conn.execute(
            """
            INSERT INTO order_lifecycle_event
            (event_id, decision_id, trade_id, event_type, event_ts, price, volume, status)
            VALUES ('fill1', 'dec_open', 'p1', 'filled', 702.0, 4000.1, 100.0, 'filled')
            """
        )
        conn.execute(
            """
            INSERT INTO factor_contribution_review
            (review_id, trade_id, factor, net_contribution, confidence, notes)
            VALUES ('rev1', 'p1', 'dsl_factor', -0.5, 0.8, '{}')
            """
        )
        conn.commit()
    finally:
        conn.close()

    result = al.backfill_trade_review_timing_and_system_markers(db_path=db_path, limit=20)
    al.materialize_autonomous_learning_samples(db_path=db_path, limit=20)

    assert result["updated"] >= 1
    assert result["contaminated"] >= 1
    assert result["factor_contribution_rows_updated"] == 1
    conn = sqlite3.connect(str(db_path))
    try:
        review_raw = conn.execute("SELECT review_json FROM trade_outcome_review WHERE review_id='rev1'").fetchone()[0]
        factor = conn.execute(
            "SELECT confidence, notes FROM factor_contribution_review WHERE review_id='rev1'"
        ).fetchone()
        sample = conn.execute(
            """
            SELECT integrity, train_weight, evidence_contract_json
            FROM training_sample_row
            WHERE sample_type='trade_review_outcome' AND source_id='rev1'
            """
        ).fetchone()
    finally:
        conn.close()

    review = json.loads(review_raw)
    assert review["entry_ts"] == 702.0
    assert review["holding_seconds"] == 198.0
    assert review["primary_responsibility"] == "data_quality"
    assert "signal_execution_delay" in review["responsibility_labels"]
    notes = json.loads(factor[1])
    assert factor[0] < 0.8
    assert notes["system_contaminated"] is True
    assert sample[0] == "partial"
    assert sample[1] == 0.25
    assert "supervised_training" not in json.loads(sample[2])["allowed_uses"]


def test_system_contaminated_trade_review_materializes_partial_learning_samples(tmp_path):
    db_path = tmp_path / "state.db"
    _create_sample_db(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        raw = conn.execute("SELECT review_json FROM trade_outcome_review WHERE review_id='rev1'").fetchone()[0]
        review = json.loads(raw)
        review["system_issue_context"] = {
            "schema_version": "trade_review_system_issue.v1",
            "system_contaminated": True,
            "contaminates_learning": True,
            "primary_responsibility": "data_quality",
            "labels": ["market_data_stale", "signal_execution_delay", "data_quality_issue"],
            "evidence": {"data_lag_seconds": 619.0},
        }
        review["responsibility_labels"] = ["market_data_stale", "signal_execution_delay"]
        review["primary_responsibility"] = "data_quality"
        conn.execute(
            """
            UPDATE trade_outcome_review
            SET review_json=?, failure_tags_json='["bad_loss","market_data_stale"]'
            WHERE review_id='rev1'
            """,
            (json.dumps(review),),
        )
        conn.commit()
    finally:
        conn.close()

    al.materialize_autonomous_learning_samples(db_path=db_path, limit=20)

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            """
            SELECT sample_type, integrity, train_weight, evidence_contract_json,
                   verdict_json, label_json
            FROM training_sample_row
            WHERE source_id IN ('rev1', 'dec_open', 'dec_sup')
               OR (sample_type='shadow_open_decision' AND position_id='p1')
            ORDER BY sample_type
            """
        ).fetchall()
    finally:
        conn.close()

    by_type = {row[0]: row for row in rows}
    trade = by_type["trade_review_outcome"]
    assert trade[1] == "partial"
    assert trade[2] == 0.25
    trade_contract = json.loads(trade[3])
    assert trade_contract["model_ready"] is False
    assert "supervised_training" not in trade_contract["allowed_uses"]
    assert json.loads(trade[4])["system_contamination"]["contaminated"] is True

    open_sample = by_type["shadow_open_decision"]
    assert open_sample[1] == "partial"
    assert open_sample[2] == 0.25
    open_contract = json.loads(open_sample[3])
    assert open_contract["model_ready"] is False
    assert "supervised_training" not in open_contract["allowed_uses"]
    assert json.loads(open_sample[5])["system_contamination"]["contaminated"] is True

    trajectory = by_type["supervisor_trajectory"]
    assert trajectory[1] == "partial"
    assert trajectory[2] == 0.25
    trajectory_contract = json.loads(trajectory[3])
    assert trajectory_contract["model_ready"] is False
    assert "supervised_training" not in trajectory_contract["allowed_uses"]
    assert json.loads(trajectory[4])["system_contamination"]["contaminated"] is True


def test_trade_review_minimal_integrity_materializes_as_missing(tmp_path):
    db_path = tmp_path / "state.db"
    _create_sample_db(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        review = {
            "symbol": "XAUUSD+",
            "timeframe": "M5",
            "close_ts": 180.0,
            "context_integrity": "minimal",
            "close_reason_source": "external_broker_close",
        }
        conn.execute(
            """
            UPDATE trade_outcome_review
            SET review_json=?
            WHERE review_id='rev1'
            """,
            (json.dumps(review),),
        )
        conn.commit()
    finally:
        conn.close()

    al.materialize_autonomous_learning_samples(db_path=db_path, limit=20)

    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            """
            SELECT integrity, train_weight, evidence_contract_json
            FROM training_sample_row
            WHERE sample_type='trade_review_outcome' AND source_id='rev1'
            """
        ).fetchone()
    finally:
        conn.close()

    assert row[0] == "missing"
    assert row[1] == 0.0
    contract = json.loads(row[2])
    assert contract["integrity"] == "missing"
    assert contract["model_ready"] is False


def test_position_supervisor_trace_maturation_labels_over_protection(tmp_path):
    db_path = tmp_path / "state.db"
    _create_sample_db(db_path)

    result = al.mature_position_supervisor_traces(db_path=db_path, limit=20)

    assert result["matured"] == 1
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            """
            SELECT label_status, integrity, train_weight, label_json, evidence_contract_json
            FROM training_sample_row
            WHERE sample_type='supervisor_execution_trace' AND source_id='trace1'
            """
        ).fetchone()
        decision = conn.execute(
            """
            SELECT decision_type, decision_json
            FROM evolution_decision
            WHERE decision_type='mature_traces'
            """
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "matured"
    assert row[1] == "full"
    assert row[2] > 0
    assert json.loads(row[3])["label"] == "over_protected"
    assert json.loads(row[3])["recommended_action"] == "less_tighten"
    contract = json.loads(row[4])
    assert contract["model_ready"] is True
    assert "supervised_training" in contract["allowed_uses"]
    assert decision is not None
    assert decision[0] == "mature_traces"
    assert json.loads(decision[1])["status"] == "completed"


def test_position_supervisor_trace_maturation_uses_latest_clean_counterfactual(tmp_path):
    db_path = tmp_path / "state.db"
    _create_sample_db(db_path)
    _seed_unusable_counterfactuals_ahead_of_clean_evidence(db_path)

    result = al.mature_position_supervisor_traces(db_path=db_path, limit=20)

    assert result["matured"] == 1
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            """
            SELECT label_json, trace_json
            FROM training_sample_row
            WHERE sample_type='supervisor_execution_trace' AND source_id='trace1'
            """
        ).fetchone()
    finally:
        conn.close()
    assert json.loads(row[0])["label"] == "over_protected"
    assert json.loads(row[1])["counterfactual_id"] == "scf1"


def test_materialization_does_not_downgrade_matured_supervisor_trace(tmp_path):
    db_path = tmp_path / "state.db"
    _create_sample_db(db_path)

    al.mature_position_supervisor_traces(db_path=db_path, limit=20)
    al.materialize_autonomous_learning_samples(db_path=db_path, limit=20)

    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            """
            SELECT label_status, label_json, evidence_contract_json
            FROM training_sample_row
            WHERE sample_type='supervisor_execution_trace' AND source_id='trace1'
            """
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "matured"
    assert json.loads(row[1])["label"] == "over_protected"
    assert json.loads(row[2])["model_ready"] is True


def test_position_supervisor_trace_backfill_from_decision_ledger(tmp_path):
    db_path = tmp_path / "state.db"
    _create_sample_db(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO decision_ledger
            (decision_id, trade_id, position_id, event_type, symbol, timeframe,
             decision_ts, action_reason, action_score, action_json, created_at)
            VALUES ('dec_legacy_close', 'p2', 'p2', 'supervisor_close', 'XAUUSD+', 'M5',
                    130.0, 'thesis_broken', 0.7, ?, 130.0)
            """,
            (
                json.dumps(
                    {
                        "supervisor_verdict": {
                            "action": "close",
                            "summary_reason": "thesis_broken",
                            "confidence": 0.7,
                        }
                    }
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    result = al.backfill_position_supervisor_traces(db_path=db_path, limit=20)

    assert result["inserted"] == 1
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            """
            SELECT action, stage, outcome, trace_integrity, evolution_run_id
            FROM position_supervisor_trace
            WHERE decision_id='dec_legacy_close'
            """
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "close"
    assert row[1] == "legacy_backfill"
    assert row[2] == "legacy_recovered"
    assert row[3] == "recovered"
    assert row[4]


def test_candidate_observation_replays_current_applied_supervisor_effect(tmp_path):
    db_path = tmp_path / "state.db"
    candidate_started_at = time.time() - 7200
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(STATE_DB_DDL)
        conn.execute(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, status,
             applied_mutation_id, created_at)
            VALUES ('applied_current', 'position_supervisor_template',
                    'position_supervisor:conservative.v1',
                    'switch_position_supervisor_template', 'applied',
                    'mutation_current', ?)
            """,
            (candidate_started_at,),
        )
        conn.execute(
            """
            INSERT INTO trade_outcome_review
            (review_id, trade_id, position_id, review_json, created_at)
            VALUES ('review_current', 'trade_current', 'position_current', '{}', ?)
            """,
            (candidate_started_at + 3600,),
        )
        conn.execute(
            """
            INSERT INTO supervisor_counterfactual_review
            (counterfactual_id, review_id, trade_id, position_id, close_ts,
             evidence_json, created_at, updated_at)
            VALUES ('cf_current', 'review_current', 'trade_current',
                    'position_current', ?, ?, ?, ?)
            """,
            (
                candidate_started_at + 3600,
                json.dumps(
                    {
                        "maturity": {"governance_eligible": True},
                        "regime": "current_regime",
                    }
                ),
                candidate_started_at + 3600,
                candidate_started_at + 3600,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    from backend.services.learning_application_store import LearningApplicationStore

    LearningApplicationStore(db_path).write_effect(
        application_id="effect_current",
        scope_type="position_supervisor_template",
        scope_key="position_supervisor:conservative.v1",
        action="switch_position_supervisor_template",
        status="observing",
        mutation_id="mutation_current",
        updated_at=candidate_started_at + 1,
    )

    from backend.services.position_supervisor_governance import (
        materialize_position_supervisor_candidate_observations,
    )

    result = materialize_position_supervisor_candidate_observations(
        db_path=db_path,
        limit=10,
        run_id="run_current_applied",
    )

    assert result["inserted"] == 1
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            """
            SELECT template_id, stage, execution_reason, trace_integrity
            FROM position_supervisor_trace
            WHERE position_id='position_current'
            """
        ).fetchone()
    finally:
        conn.close()
    assert row == (
        "position_supervisor:conservative.v1",
        "learning_shadow",
        "learning_worker_candidate_replay:applied_current",
        "recovered",
    )


def test_parameter_template_recommendations_auto_materialize_and_dedupe(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE policy_suggestion (
            suggestion_id TEXT PRIMARY KEY,
            scope_type TEXT NOT NULL,
            scope_key TEXT NOT NULL,
            action TEXT NOT NULL,
            confidence REAL DEFAULT 0.0,
            reason TEXT DEFAULT '',
            evidence_json TEXT DEFAULT '{}',
            status TEXT DEFAULT 'proposed',
            reviewed_at REAL DEFAULT 0.0,
            review_note TEXT DEFAULT '',
            created_at REAL NOT NULL DEFAULT 0.0
        );
        CREATE TABLE parameter_template_release_candidate (
            candidate_id TEXT PRIMARY KEY,
            factor_id TEXT NOT NULL,
            template_id TEXT NOT NULL,
            regime_key TEXT DEFAULT '',
            status TEXT DEFAULT 'pending_review',
            boundary_json TEXT DEFAULT '{}',
            validation_summary_json TEXT DEFAULT '{}',
            validation_report_path TEXT DEFAULT '',
            created_at REAL NOT NULL DEFAULT 0.0,
            updated_at REAL NOT NULL DEFAULT 0.0
        );
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY,
            kind TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            params_json TEXT DEFAULT '{}',
            result_json TEXT DEFAULT '{}',
            progress REAL DEFAULT 0.0,
            error TEXT DEFAULT '',
            created_at REAL,
            updated_at REAL
        );
        """
    )
    conn.commit()
    conn.close()

    calls = []

    class FakeParameterTemplateService:
        def __init__(self, db_path_arg):
            self.db_path_arg = db_path_arg

        def list_recommendations(self, limit=20):
            return [
                {
                    "recommendation_id": "rec_online",
                    "recommended_action": "suggest_switch",
                    "factor_id": "ema_slope",
                    "target_template_id": "ema_slope:conservative.v1:default",
                }
            ]

        def create_suggestion_from_recommendation(self, recommendation_id, note=""):
            calls.append((recommendation_id, note))
            return {"item": {"suggestion_id": "psg_online"}}

    import backend.services.parameter_templates as parameter_templates

    monkeypatch.setattr(parameter_templates, "ParameterTemplateService", FakeParameterTemplateService)

    first = al.materialize_parameter_template_recommendations(db_path=db_path, limit=10)
    assert first["counts"]["suggested"] == 1
    assert calls == [("rec_online", "autonomous materialize from parameter template recommendation")]

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, evidence_json, status, created_at)
            VALUES ('psg_existing', 'parameter_template', 'ema_slope:default',
                    'switch_parameter_template', ?, 'proposed', 1.0)
            """,
            (json.dumps({"evidence_context": {"recommendation_id": "rec_online"}}),),
        )
        conn.commit()
    finally:
        conn.close()

    second = al.materialize_parameter_template_recommendations(db_path=db_path, limit=10)
    assert second["counts"]["skipped_existing"] == 1
    assert len(calls) == 1


def test_auto_apply_position_supervisor_template_is_blocked_while_expansion_frozen(tmp_path):
    rc.reset_for_tests()
    rc.replace(
        rc.RuntimeConfig(
            autonomy_mode="live_candidate",
            autonomy_expansion_frozen=True,
        )
    )
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(STATE_DB_DDL)
        conn.execute(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence, reason,
             evidence_json, status, reviewed_at, created_at)
            VALUES (?, 'position_supervisor_template', ?, 'increase_min_hold_window',
                    0.82, 'test supervisor switch', ?, 'approved', ?, ?)
            """,
            (
                "psv_auto_overlay",
                "position_supervisor:conservative.v1",
                json.dumps(
                    {
                        "replay_summary": {"sample_count": 8},
                        "counterfactual_summary": {"total": 12},
                    },
                    ensure_ascii=False,
                ),
                time.time(),
                time.time(),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    try:
        result = al._auto_apply_position_supervisor_template_suggestions(
            db_path=db_path,
            experiment_id="demoauto_pytest",
            run_id="evorun_pytest",
        )

        assert result["applied"] == []
        assert result["status"] == "observation_only"
        assert result["skipped"][0]["reason"] == "autonomy_expansion_frozen"
        assert rc.shared().position_supervisor_template_id == "position_supervisor:default.v1"

        conn = sqlite3.connect(str(db_path))
        try:
            assert conn.execute("SELECT COUNT(*) FROM runtime_config_overlay").fetchone()[0] == 0
            assert conn.execute(
                "SELECT status FROM policy_suggestion WHERE suggestion_id='psv_auto_overlay'"
            ).fetchone()[0] == "approved"
            assert conn.execute("SELECT COUNT(*) FROM learning_application_log").fetchone()[0] == 0
        finally:
            conn.close()
    finally:
        rc.reset_for_tests()


def test_auto_apply_position_supervisor_template_requires_matching_shadow_trace(tmp_path):
    rc.reset_for_tests()
    db_path = tmp_path / "state.db"
    created_at = time.time() - 7200
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(STATE_DB_DDL)
        conn.execute(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence, reason,
             evidence_json, status, reviewed_at, created_at)
            VALUES ('psv_shadow_scope', 'position_supervisor_template',
                    'position_supervisor:conservative.v1', 'increase_min_hold_window',
                    0.82, 'scope test', ?, 'approved', ?, ?)
            """,
            (
                json.dumps(_v16_supervisor_bridge_evidence("candidate_shadow_scope")),
                time.time(),
                created_at,
            ),
        )
        conn.execute(
            """
            INSERT INTO supervisor_counterfactual_review
            (counterfactual_id, position_id, close_ts, evidence_json, created_at, updated_at)
            VALUES ('cf_unmatched', 'position_without_shadow', ?, ?, ?, ?)
            """,
            (
                created_at + 3600,
                json.dumps({"regime": "trend", "maturity": {"governance_eligible": True}}),
                time.time(),
                time.time(),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    rc.replace(
        rc.RuntimeConfig(
            autonomy_mode="live_candidate",
            autonomy_expansion_frozen=False,
            supervisor_canary_mature_trade_count=1,
        )
    )
    try:
        result = al._auto_apply_position_supervisor_template_suggestions(
            db_path=db_path,
            experiment_id="demoauto_shadow_scope",
            run_id="evorun_shadow_scope",
        )
    finally:
        rc.reset_for_tests()

    assert result["applied"] == []
    assert result["skipped"][0]["reason"] == "supervisor_canary_not_ready"
    assert result["skipped"][0]["mature_trade_count"] == 0


def test_auto_apply_position_supervisor_template_excludes_unusable_canary_evidence(tmp_path):
    rc.reset_for_tests()
    db_path = tmp_path / "state.db"
    created_at = time.time() - 7200
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(STATE_DB_DDL)
        conn.execute(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence, reason,
             evidence_json, status, reviewed_at, created_at)
            VALUES ('psv_unusable_canary', 'position_supervisor_template',
                    'position_supervisor:conservative.v1', 'increase_min_hold_window',
                    0.82, 'canonical evidence test', ?, 'approved', ?, ?)
            """,
            (
                json.dumps(_v16_supervisor_bridge_evidence("candidate_unusable_canary")),
                time.time(),
                created_at,
            ),
        )
        conn.executemany(
            """
            INSERT INTO trade_outcome_review
            (review_id, trade_id, position_id, review_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    "review_contaminated_canary",
                    "position_contaminated_canary",
                    "position_contaminated_canary",
                    json.dumps(
                        {
                            "system_issue_context": {
                                "contaminates_learning": True,
                                "labels": ["market_data_stale"],
                            }
                        }
                    ),
                    created_at + 3600,
                ),
                (
                    "review_invalidated_canary",
                    "position_invalidated_canary",
                    "position_invalidated_canary",
                    "{}",
                    created_at + 7200,
                ),
            ],
        )
        conn.executemany(
            """
            INSERT INTO supervisor_counterfactual_review
            (counterfactual_id, review_id, trade_id, position_id, close_ts,
             evidence_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "cf_contaminated_canary",
                    "review_contaminated_canary",
                    "position_contaminated_canary",
                    "position_contaminated_canary",
                    created_at + 3600,
                    json.dumps(
                        {
                            "regime": "trend",
                            "maturity": {"governance_eligible": True},
                        }
                    ),
                    time.time(),
                    time.time(),
                ),
                (
                    "cf_invalidated_canary",
                    "review_invalidated_canary",
                    "position_invalidated_canary",
                    "position_invalidated_canary",
                    created_at + 7200,
                    json.dumps(
                        {
                            "regime": "range",
                            "evidence_invalidated": True,
                            "maturity": {"governance_eligible": True},
                        }
                    ),
                    time.time(),
                    time.time(),
                ),
            ],
        )
        conn.executemany(
            """
            INSERT INTO position_supervisor_trace
            (trace_id, position_id, trade_id, event_ts, template_id, stage,
             execution_status, execution_reason, trace_integrity, created_at)
            VALUES (?, ?, ?, ?, 'position_supervisor:conservative.v1',
                    'learning_shadow', 'observation_only',
                    'learning_worker_candidate_replay:psv_unusable_canary',
                    'recovered', ?)
            """,
            [
                (
                    "trace_contaminated_canary",
                    "position_contaminated_canary",
                    "position_contaminated_canary",
                    created_at + 3500,
                    created_at + 3500,
                ),
                (
                    "trace_invalidated_canary",
                    "position_invalidated_canary",
                    "position_invalidated_canary",
                    created_at + 7100,
                    created_at + 7100,
                ),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    rc.replace(
        rc.RuntimeConfig(
            autonomy_mode="live_candidate",
            autonomy_expansion_frozen=False,
            supervisor_canary_mature_trade_count=1,
        )
    )
    try:
        result = al._auto_apply_position_supervisor_template_suggestions(
            db_path=db_path,
            experiment_id="demoauto_unusable_canary",
            run_id="evorun_unusable_canary",
        )
    finally:
        rc.reset_for_tests()

    assert result["applied"] == []
    assert result["skipped"][0]["reason"] == "supervisor_canary_not_ready"
    assert result["skipped"][0]["mature_trade_count"] == 0


def test_demo_auto_applies_supervisor_template_without_mature_canary(tmp_path, monkeypatch):
    rc.reset_for_tests()
    from backend.core import static_feature_flags

    monkeypatch.setattr(
        static_feature_flags,
        "shared_static_feature_flags",
        lambda: type("Flags", (), {"governance_mutation_coordinator_v2_mode": "dual_record"})(),
    )
    db_path = tmp_path / "state.db"
    now = time.time()
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(STATE_DB_DDL)
        conn.execute(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence, reason,
             evidence_json, status, reviewed_at, governance_eligible,
             governance_eligibility_version, governance_eligibility_fingerprint,
             created_at)
            VALUES ('psv_demo_aggressive', 'position_supervisor_template',
                    'position_supervisor:conservative.v1', 'increase_min_hold_window',
                    0.82, 'demo aggressive test', ?, 'approved', ?, 1,
                    'governance_eligibility.v1', 'eligible-candidate_demo_aggressive', ?)
            """,
            (
                json.dumps(
                    _v16_supervisor_bridge_evidence("candidate_demo_aggressive")
                ),
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    _seed_v16_supervisor_command(
        db_path,
        candidate_id="candidate_demo_aggressive",
        evidence_fingerprint="command-evidence-demo-aggressive",
    )

    from backend.services import governance_mutation_coordinator as coordinator_module

    # Bootstrap the local fixture with the same coordinator tables that the
    # production migration owns before exercising production-like V16 finalize.
    coordinator_module.GovernanceMutationCoordinator(db_path)._prepare_storage()

    class ProductionLikeCoordinator(coordinator_module.GovernanceMutationCoordinator):
        @property
        def production_state(self):
            return True

    monkeypatch.setattr(
        coordinator_module,
        "GovernanceMutationCoordinator",
        ProductionLikeCoordinator,
    )

    rc.replace(
        rc.RuntimeConfig(
            autonomy_mode="demo_nursery",
            autonomy_expansion_frozen=True,
            supervisor_canary_mature_trade_count=50,
            position_supervisor_template_id="position_supervisor:auto_tpsl.active.v1",
        )
    )
    try:
        result = al._auto_apply_position_supervisor_template_suggestions(
            db_path=db_path,
            experiment_id="demoauto_aggressive",
            run_id="evorun_aggressive",
        )
        assert len(result["applied"]) == 1, result
        assert rc.shared().position_supervisor_template_id == "position_supervisor:conservative.v1"
        from backend.services.learning_application_store import LearningApplicationStore

        conn = sqlite3.connect(str(db_path))
        try:
            app = LearningApplicationStore(db_path).latest_application(
                scope_type="position_supervisor_template"
            )
            details = dict(app or {})
            v16_status = conn.execute(
                "SELECT command_id, claim_status, apply_count FROM v16_brain_command"
            ).fetchone()
            intent = conn.execute(
                "SELECT idempotency_key, evidence_fingerprint FROM governance_mutation_intent"
            ).fetchone()
        finally:
            conn.close()
        assert details["demo_aggressive_governance"] is True
        assert details["canary_evidence_ready"] is False
        assert v16_status == (
            "v16_supervisor_candidate_demo_aggressive",
            "finalized",
            1,
        )
        assert intent[0].endswith(f":v16:{v16_status[0]}")
        assert intent[1] == "command-evidence-demo-aggressive"
    finally:
        rc.reset_for_tests()


def test_demo_auto_supervisor_template_requires_matching_v16_command(tmp_path):
    rc.reset_for_tests()
    db_path = tmp_path / "state.db"
    now = time.time()
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(STATE_DB_DDL)
        conn.execute(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence, reason,
             evidence_json, status, reviewed_at, created_at)
            VALUES ('psv_v16_required', 'position_supervisor_template',
                    'position_supervisor:conservative.v1',
                    'switch_position_supervisor_template', 0.82, 'missing V16 command',
                    ?, 'approved', ?, ?)
            """,
            (
                json.dumps(_v16_supervisor_bridge_evidence("candidate_missing_command")),
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    rc.replace(
        rc.RuntimeConfig(
            autonomy_mode="demo_nursery",
            autonomy_expansion_frozen=True,
            supervisor_canary_mature_trade_count=50,
        )
    )
    try:
        result = al._auto_apply_position_supervisor_template_suggestions(
            db_path=db_path,
            experiment_id="demoauto_v16_required",
            run_id="evorun_v16_required",
        )
        assert result["applied"] == []
        assert result["skipped"][0]["reason"] == "v16_command_required"
        conn = sqlite3.connect(str(db_path))
        try:
            assert conn.execute(
                "SELECT status FROM policy_suggestion WHERE suggestion_id='psv_v16_required'"
            ).fetchone()[0] == "approved"
            assert conn.execute("SELECT COUNT(*) FROM learning_application_log").fetchone()[0] == 0
        finally:
            conn.close()
    finally:
        rc.reset_for_tests()


def test_demo_autonomy_supersedes_unbridged_supervisor_suggestion(tmp_path):
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(STATE_DB_DDL)
        conn.execute(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence, reason,
             evidence_json, status, created_at)
            VALUES ('psv_legacy_direct', 'position_supervisor_template',
                    'position_supervisor:conservative.v1',
                    'switch_position_supervisor_template', 0.82, 'legacy direct',
                    ?, 'proposed', ?)
            """,
            (
                json.dumps(
                    {
                        "replay_summary": {"sample_count": 8},
                        "counterfactual_summary": {"total": 12},
                        "source_agent": "autonomous_learning",
                    }
                ),
                time.time(),
            ),
        )
        conn.commit()

        result = al._approve_demo_policy_suggestions(
            conn,
            experiment_id="demoauto_legacy_supervisor",
            db_path=db_path,
            run_id="evorun_legacy_supervisor",
        )
        assert result["approved"] == []
        assert result["skipped"][0]["reason"] == "superseded_non_v16_supervisor_suggestion"
        assert conn.execute(
            "SELECT status FROM policy_suggestion WHERE suggestion_id='psv_legacy_direct'"
        ).fetchone()[0] == "superseded"
    finally:
        conn.close()


def test_demo_autonomy_delegates_policy_review_to_governor(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE policy_suggestion (
            suggestion_id TEXT PRIMARY KEY,
            scope_type TEXT NOT NULL,
            scope_key TEXT NOT NULL,
            action TEXT NOT NULL,
            confidence REAL DEFAULT 0.0,
            reason TEXT DEFAULT '',
            evidence_json TEXT DEFAULT '{}',
            status TEXT DEFAULT 'proposed',
            reviewed_at REAL DEFAULT 0.0,
            review_note TEXT DEFAULT '',
            created_at REAL NOT NULL DEFAULT 0.0
        );
        CREATE TABLE learning_application_log (
            application_id TEXT PRIMARY KEY,
            run_id TEXT DEFAULT '',
            source TEXT DEFAULT '',
            status TEXT DEFAULT 'prepared',
            details_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL DEFAULT 0.0,
            updated_at REAL DEFAULT 0.0
        );
        CREATE TABLE learning_application_effect (
            effect_id TEXT PRIMARY KEY,
            application_id TEXT NOT NULL DEFAULT '',
            scope TEXT DEFAULT '',
            effect_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL DEFAULT 0.0
        );
        CREATE TABLE experience_pattern_stats (
            scope_type TEXT NOT NULL,
            scope_key TEXT NOT NULL,
            sample_count INTEGER DEFAULT 0,
            win_count INTEGER DEFAULT 0,
            bad_loss_count INTEGER DEFAULT 0,
            avg_reward REAL DEFAULT 0.0,
            last_outcome_label TEXT DEFAULT '',
            recommended_action TEXT DEFAULT '',
            updated_at REAL NOT NULL DEFAULT 0.0,
            PRIMARY KEY (scope_type, scope_key)
        );
        """
    )
    conn.execute(
        """
        INSERT INTO experience_pattern_stats
        (scope_type, scope_key, sample_count, win_count, bad_loss_count, avg_reward,
         last_outcome_label, recommended_action, updated_at)
        VALUES ('factor', 'ema_slope', 4, 0, 3, -0.35, 'bad_loss', 'downweight', 1.0)
        """
    )
    conn.execute(
        """
        INSERT INTO policy_suggestion
        (suggestion_id, scope_type, scope_key, action, confidence, evidence_json, status, created_at)
        VALUES ('psg_factor', 'factor', 'ema_slope', 'downweight', 0.8, '{}', 'proposed', 1.0)
        """
    )
    conn.commit()
    conn.close()

    al.ensure_autonomous_learning_tables(db_path)
    conn = sqlite3.connect(str(db_path))
    fingerprint = "eligible-factor-evidence"
    conn.execute(
        """
        UPDATE experience_pattern_stats
        SET effective_sample_count=4.0,
            weighted_win_count=0.0,
            weighted_bad_loss_count=3.0,
            weighted_avg_reward=-0.35,
            governance_eligibility_version=?,
            governance_eligibility_fingerprint=?
        WHERE scope_type='factor' AND scope_key='ema_slope'
        """,
        (al.GOVERNANCE_ELIGIBILITY_VERSION, fingerprint),
    )
    conn.execute(
        """
        UPDATE policy_suggestion
        SET governance_eligible=1,
            governance_eligibility_version=?,
            governance_eligibility_fingerprint=?
        WHERE suggestion_id='psg_factor'
        """,
        (al.GOVERNANCE_ELIGIBILITY_VERSION, fingerprint),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(al, "_sync_factor_weights_for_demo", lambda experiment_id: {"synced": True})
    monkeypatch.setattr(
        al,
        "_auto_apply_parameter_template_suggestions",
        lambda **kwargs: {"applied": [], "skipped": []},
    )
    monkeypatch.setattr(
        al,
        "_auto_release_parameter_template_candidates",
        lambda **kwargs: {"approved": [], "released": [], "skipped": []},
    )
    monkeypatch.setattr(
        al,
        "_auto_apply_position_supervisor_template_suggestions",
        lambda **kwargs: {"applied": [], "skipped": []},
    )

    result = al.apply_demo_autonomy(db_path=db_path)

    assert result["enabled"] is True
    assert result["approvals"]["approved"][0]["suggestion_id"] == "psg_factor"
    conn = sqlite3.connect(str(db_path))
    try:
        status, note = conn.execute(
            "SELECT status, review_note FROM policy_suggestion WHERE suggestion_id='psg_factor'"
        ).fetchone()
        events = [row[0] for row in conn.execute("SELECT event_type FROM evolution_events").fetchall()]
        decisions = conn.execute(
            "SELECT decision_type, decision_json FROM evolution_decision"
        ).fetchall()
    finally:
        conn.close()
    assert status == "approved"
    assert "approved by governor" in note
    assert "demo_autonomy_governor_review" in events
    assert "demo_autonomy_apply" in events
    assert not any(
        dt == "demo_auto_approve"
        and json.loads(dj).get("scope_type") == "factor"
        and json.loads(dj).get("action") == "downweight"
        and json.loads(dj).get("status") == "approved"
        for dt, dj in decisions
    )


def test_sync_factor_weights_uses_current_autonomy_mode(monkeypatch):
    captured = {}

    class _Verdict:
        def to_dict(self):
            return {"allowed": True}

    class _Policy:
        def evaluate(self, action, context):
            captured["action"] = action
            captured["context"] = context
            return _Verdict()

    from risk import policy_service
    from backend.runtime import evolution_orchestrator

    monkeypatch.setattr(policy_service.RiskPolicyService, "shared", staticmethod(lambda: _Policy()))
    monkeypatch.setattr(evolution_orchestrator, "_update_weights", lambda: True)
    monkeypatch.setattr(
        al,
        "_apply_approved_factor_suggestions_for_demo",
        lambda **_kwargs: {"attempted": 0, "applied": False, "items": []},
    )
    monkeypatch.setattr(al, "_autonomy_mode", lambda: "demo_nursery")

    result = al._sync_factor_weights_for_demo(experiment_id="exp_demo")

    assert result["synced"] is True
    assert captured["action"] == "update_weight"
    assert captured["context"]["governance"]["autonomy_mode"] == "demo_nursery"


def test_sync_factor_weights_does_not_bypass_blocked_approved_suggestion(monkeypatch):
    class _Verdict:
        def to_dict(self):
            return {"allowed": True}

    class _Policy:
        def evaluate(self, _action, _context):
            return _Verdict()

    from risk import policy_service
    from backend.runtime import evolution_orchestrator

    monkeypatch.setattr(policy_service.RiskPolicyService, "shared", staticmethod(lambda: _Policy()))
    monkeypatch.setattr(
        al,
        "_apply_approved_factor_suggestions_for_demo",
        lambda **_kwargs: {
            "attempted": 1,
            "actionable_attempted": 1,
            "applied": False,
            "items": [{"status": "blocked_by_admission"}],
        },
    )
    monkeypatch.setattr(
        evolution_orchestrator,
        "_update_weights",
        lambda: (_ for _ in ()).throw(AssertionError("broad updater must not run")),
    )

    result = al._sync_factor_weights_for_demo(experiment_id="exp_blocked")

    assert result["synced"] is False
    assert result["blocked"] is True
    assert result["reason"] == "approved_factor_suggestion_not_applied"


def test_demo_factor_apply_supersedes_missing_runtime_downweight(monkeypatch):
    class _Rows:
        def fetchall(self):
            return [
                {
                    "suggestion_id": "ps_stale",
                    "scope_key": "retired_factor",
                    "action": "downweight",
                    "evidence_json": json.dumps(
                        {"expected_effect": {"current_weight": 0.4, "suggested_target_weight": 0.2}}
                    ),
                }
            ]

    class _Conn:
        def close(self):
            pass

    class _Config:
        autonomy_mode = "demo_nursery"
        factor_portfolio_weights = {"active_factor": 0.5}
        factor_signal_config = {}

    reviewed = []

    class _Governor:
        def set_status(self, suggestion_id, status, note=""):
            reviewed.append((suggestion_id, status, note))
            return True

    monkeypatch.setattr(al, "_connect", lambda *_args, **_kwargs: _Conn())
    monkeypatch.setattr(al, "_execute", lambda *_args, **_kwargs: _Rows())
    monkeypatch.setattr(rc, "shared", lambda: _Config())
    monkeypatch.setattr(
        "research.learning.governor.RuleEvolutionGovernor",
        _Governor,
    )

    result = al._apply_approved_factor_suggestions_for_demo(experiment_id="exp_stale")

    assert result["actionable_attempted"] == 0
    assert result["superseded"] == 1
    assert result["items"][0]["status"] == "superseded_stale_runtime_target"
    assert reviewed[0][0:2] == ("ps_stale", "superseded")


def test_demo_autonomy_respects_non_demo_mode(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(al, "_autonomy_mode", lambda: "manual")

    result = al.apply_demo_autonomy(db_path=db_path)

    assert result["enabled"] is False
    assert result["mode"] == "manual"


def test_demo_autonomous_enabled_accepts_demo_nursery(monkeypatch):
    monkeypatch.setattr(al, "_autonomy_mode", lambda: "demo_nursery")

    assert al._demo_autonomous_enabled() is True


def test_factor_model_bridge_runs_in_demo_autonomous(monkeypatch, tmp_path):
    monkeypatch.setattr(al, "_autonomy_mode", lambda: "demo_autonomous")

    class _FactorModel:
        def __init__(self, db_path=None):
            self.db_path = db_path

        def materialize_demo_governance_advisories(self, **kwargs):
            return {"materialized": False, "count": 0, "reason": "no_candidates"}

    class _Pruning:
        def __init__(self, db_path):
            self.db_path = db_path

        def materialize_latest(self, **kwargs):
            return {"status": "no_candidates"}

        def promote_ready(self, **kwargs):
            return {"status": "no_candidates"}

        def bridge_ready_candidates(self, **kwargs):
            assert kwargs["require_demo_nursery"] is True
            return {"status": "no_candidates", "items": []}

    monkeypatch.setattr(
        "research.factor_governance_lightgbm.FactorGovernanceLightGBMService",
        _FactorModel,
    )
    monkeypatch.setattr(
        "backend.services.factor_pruning_governance.FactorPruningGovernanceService",
        _Pruning,
    )

    result = al._run_demo_nursery_factor_pruning_governance(
        db_path=tmp_path / "state.db",
    )

    assert result["enabled"] is True
    assert result["mode"] == "demo_autonomous"
    assert result["bridge"]["status"] == "no_candidates"


def test_autonomous_learning_cycle_runs_counterfactual_then_trace_maturation(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    al.ensure_autonomous_learning_tables(db_path)
    calls = []

    class _Gov:
        def __init__(self, db_path_arg):
            self.db_path_arg = db_path_arg

        def review_pending(self):
            calls.append("review_pending")
            return {}

        def reconcile_active(self):
            calls.append("reconcile_active")
            return {}

        def reconcile_application_effects(self, **kwargs):
            calls.append("reconcile_application_effects")
            return {}

    import backend.services.supervisor_counterfactual as scf
    import research.learning.governor as governor_module

    monkeypatch.setattr(
        scf,
        "evaluate_counterfactuals",
        lambda **kwargs: calls.append("counterfactual")
        or {"count": 1, "items": [{"evidence": "must-not-escape"}]},
    )
    monkeypatch.setattr(
        al,
        "mature_position_supervisor_traces",
        lambda **kwargs: calls.append("mature_traces") or {"matured": 1, "pending": 0},
    )
    monkeypatch.setattr(
        al,
        "backfill_trade_review_integrity_markers",
        lambda **kwargs: calls.append("backfill_review_integrity") or {"updated": 1},
    )
    monkeypatch.setattr(
        al,
        "backfill_trade_review_close_sources",
        lambda **kwargs: calls.append("backfill_close_sources") or {"updated": 1},
    )
    monkeypatch.setattr(
        al,
        "materialize_autonomous_learning_samples",
        lambda **kwargs: calls.append("materialize_samples") or {"counts": {}, "total_changed": 1},
    )
    monkeypatch.setattr(
        al,
        "materialize_portfolio_shadow_trades",
        lambda **kwargs: calls.append("portfolio_shadow") or {"inserted": 1},
    )
    monkeypatch.setattr(
        al,
        "materialize_entry_quality_governance_suggestions",
        lambda **kwargs: calls.append("entry_quality_governance") or {"suggestions": 1},
    )
    monkeypatch.setattr(
        al,
        "materialize_entry_cluster_governance_suggestions",
        lambda **kwargs: calls.append("entry_cluster_governance") or {"suggestions": 1},
    )
    monkeypatch.setattr(
        al,
        "materialize_event_window_governance_suggestions",
        lambda **kwargs: calls.append("event_window_governance") or {"suggestions": 1},
    )
    monkeypatch.setattr(
        al,
        "repair_evidence_contracts",
        lambda **kwargs: calls.append("repair_contracts") or {"repaired": 1},
    )
    monkeypatch.setattr(
        al,
        "materialize_parameter_template_recommendations",
        lambda **kwargs: calls.append("recommendations") or {"counts": {}},
    )
    monkeypatch.setattr(
        al,
        "apply_demo_autonomy",
        lambda **kwargs: calls.append("demo_apply") or {"enabled": True},
    )
    monkeypatch.setattr(governor_module, "RuleEvolutionGovernor", _Gov)

    result = al.run_autonomous_learning_cycle(db_path=db_path, sample_limit=20, apply_demo=True)

    assert result["schema_version"] == "autonomous_learning_cycle.v2"
    assert result["status"] == "completed"
    assert result["stages"]["counterfactuals"]["count"] == 1
    assert result["stages"]["trace_maturation"]["matured"] == 1
    assert result["stages"]["close_source_backfill"]["updated"] == 1
    assert result["stages"]["entry_quality_governance"]["suggestions"] == 1
    assert result["stages"]["entry_cluster_governance"]["suggestions"] == 1
    assert result["stages"]["event_window_governance"]["suggestions"] == 1
    assert result["stages"]["evidence_contract_repair"]["repaired"] == 1
    assert len(result["memory_profile"]) == 17
    assert "must-not-escape" not in json.dumps(result)
    assert calls[:5] == [
        "counterfactual",
        "mature_traces",
        "backfill_review_integrity",
        "backfill_close_sources",
        "materialize_samples",
    ]
    assert calls[5] == "portfolio_shadow"
    assert calls[6] == "entry_quality_governance"
    assert calls[7] == "entry_cluster_governance"
    assert calls[8] == "event_window_governance"
    assert calls[9] == "repair_contracts"
    assert calls[-1] == "demo_apply"

    conn = sqlite3.connect(str(db_path))
    try:
        stored_cycle = json.loads(
            conn.execute(
                """
                SELECT payload_json
                FROM evolution_events
                WHERE event_type='autonomous_learning_cycle'
                ORDER BY timestamp DESC
                LIMIT 1
                """
            ).fetchone()[0]
        )
    finally:
        conn.close()
    assert stored_cycle["schema_version"] == "autonomous_learning_cycle.v2"
    assert "items" not in stored_cycle["stages"]["counterfactuals"]

    calls.clear()
    result = al.run_autonomous_learning_cycle(db_path=db_path, sample_limit=20)

    assert result["demo_autonomy"]["status"] == "skipped_explicit_apply_required"
    assert "demo_apply" not in calls

    calls.clear()
    result = al.run_autonomous_learning_cycle(
        db_path=db_path,
        sample_limit=20,
        apply_demo=True,
        mutation_capability=False,
    )

    assert "counterfactual" in calls
    assert "materialize_samples" in calls
    assert "recommendations" in calls
    assert "review_pending" not in calls
    assert "reconcile_active" not in calls
    assert "reconcile_application_effects" not in calls
    assert "demo_apply" not in calls
    assert result["governance"]["review_pending"]["status"] == "mutation_circuit_open"
    assert result["demo_autonomy"]["status"] == "mutation_circuit_open"


def test_process_memory_snapshot_missing_proc_is_non_blocking(monkeypatch):
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda self, **kwargs: (_ for _ in ()).throw(OSError("proc unavailable")),
    )

    assert al._process_memory_snapshot() == {}
