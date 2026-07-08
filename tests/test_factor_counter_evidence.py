import json
import time

from backend.core.db import STATE_DB_DDL, connect_sqlite
from backend.services.factor_counter_evidence import FactorCounterEvidenceService


def test_factor_counter_evidence_blocks_strong_shadow_and_contribution_keep_signal(tmp_path):
    db_path = tmp_path / "state.db"
    now = time.time()
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.execute(
            """
            INSERT INTO shadow_factor_perf
            (factor, oos_bars, cumulative_pnl, hit_rate, max_drawdown, updated_at)
            VALUES ('factor_keep', 240, 120.0, 0.58, -12.0, ?)
            """,
            (now,),
        )
        for idx in range(4):
            review_id = f"review_keep_{idx}"
            conn.execute(
                """
                INSERT INTO trade_outcome_review
                (review_id, trade_id, pnl, outcome_label, review_json, created_at)
                VALUES (?, ?, 25.0, 'good_win', ?, ?)
                """,
                (review_id, f"trade_{idx}", json.dumps({"regime": "trend"}), now + idx),
            )
            conn.execute(
                """
                INSERT INTO factor_contribution_review
                (review_id, trade_id, factor, net_contribution, confidence, notes)
                VALUES (?, ?, 'factor_keep', 0.18, 0.8, 'positive contribution')
                """,
                (review_id, f"trade_{idx}"),
            )
        conn.commit()
    finally:
        conn.close()

    result = FactorCounterEvidenceService(db_path).build_for_factor("factor_keep")

    assert result["schema_version"] == "factor_counter_evidence.v1"
    assert result["recommended_stage"] == "block_pruning"
    assert result["keep_score"] >= 0.65
    assert result["sources"]["shadow_factor_perf"]["sample_count"] == 240
    assert result["sources"]["factor_contribution_review"]["sample_count"] == 4


def test_factor_counter_evidence_allows_when_no_keep_signal(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.commit()
    finally:
        conn.close()

    result = FactorCounterEvidenceService(db_path).build_for_factor("dsl_auto_weak")

    assert result["recommended_stage"] == "allow_pruning"
    assert result["keep_score"] == 0.0
    assert result["regime_exception"]["exists"] is False
