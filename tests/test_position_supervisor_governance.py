import json
import sqlite3

from backend.services.position_supervisor_governance import (
    build_position_supervisor_advisories,
    replay_position_supervisor_templates,
)
from backend.services.position_supervisor_templates import (
    CONSERVATIVE_TEMPLATE_ID,
    DEFAULT_TEMPLATE_ID,
)


def _create_db(path):
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE trade_outcome_review (
            review_id TEXT PRIMARY KEY,
            trade_id TEXT DEFAULT '',
            position_id TEXT DEFAULT '',
            entry_decision_id TEXT DEFAULT '',
            exit_decision_id TEXT DEFAULT '',
            pnl REAL DEFAULT 0.0,
            mae REAL DEFAULT 0.0,
            mfe REAL DEFAULT 0.0,
            outcome_label TEXT DEFAULT '',
            failure_tags_json TEXT DEFAULT '[]',
            summary_text TEXT DEFAULT '',
            review_json TEXT DEFAULT '{}',
            created_at REAL NOT NULL DEFAULT 0.0
        );
        CREATE TABLE position_lifecycle_event (
            event_id TEXT PRIMARY KEY,
            position_id TEXT NOT NULL,
            trade_id TEXT DEFAULT '',
            symbol TEXT DEFAULT '',
            event_type TEXT NOT NULL,
            event_ts REAL NOT NULL DEFAULT 0.0,
            net_volume REAL DEFAULT 0.0,
            avg_price REAL DEFAULT 0.0,
            unrealized_pnl REAL DEFAULT 0.0,
            realized_pnl REAL DEFAULT 0.0,
            details_json TEXT DEFAULT '{}'
        );
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
        """
    )
    # 2026-06-26 10:00 Asia/Shanghai.
    created_at = 1782439200.0
    review = {
        "position_id": "1001",
        "entry_ts": created_at - 60,
        "close_ts": created_at,
        "holding_seconds": 60.0,
        "mfe": 0.0,
        "mae": 1.4,
        "giveback_ratio": 0.0,
        "profit_capture_ratio": 0.0,
        "holding_efficiency": 0.4,
        "time_decay_score": 0.9,
        "thesis_status": "broken",
        "thesis_status_at_exit": "broken",
        "regime_shift": "none",
        "close_price": 2999.0,
        "close_reason": "thesis_broken",
        "real_pnl": {"gross": -1.0, "net": -1.0, "entry_price": 3000.0},
    }
    conn.execute(
        """
        INSERT INTO trade_outcome_review
        (review_id, trade_id, position_id, pnl, mae, mfe, outcome_label,
         failure_tags_json, summary_text, review_json, created_at)
        VALUES ('rev_1', '1001', '1001', -1.0, 1.4, 0.0, 'good_loss',
                '[]', 'small loss', ?, ?)
        """,
        (json.dumps(review), created_at),
    )
    conn.execute(
        """
        INSERT INTO position_lifecycle_event
        (event_id, position_id, trade_id, symbol, event_type, event_ts, details_json)
        VALUES ('open_1', '1001', '1001', 'XAUUSD+', 'opened', ?, ?)
        """,
        (created_at - 60, json.dumps({"sl": 2980.0, "tp": 3040.0})),
    )
    conn.commit()
    conn.close()


def test_replay_position_supervisor_templates_compares_default_and_candidate(tmp_path):
    db_path = tmp_path / "state.db"
    _create_db(db_path)

    result = replay_position_supervisor_templates(day="2026-06-26", db_path=db_path)

    assert result["sample_count"] == 1
    summaries = {item["template_id"]: item for item in result["templates"]}
    assert summaries[DEFAULT_TEMPLATE_ID]["actions"]["close"] == 1
    assert summaries[CONSERVATIVE_TEMPLATE_ID]["actions"]["tighten"] == 1
    assert result["comparison"]["small_loss_closes_reduced"] == 1


def test_position_supervisor_advisories_are_advisory_only_and_materializable(tmp_path):
    db_path = tmp_path / "state.db"
    _create_db(db_path)

    result = build_position_supervisor_advisories(
        day="2026-06-26",
        db_path=db_path,
        materialize=True,
    )

    assert result["advisory_only"] is True
    assert result["materialized"] is True
    actions = {item["action"] for item in result["items"]}
    assert "relax_thesis_break" in actions

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT scope_type, action, status FROM policy_suggestion").fetchall()
    finally:
        conn.close()
    assert rows
    assert rows[0][0] == "position_supervisor_template"
    assert rows[0][2] == "proposed"
