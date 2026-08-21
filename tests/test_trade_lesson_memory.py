import json
import time

from backend.core.db import STATE_DB_DDL, connect_sqlite
from backend.services.canonical_v2 import record_review
from backend.services.canonical_v2_reader import iter_review_rows
from backend.services.v16_brain_snapshot import BrainMemoryService
from backend.services.trade_lesson_memory import upsert_trade_lesson_memory
from tests.canonical_fixture import make_canonical_sqlite


def test_trade_lesson_memory_upserts_stable_experience_and_brain_reads_it(tmp_path):
    db_path = tmp_path / "state.db"
    now = time.time()
    conn = make_canonical_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        record_review(
            conn,
            review_id="review_lesson_1",
            trade_id="trade_1",
            position_id="pos_1",
            entry_decision_id="entry_1",
            exit_decision_id="exit_1",
            pnl=-12.5,
            mae=-18.0,
            mfe=4.0,
            outcome_label="bad_loss",
            failure_tags=["weak_entry_signal"],
            summary_text="weak entry failed during noisy session",
            review={
                "regime": "noisy_range",
                "top_factor": "rsi_14",
                "signal_score": 0.21,
                "demo_nursery_observations": [{"reason": "var_gate", "source": "var_gate"}],
                "temporal_context": {"timeframe": "M5"},
            },
            created_at=now,
        )
        row = next(
            row
            for row in iter_review_rows(conn, limit=0)
            if row["review_id"] == "review_lesson_1"
        )
        first = upsert_trade_lesson_memory(conn, row)
        second = upsert_trade_lesson_memory(conn, row)
        conn.commit()

        count = conn.execute(
            "SELECT COUNT(*) FROM experience_memory WHERE experience_id='trade_lesson:review_lesson_1'"
        ).fetchone()[0]
        stored = conn.execute(
            """
            SELECT append_source, source_table, source_id, recommended_action, decision_context_json
            FROM experience_memory
            WHERE experience_id='trade_lesson:review_lesson_1'
            """
        ).fetchone()
    finally:
        conn.close()

    assert first["experience_id"] == second["experience_id"] == "trade_lesson:review_lesson_1"
    assert count == 1
    assert stored["append_source"] == "trade_lesson_memory.v1"
    assert stored["source_table"] == "canonical_v2.trade_review"
    assert stored["source_id"] == "review_lesson_1"
    assert stored["recommended_action"] == "tighten_entry_review"
    context = json.loads(stored["decision_context_json"])
    assert "review_json" not in context
    assert context["market_state"]["regime"] == "noisy_range"
    assert context["risk_observations"][0]["reason"] == "var_gate"
    assert context["result"]["outcome_label"] == "bad_loss"
    assert context["result"] == context["outcome"]
    assert context["reusable_lesson"] == "weak entry failed during noisy session"
    assert context["allowed_uses"] == ["memory_retrieval", "critic_context", "demo_learning_review"]
    assert context["confidence"] == context["lesson"]["confidence"]
    assert context["recommended_action"] == "tighten_entry_review"

    memory = BrainMemoryService(db_path).retrieve(
        world_model={"market_regime": "noisy_range"},
        hypotheses=[],
        persist=False,
        limit=10,
    )

    item = next(
        item for item in memory["items"]
        if item["source_table"] == "experience_memory" and item["source_id"] == "trade_lesson:review_lesson_1"
    )
    assert memory["raw_item_count"] == 2
    assert memory["evidence_unit_count"] == 1
    assert item["evidence_unit_id"] == "trade_review:review_lesson_1"
    assert len(item["evidence_sources"]) == 2
    assert item["structured"]["append_source"] == "trade_lesson_memory.v1"
    assert item["structured"]["lesson"]["recommended_action"] == "tighten_entry_review"
    assert item["structured"]["recommended_action"] == "tighten_entry_review"
