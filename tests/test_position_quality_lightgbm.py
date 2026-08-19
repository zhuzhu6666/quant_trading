import json
import sqlite3

from research.position_quality_lightgbm import (
    MODEL_TYPE,
    PositionQualityLightGBMService,
)
from backend.services.state_payload_archive import archive_json_payload


def _create_reviews(path):
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
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
        CREATE TABLE position_supervisor_trace (
            trace_id TEXT PRIMARY KEY,
            position_id TEXT DEFAULT '',
            trade_id TEXT DEFAULT '',
            stage TEXT DEFAULT '',
            trace_integrity TEXT DEFAULT 'full',
            verdict_json TEXT DEFAULT '{}',
            context_json TEXT DEFAULT '{}',
            template_id TEXT DEFAULT '',
            template_version TEXT DEFAULT '',
            config_version INTEGER DEFAULT 0,
            config_hash TEXT DEFAULT '',
            event_ts REAL NOT NULL DEFAULT 0.0
        );
        """
    )
    for i in range(8):
        positive = i % 2 == 0
        payload = {
            "execution_quality_state": "full",
            "execution_quality_evidence": {
                "schema_version": "execution_quality_evidence.v2",
                "evidence_state": "full",
            },
            "holding_seconds": 1252 if i == 0 else 120 + i * 10,
            "mfe": 3.0 if positive else 0.2,
            "mae": 0.5 if positive else 3.0,
            "giveback_ratio": 0.1 if positive else 0.9,
            "profit_capture_ratio": 0.7 if positive else 0.0,
            "time_in_profit": 90 if positive else 0,
            "holding_efficiency": 0.8 if positive else 0.05,
            "time_decay_score": 0.8 if positive else 0.2,
            "thesis_status": "intact" if positive else "broken",
            "regime_shift": "none",
            "close_reason": "broker_close" if positive else "thesis_broken",
        }
        conn.execute(
            """
            INSERT INTO trade_outcome_review
            (review_id, trade_id, position_id, pnl, mae, mfe, outcome_label,
             review_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"rev_{i}",
                f"trade_{i}",
                f"pos_{i}",
                1.0 if positive else -1.0,
                payload["mae"],
                payload["mfe"],
                "small_win" if positive else "bad_loss",
                json.dumps(payload),
                1000.0 + i,
            ),
        )
        conn.execute(
            """
            INSERT INTO position_supervisor_trace
            (trace_id, position_id, trade_id, stage, trace_integrity,
             verdict_json, context_json, template_id, template_version,
             config_version, config_hash, event_ts)
            VALUES (?, ?, ?, 'evaluated', 'full', ?, '{}', 'default', 'v-current',
                    1, 'cfg-current', ?)
            """,
            (
                f"trace_{i}", f"pos_{i}", f"trade_{i}",
                json.dumps({"action": "hold", "evidence": payload}),
                900.0 + i,
            ),
        )
    conn.commit()
    conn.close()


def test_position_quality_lightgbm_trains_or_reports_missing_dependency(tmp_path):
    db_path = tmp_path / "state.db"
    artifact_dir = tmp_path / "artifacts"
    _create_reviews(db_path)

    service = PositionQualityLightGBMService(db_path=db_path, artifact_dir=artifact_dir)
    samples = service.load_samples(limit=20)
    assert samples[0]["features"]["completed_bars_after_entry"] == 4.0
    result = service.train(limit=20, min_samples=4, register=False)

    if not result["ok"]:
        assert result["error"] == "dependency_missing"
        return

    assert result["model_type"] == MODEL_TYPE
    assert result["feature_schema_version"] == "pit.v2.position_h30"
    assert result["metrics"]["split"] == "time_ordered_grouped_purged"
    assert result["metrics"]["holdout"]["majority_baseline_accuracy"] is not None
    assert result["metrics"]["holdout"]["balanced_accuracy"] is not None
    assert result["metrics"]["holdout"]["negative_recall"] is not None
    assert result["capabilities"]["live_trading"] is False
    shadow = service.score_samples(artifact_path=result["artifact_path"], limit=8)
    assert shadow["ok"] is True
    assert shadow["count"] == 8
    audits = service.list_audits(limit=20)
    assert audits["count"] == 8
    assert audits["items"][0]["result"]["capabilities"]["shadow_only"] is True


def test_inspect_training_window_is_read_only_and_emits_manifest(tmp_path):
    db_path = tmp_path / "inspect.db"
    _create_reviews(db_path)
    service = PositionQualityLightGBMService(db_path=db_path, artifact_dir=tmp_path / "artifacts")

    result = service.inspect_training_window(limit=20, horizon_minutes=30)

    assert result["ok"] is True
    assert result["read_only"] is True
    assert result["sample_count"] == 8
    assert result["data_quality"]["raw_candidate_row_count"] == 8
    assert result["data_quality"]["peak_buffered_bytes"] >= 0
    assert result["sample_manifest"]["sample_id_digest"]
    assert result["sample_manifest"]["feature_schema_hash"]
    assert result["data_quality"]["label_distribution"] == {"negative": 4, "positive": 4}
    assert result["data_quality"]["target_source_counts"] == {"closed_before_horizon": 8}
    assert len(result["explain"]) == 3
    assert all(item["analyze"] is False for item in result["explain"])
    assert all(item["plan"] is not None for item in result["explain"])

    conn = sqlite3.connect(str(db_path))
    try:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='position_quality_shadow_audit'"
        ).fetchone() is None
    finally:
        conn.close()


def test_oversized_trace_window_is_excluded_before_streaming(tmp_path, monkeypatch):
    db_path = tmp_path / "oversized-trace-window.db"
    _create_reviews(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        trace_payload = conn.execute(
            "SELECT verdict_json FROM position_supervisor_trace WHERE trace_id='trace_0'"
        ).fetchone()[0]
        for index in range(2):
            conn.execute(
                """
                INSERT INTO position_supervisor_trace
                (trace_id, position_id, trade_id, stage, trace_integrity,
                 verdict_json, context_json, template_id, template_version,
                 config_version, config_hash, event_ts)
                VALUES (?, 'pos_0', 'trade_0', 'evaluated', 'full', ?, '{}',
                        'default', 'v-current', 1, 'cfg-current', ?)
                """,
                (f"trace_0_extra_{index}", trace_payload, 901.0 + index),
            )
        conn.commit()
        safe_window_bytes = max(
            len(row[0].encode("utf-8"))
            for row in conn.execute(
                """
                SELECT verdict_json
                FROM position_supervisor_trace
                WHERE position_id <> 'pos_0'
                """
            )
        )
    finally:
        conn.close()

    import research.position_quality_lightgbm as module

    monkeypatch.setattr(module, "MAX_TRACE_WINDOW_BYTES", safe_window_bytes)
    service = PositionQualityLightGBMService(db_path=db_path, artifact_dir=tmp_path / "artifacts")
    samples = service.load_samples(limit=20)

    assert "pos_0" not in {item["position_id"] for item in samples}
    assert len({item["position_id"] for item in samples}) == 7
    assert service.last_data_quality["excluded_trace_window_position_count"] == 1
    assert service.last_data_quality["excluded_reason_counts"]["trace_window_budget_exceeded"] == 3
    assert service.last_data_quality["trace_window_budget_policy"] == "exclude_oversized_position"


def test_position_quality_lightgbm_excludes_system_contaminated_reviews(tmp_path):
    db_path = tmp_path / "state.db"
    _create_reviews(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT review_json FROM trade_outcome_review WHERE review_id='rev_0'"
        ).fetchone()
        review = json.loads(row[0])
        review["system_issue_context"] = {
            "system_contaminated": True,
            "contaminates_learning": True,
        }
        conn.execute(
            "UPDATE trade_outcome_review SET review_json=? WHERE review_id='rev_0'",
            (json.dumps(review),),
        )
        conn.commit()
    finally:
        conn.close()

    samples = PositionQualityLightGBMService(
        db_path=db_path,
        artifact_dir=tmp_path / "artifacts",
    ).load_samples(limit=20)

    assert "rev_0" not in {sample["review_id"] for sample in samples}


def test_review_text_contaminated_matches_sql_semantics():
    from research.position_quality_lightgbm import _review_text_contaminated

    # Standard separators
    assert _review_text_contaminated(
        '{"system_issue_context": {"contaminates_learning": true}}'
    )
    assert not _review_text_contaminated(
        '{"system_issue_context": {"contaminates_learning": false}}'
    )
    # Compact separators
    assert _review_text_contaminated(
        '{"system_issue_context":{"contaminates_learning":true}}'
    )
    assert not _review_text_contaminated(
        '{"system_issue_context":{"contaminates_learning":false}}'
    )
    # Legacy rows without the flag: quoted failure-tag fallback
    assert _review_text_contaminated(
        '{"failure_tags": ["decision_bar_stale", "other"]}'
    )
    assert not _review_text_contaminated('{"failure_tags": ["other_tag"]}')
    assert not _review_text_contaminated("")
    assert not _review_text_contaminated('{"summary_text": "clean review"}')


def test_multiple_reviews_for_one_position_fail_closed(tmp_path):
    db_path = tmp_path / "ambiguous-review.db"
    _create_reviews(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        payload = {
            "execution_quality_state": "full",
            "execution_quality_evidence": {
                "schema_version": "execution_quality_evidence.v2",
                "evidence_state": "full",
            },
        }
        conn.execute(
            """
            INSERT INTO trade_outcome_review
            (review_id, trade_id, position_id, outcome_label, review_json, created_at)
            VALUES ('rev_0_duplicate', 'trade_0', 'pos_0', 'small_win', ?, 2000.0)
            """,
            (json.dumps(payload),),
        )
        conn.commit()
    finally:
        conn.close()

    service = PositionQualityLightGBMService(db_path=db_path, artifact_dir=tmp_path / "artifacts")
    samples = service.load_samples(limit=20)
    assert "pos_0" not in {item["position_id"] for item in samples}
    assert service.last_data_quality["excluded_review_cardinality_ambiguous_count"] == 1


def test_review_payload_is_parsed_once_even_when_trace_density_is_high(tmp_path, monkeypatch):
    db_path = tmp_path / "review-once.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE trade_outcome_review (
            review_id TEXT PRIMARY KEY, trade_id TEXT, position_id TEXT,
            pnl REAL DEFAULT 0.0, outcome_label TEXT DEFAULT '',
            failure_tags_json TEXT DEFAULT '[]', review_json TEXT DEFAULT '{}',
            created_at REAL DEFAULT 0.0
        );
        CREATE TABLE position_supervisor_trace (
            trace_id TEXT PRIMARY KEY, position_id TEXT, trade_id TEXT,
            stage TEXT, trace_integrity TEXT, verdict_json TEXT,
            template_version TEXT, config_hash TEXT, event_ts REAL
        );
        """
    )
    review = {
        "execution_quality_state": "full",
        "execution_quality_evidence": {
            "schema_version": "execution_quality_evidence.v2",
            "evidence_state": "full",
        },
    }
    conn.execute(
        "INSERT INTO trade_outcome_review VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("review-1", "trade-1", "position-1", 1.0, "small_win", "[]", json.dumps(review), 5000.0),
    )
    for index in range(100):
        conn.execute(
            "INSERT INTO position_supervisor_trace VALUES (?, ?, ?, 'evaluated', 'full', ?, 'v-current', 'cfg', ?)",
            (
                f"trace-{index}",
                "position-1",
                "trade-1",
                json.dumps({"evidence": {"current_pnl": float(index)}}),
                4000.0 + index * 30.0,
            ),
        )
    conn.commit()
    conn.close()

    import research.position_quality_lightgbm as module

    calls = {"count": 0}
    original = module._review_execution_evidence_complete

    def counted(row):
        calls["count"] += 1
        return original(row)

    monkeypatch.setattr(module, "_review_execution_evidence_complete", counted)
    service = PositionQualityLightGBMService(db_path=db_path, artifact_dir=tmp_path / "artifacts")
    service.load_samples(limit=20)
    assert calls["count"] == 1
    assert service.last_data_quality["unique_review_count"] == 1


def test_archived_review_is_restored_before_feature_extraction(tmp_path):
    db_path = tmp_path / "archived-review.db"
    _create_reviews(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE state_payload_archive (
                archive_hash TEXT PRIMARY KEY,
                source_table TEXT NOT NULL,
                source_id TEXT NOT NULL,
                payload_kind TEXT NOT NULL,
                codec TEXT NOT NULL,
                raw_sha256 TEXT NOT NULL,
                raw_bytes INTEGER NOT NULL,
                compressed_bytes INTEGER NOT NULL,
                payload_bytes BLOB NOT NULL,
                created_at REAL NOT NULL
            );
            ALTER TABLE trade_outcome_review ADD COLUMN review_archive_hash TEXT NOT NULL DEFAULT '';
            ALTER TABLE trade_outcome_review ADD COLUMN review_raw_bytes INTEGER NOT NULL DEFAULT 0;
            """
        )
        full_review = {
            "execution_quality_state": "full",
            "execution_quality_evidence": {
                "schema_version": "execution_quality_evidence.v2",
                "evidence_state": "full",
            },
            "system_issue_context": {
                "system_contaminated": True,
                "contaminates_learning": True,
            },
        }
        archive = archive_json_payload(
            conn,
            source_table="trade_outcome_review",
            source_id="rev_0",
            payload_kind="trade_review",
            raw_json=json.dumps(full_review, separators=(",", ":")),
        )
        conn.execute(
            "UPDATE trade_outcome_review SET review_json=?, review_archive_hash=?, review_raw_bytes=? WHERE review_id='rev_0'",
            (json.dumps({"execution_quality_state": "full"}), archive["archive_hash"], archive["raw_bytes"]),
        )
        conn.commit()
    finally:
        conn.close()

    service = PositionQualityLightGBMService(db_path=db_path, artifact_dir=tmp_path / "artifacts")
    samples = service.load_samples(limit=20)
    assert "rev_0" not in {item["review_id"] for item in samples}
    assert service.last_data_quality["archive_review_count"] == 1
    assert service.last_data_quality["inline_review_count"] == 7
