import json
import sqlite3
import time

from backend.api import risk as risk_api
from backend.services.position_supervisor import evaluate_position_supervisor


def test_position_supervisor_recommends_reduce_after_large_giveback():
    verdict = evaluate_position_supervisor(
        {
            "position": {
                "position_id": "9001",
                "direction": 1,
                "entry_price": 3000.0,
                "current_price": 3010.0,
                "volume": 100.0,
                "unrealized_pnl": 10.0,
                "sl": 2990.0,
                "tp": 3040.0,
            },
            "risk": {
                "max_holding_seconds": 7200.0,
                "holding_timeout_ratio": 0.55,
                "mfe": 40.0,
                "mae": 5.0,
                "giveback_ratio": 0.75,
                "profit_capture_ratio": 0.25,
                "time_in_profit": 1800.0,
                "holding_efficiency": 0.35,
                "time_decay_score": 0.42,
                "thesis_status": "weakening",
                "regime_shift": "none",
            },
            "temporal_context": {
                "decision_ts": time.time(),
                "holding_seconds": 4000.0,
            },
            "market_space_context": {},
            "entry_context": {},
            "runtime": {},
        }
    )

    assert verdict["action"] == "reduce"
    assert verdict["summary_reason"] == "profit_giveback_after_mfe"
    assert verdict["recommended_controls"]["reduce_fraction"] == 0.5


def test_position_supervisor_recommends_close_when_timeout_exceeded():
    verdict = evaluate_position_supervisor(
        {
            "position": {
                "position_id": "9002",
                "direction": -1,
                "entry_price": 3000.0,
                "current_price": 3015.0,
                "volume": 100.0,
                "unrealized_pnl": -15.0,
                "sl": 3020.0,
                "tp": 2960.0,
            },
            "risk": {
                "max_holding_seconds": 3600.0,
                "holding_timeout_ratio": 1.08,
                "mfe": 8.0,
                "mae": 20.0,
                "giveback_ratio": 1.0,
                "profit_capture_ratio": 0.0,
                "time_in_profit": 300.0,
                "holding_efficiency": 0.12,
                "time_decay_score": 0.18,
                "thesis_status": "broken",
                "regime_shift": "confirmed",
            },
            "temporal_context": {
                "decision_ts": time.time(),
                "holding_seconds": 3900.0,
            },
            "market_space_context": {},
            "entry_context": {},
            "runtime": {},
        }
    )

    assert verdict["action"] == "close"
    assert verdict["summary_reason"] == "holding_timeout_exceeded"
    assert verdict["recommended_controls"]["protection_mode"] == "full_exit"


def test_trade_trace_exposes_position_supervisor_events(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
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
        CREATE TABLE order_lifecycle_event (
            event_id TEXT PRIMARY KEY,
            decision_id TEXT DEFAULT '',
            trade_id TEXT DEFAULT '',
            order_id TEXT DEFAULT '',
            broker_order_id TEXT DEFAULT '',
            event_type TEXT NOT NULL,
            event_ts REAL NOT NULL DEFAULT 0.0,
            price REAL DEFAULT 0.0,
            volume REAL DEFAULT 0.0,
            status TEXT DEFAULT '',
            details_json TEXT DEFAULT '{}'
        );
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
        CREATE TABLE factor_contribution_review (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            review_id TEXT NOT NULL,
            trade_id TEXT DEFAULT '',
            factor TEXT NOT NULL,
            entry_contribution REAL DEFAULT 0.0,
            hold_contribution REAL DEFAULT 0.0,
            exit_contribution REAL DEFAULT 0.0,
            net_contribution REAL DEFAULT 0.0,
            confidence REAL DEFAULT 0.0,
            notes TEXT DEFAULT ''
        );
        CREATE TABLE recovery_position_state (
            position_id INTEGER PRIMARY KEY,
            broker TEXT DEFAULT 'ctrader',
            symbol TEXT DEFAULT '',
            direction INTEGER DEFAULT 0,
            open_price REAL DEFAULT 0.0,
            volume REAL DEFAULT 0.0,
            first_seen_at REAL DEFAULT 0.0,
            last_seen_at REAL DEFAULT 0.0,
            status TEXT DEFAULT 'open',
            strategy_name TEXT DEFAULT '',
            entry_decision_id TEXT DEFAULT '',
            context_integrity TEXT DEFAULT 'full',
            recovery_meta_json TEXT DEFAULT '{}',
            closed_at REAL DEFAULT 0.0,
            close_reason TEXT DEFAULT '',
            close_pnl REAL DEFAULT 0.0
        );
        """
    )
    conn.executemany(
        """
        INSERT INTO decision_ledger
        (decision_id, trade_id, position_id, event_type, symbol, timeframe, decision_ts,
         portfolio_state_json, risk_state_json, action_score, action_reason, action_json, created_at)
        VALUES (?, ?, ?, ?, 'XAUUSD+', 'M5', ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "dec_open",
                "9001",
                "9001",
                "open",
                100.0,
                "{}",
                "{}",
                0.7,
                "opened",
                json.dumps({"side": "sell"}),
                100.0,
            ),
            (
                "dec_supervisor",
                "9001",
                "9001",
                "supervisor_reduce",
                150.0,
                "{}",
                json.dumps({"policy_verdict": {"allowed": True, "reason": "risk_reducing_action"}}),
                0.86,
                "profit_giveback_after_mfe",
                json.dumps(
                    {
                        "supervisor_verdict": {
                            "action": "reduce",
                            "human_summary": "系统判断这笔仓位仍有逻辑，但不值得继续满仓承受同样风险，建议先降一部分。",
                        },
                        "risk_verdict": {"allowed": True, "reason": "risk_reducing_action"},
                    }
                ),
                150.0,
            ),
        ],
    )
    conn.execute(
        """
        INSERT INTO recovery_position_state
        (position_id, broker, symbol, direction, open_price, volume, status, entry_decision_id, context_integrity, recovery_meta_json)
        VALUES (9001, 'ctrader', 'XAUUSD+', -1, 3992.0, 100, 'open', 'dec_open', 'full', ?)
        """,
        (json.dumps({"latest_supervisor": {"action": "reduce"}}),),
    )
    conn.commit()
    conn.close()

    def _conn():
        c = sqlite3.connect(str(db_path))
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(risk_api, "get_state_conn", _conn)

    result = risk_api._trade_trace(position_id="9001")

    assert result["summary"]["supervisor_events"] == 1
    assert result["summary"]["latest_supervisor_action"] == "reduce"
    assert result["position_supervisor"]["latest"]["event_type"] == "supervisor_reduce"
    assert result["position_supervisor"]["events"][0]["action"]["supervisor_verdict"]["action"] == "reduce"
