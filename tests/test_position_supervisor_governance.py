import json
import sqlite3

from backend.services.position_supervisor_governance import (
    _counterfactual_summary,
    build_position_supervisor_advisories,
    replay_position_supervisor_templates,
)
from backend.services.position_supervisor_templates import (
    CONSERVATIVE_TEMPLATE_ID,
    DEFAULT_TEMPLATE_ID,
    PROFIT_PROTECTION_TEMPLATE_ID,
    list_position_supervisor_templates,
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
            governance_eligible INTEGER NOT NULL DEFAULT 0,
            governance_eligibility_version TEXT NOT NULL DEFAULT '',
            governance_eligibility_fingerprint TEXT NOT NULL DEFAULT '',
            governance_ineligible_reason TEXT NOT NULL DEFAULT '',
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
    conn.execute(
        """
        INSERT INTO supervisor_counterfactual_review
        (counterfactual_id, review_id, trade_id, position_id, close_ts,
         close_reason, supervisor_event_type, supervisor_reason, label,
         confidence, horizons_json, evidence_json, created_at, updated_at)
        VALUES ('cf_1', 'rev_1', '1001', '1001', ?, 'thesis_broken',
                'supervisor_close', 'thesis_broken', 'protection_too_tight',
                0.72, '[]', '{}', ?, ?)
        """,
        (created_at, created_at, created_at),
    )
    conn.commit()
    conn.close()


def test_replay_position_supervisor_templates_compares_default_and_candidate(tmp_path):
    db_path = tmp_path / "state.db"
    _create_db(db_path)

    result = replay_position_supervisor_templates(day="2026-06-26", db_path=db_path)

    assert result["sample_count"] == 1
    summaries = {item["template_id"]: item for item in result["templates"]}
    assert summaries[DEFAULT_TEMPLATE_ID]["actions"]["close"] == 0
    assert summaries[CONSERVATIVE_TEMPLATE_ID]["actions"]["close"] == 0
    assert result["comparison"]["small_loss_closes_reduced"] == 0


def test_replay_and_advisory_filter_contamination_before_effective_limit(tmp_path):
    db_path = tmp_path / "state.db"
    _create_db(db_path)
    contaminated = {
        "position_id": "contaminated",
        "close_ts": 1782439100.0,
        "mfe": 4.0,
        "mae": 1.0,
        "giveback_ratio": 0.96,
        "profit_capture_ratio": 0.02,
        "close_price": 2999.0,
        "real_pnl": {"gross": -1.0, "net": -1.0, "entry_price": 3000.0},
        "system_issue_context": {
            "system_contaminated": True,
            "contaminates_learning": True,
        },
    }
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO trade_outcome_review
            (review_id, trade_id, position_id, pnl, mae, mfe, outcome_label,
             failure_tags_json, summary_text, review_json, created_at)
            VALUES ('rev_contaminated', 'contaminated', 'contaminated',
                    -1.0, 1.0, 4.0, 'bad_loss', '[]',
                    'contaminated capture failure', ?, 1782439100.0)
            """,
            (json.dumps(contaminated),),
        )
        conn.commit()
    finally:
        conn.close()

    replay = replay_position_supervisor_templates(
        day="2026-06-26",
        db_path=db_path,
        limit=1,
    )
    advisory = build_position_supervisor_advisories(
        day="2026-06-26",
        db_path=db_path,
    )

    assert replay["sample_count"] == 1
    assert [sample["review_id"] for sample in replay["samples"]] == ["rev_1"]
    assert advisory["replay_summary"]["capture_failure_summary"]["capture_failed_count"] == 0
    actions = {item["action"] for item in advisory["items"]}
    assert "switch_position_supervisor_template" not in actions
    assert "tighten_mfe_capture_protection" not in actions


def test_position_supervisor_advisories_do_not_materialize_without_eligible_evidence(tmp_path):
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
    assert "relax_thesis_break" not in actions
    assert result["replay_summary"]["counterfactual_summary"]["total"] == 0

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT scope_type, action, status, evidence_json FROM policy_suggestion").fetchall()
    finally:
        conn.close()
    assert result["items"] == []
    assert rows == []


def test_counterfactual_summary_excludes_contaminated_source_review(tmp_path):
    db_path = tmp_path / "state.db"
    _create_db(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            """
            UPDATE supervisor_counterfactual_review
            SET evidence_json=?
            WHERE counterfactual_id='cf_1'
            """,
            (json.dumps({"maturity": {"governance_eligible": True}}),),
        )
        conn.commit()
        assert _counterfactual_summary(conn, day="2026-06-26")["total"] == 1

        review = json.loads(
            conn.execute(
                "SELECT review_json FROM trade_outcome_review WHERE review_id='rev_1'"
            ).fetchone()[0]
        )
        review["system_issue_context"] = {
            "system_contaminated": True,
            "contaminates_learning": True,
        }
        conn.execute(
            "UPDATE trade_outcome_review SET review_json=? WHERE review_id='rev_1'",
            (json.dumps(review),),
        )
        conn.commit()

        assert _counterfactual_summary(conn, day="2026-06-26")["total"] == 0
    finally:
        conn.close()


def test_position_supervisor_advisories_materialize_mfe_capture_failure_template(tmp_path):
    db_path = tmp_path / "state.db"
    _create_db(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        base_ts = 1782439300.0
        for idx in range(2):
            position_id = f"cap_{idx}"
            review = {
                "position_id": position_id,
                "entry_ts": base_ts + idx - 90,
                "close_ts": base_ts + idx,
                "holding_seconds": 90.0,
                "mfe": 2.0 + idx,
                "mae": 1.0,
                "giveback_ratio": 0.96,
                "profit_capture_ratio": 0.02,
                "holding_efficiency": 0.1,
                "time_decay_score": 0.4,
                "thesis_status": "weakening",
                "thesis_status_at_exit": "weakening",
                "regime_shift": "none",
                "close_price": 2999.0,
                "close_reason": "broker_close",
                "close_reason_source": "supervisor_tighten_stopout",
                "inferred_close_supervisor": {
                    "event_type": "supervisor_tighten",
                    "action": "tighten",
                    "action_reason": "profit_giveback_after_mfe",
                },
                "real_pnl": {"gross": -1.0, "net": -1.0, "entry_price": 3000.0},
            }
            conn.execute(
                """
                INSERT INTO trade_outcome_review
                (review_id, trade_id, position_id, pnl, mae, mfe, outcome_label,
                 failure_tags_json, summary_text, review_json, created_at)
                VALUES (?, ?, ?, -1.0, 1.0, ?, 'bad_loss',
                        '[]', 'capture failed', ?, ?)
                """,
                (f"cap_rev_{idx}", position_id, position_id, 2.0 + idx, json.dumps(review), base_ts + idx),
            )
            conn.execute(
                """
                INSERT INTO position_lifecycle_event
                (event_id, position_id, trade_id, symbol, event_type, event_ts, details_json)
                VALUES (?, ?, ?, 'XAUUSD+', 'opened', ?, ?)
                """,
                (f"cap_open_{idx}", position_id, position_id, base_ts + idx - 90, json.dumps({"sl": 2980.0, "tp": 3040.0})),
            )
        conn.commit()
    finally:
        conn.close()

    result = build_position_supervisor_advisories(
        day="2026-06-26",
        db_path=db_path,
        materialize=True,
    )

    capture_summary = result["replay_summary"]["capture_failure_summary"]
    assert capture_summary["capture_failed_count"] == 2
    items = {item["action"]: item for item in result["items"]}
    capture_item = items["tighten_mfe_capture_protection"]
    assert capture_item["scope_key"].startswith("position_supervisor:auto_mfe_capture_protection.")
    capture_evidence = capture_item["evidence"]
    assert capture_evidence["base_template"]["template_id"] == DEFAULT_TEMPLATE_ID
    assert capture_evidence["candidate_patch"]["path"] == "thresholds.giveback_reduce_threshold"
    assert capture_evidence["generation_context"]["regime_stratum"] == "range_capture"
    generated = items["switch_position_supervisor_template"]
    assert generated["scope_key"].startswith("position_supervisor:auto_tpsl.")
    assert generated["evidence"]["candidate_template"]["tp_policy"]["extension_enabled"] is True
    assert generated["evidence"]["candidate_patch"]["path"] == "sl_policy.profit_lock_multiplier"
    assert generated["evidence"]["generation_context"]["regime_stratum"] == "range_capture"

    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            """
            SELECT scope_key, action, status
            FROM policy_suggestion
            WHERE action='tighten_mfe_capture_protection'
            """
        ).fetchone()
        generated_row = conn.execute(
            """
            SELECT scope_key, action, status, evidence_json
            FROM policy_suggestion
            WHERE action='switch_position_supervisor_template'
            """
        ).fetchone()
        eligible_row = conn.execute(
            """
            SELECT governance_eligible, governance_eligibility_version,
                   governance_eligibility_fingerprint, governance_ineligible_reason
            FROM policy_suggestion
            WHERE action='switch_position_supervisor_template'
            """
        ).fetchone()
    finally:
        conn.close()
    assert row == (
        capture_item["scope_key"],
        "tighten_mfe_capture_protection",
        "proposed",
    )
    assert generated_row[0] == generated["scope_key"]
    assert json.loads(generated_row[3])["candidate_template"]["template_id"] == generated["scope_key"]

    eligibility = json.loads(generated_row[3])["governance_eligibility"]
    assert eligibility["governance_eligible"] is True
    assert eligibility["governance_eligibility_version"] == "governance_eligibility.v1"
    assert eligibility["governance_eligibility_fingerprint"]

    assert eligible_row[0] == 1
    assert eligible_row[1] == "governance_eligibility.v1"
    assert eligible_row[2]
    assert eligible_row[3] == ""

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            UPDATE policy_suggestion
            SET status='rejected', governance_eligible=0,
                governance_eligibility_version='',
                governance_eligibility_fingerprint='',
                governance_ineligible_reason='eligibility_contract_invalid'
            WHERE action='switch_position_supervisor_template'
            """
        )
        conn.commit()
    finally:
        conn.close()

    build_position_supervisor_advisories(
        day="2026-06-26",
        db_path=db_path,
        materialize=True,
    )

    conn = sqlite3.connect(str(db_path))
    try:
        repaired_row = conn.execute(
            """
            SELECT status, governance_eligible, governance_eligibility_version,
                   governance_eligibility_fingerprint, governance_ineligible_reason
            FROM policy_suggestion
            WHERE action='switch_position_supervisor_template'
            """
        ).fetchone()
    finally:
        conn.close()
    assert repaired_row[0] == "proposed"
    assert repaired_row[1] == 1
    assert repaired_row[2] == "governance_eligibility.v1"
    assert repaired_row[3]
    assert repaired_row[4] == ""

    templates = {item["template_id"]: item for item in list_position_supervisor_templates(db_path=db_path)}
    assert generated["scope_key"] in templates
    assert templates[generated["scope_key"]]["source"] == "generated_from_supervisor_learning"


def test_counterfactual_overprotection_blocks_tighter_generated_template(tmp_path):
    db_path = tmp_path / "state.db"
    _create_db(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        base_ts = 1782439300.0
        for idx in range(2):
            position_id = f"overprotected_{idx}"
            review = {
                "position_id": position_id,
                "entry_ts": base_ts + idx - 90,
                "close_ts": base_ts + idx,
                "holding_seconds": 90.0,
                "mfe": 2.0,
                "mae": 1.0,
                "giveback_ratio": 0.96,
                "profit_capture_ratio": 0.02,
                "close_price": 2999.0,
                "real_pnl": {"gross": -1.0, "net": -1.0, "entry_price": 3000.0},
            }
            conn.execute(
                """
                INSERT INTO trade_outcome_review
                (review_id, trade_id, position_id, pnl, mae, mfe, outcome_label,
                 failure_tags_json, summary_text, review_json, created_at)
                VALUES (?, ?, ?, -1.0, 1.0, 2.0, 'bad_loss',
                        '[]', 'capture failed', ?, ?)
                """,
                (
                    f"overprotected_rev_{idx}",
                    position_id,
                    position_id,
                    json.dumps(review),
                    base_ts + idx,
                ),
            )
            conn.execute(
                """
                INSERT INTO position_lifecycle_event
                (event_id, position_id, trade_id, symbol, event_type, event_ts, details_json)
                VALUES (?, ?, ?, 'XAUUSD+', 'opened', ?, ?)
                """,
                (
                    f"overprotected_open_{idx}",
                    position_id,
                    position_id,
                    base_ts + idx - 90,
                    json.dumps({"sl": 2980.0, "tp": 3040.0}),
                ),
            )
            conn.execute(
                """
                INSERT INTO supervisor_counterfactual_review
                (counterfactual_id, review_id, trade_id, position_id, close_ts,
                 close_reason, supervisor_event_type, supervisor_reason, label,
                 confidence, horizons_json, evidence_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'stopout', 'supervisor_tighten',
                        'profit_giveback_after_mfe', 'protection_too_tight',
                        0.8, '[]', ?, ?, ?)
                """,
                (
                    f"overprotected_cf_{idx}",
                    f"overprotected_rev_{idx}",
                    position_id,
                    position_id,
                    base_ts + idx,
                    json.dumps({"maturity": {"governance_eligible": True}}),
                    base_ts + idx,
                    base_ts + idx,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    result = build_position_supervisor_advisories(
        day="2026-06-26",
        db_path=db_path,
    )

    switch_items = [
        item for item in result["items"]
        if item["action"] == "switch_position_supervisor_template"
    ]
    assert len(switch_items) == 1
    assert switch_items[0]["scope_key"].startswith(
        "position_supervisor:auto_overprotection_relief."
    )
    assert switch_items[0]["evidence"]["candidate_patch"]["path"] == (
        "thresholds.min_thesis_break_seconds"
    )
    assert all(
        item["action"] != "tighten_mfe_capture_protection"
        for item in result["items"]
    )
    assert any(
        item["reason"] == "counterfactual evidence shows protection is already too aggressive"
        for item in result["skipped"]
    )


def test_position_supervisor_advisories_skip_unexecutable_stop_legality(tmp_path):
    db_path = tmp_path / "state.db"
    _create_db(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO position_lifecycle_event
            (event_id, position_id, trade_id, symbol, event_type, event_ts, details_json)
            VALUES ('amend_bad_1', '1001', '1001', 'XAUUSD+', 'amend_failed', ?, '{}')
            """,
            (1782439200.0,),
        )
        conn.commit()
    finally:
        conn.close()

    result = build_position_supervisor_advisories(
        day="2026-06-26",
        db_path=db_path,
        materialize=True,
    )

    actions = {item["action"] for item in result["items"]}
    skipped_actions = {item["action"] for item in result["skipped"]}
    assert "fix_stop_legality" not in actions
    assert "fix_stop_legality" in skipped_actions

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT action FROM policy_suggestion").fetchall()
    finally:
        conn.close()
    assert "fix_stop_legality" not in {row[0] for row in rows}
