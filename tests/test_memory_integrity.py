import json
import time

from backend.core.db import STATE_DB_DDL, connect_sqlite
from backend.services.brain_memory import BrainMemoryService
from backend.services.memory_integrity import MemoryIntegrityReportService
from backend.services.trade_lesson_memory import upsert_trade_lesson_memory


def _seed_review(db_path, *, review_id="review-1", contaminated=False):
    now = time.time()
    conn = connect_sqlite(db_path)
    conn.row_factory = __import__("sqlite3").Row
    try:
        conn.executescript(STATE_DB_DDL)
        conn.execute(
            """
            INSERT INTO trade_outcome_review
            (review_id, trade_id, position_id, outcome_label, failure_tags_json,
             summary_text, review_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                review_id,
                f"trade-{review_id}",
                f"position-{review_id}",
                "bad_loss",
                json.dumps(["weak_entry_signal"]),
                "weak entry failed during noisy range",
                json.dumps(
                    {
                        "regime": "noisy_range",
                        "system_issue_context": {
                            "contaminates_learning": contaminated,
                        },
                    }
                ),
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM trade_outcome_review WHERE review_id=?", (review_id,)
        ).fetchone()
        upsert_trade_lesson_memory(conn, row)
        conn.commit()
    finally:
        conn.close()
    return now


def test_memory_integrity_reports_healthy_rebuildable_three_layer_path(tmp_path):
    db_path = tmp_path / "state.db"
    created_at = _seed_review(db_path)
    BrainMemoryService(db_path).retrieve(
        world_model={"market_regime": "noisy_range"},
        hypotheses=[],
        persist=True,
    )

    report = MemoryIntegrityReportService(db_path).build()

    assert report["status"] == "healthy"
    assert report["ok"] is True
    assert report["raw_evidence"]["total"] == 1
    assert report["experience_projection"]["total"] == 1
    assert report["experience_projection"]["source_lag_seconds"] == 0.0
    assert report["retrieval_index"]["role"] == "bounded_rebuildable_index_not_archive"
    assert report["retrieval_index"]["window_available"] is True
    assert report["boundary"]["does_not_authorize_actions"] is True
    assert report["boundary"]["affects_trading"] is False
    assert report["raw_evidence"]["latest_created_at"] == created_at


def test_memory_integrity_exposes_projection_and_index_breaks_without_mutating_sources(tmp_path):
    db_path = tmp_path / "state.db"
    _seed_review(db_path)
    BrainMemoryService(db_path).retrieve(
        world_model={"market_regime": "noisy_range"},
        hypotheses=[],
        persist=True,
    )
    conn = connect_sqlite(db_path)
    conn.row_factory = __import__("sqlite3").Row
    try:
        original = conn.execute(
            "SELECT review_json, created_at FROM trade_outcome_review WHERE review_id='review-1'"
        ).fetchone()
        conn.execute(
            """
            INSERT INTO experience_memory
            (experience_id, source_table, source_id, append_source, created_at)
            VALUES ('duplicate-lesson', 'trade_outcome_review', 'review-1',
                    'trade_lesson_memory.v1', ?)
            """,
            (float(original["created_at"]) + 1.0,),
        )
        conn.execute(
            """
            INSERT INTO brain_memory
            (memory_id, source_table, source_id, created_at, last_used_at)
            VALUES ('orphan-index', 'experience_memory', 'missing-lesson', 1.0, 1.0)
            """
        )
        conn.commit()
    finally:
        conn.close()

    report = MemoryIntegrityReportService(db_path).build()

    assert report["status"] == "degraded"
    assert report["experience_projection"]["duplicate_source_count"] == 1
    assert report["experience_projection"]["timestamp_mismatch_count"] == 1
    assert report["retrieval_index"]["missing_source_reference_count"] == 1

    conn = connect_sqlite(db_path, read_only=True)
    conn.row_factory = __import__("sqlite3").Row
    try:
        unchanged = conn.execute(
            "SELECT review_json, created_at FROM trade_outcome_review WHERE review_id='review-1'"
        ).fetchone()
    finally:
        conn.close()
    assert dict(unchanged) == dict(original)


def test_memory_integrity_exposes_missing_and_noncanonical_projections(tmp_path):
    db_path = tmp_path / "state.db"
    _seed_review(db_path)
    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            "DELETE FROM experience_memory WHERE source_id='review-1'"
        )
        conn.execute(
            """
            INSERT INTO experience_memory
            (experience_id, source_table, source_id, append_source, created_at)
            VALUES ('legacy-lesson', 'legacy_review', 'review-1', 'legacy.v0', 1.0)
            """
        )
        conn.commit()
    finally:
        conn.close()

    report = MemoryIntegrityReportService(db_path).build()

    assert report["status"] == "degraded"
    assert report["experience_projection"]["missing_source_count"] == 1
    assert report["experience_projection"]["noncanonical_projection_count"] == 1


def test_memory_integrity_treats_an_empty_initialized_corpus_as_healthy(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.commit()
    finally:
        conn.close()

    report = MemoryIntegrityReportService(db_path).build()

    assert report["status"] == "healthy"
    assert report["raw_evidence"]["total"] == 0
    assert report["experience_projection"]["total"] == 0
    assert report["retrieval_index"]["window_available"] is True


def test_memory_integrity_counts_contaminated_sources_without_indexing_them(tmp_path):
    db_path = tmp_path / "state.db"
    _seed_review(db_path, review_id="clean")
    _seed_review(db_path, review_id="contaminated", contaminated=True)
    BrainMemoryService(db_path).retrieve(
        world_model={"market_regime": "noisy_range"},
        hypotheses=[],
        persist=True,
    )

    report = MemoryIntegrityReportService(db_path).build()

    assert report["status"] == "healthy"
    assert report["raw_evidence"]["contaminated_quarantined_count"] == 1
    assert report["retrieval_index"]["contaminated_indexed_count"] == 0


def test_brain_memory_refresh_removes_only_orphaned_derived_references(tmp_path):
    db_path = tmp_path / "state.db"
    _seed_review(db_path)
    BrainMemoryService(db_path).retrieve(
        world_model={"market_regime": "noisy_range"},
        hypotheses=[],
        persist=True,
    )
    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            """
            INSERT INTO brain_memory
            (memory_id, source_table, source_id, created_at, last_used_at)
            VALUES ('retired-lesson-index', 'experience_memory', 'retired-experience', 1.0, 1.0)
            """
        )
        conn.commit()
    finally:
        conn.close()

    before = MemoryIntegrityReportService(db_path).build()
    BrainMemoryService(db_path).retrieve(
        world_model={"market_regime": "noisy_range"},
        hypotheses=[],
        persist=True,
    )
    after = MemoryIntegrityReportService(db_path).build()

    assert before["retrieval_index"]["missing_source_reference_count"] == 1
    assert after["status"] == "healthy"
    assert after["retrieval_index"]["missing_source_reference_count"] == 0


def test_memory_integrity_is_unavailable_when_required_schema_is_absent(tmp_path):
    report = MemoryIntegrityReportService(tmp_path / "missing.db").build()

    assert report["status"] == "unavailable"
    assert report["ok"] is False
    assert report["errors"]
