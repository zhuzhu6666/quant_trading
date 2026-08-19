import json
import sqlite3

import pandas as pd

from backend.services import supervisor_counterfactual as scf


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
        CREATE TABLE decision_ledger (
            decision_id TEXT PRIMARY KEY,
            trade_id TEXT DEFAULT '',
            position_id TEXT DEFAULT '',
            event_type TEXT NOT NULL,
            symbol TEXT DEFAULT '',
            timeframe TEXT DEFAULT '',
            decision_ts REAL NOT NULL DEFAULT 0.0,
            regime_id TEXT DEFAULT '',
            regime_confidence REAL DEFAULT 0.0,
            portfolio_state_json TEXT DEFAULT '{}',
            risk_state_json TEXT DEFAULT '{}',
            policy_version TEXT DEFAULT '',
            factor_set_version TEXT DEFAULT '',
            action_score REAL DEFAULT 0.0,
            action_reason TEXT DEFAULT '',
            action_json TEXT DEFAULT '{}',
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
        """
    )
    review = {
        "position_id": "p1",
        "trade_id": "p1",
        "close_ts": 1000.0,
        "close_price": 100.0,
        "timeframe": "M5",
        "close_reason": "broker_close",
        "real_pnl": {"entry_price": 101.0, "net": -1.0},
    }
    conn.execute(
        """
        INSERT INTO trade_outcome_review
        (review_id, trade_id, position_id, entry_decision_id, pnl, review_json, created_at)
        VALUES ('r1', 'p1', 'p1', 'dec_r1', -1.0, ?, 1000.0)
        """,
        (json.dumps(review),),
    )
    conn.execute(
        """
        INSERT INTO decision_ledger
        (decision_id, trade_id, position_id, event_type, action_reason,
         action_score, action_json, created_at, decision_ts)
        VALUES ('d1', 'p1', 'p1', 'supervisor_tighten', 'thesis_weakening',
                0.6, ?, 1300.0, 990.0)
        """,
        (
            json.dumps(
                {
                    "supervisor_verdict": {
                        "action": "tighten",
                        "summary_reason": "thesis_weakening",
                        "evidence": {"trigger_tags": ["thesis_weakening"]},
                    }
                }
            ),
        ),
    )
    conn.execute(
        """
        INSERT INTO position_lifecycle_event
        (event_id, position_id, trade_id, event_type, event_ts, details_json)
        VALUES ('open1', 'p1', 'p1', 'opened', 900.0, ?)
        """,
        (json.dumps({"direction": -1, "sl": 103.0, "tp": 95.0}),),
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
    _create_db(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        review = json.loads(
            conn.execute(
                "SELECT review_json FROM trade_outcome_review WHERE review_id='r1'"
            ).fetchone()[0]
        )
        review["system_issue_context"] = {
            "contaminates_learning": True,
            "labels": ["market_data_stale"],
        }
        conn.execute(
            "UPDATE trade_outcome_review SET review_json=? WHERE review_id='r1'",
            (json.dumps(review),),
        )
        conn.commit()
    finally:
        conn.close()

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
        **first["items"][0]["evidence"],
        "evidence_invalidated": True,
        "invalidation_reason": "broker_execution_price_scale_repair_v1",
        "maturity": {
            **dict(first["items"][0]["evidence"].get("maturity") or {}),
            "governance_eligible": False,
        },
    }

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            UPDATE supervisor_counterfactual_review
            SET evidence_json=?
            WHERE counterfactual_id=?
            """,
            (json.dumps(invalidated), counterfactual_id),
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
    assert evidence["invalidation_reason"] == (
        "broker_execution_price_scale_repair_v1"
    )
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


def test_counterfactual_includes_supervisor_reduce_close_with_m1_evidence(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    _create_db(db_path)
    conn = sqlite3.connect(str(db_path))
    review = {
        "position_id": "p1",
        "trade_id": "p1",
        "close_ts": 1000.0,
        "close_price": 100.0,
        "timeframe": "M5",
        "close_reason": "supervisor_reduce",
        "entry_action": {"direction": -1},
        "real_pnl": {"entry_price": 101.0, "net": -1.0},
    }
    conn.execute("UPDATE trade_outcome_review SET review_json=? WHERE review_id='r1'", (json.dumps(review),))
    conn.execute(
        """
        UPDATE decision_ledger
        SET event_type='supervisor_reduce',
            action_reason='profit_giveback_after_mfe',
            action_json=?
        WHERE decision_id='d1'
        """,
        (
            json.dumps(
                {
                    "supervisor_verdict": {
                        "action": "reduce",
                        "summary_reason": "profit_giveback_after_mfe",
                        "evidence": {"trigger_tags": ["profit_giveback_after_mfe"]},
                    }
                }
            ),
        ),
    )
    conn.commit()
    conn.close()

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
