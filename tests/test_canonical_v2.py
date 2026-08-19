from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from backend.services.canonical_v2 import (
    CanonicalV2ConflictError,
    CanonicalV2Error,
    append_event,
    append_relation,
    finish_projection_run,
    put_dataset_manifest,
    put_dataset_members,
    put_payload,
    put_state_version,
    put_training_sample,
    read_payload,
    record_review,
    start_projection_run,
)


def _canonical_sqlite() -> sqlite3.Connection:
    from tests.canonical_fixture import make_canonical_sqlite
    return make_canonical_sqlite()


def test_payload_is_content_addressed_and_restorable() -> None:
    conn = _canonical_sqlite()
    value = {"after": {"weight": 0.25}, "before": {"weight": 0.1}}

    first = put_payload(conn, value, payload_kind="factor_state", schema_version="v1")
    second = put_payload(conn, value, payload_kind="factor_state", schema_version="v1")

    assert first == second
    assert conn.execute("SELECT COUNT(*) FROM  payload_blob").fetchone()[0] == 1
    assert read_payload(conn, first.payload_hash) == value

    other_kind = put_payload(conn, value, payload_kind="governance_target", schema_version="v1")
    assert other_kind.payload_hash != first.payload_hash


def test_event_idempotency_does_not_merge_real_occurrences() -> None:
    conn = _canonical_sqlite()
    first_payload = put_payload(conn, {"value": 1}, payload_kind="decision", schema_version="v1")
    second_payload = put_payload(conn, {"value": 2}, payload_kind="decision", schema_version="v1")

    first = append_event(
        conn,
        event_type="risk_decision",
        entity_type="decision",
        entity_id="decision-1",
        payload_hash=first_payload.payload_hash,
        producer="test-writer",
        idempotency_key="request-1",
    )
    retry = append_event(
        conn,
        event_type="risk_decision",
        entity_type="decision",
        entity_id="decision-1",
        payload_hash=first_payload.payload_hash,
        producer="test-writer",
        idempotency_key="request-1",
    )
    assert retry["event_id"] == first["event_id"]
    assert retry["created"] is False

    with pytest.raises(CanonicalV2ConflictError):
        append_event(
            conn,
            event_type="risk_decision",
            entity_type="decision",
            entity_id="decision-1",
            payload_hash=second_payload.payload_hash,
            producer="test-writer",
            idempotency_key="request-1",
        )

    occurrence = append_event(
        conn,
        event_type="risk_decision",
        entity_type="decision",
        entity_id="decision-1",
        payload_hash=first_payload.payload_hash,
        producer="test-writer",
    )
    assert occurrence["event_id"] != first["event_id"]
    assert conn.execute("SELECT COUNT(*) FROM  event").fetchone()[0] == 2
    assert append_relation(
        conn,
        from_event_id=occurrence["event_id"],
        to_event_id=first["event_id"],
        relation_type="caused_by",
    )
    assert not append_relation(
        conn,
        from_event_id=occurrence["event_id"],
        to_event_id=first["event_id"],
        relation_type="caused_by",
    )
    before = put_state_version(
        conn,
        state_version_id="state-before",
        entity_type="governance_target",
        entity_id="target-1",
        version=1,
        valid_from=datetime.now(timezone.utc),
        source_event_id=first["event_id"],
        payload_hash=first_payload.payload_hash,
    )
    rollback = put_state_version(
        conn,
        state_version_id="state-rollback",
        entity_type="governance_target",
        entity_id="target-1",
        version=2,
        valid_from=datetime.now(timezone.utc),
        source_event_id=occurrence["event_id"],
        payload_hash=before["payload_hash"],
    )
    assert rollback["payload_hash"] == before["payload_hash"]


def test_training_manifest_and_projection_run_are_reference_only() -> None:
    conn = _canonical_sqlite()
    payload = put_payload(conn, {"decision": "open"}, payload_kind="decision", schema_version="v1")
    event = append_event(
        conn,
        event_type="risk_decision",
        entity_type="decision",
        entity_id="decision-2",
        payload_hash=payload.payload_hash,
        producer="test-writer",
        idempotency_key="request-2",
    )
    sample = put_training_sample(
        conn,
        sample_id="sample-1",
        sample_type="open_outcome",
        source_event_ids=[event["event_id"]],
        feature_hash="feature-hash",
        feature_schema_hash="feature-schema",
        label_hash="label-hash",
        trace_hash="trace-hash",
        evidence_contract={"schema_version": "learning_evidence_contract.v1"},
        config_version=15,
        config_hash="config-hash",
        horizon_minutes=30,
        target_source="trade_review",
        sample_status="ready",
    )
    assert sample["created"] is True
    retry = put_training_sample(
        conn,
        sample_id="sample-1",
        sample_type="open_outcome",
        source_event_ids=[event["event_id"]],
        feature_hash="feature-hash",
        feature_schema_hash="feature-schema",
        label_hash="label-hash",
        trace_hash="trace-hash",
        evidence_contract={"schema_version": "learning_evidence_contract.v1"},
        config_version=15,
        config_hash="config-hash",
        horizon_minutes=30,
        target_source="trade_review",
        sample_status="ready",
    )
    assert retry["created"] is False

    manifest = put_dataset_manifest(
        conn,
        dataset_id="dataset-1",
        purpose="position-quality",
        training_window="window-1",
        horizon_minutes=30,
        query_contract_hash="query-hash",
        sample_digest="sample-digest",
        feature_schema_hash="feature-schema",
        label_contract_hash="label-contract",
        target_source="trade_review",
        config_hash="config-hash",
        source_watermark="event-1",
        code_commit="commit-1",
    )
    assert manifest["created"] is True
    assert put_dataset_members(
        conn,
        dataset_id="dataset-1",
        members=[("sample-1", 0, "sample-digest")],
    ) == 1
    assert put_dataset_members(
        conn,
        dataset_id="dataset-1",
        members=[("sample-1", 0, "sample-digest")],
    ) == 0
    with pytest.raises(CanonicalV2ConflictError):
        put_dataset_members(
            conn,
            dataset_id="dataset-1",
            members=[("sample-1", 0, "different-digest")],
        )

    started = start_projection_run(
        conn,
        projection_run_id="run-1",
        run_kind="projection",
        projection_name="brain_memory",
        source_watermark="event-1",
        code_version="code-1",
        input_digest="input-1",
        started_at=datetime.now(timezone.utc),
    )
    assert started["status"] == "running"
    retry_run = start_projection_run(
        conn,
        projection_run_id="run-2",
        run_kind="projection",
        projection_name="brain_memory",
        source_watermark="event-1",
        code_version="code-1",
        input_digest="input-1",
    )
    assert retry_run["projection_run_id"] == "run-1"
    assert retry_run["created"] is False
    with pytest.raises(CanonicalV2ConflictError):
        start_projection_run(
            conn,
            projection_run_id="run-1",
            run_kind="projection",
            projection_name="brain_memory",
            source_watermark="event-2",
            code_version="code-1",
            input_digest="input-1",
        )
    finish_projection_run(
        conn,
        projection_run_id="run-1",
        status="completed",
        output_digest="output-1",
    )
    finish_projection_run(
        conn,
        projection_run_id="run-1",
        status="completed",
        output_digest="output-1",
    )
    with pytest.raises(CanonicalV2ConflictError):
        finish_projection_run(
            conn,
            projection_run_id="run-1",
            status="completed",
            output_digest="different-output",
        )
    assert conn.execute("SELECT COUNT(*) FROM  training_sample").fetchone()[0] == 1


def test_vertical_decision_to_dataset_lineage_has_one_payload_authority() -> None:
    conn = _canonical_sqlite()
    payloads = {
        kind: put_payload(
            conn,
            {"kind": kind, "value": 1},
            payload_kind=kind,
            schema_version="v1",
        )
        for kind in ("decision", "execution", "position", "review", "label", "trace")
    }
    events = []
    for event_type, entity_type, entity_id, kind in (
        ("risk_decision", "decision", "decision-vertical", "decision"),
        ("broker_execution", "execution", "execution-vertical", "execution"),
        ("position_transition", "position", "position-vertical", "position"),
        ("trade_review", "review", "review-vertical", "review"),
        ("label_observation", "label", "label-vertical", "label"),
        ("training_run", "training", "run-vertical", "trace"),
    ):
        events.append(
            append_event(
                conn,
                event_type=event_type,
                entity_type=entity_type,
                entity_id=entity_id,
                payload_hash=payloads[kind].payload_hash,
                producer="vertical-test",
                idempotency_key=f"{kind}-vertical",
            )
        )

    for from_event, to_event, relation_type in (
        (events[1], events[0], "caused_by"),
        (events[2], events[1], "caused_by"),
        (events[3], events[2], "reviews"),
        (events[4], events[3], "labels"),
        (events[5], events[4], "derived_from"),
    ):
        assert append_relation(
            conn,
            from_event_id=from_event["event_id"],
            to_event_id=to_event["event_id"],
            relation_type=relation_type,
        )

    sample = put_training_sample(
        conn,
        sample_id="sample-vertical",
        sample_type="position-quality",
        source_event_ids=[event["event_id"] for event in events[:5]],
        feature_hash=payloads["trace"].payload_hash,
        feature_schema_hash="feature-schema-vertical",
        label_hash=payloads["label"].payload_hash,
        trace_hash=payloads["trace"].payload_hash,
        evidence_contract={"schema_version": "learning_evidence_contract.v1"},
        config_version=15,
        config_hash="config-vertical",
        horizon_minutes=30,
        target_source="trade_review",
        sample_status="ready",
    )
    manifest = put_dataset_manifest(
        conn,
        dataset_id="dataset-vertical",
        purpose="position-quality",
        training_window="vertical-window",
        horizon_minutes=30,
        query_contract_hash="query-vertical",
        sample_digest="sample-vertical-digest",
        feature_schema_hash="feature-schema-vertical",
        label_contract_hash="label-vertical",
        target_source="trade_review",
        config_hash="config-vertical",
        source_watermark="watermark-vertical",
        code_commit="commit-vertical",
    )
    assert put_dataset_members(
        conn,
        dataset_id=manifest["dataset_id"],
        members=[(sample["sample_id"], 0, "sample-vertical-digest")],
    ) == 1
    assert conn.execute("SELECT COUNT(*) FROM  payload_blob").fetchone()[0] == 6
    assert conn.execute("SELECT COUNT(*) FROM  event").fetchone()[0] == 6
    assert conn.execute("SELECT COUNT(*) FROM  event_relation").fetchone()[0] == 5
    assert conn.execute("SELECT COUNT(*) FROM  training_sample").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM  dataset_manifest_member").fetchone()[0] == 1


def test_reader_iter_decisions_streams_all_with_stable_keyset() -> None:
    """iter_decisions must stream every decision in (observed_at, event_id) order
    with composite keyset pagination, including same-timestamp collisions."""
    from backend.services.canonical_v2_reader import iter_decisions

    conn = _canonical_sqlite()
    start = start_projection_run(
        conn,
        projection_run_id="reader-test-run",
        run_kind="backfill",
        projection_name="reader-test",
        source_watermark="wm",
        code_version="v1",
        input_digest="in",
    )
    assert start["created"] is True

    shared_ts = datetime(2026, 8, 1, 10, 0, 0, tzinfo=timezone.utc)
    stamps = [
        datetime(2026, 8, 1, 9, 59, 0, tzinfo=timezone.utc),
        shared_ts,
        shared_ts,
        datetime(2026, 8, 1, 10, 0, 1, tzinfo=timezone.utc),
        datetime(2026, 8, 1, 10, 0, 1, tzinfo=timezone.utc),
    ]
    decision_ids: list[str] = []
    for index, stamp in enumerate(stamps, start=1):
        ref = put_payload(
            conn,
            {"decision_id": f"decision-{index}", "event_type": "open"},
            payload_kind="risk_decision",
            schema_version="v1",
        )
        event = append_event(
            conn,
            event_type="risk_decision",
            entity_type="decision",
            entity_id=f"decision-{index}",
            payload_hash=ref.payload_hash,
            producer="reader-test",
            observed_at=stamp,
        )
        assert event["created"] is True
        decision_ids.append(str(event["event_id"]))

    records = list(iter_decisions(conn, limit=0))
    assert len(records) == 5
    assert {record["decision_id"] for record in records} == {f"decision-{i}" for i in range(1, 6)}
    expected_order = sorted(zip(stamps, decision_ids))
    # order must equal (observed_at, event_id) sort, including same-timestamp collisions
    assert [record["event_id"] for record in records] == [event_id for _, event_id in expected_order]

    # bounded limit path
    bounded = list(iter_decisions(conn, limit=2))
    assert len(bounded) == 2
    assert bounded[0]["event_id"] == expected_order[0][1]

    # small batch size forces multiple pagination rounds on the same cursor
    streamed = list(iter_decisions(conn, limit=0, batch_size=2))
    assert [record["event_id"] for record in streamed] == [event_id for _, event_id in expected_order]
    assert len({record["event_id"] for record in streamed}) == 5


def test_reader_read_trade_chain_resolves_relations() -> None:
    """read_trade_chain must resolve review -> decision -> order/position via
    derived_from / caused_by without tuple indexing assumptions."""
    from backend.services.canonical_v2_reader import read_trade_chain

    conn = _canonical_sqlite()
    start_projection_run(
        conn,
        projection_run_id="chain-run",
        run_kind="backfill",
        projection_name="chain-test",
        source_watermark="wm",
        code_version="v1",
        input_digest="in",
    )

    def _event(event_type: str, entity_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        ref = put_payload(conn, payload, payload_kind=event_type, schema_version="v1")
        prefix = {
            "risk_decision": "live_decision_",
            "broker_execution": "live_ordevt_",
            "position_transition": "live_posevt_",
            "trade_review": "live_review_",
        }.get(event_type, "")
        return append_event(
            conn,
            event_id=f"{prefix}{entity_id}",
            event_type=event_type,
            entity_type=entity_id.split("-", 1)[0],
            entity_id=entity_id,
            payload_hash=ref.payload_hash,
            producer="chain-test",
        )

    decision = _event("risk_decision", "decision-d1", {"decision_id": "d1", "action": "open"})
    order = _event("broker_execution", "order-o1", {"event_type": "opened", "trade_id": "t1"})
    position = _event("position_transition", "position-p1", {"event_type": "opened", "trade_id": "t1"})
    review = _event("trade_review", "review-r1", {"review_id": "r1", "entry_decision_id": "d1"})

    assert append_relation(conn, from_event_id=review["event_id"], to_event_id=decision["event_id"], relation_type="derived_from")
    assert append_relation(conn, from_event_id=order["event_id"], to_event_id=decision["event_id"], relation_type="caused_by")
    assert append_relation(conn, from_event_id=position["event_id"], to_event_id=decision["event_id"], relation_type="caused_by")

    # the legacy primary key resolves to the canonical review event via the
    # live-event id convention (legacy_mapping is gone since S5/R1)
    chain = read_trade_chain(conn, "review-r1")
    assert chain is not None
    assert chain["review"]["source"] == "canonical"
    assert chain["review"]["payload"]["review_id"] == "r1"
    assert chain["decision"]["payload"]["decision_id"] == "d1"
    assert {item["entity_id"] for item in chain["orders"]} == {"order-o1"}
    assert {item["entity_id"] for item in chain["positions"]} == {"position-p1"}


def test_reader_legacy_fallback_without_canonical_schema() -> None:
    """Reader falls back to the legacy occurrence table when the canonical
    schema is absent (SQLite fixtures / pre-cutover environments)."""
    from backend.services.canonical_v2_reader import canonical_ready, iter_reviews, read_review

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE trade_outcome_review (
            review_id TEXT PRIMARY KEY,
            trade_id TEXT,
            position_id TEXT,
            entry_decision_id TEXT,
            pnl REAL,
            outcome_label TEXT,
            created_at REAL
        );
        """
    )
    conn.execute(
        "INSERT INTO trade_outcome_review VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("review-legacy-1", "trade-1", "position-1", "decision-1", -3.5, "loss", 1786800000.0),
    )
    assert canonical_ready(conn) is False

    record = read_review(conn, "review-legacy-1")
    assert record is not None
    assert record["source"] == "legacy"
    assert record["payload"]["pnl"] == -3.5

    rows = iter_reviews(conn, limit=0)
    assert len(rows) == 1
    assert rows[0]["review_id"] == "review-legacy-1"


def test_reader_order_position_rows_shape_legacy_row_shapes() -> None:
    """order_row/position_row must expose legacy shapes: epoch event_ts and a
    details_json text column restored from the nested payload details."""
    from backend.services.canonical_v2_reader import (
        iter_order_rows,
        iter_position_rows,
        order_row,
        position_row,
    )

    conn = _canonical_sqlite()
    start_projection_run(
        conn,
        projection_run_id="shape-run",
        run_kind="backfill",
        projection_name="shape-test",
        source_watermark="wm",
        code_version="v1",
        input_digest="in",
    )
    stamp = datetime(2026, 8, 2, 8, 30, 15, tzinfo=timezone.utc)

    order_ref = put_payload(
        conn,
        {
            "event_id": "oevt-1",
            "decision_id": "d1",
            "trade_id": "t1",
            "order_id": "o1",
            "broker_order_id": "bo1",
            "event_type": "filled",
            "event_ts": stamp.isoformat(),
            "price": 4132.395,
            "volume": 0.1,
            "status": "filled",
            "details": {"sl": 4100.0, "tp": 4160.0},
            "execution_intent_id": "ei1",
        },
        payload_kind="broker_execution",
        schema_version="v1",
    )
    order_event = append_event(
        conn,
        event_id="live_ordevt_oevt-1",
        event_type="broker_execution",
        entity_type="order",
        entity_id="oevt-1",
        payload_hash=order_ref.payload_hash,
        producer="shape-test",
    )
    position_ref = put_payload(
        conn,
        {
            "event_id": "pevt-1",
            "position_id": "p1",
            "trade_id": "t1",
            "symbol": "XAUUSD",
            "event_type": "opened",
            "event_ts": stamp.isoformat(),
            "net_volume": 0.1,
            "avg_price": 4132.4,
            "unrealized_pnl": 0.0,
            "realized_pnl": 0.0,
            "details": {"entry": {"price": 4132.4}},
        },
        payload_kind="position_transition",
        schema_version="v1",
    )
    position_event = append_event(
        conn,
        event_id="live_posevt_pevt-1",
        event_type="position_transition",
        entity_type="position",
        entity_id="pevt-1",
        payload_hash=position_ref.payload_hash,
        producer="shape-test",
    )
    shaped_order = order_row(conn, "oevt-1")
    assert shaped_order is not None
    assert shaped_order["event_id"] == "oevt-1"
    assert abs(shaped_order["event_ts"] - stamp.timestamp()) < 1e-6
    assert shaped_order["details_json"] == json.dumps({"sl": 4100.0, "tp": 4160.0}, sort_keys=True)
    # nested details key is not part of the legacy-shaped row
    assert "details" not in shaped_order

    shaped_position = position_row(conn, "pevt-1")
    assert shaped_position is not None
    assert abs(shaped_position["event_ts"] - stamp.timestamp()) < 1e-6
    assert shaped_position["net_volume"] == 0.1
    assert "details" not in shaped_position

    rows = iter_order_rows(conn, limit=0)
    assert len(rows) == 1
    assert rows[0]["event_id"] == "oevt-1"
    assert abs(rows[0]["event_ts"] - stamp.timestamp()) < 1e-6
    positions = iter_position_rows(conn, limit=0)
    assert len(positions) == 1
    assert positions[0]["position_id"] == "p1"


def test_reader_iter_rows_shape_epoch_timestamps() -> None:
    """iter_review_rows / iter_decision_rows must apply legacy shaping (epoch
    timestamps, JSON column restoration) on the canonical streaming path."""
    from backend.services.canonical_v2_reader import iter_decision_rows, iter_review_rows

    conn = _canonical_sqlite()
    start_projection_run(
        conn,
        projection_run_id="iter-shape-run",
        run_kind="backfill",
        projection_name="iter-shape-test",
        source_watermark="wm",
        code_version="v1",
        input_digest="in",
    )
    stamp = datetime(2026, 8, 3, 9, 0, 0, tzinfo=timezone.utc)
    review_ref = put_payload(
        conn,
        {
            "review_id": "r-shape-1",
            "trade_id": "t-shape",
            "position_id": "p-shape",
            "entry_decision_id": "d-shape",
            "pnl": -2.5,
            "outcome_label": "loss",
            "failure_tags": ["lucky_win"],
            "created_at": stamp.isoformat(),
            "review": {"close_reason": "tp_hit"},
        },
        payload_kind="trade_review",
        schema_version="v1",
    )
    review_event = append_event(
        conn,
        event_type="trade_review",
        entity_type="review",
        entity_id="r-shape-1",
        payload_hash=review_ref.payload_hash,
        producer="iter-shape-test",
    )
    decision_ref = put_payload(
        conn,
        {
            "decision_id": "d-shape",
            "trade_id": "t-shape",
            "event_type": "open",
            "symbol": "XAUUSD",
            "timeframe": "M5",
            "decision_ts": stamp.isoformat(),
            "action": {"skip_stage": ""},
            "created_at": stamp.isoformat(),
        },
        payload_kind="risk_decision",
        schema_version="v1",
    )
    decision_event = append_event(
        conn,
        event_type="risk_decision",
        entity_type="decision",
        entity_id="d-shape",
        payload_hash=decision_ref.payload_hash,
        producer="iter-shape-test",
    )
    review_rows = iter_review_rows(conn, limit=0)
    assert len(review_rows) == 1
    assert abs(review_rows[0]["created_at"] - stamp.timestamp()) < 1e-6
    assert review_rows[0]["failure_tags_json"] == '["lucky_win"]'
    assert review_rows[0]["review_json"] == {"close_reason": "tp_hit"}

    decision_rows = list(iter_decision_rows(conn, limit=0))
    assert len(decision_rows) == 1
    assert abs(decision_rows[0]["decision_ts"] - stamp.timestamp()) < 1e-6
    assert decision_rows[0]["action_json"] == '{"skip_stage": ""}'


def test_reader_iter_decisions_bounded_and_reverse() -> None:
    """iter_decisions window bounds and reverse keyset must prune and reorder."""
    from backend.services.canonical_v2_reader import iter_decisions

    conn = _canonical_sqlite()
    stamps = [
        datetime(2026, 8, 1, 10, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 2, 10, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 3, 10, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 4, 10, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 5, 10, 0, 0, tzinfo=timezone.utc),
    ]
    ids: list[str] = []
    for index, stamp in enumerate(stamps, start=1):
        ref = put_payload(
            conn,
            {"decision_id": f"d-window-{index}", "event_type": "open"},
            payload_kind="risk_decision",
            schema_version="v1",
        )
        event = append_event(
            conn,
            event_type="risk_decision",
            entity_type="decision",
            entity_id=f"d-window-{index}",
            payload_hash=ref.payload_hash,
            producer="reader-window-test",
            observed_at=stamp,
        )
        ids.append(str(event["event_id"]))

    # bounded window: Aug 2..Aug 4 inclusive
    window = [r["decision_id"] for r in iter_decisions(
        conn,
        limit=0,
        min_observed_epoch=stamps[1].timestamp(),
        max_observed_epoch=stamps[3].timestamp(),
    )]
    assert window == ["d-window-2", "d-window-3", "d-window-4"]

    # reverse bounded limit: newest 2
    newest = [r["decision_id"] for r in iter_decisions(conn, limit=2, reverse=True)]
    assert newest == ["d-window-5", "d-window-4"]

    # reverse full stream is the exact reverse of the forward stream
    fwd = [r["decision_id"] for r in iter_decisions(conn, limit=0)]
    rev = [r["decision_id"] for r in iter_decisions(conn, limit=0, reverse=True)]
    assert rev == list(reversed(fwd))



def test_record_review_mirrors_live_review_idempotently_and_readable() -> None:
    """A1 live trade-review writer: idempotent per review_id and survives the
    canonical trade_review stream read path."""
    conn = _canonical_sqlite()
    first = record_review(
        conn,
        review_id="rv_live_1",
        trade_id="trade_1",
        position_id="pos_1",
        entry_decision_id="dec_entry_1",
        exit_decision_id="dec_exit_1",
        entry_quality=0.8,
        hold_quality=0.7,
        exit_quality=0.9,
        regime_fit_score=0.5,
        execution_quality=0.6,
        pnl=-4.2,
        mae=-9.0,
        mfe=1.5,
        outcome_label="bad_loss",
        failure_tags=["execution_timing"],
        summary_text="live bad loss",
        review={"primary_responsibility": "execution_timing"},
        created_at=1_728_500_000.0,
        producer="live_closed_position",
    )
    second = record_review(
        conn,
        review_id="rv_live_1",
        trade_id="trade_1",
        position_id="pos_1",
        entry_decision_id="dec_entry_1",
        exit_decision_id="dec_exit_1",
        entry_quality=0.8,
        hold_quality=0.7,
        exit_quality=0.9,
        regime_fit_score=0.5,
        execution_quality=0.6,
        pnl=-4.2,
        mae=-9.0,
        mfe=1.5,
        outcome_label="bad_loss",
        failure_tags=["execution_timing"],
        summary_text="live bad loss",
        review={"primary_responsibility": "execution_timing"},
        created_at=1_728_500_000.0,
        producer="live_closed_position",
    )
    try:
        assert first["created"] is True
        assert second["created"] is False
        assert first["event_id"] == second["event_id"]
        assert conn.execute(
            "SELECT COUNT(*) FROM  event"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM  payload_blob"
        ).fetchone()[0] == 1

        from backend.services.canonical_v2_reader import iter_reviews, read_review

        reviews = iter_reviews(conn, limit=0)
        assert [r["review_id"] for r in reviews] == ["rv_live_1"]
        assert reviews[0]["source"] == "canonical"
        record = read_review(conn, "rv_live_1")
        assert record is not None
        assert record["source"] == "canonical"
        payload = record["payload"]
        assert payload["outcome_label"] == "bad_loss"
        assert payload["failure_tags"] == ["execution_timing"]
        assert payload["review"]["primary_responsibility"] == "execution_timing"
        assert payload["pnl"] == -4.2
    finally:
        conn.close()
