import json
import sqlite3

from backend.services.canonical_v2 import (
    record_review,
    record_supervisor_trace_event,
)
from backend.services.canonical_v2_reader import iter_supervisor_trace_rows
from research.position_quality_lightgbm import (
    MODEL_TYPE,
    PositionQualityLightGBMService,
)
from tests.canonical_fixture import make_canonical_sqlite


def _create_reviews(path, *, contaminated_indices=None):
    contaminated_indices = set(contaminated_indices or ())
    conn = make_canonical_sqlite(path)
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
        if i in contaminated_indices:
            payload["system_issue_context"] = {
                "system_contaminated": True,
                "contaminates_learning": True,
            }
        record_review(
            conn,
            review_id=f"rev_{i}",
            trade_id=f"trade_{i}",
            position_id=f"pos_{i}",
            pnl=1.0 if positive else -1.0,
            mae=payload["mae"],
            mfe=payload["mfe"],
            outcome_label="small_win" if positive else "bad_loss",
            failure_tags=[],
            review=payload,
            created_at=1000.0 + i,
        )
        record_supervisor_trace_event(
            conn,
            trace_id=f"trace_{i}",
            event_ts=900.0 + i,
            payload={
                "trace_id": f"trace_{i}",
                "position_id": f"pos_{i}",
                "trade_id": f"trade_{i}",
                "stage": "evaluated",
                "trace_integrity": "full",
                "template_version": "v-current",
                "config_hash": "cfg-current",
                "verdict": {"action": "hold", "evidence": payload},
            },
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
    conn = make_canonical_sqlite(db_path)
    try:
        trace_payload = iter_supervisor_trace_rows(
            conn, trace_id="trace_0", reverse=False
        )[0]["verdict_json"]
        for index in range(2):
            record_supervisor_trace_event(
                conn,
                trace_id=f"trace_0_extra_{index}",
                event_ts=901.0 + index,
                payload={
                    "trace_id": f"trace_0_extra_{index}",
                    "position_id": "pos_0",
                    "trade_id": "trade_0",
                    "stage": "evaluated",
                    "trace_integrity": "full",
                    "template_version": "v-current",
                    "config_hash": "cfg-current",
                    "verdict": json.loads(trace_payload),
                },
            )
        conn.commit()
        safe_window_bytes = max(
            len(str(row["verdict_json"]).encode("utf-8"))
            for row in iter_supervisor_trace_rows(conn, reverse=False)
            if row.get("position_id") != "pos_0"
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
    _create_reviews(db_path, contaminated_indices={0})

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
    # Rows without the flag: quoted failure-tag fallback
    assert _review_text_contaminated(
        '{"failure_tags": ["decision_bar_stale", "other"]}'
    )
    assert not _review_text_contaminated('{"failure_tags": ["other_tag"]}')
    assert not _review_text_contaminated("")
    assert not _review_text_contaminated('{"summary_text": "clean review"}')


def test_multiple_reviews_for_one_position_fail_closed(tmp_path):
    db_path = tmp_path / "ambiguous-review.db"
    _create_reviews(db_path)
    conn = make_canonical_sqlite(db_path)
    try:
        payload = {
            "execution_quality_state": "full",
            "execution_quality_evidence": {
                "schema_version": "execution_quality_evidence.v2",
                "evidence_state": "full",
            },
        }
        record_review(
            conn,
            review_id="rev_0_duplicate",
            trade_id="trade_0",
            position_id="pos_0",
            outcome_label="small_win",
            failure_tags=[],
            review=payload,
            created_at=2000.0,
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
    conn = make_canonical_sqlite(db_path)
    review = {
        "execution_quality_state": "full",
        "execution_quality_evidence": {
            "schema_version": "execution_quality_evidence.v2",
            "evidence_state": "full",
        },
    }
    record_review(
        conn,
        review_id="review-1",
        trade_id="trade-1",
        position_id="position-1",
        pnl=1.0,
        outcome_label="small_win",
        failure_tags=[],
        review=review,
        created_at=5000.0,
    )
    for index in range(100):
        record_supervisor_trace_event(
            conn,
            trace_id=f"trace-{index}",
            event_ts=4000.0 + index * 30.0,
            payload={
                "trace_id": f"trace-{index}",
                "position_id": "position-1",
                "trade_id": "trade-1",
                "stage": "evaluated",
                "trace_integrity": "full",
                "template_version": "v-current",
                "config_hash": "cfg",
                "verdict": {"evidence": {"current_pnl": float(index)}},
            },
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
