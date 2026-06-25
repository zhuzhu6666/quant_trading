import json
import sqlite3

from backend.api import risk as risk_api


def test_recent_policy_verdicts_summarizes_decision_ledger(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE decision_ledger (
            decision_id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            symbol TEXT DEFAULT '',
            timeframe TEXT DEFAULT '',
            decision_ts REAL NOT NULL DEFAULT 0.0,
            action_reason TEXT DEFAULT '',
            action_json TEXT DEFAULT '{}',
            risk_state_json TEXT DEFAULT '{}',
            created_at REAL NOT NULL DEFAULT 0.0
        );
        """
    )
    conn.executemany(
        """
        INSERT INTO decision_ledger
        (decision_id, event_type, symbol, timeframe, decision_ts, action_reason, action_json, risk_state_json, created_at)
        VALUES (?, ?, 'XAUUSD+', 'M5', ?, ?, ?, ?, ?)
        """,
        [
            (
                "dec_allowed",
                "open",
                200.0,
                "executed",
                json.dumps({"risk_verdict": {"allowed": True, "reason": "ok", "audit_payload": {"action": "open_trade"}}}),
                "{}",
                200.0,
            ),
            (
                "dec_blocked",
                "skip",
                100.0,
                "仓位上限: 3/3",
                "{}",
                json.dumps({"policy_verdict": {"allowed": False, "reason": "仓位上限: 3/3", "audit_payload": {"action": "open_trade"}}}),
                100.0,
            ),
        ],
    )
    conn.commit()
    conn.close()

    def _conn():
        c = sqlite3.connect(str(db_path))
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(risk_api, "get_state_conn", _conn)

    result = risk_api._recent_policy_verdicts(limit=10)

    assert result["total"] == 2
    assert result["counts"] == {"allowed": 1, "blocked": 1}
    assert result["by_action"] == {"open_trade": 2}
    assert result["by_reason"]["ok"] == 1
    assert result["items"][0]["decision_id"] == "dec_allowed"
