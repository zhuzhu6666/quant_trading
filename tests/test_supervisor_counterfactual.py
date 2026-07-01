import json
import sqlite3

import pandas as pd

from backend.services import supervisor_counterfactual as scf


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
        (review_id, trade_id, position_id, pnl, review_json, created_at)
        VALUES ('r1', 'p1', 'p1', -1.0, ?, 1000.0)
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

    bars = pd.DataFrame(
        [
            {"time": 1100.0, "open": 100.0, "high": 100.1, "low": 98.0, "close": 98.5, "volume": 1},
            {"time": 1300.0, "open": 98.5, "high": 99.0, "low": 94.9, "close": 95.2, "volume": 1},
        ]
    )
    monkeypatch.setattr(scf, "_load_future_bars", lambda *args, **kwargs: bars)

    result = scf.evaluate_counterfactuals(db_path=db_path, limit=10, materialize=True)

    assert result["count"] == 1
    assert result["items"][0]["label"] == "premature_tighten"
    stored = scf.list_counterfactuals(db_path=db_path)
    assert stored["count"] == 1
    assert stored["items"][0]["evidence"]["advisory_only"] is True


def test_counterfactual_respects_original_sl_before_later_tp(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    _create_db(db_path)

    bars = pd.DataFrame(
        [
            {"time": 1100.0, "open": 100.0, "high": 103.2, "low": 99.0, "close": 102.8, "volume": 1},
            {"time": 1300.0, "open": 102.8, "high": 103.0, "low": 94.9, "close": 95.2, "volume": 1},
        ]
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

    bars = pd.DataFrame(
        [
            {"time": 1000.0 + 70 * 60, "open": 100.0, "high": 100.2, "low": 97.5, "close": 98.0, "volume": 1},
            {"time": 1000.0 + 90 * 60, "open": 98.0, "high": 98.2, "low": 94.9, "close": 95.2, "volume": 1},
        ]
    )
    monkeypatch.setattr(scf, "_load_future_bars", lambda *args, **kwargs: bars)

    result = scf.evaluate_counterfactuals(db_path=db_path, limit=10, materialize=True)

    assert result["count"] == 1
    item = result["items"][0]
    assert item["label"] == "premature_tighten"
    assert [h["horizon_minutes"] for h in item["horizons"]] == [120]
    assert item["horizons"][0]["first_original_hit"] == "tp"
