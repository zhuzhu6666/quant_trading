import json
import time

from backend.core.db import STATE_DB_DDL, connect_sqlite
from backend.services.canonical_v2 import ensure_sqlite_schema, record_review
from backend.services.factor_counter_evidence import FactorCounterEvidenceService


def test_factor_counter_evidence_blocks_strong_shadow_and_contribution_keep_signal(tmp_path):
    db_path = tmp_path / "state.db"
    now = time.time()
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        ensure_sqlite_schema(conn)
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
            record_review(
                conn,
                review_id=review_id,
                trade_id=f"trade_{idx}",
                pnl=25.0,
                outcome_label="good_win",
                review={"regime": "trend"},
                created_at=now + idx,
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
        ensure_sqlite_schema(conn)
        conn.commit()
    finally:
        conn.close()

    result = FactorCounterEvidenceService(db_path).build_for_factor("dsl_auto_weak")

    assert result["recommended_stage"] == "allow_pruning"
    assert result["keep_score"] == 0.0
    assert result["regime_exception"]["exists"] is False


def test_factor_counter_evidence_ignores_non_entry_responsibility_for_factor_penalty(tmp_path):
    db_path = tmp_path / "state.db"
    now = time.time()
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        ensure_sqlite_schema(conn)
        for idx in range(4):
            review_id = f"review_exit_{idx}"
            record_review(
                conn,
                review_id=review_id,
                trade_id=f"trade_exit_{idx}",
                pnl=-10.0,
                outcome_label="bad_loss",
                review={
                    "primary_responsibility": "exit" if idx % 2 == 0 else "data_quality",
                    "largest_contribution_factor": "engulfing",
                    "factor_attribution": {
                        "largest_contribution_factor": "engulfing",
                        "causal_level": "observational",
                        "causal_claim": False,
                    },
                },
                created_at=now + idx,
            )
            conn.execute(
                """
                INSERT INTO factor_contribution_review
                (review_id, trade_id, factor, net_contribution, confidence, notes)
                VALUES (?, ?, 'engulfing', -0.50, 0.9, '{}')
                """,
                (review_id, f"trade_exit_{idx}"),
            )
        conn.commit()
    finally:
        conn.close()

    result = FactorCounterEvidenceService(db_path).build_for_factor("engulfing")

    assert result["sources"]["factor_contribution_review"]["sample_count"] == 0
    assert result["sources"]["factor_contribution_review"]["prune_score"] == 0.0
    assert result["prune_score"] == 0.0
