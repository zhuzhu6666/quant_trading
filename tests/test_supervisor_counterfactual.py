import json

import pandas as pd

from backend.services import supervisor_counterfactual as scf
from backend.services.canonical_v2 import (
    ensure_sqlite_schema,
    record_counterfactual_event,
    record_decision_event,
    record_position_event,
    record_review,
    record_supervisor_trace_event,
)


def _complete_m1_bars(*, close_ts=1000.0, minutes=120, overrides=None):
    overrides = overrides or {}
    rows = []
    for minute in range(1, minutes + 1):
        row = {
            "time": close_ts + minute * 60,
            "open": 100.0,
            "high": 100.1,
            "low": 99.9,
            "close": 100.0,
            "volume": 1,
        }
        row.update(overrides.get(minute, {}))
        rows.append(row)
    return pd.DataFrame(rows)


def _create_db(
    path,
    *,
    event_type="supervisor_tighten",
    action="tighten",
    close_reason="broker_close",
    contaminated=False,
    action_reason="thesis_weakening",
):
    import sqlite3

    conn = sqlite3.connect(str(path))
    ensure_sqlite_schema(conn)
    record_decision_event(
        conn,
        decision_id="d1",
        trade_id="p1",
        position_id="p1",
        event_type=event_type,
        symbol="XAUUSD+",
        timeframe="M5",
        decision_ts=990.0,
        action_score=0.6,
        action_reason=action_reason,
        action={
            "supervisor_verdict": {
                "action": action,
                "summary_reason": action_reason,
                "evidence": {"trigger_tags": [action_reason]},
            }
        },
        created_at=991.0,
    )
    record_position_event(
        conn,
        event_id="open1",
        position_id="p1",
        trade_id="p1",
        event_type="opened",
        event_ts=900.0,
        details={"direction": -1, "sl": 103.0, "tp": 95.0},
    )
    record_supervisor_trace_event(
        conn,
        trace_id="t1",
        decision_id="d1",
        event_ts=995.0,
        payload={
            "trace_id": "t1",
            "decision_id": "d1",
            "position_id": "p1",
            "trade_id": "p1",
            "action": action,
            "stage": "executed",
            "outcome": "applied",
            "execution_json": json.dumps(
                {
                    "is_real_execution": True,
                    "broker_action_confirmed": True,
                    "reconcile_confirmed": True,
                }
            ),
        },
    )
    review_payload = {
        "position_id": "p1",
        "trade_id": "p1",
        "close_ts": 1000.0,
        "close_price": 100.0,
        "timeframe": "M5",
        "close_reason": close_reason,
        "entry_action": {"direction": -1},
        "real_pnl": {"entry_price": 101.0, "net": -1.0},
    }
    if contaminated:
        review_payload["system_issue_context"] = {
            "contaminates_learning": True,
            "labels": ["market_data_stale"],
        }
    record_review(
        conn,
        review_id="r1",
        trade_id="p1",
        position_id="p1",
        entry_decision_id="d1",
        pnl=-1.0,
        review=review_payload,
        created_at=1000.0,
    )
    conn.commit()
    conn.close()


def test_counterfactual_labels_premature_tighten_when_future_recovers(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    _create_db(db_path)

    bars = _complete_m1_bars(overrides={2: {"low": 94.9, "close": 95.2}})
    monkeypatch.setattr(scf, "_load_future_bars", lambda *args, **kwargs: bars)

    result = scf.evaluate_counterfactuals(db_path=db_path, limit=10, materialize=True)

    assert result["count"] == 1
    assert result["items"][0]["label"] == "premature_tighten"
    stored = scf.list_counterfactuals(db_path=db_path)
    assert stored["count"] == 1
    assert stored["items"][0]["evidence"]["advisory_only"] is True


def test_counterfactual_skips_system_contaminated_review(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    _create_db(db_path, contaminated=True)
    monkeypatch.setattr(scf, "_load_future_bars", lambda *args, **kwargs: _complete_m1_bars())

    result = scf.evaluate_counterfactuals(db_path=db_path, limit=10, materialize=True)

    assert result["candidate_count"] == 0
    assert result["items"] == []
    assert scf.list_counterfactuals(db_path=db_path)["items"] == []


def test_counterfactual_can_target_review_ids_without_limit_window(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    _create_db(db_path)
    monkeypatch.setattr(scf, "_load_future_bars", lambda *args, **kwargs: _complete_m1_bars())

    selected = scf.evaluate_counterfactuals(
        db_path=db_path,
        limit=0,
        review_ids=["r1"],
        materialize=False,
    )
    missing = scf.evaluate_counterfactuals(
        db_path=db_path,
        limit=10,
        review_ids=[],
        materialize=False,
    )

    assert selected["requested_review_count"] == 1
    assert selected["count"] == 1
    assert missing["requested_review_count"] == 0
    assert missing["items"] == []


def test_counterfactual_terminal_invalidation_cannot_be_rematerialized(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    _create_db(db_path)
    monkeypatch.setattr(scf, "_load_future_bars", lambda *args, **kwargs: _complete_m1_bars())

    first = scf.evaluate_counterfactuals(
        db_path=db_path,
        review_ids=["r1"],
        materialize=True,
    )
    counterfactual_id = first["items"][0]["counterfactual_id"]
    invalidated = {
        **first["items"][0],
        "evidence": {
            **first["items"][0]["evidence"],
            "evidence_invalidated": True,
            "invalidation_reason": "broker_execution_price_scale_repair_v1",
            "maturity": {
                **dict(first["items"][0]["evidence"].get("maturity") or {}),
                "governance_eligible": False,
            },
        },
    }
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        ensure_sqlite_schema(conn)
        record_counterfactual_event(
            conn,
            counterfactual_id=counterfactual_id,
            review_id="r1",
            decision_id="d1",
            trace_id="t1",
            event_ts=float(first["items"][0]["close_ts"]) + 1.0,
            payload=invalidated,
        )
        conn.commit()
    finally:
        conn.close()

    repeated = scf.evaluate_counterfactuals(
        db_path=db_path,
        review_ids=["r1"],
        materialize=True,
    )
    stored = scf.list_counterfactuals(db_path=db_path)

    assert repeated["items"][0]["counterfactual_id"] == counterfactual_id
    assert stored["count"] == 1
    evidence = stored["items"][0]["evidence"]
    assert evidence["evidence_invalidated"] is True
    assert evidence["invalidation_reason"] == "broker_execution_price_scale_repair_v1"
    assert evidence["maturity"]["governance_eligible"] is False


def test_counterfactual_sparse_future_bars_are_not_governance_ready(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    _create_db(db_path)
    bars = _complete_m1_bars(minutes=59, overrides={2: {"low": 94.9, "close": 95.2}})
    monkeypatch.setattr(scf, "_load_future_bars", lambda *args, **kwargs: bars)

    item = scf.evaluate_counterfactuals(db_path=db_path, limit=10, materialize=False)["items"][0]

    assert item["maturity_status"] == "partially_matured"
    assert item["governance_eligible"] is False
    horizon_60 = next(row for row in item["horizons"] if row["horizon_minutes"] == 60)
    assert horizon_60["expected_bars"] == 60
    assert horizon_60["observed_bars"] == 59
    assert horizon_60["matured"] is False


def test_counterfactual_respects_original_sl_before_later_tp(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    _create_db(db_path)

    bars = _complete_m1_bars(
        overrides={1: {"high": 103.2, "low": 99.0, "close": 102.8}, 2: {"low": 94.9, "close": 95.2}}
    )
    monkeypatch.setattr(scf, "_load_future_bars", lambda *args, **kwargs: bars)

    result = scf.evaluate_counterfactuals(db_path=db_path, limit=10, materialize=True)

    assert result["count"] == 1
    item = result["items"][0]
    assert item["label"] == "correct_stop"
    assert item["horizons"][0]["first_original_hit"] == "sl"


def test_counterfactual_default_horizon_catches_two_hour_recovery(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    _create_db(db_path)

    bars = _complete_m1_bars(overrides={90: {"low": 94.9, "close": 95.2}})
    monkeypatch.setattr(scf, "_load_future_bars", lambda *args, **kwargs: bars)

    result = scf.evaluate_counterfactuals(db_path=db_path, limit=10, materialize=True)

    assert result["count"] == 1
    item = result["items"][0]
    assert item["label"] == "premature_tighten"
    assert item["maturity_status"] == "fully_matured"
    assert item["horizons"][-1]["horizon_minutes"] == 120
    assert item["horizons"][-1]["first_original_hit"] == "tp"


def test_counterfactual_includes_supervisor_reduce_with_m1_evidence(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    _create_db(
        db_path,
        event_type="supervisor_reduce",
        action="reduce",
        close_reason="supervisor_reduce",
        action_reason="profit_giveback_after_mfe",
    )

    bars = _complete_m1_bars(overrides={2: {"low": 94.9, "close": 95.2}})
    calls = []

    def _future(symbol, timeframe, close_ts, max_minutes):
        calls.append((symbol, timeframe, close_ts, max_minutes))
        return bars

    monkeypatch.setattr(scf, "_load_future_bars", _future)
    result = scf.evaluate_counterfactuals(db_path=db_path, limit=10, materialize=True)

    assert result["count"] == 1
    item = result["items"][0]
    assert item["close_reason"] == "supervisor_reduce"
    assert item["supervisor_event_type"] == "supervisor_reduce"
    assert item["label"] == "protection_too_tight"
    assert item["evidence"]["bar_timeframe"] == "M1"
    assert item["evidence"]["trade_timeframe"] == "M5"
    assert calls[0][1] == "M1"
