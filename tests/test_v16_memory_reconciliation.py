from backend.core.db import STATE_DB_DDL, connect_sqlite
from backend.services.canonical_v2 import (
    record_counterfactual_event,
    record_review,
)
from backend.services.canonical_v2_reader import iter_review_rows
from backend.services.v16_brain_snapshot import BrainMemoryService
from backend.services.trade_lesson_memory import upsert_trade_lesson_memory
from tests.canonical_fixture import make_canonical_sqlite


def _canonical_connection(db_path):
    conn = make_canonical_sqlite(db_path)
    conn.executescript(STATE_DB_DDL)
    return conn


def _seed_review_and_counterfactual(db_path):
    conn = _canonical_connection(db_path)
    record_review(
        conn,
        review_id="review-current",
        trade_id="trade-current",
        position_id="position-current",
        pnl=-0.64,
        outcome_label="good_loss",
        failure_tags=["good_loss", "thesis_broken"],
        summary_text="current trade",
        review={"primary_responsibility": "thesis"},
        created_at=2000.0,
    )
    row = next(
        row
        for row in iter_review_rows(conn, limit=0)
        if row["review_id"] == "review-current"
    )
    upsert_trade_lesson_memory(conn, row)
    record_counterfactual_event(
        conn,
        counterfactual_id="cf-current",
        review_id="review-current",
        event_ts=2000.0,
        payload={
            "counterfactual_id": "cf-current",
            "review_id": "review-current",
            "trade_id": "trade-current",
            "position_id": "position-current",
            "close_ts": 2000.0,
            "label": "premature_tighten",
            "confidence": 0.8,
            "horizons": [{"horizon_minutes": 30, "future_pnl": 4.0}],
            "evidence": {"tags": ["future_recovered", "original_tp_first"]},
            "created_at": 2010.0,
            "updated_at": 2010.0,
        },
    )
    conn.commit()
    conn.close()


def test_counterfactual_window_uses_event_time_not_recompute_time(tmp_path):
    db_path = tmp_path / "state.db"
    conn = _canonical_connection(db_path)
    for index in range(60):
        record_review(
            conn,
            review_id=f"review-{index}",
            trade_id=f"trade-{index}",
            position_id=f"position-{index}",
            created_at=1000.0 + index,
        )
        record_counterfactual_event(
            conn,
            counterfactual_id=f"cf-{index}",
            review_id=f"review-{index}",
            event_ts=1000.0 + index,
            payload={
                "counterfactual_id": f"cf-{index}",
                "review_id": f"review-{index}",
                "trade_id": f"trade-{index}",
                "position_id": f"position-{index}",
                "close_ts": 1000.0 + index,
                "label": "correct_stop",
                "confidence": 0.8,
                "horizons": [{"horizon_minutes": 30, "future_pnl": 1.0}],
                "evidence": {"tags": ["future_bars_complete"]},
                "created_at": 10000.0 + (59 - index),
                "updated_at": 10000.0 + (59 - index),
            },
        )
    record_review(
        conn,
        review_id="review-contaminated",
        trade_id="trade-contaminated",
        position_id="position-contaminated",
        review={"system_issue_context": {"contaminates_learning": True}},
        created_at=9000.0,
    )
    for counterfactual_id, review_id, event_ts, trade_id, position_id, evidence in (
        ("cf-orphan", "review-missing", 9002.0, "trade-orphan", "position-orphan", {}),
        ("cf-contaminated", "review-contaminated", 9001.0, "trade-contaminated", "position-contaminated", {}),
        ("cf-invalidated", "review-59", 9000.0, "trade-invalidated", "position-invalidated", {"evidence_invalidated": True}),
    ):
        record_counterfactual_event(
            conn,
            counterfactual_id=counterfactual_id,
            review_id=review_id,
            event_ts=event_ts,
            payload={
                "counterfactual_id": counterfactual_id,
                "review_id": review_id,
                "trade_id": trade_id,
                "position_id": position_id,
                "close_ts": event_ts,
                "label": "correct_stop",
                "confidence": 0.9,
                "horizons": [],
                "evidence": evidence,
                "created_at": event_ts,
                "updated_at": event_ts,
            },
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
