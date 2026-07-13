import json
import sqlite3

from backend.core.db import STATE_DB_DDL, connect_sqlite
from backend.services.brain_memory import BrainMemoryService
from backend.services.trade_lesson_memory import upsert_trade_lesson_memory


def _seed_review_and_counterfactual(db_path):
    conn = connect_sqlite(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(STATE_DB_DDL)
    conn.execute(
        """
        INSERT INTO trade_outcome_review
        (review_id, trade_id, position_id, pnl, outcome_label,
         failure_tags_json, summary_text, review_json, created_at)
        VALUES ('review-current', 'trade-current', 'position-current', -0.64,
                'good_loss', ?, 'current trade', ?, 2000.0)
        """,
        (
            json.dumps(["good_loss", "thesis_broken"]),
            json.dumps({"primary_responsibility": "thesis"}),
        ),
    )
    row = conn.execute(
        "SELECT * FROM trade_outcome_review WHERE review_id='review-current'"
    ).fetchone()
    upsert_trade_lesson_memory(conn, row)
    conn.execute(
        """
        INSERT INTO supervisor_counterfactual_review
        (counterfactual_id, review_id, trade_id, position_id, close_ts,
         label, confidence, horizons_json, evidence_json, created_at, updated_at)
        VALUES ('cf-current', 'review-current', 'trade-current', 'position-current',
                2000.0, 'premature_tighten', 0.8, ?, ?, 2010.0, 2010.0)
        """,
        (
            json.dumps([{"horizon_minutes": 30, "future_pnl": 4.0}]),
            json.dumps({"tags": ["future_recovered", "original_tp_first"]}),
        ),
    )
    conn.commit()
    conn.close()


def test_counterfactual_window_uses_event_time_not_recompute_time(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(STATE_DB_DDL)
    for index in range(60):
        conn.execute(
            """
            INSERT INTO supervisor_counterfactual_review
            (counterfactual_id, trade_id, position_id, close_ts, label,
             confidence, horizons_json, evidence_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'correct_stop', 0.8, ?, ?, ?, ?)
            """,
            (
                f"cf-{index}",
                f"trade-{index}",
                f"position-{index}",
                1000.0 + index,
                json.dumps([{"horizon_minutes": 30, "future_pnl": 1.0}]),
                json.dumps({"tags": ["future_bars_complete"]}),
                10000.0 + (59 - index),
                10000.0 + (59 - index),
            ),
        )
    conn.commit()

    items = BrainMemoryService(db_path)._counterfactual_memories(conn, set(), [])
    conn.close()

    assert len(items) == 50
    assert items[0]["structured"]["trade_id"] == "trade-59"
    assert items[-1]["structured"]["trade_id"] == "trade-10"


def test_mature_supervisor_posterior_reconciles_entry_memory(tmp_path):
    db_path = tmp_path / "state.db"
    _seed_review_and_counterfactual(db_path)

    memory = BrainMemoryService(db_path).retrieve(
        world_model={"market_regime": "trend=weak"},
        hypotheses=[],
        persist=False,
        limit=10,
    )
    item = next(
        item for item in memory["items"]
        if item["source_table"] == "experience_memory"
    )
    reconciliation = item["structured"]["posterior_reconciliation"]

    assert memory["posterior_arbitration"]["selected_scope"] == "supervisor"
    assert memory["posterior_memory"]["structured"]["final_memory"] is True
    assert reconciliation["status"] == "entry_conclusion_retained"
    assert reconciliation["action_owner"] == "autonomous_learning"
    assert item["polarity"] == "neutral"
    assert item["evidence_eligible"] is False


def test_stale_policy_suggestions_are_not_indexed_as_current_memory(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    conn.executescript(STATE_DB_DDL)
    conn.execute(
        """
        INSERT INTO policy_suggestion
        (suggestion_id, scope_type, scope_key, action, confidence, reason, status, created_at)
        VALUES ('stale-suggestion', 'factor', 'rsi_14', 'downweight', 0.9,
                'old proposal', 'superseded', 1000.0)
        """
    )
    conn.execute(
        """
        INSERT INTO policy_suggestion
        (suggestion_id, scope_type, scope_key, action, confidence, reason, status, created_at)
        VALUES ('risk-blocked-suggestion', 'factor', 'adx', 'downweight', 0.7,
                'risk evidence', 'blocked_by_risk', 1001.0)
        """
    )
    conn.commit()
    conn.close()

    BrainMemoryService(db_path).retrieve(
        world_model={"market_regime": "factor"},
        hypotheses=[],
        persist=True,
        limit=10,
    )
    indexed = BrainMemoryService(db_path).latest_indexed(limit=50)
    source_ids = {
        item["source_id"] for item in indexed["items"]
        if item["source_table"] == "policy_suggestion"
    }

    assert "stale-suggestion" not in source_ids
    assert "risk-blocked-suggestion" in source_ids
